"""Convert SillyTavern presets, character cards and world info into Anyworld
import configs.

This is a deterministic field mapper rather than a rewriting step: fields that
have a direct Anyworld equivalent are copied (and lightly sanitised), while
everything that depends on SillyTavern-only machinery — tavern_helper scripts,
regex scripts, EJS templates, the MVU variable store, macros, extra client
settings — is reported through ``warnings`` instead of being silently dropped.
"""

from __future__ import annotations

import base64
import json
import re
import zlib
from typing import Any

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_SCRIPT_PATTERNS = (
    re.compile(r"<UpdateVariable>.*?</UpdateVariable>", re.S | re.I),
    re.compile(r"<StatusPlaceHolderImpl\s*/?>", re.I),
    re.compile(r"<%[_=#-]?.*?_%>", re.S),
)
_ROLE_BY_INDEX = {0: "system", 1: "user", 2: "assistant"}
_MACRO = re.compile(r"\{\{[^{}]*\}\}")


def _clean(text: Any, char_name: str = "") -> str:
    """Strip SillyTavern script markup and expand the two common macros."""
    if not isinstance(text, str):
        return ""
    for pattern in _SCRIPT_PATTERNS:
        text = pattern.sub("", text)
    text = text.replace("{{user}}", "玩家").replace("{{char}}", char_name or "角色")
    return text.strip()


def _truncate(text: str, limit: int) -> str:
    return text[:limit]


def _collect_macros(*texts: str) -> list[str]:
    found: set[str] = set()
    for text in texts:
        found.update(_MACRO.findall(text))
    return sorted(found)


def extract_png_card(data: bytes) -> dict | None:
    """Return the character JSON embedded in a SillyTavern PNG card, if present."""
    if not data.startswith(PNG_SIGNATURE):
        return None
    offset = len(PNG_SIGNATURE)
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset : offset + 4], "big")
        chunk_type = data[offset + 4 : offset + 8]
        chunk = data[offset + 8 : offset + 8 + length]
        offset += 12 + length
        text: bytes | None = None
        if chunk_type == b"tEXt":
            key, _, value = chunk.partition(b"\x00")
            if key == b"chara":
                text = value
        elif chunk_type == b"iTXt":
            key, _, rest = chunk.partition(b"\x00")
            if key == b"chara" and len(rest) >= 2:
                comp_flag = rest[0]
                body = rest[2:]
                _, _, body = body.partition(b"\x00")  # language tag
                _, _, body = body.partition(b"\x00")  # translated keyword
                if comp_flag == 1:
                    try:
                        body = zlib.decompress(body)
                    except zlib.error:
                        body = b""
                text = body
        if text is not None:
            try:
                return json.loads(base64.b64decode(text).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return None
    return None


def _looks_like_card(obj: dict) -> bool:
    data = obj.get("data")
    if isinstance(data, dict) and (
        "first_mes" in data or "character_book" in data or "description" in data
    ):
        return True
    return "name" in obj and ("first_mes" in obj or "character_book" in obj)


def _looks_like_preset(obj: dict) -> bool:
    return isinstance(obj.get("prompts"), list) or "prompt_order" in obj


def _looks_like_world_info(obj: dict) -> bool:
    return isinstance(obj.get("entries"), (dict, list))


def _entries_of(book: Any) -> list[dict]:
    if isinstance(book, dict):
        entries = book.get("entries")
    else:
        entries = book
    if isinstance(entries, dict):
        return [entry for entry in entries.values() if isinstance(entry, dict)]
    if isinstance(entries, list):
        return [entry for entry in entries if isinstance(entry, dict)]
    return []


def _entry_keys(entry: dict) -> list[str]:
    raw: list[str] = []
    for field in ("keys", "key"):
        value = entry.get(field)
        if isinstance(value, list):
            raw.extend(str(item) for item in value)
        elif isinstance(value, str):
            raw.append(value)
    cleaned: list[str] = []
    for key in raw:
        for part in re.split(r"[，,、]", key):
            part = part.strip()[:40]
            if part and part.casefold() not in {k.casefold() for k in cleaned}:
                cleaned.append(part)
    if cleaned:
        return cleaned[:100]
    comment = _truncate(_clean(entry.get("comment") or entry.get("title") or ""), 40)
    return [comment or "条目"]


def _entry_role_depth(entry: dict) -> tuple[str, int | None]:
    extensions = entry.get("extensions") if isinstance(entry.get("extensions"), dict) else {}
    role = _ROLE_BY_INDEX.get(extensions.get("role"), "system")
    depth = extensions.get("depth")
    if isinstance(depth, bool) or not isinstance(depth, int) or depth <= 0:
        return role, None
    return role, min(depth, 100)


def _convert_lorebook(entries: list[dict], warnings: list[str]) -> list[dict]:
    result: list[dict] = []
    for entry in entries:
        content = _truncate(_clean(entry.get("content"), "角色"), 10_000)
        if not content:
            continue
        role, depth = _entry_role_depth(entry)
        order = entry.get("insertion_order", entry.get("order", 0))
        if not isinstance(order, int) or isinstance(order, bool):
            order = 0
        result.append(
            {
                "title": _truncate(_clean(entry.get("comment") or entry.get("title") or ""), 80),
                "keys": _entry_keys(entry),
                "content": content,
                "order": max(0, min(order, 10_000)),
                "enabled": bool(entry.get("enabled", True)),
                "constant": bool(entry.get("constant", False)),
                "role": role,
                "depth": depth,
            }
        )
        if entry.get("secondary_keys") or entry.get("keysecondary"):
            warnings.append(f"世界书条目「{result[-1]['title']}」的次级关键词未映射。")
    return result


def _character(card: dict, warnings: list[str]) -> dict:
    character = {
        "name": _truncate(_clean(card.get("name"), "角色") or "角色", 40),
        "description": _truncate(_clean(card.get("description")), 50_000),
        "personality": _truncate(_clean(card.get("personality")), 10_000),
        "style": _truncate(_clean(card.get("style")), 10_000),
        "example_dialogue": _truncate(_clean(card.get("mes_example")), 10_000),
    }
    return character


def convert_card(obj: dict, warnings: list[str]) -> dict:
    """Map a character card (v2/v3) onto scenario, cast, guidance and lorebook."""
    card = obj.get("data") if isinstance(obj.get("data"), dict) else obj
    name = _clean(card.get("name"), "") or "角色"
    scenario_parts = [_clean(card.get("scenario"), name), _clean(card.get("first_mes"), name)]
    scenario = "\n\n".join(part for part in scenario_parts if part)

    guidance_parts: list[str] = []
    greetings = card.get("alternate_greetings")
    if isinstance(greetings, list) and greetings:
        openings = [_clean(greeting, name) for greeting in greetings if greeting]
        if openings:
            guidance_parts.append(
                "【备用开场（原卡 alternate greetings，供选开场参考）】\n\n"
                + "\n\n---\n\n".join(openings)
            )
    if card.get("creator_notes"):
        guidance_parts.append("【原卡作者注】\n" + _clean(card.get("creator_notes"), name))

    blocks = []
    for source, title, position in (
        (card.get("system_prompt"), "角色卡系统提示", "system"),
        (card.get("post_history_instructions"), "角色卡历史后指令", "output"),
    ):
        content = _truncate(_clean(source, name), 10_000)
        if content:
            blocks.append(
                {"title": title, "content": content, "position": position, "enabled": True}
            )

    extensions = card.get("extensions") if isinstance(card.get("extensions"), dict) else {}
    depth_prompt = extensions.get("depth_prompt")
    if isinstance(depth_prompt, dict) and _clean(depth_prompt.get("prompt"), name):
        depth = depth_prompt.get("depth", 4)
        role = depth_prompt.get("role", "system")
        blocks.append(
            {
                "title": "角色卡深度提示",
                "content": _truncate(_clean(depth_prompt.get("prompt"), name), 10_000),
                "position": "history",
                "role": role if role in ("system", "assistant", "user") else "system",
                "depth": depth if isinstance(depth, int) and not isinstance(depth, bool) else 4,
                "enabled": True,
            }
        )

    config: dict = {"characters": [_character(card, warnings)]}
    if scenario:
        config["scenario"] = _truncate(scenario, 500_000)
    if guidance_parts:
        config["guidance"] = _truncate("\n\n".join(guidance_parts), 500_000)
    if blocks:
        config["prompt_blocks"] = blocks
    lorebook = _convert_lorebook(_entries_of(card.get("character_book")), warnings)
    if lorebook:
        config["lorebook"] = lorebook

    macros = _collect_macros(scenario, "\n".join(guidance_parts), *(b["content"] for b in blocks))
    if macros:
        warnings.append("未展开的宏（原样保留）：" + " ".join(macros))
    if extensions.get("regex_scripts"):
        warnings.append("未导入：角色卡的 regex 脚本（本项目无正则管线）。")
    helper = extensions.get("tavern_helper")
    if isinstance(helper, dict) and helper.get("scripts"):
        warnings.append("未导入：tavern_helper 脚本（MVU 变量引擎等，本项目不执行前端 JS）。")
    return config


def _preset_enabled(preset: dict) -> dict[str, bool]:
    enabled: dict[str, bool] = {}
    orders = preset.get("prompt_order")
    if isinstance(orders, list):
        for order in orders:
            if not isinstance(order, dict):
                continue
            for item in order.get("order", []):
                if isinstance(item, dict) and isinstance(item.get("identifier"), str):
                    key = item["identifier"]
                    enabled[key] = enabled.get(key, False) or bool(item.get("enabled"))
    return enabled


def convert_preset(obj: dict, warnings: list[str]) -> dict:
    """Map a SillyTavern preset onto sampling and prompt blocks."""
    sampling: dict[str, Any] = {}
    for field, minimum, maximum in (
        ("temperature", 0, 2),
        ("top_p", 0, 1),
        ("frequency_penalty", -2, 2),
        ("presence_penalty", -2, 2),
        ("top_k", 0, 1_000_000),
        ("min_p", 0, 1),
        ("repetition_penalty", 0, 5),
    ):
        value = obj.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value < minimum or value > maximum:
            warnings.append(f"采样参数 {field}={value} 超出范围，已跳过。")
            continue
        sampling[field] = value

    enabled = _preset_enabled(obj)
    prompts = obj.get("prompts")
    blocks: list[dict] = []
    if isinstance(prompts, list):
        for prompt in prompts:
            if not isinstance(prompt, dict) or prompt.get("marker"):
                continue
            identifier = prompt.get("identifier")
            if enabled and identifier in enabled and not enabled[identifier]:
                continue
            content = _truncate(_clean(prompt.get("content"), "角色"), 10_000)
            if not content:
                continue
            role = prompt.get("role")
            if role not in ("system", "assistant", "user"):
                role = "system"
            block: dict[str, Any] = {
                "title": _truncate(str(prompt.get("name") or identifier or ""), 80),
                "content": content,
                "role": role,
                "enabled": True,
            }
            if prompt.get("system_prompt"):
                block["position"] = "system"
                block["depth"] = 0
            else:
                block["position"] = "history"
                depth = prompt.get("injection_depth", 0)
                block["depth"] = depth if isinstance(depth, int) and depth >= 0 else 0
            blocks.append(block)

    config: dict = {}
    if sampling:
        config["sampling"] = sampling
    if blocks:
        config["prompt_blocks"] = blocks
    if obj.get("prompts"):
        warnings.append("预设的客户端/后端设置（流式、seed、思考、函数调用、图片等）未导入。")
    macros = _collect_macros(*(block["content"] for block in blocks))
    if macros:
        warnings.append("预设提示块中未展开的宏（原样保留）：" + " ".join(macros))
    return config


def convert_world_info(obj: dict, warnings: list[str]) -> dict:
    """Map a standalone World Info / lorebook file onto the lorebook field."""
    lorebook = _convert_lorebook(_entries_of(obj), warnings)
    return {"lorebook": lorebook} if lorebook else {}


def _merge(base: dict, extra: dict) -> dict:
    merged = dict(base)
    for key, value in extra.items():
        if key in {"lorebook", "prompt_blocks", "characters", "events", "time_rules"}:
            merged[key] = list(merged.get(key, [])) + list(value)
        else:
            merged[key] = value
    return merged


def convert(obj: Any) -> tuple[dict, list[str]]:
    """Detect the artifact kind and return ``(config, warnings)``."""
    if not isinstance(obj, dict):
        raise ValueError("文件顶层必须是对象。")
    warnings: list[str] = []
    config: dict = {}
    if _looks_like_preset(obj):
        config = _merge(config, convert_preset(obj, warnings))
    if _looks_like_card(obj):
        config = _merge(config, convert_card(obj, warnings))
    if not config and _looks_like_world_info(obj):
        config = _merge(config, convert_world_info(obj, warnings))
    if not config:
        raise ValueError("无法识别的 SillyTavern 文件（角色卡 / 世界书 / 预设）。")
    return config, warnings


def convert_bytes(data: bytes) -> tuple[dict, list[str]]:
    """Detect PNG vs JSON and convert the embedded/parsed artifact."""
    if data.startswith(PNG_SIGNATURE):
        card = extract_png_card(data)
        if card is None:
            raise ValueError("PNG 中没有找到角色卡数据。")
        return convert(card)
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("无法解析文件：既不是角色卡 PNG，也不是合法 JSON。") from exc
    return convert(obj)
