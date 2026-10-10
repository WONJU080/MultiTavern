"""SillyTavern artifact import: cards, presets, world info, PNG embedding."""

import base64
import json

import pytest

from logic.lobby import parse_cast, parse_lorebook, parse_prompt_blocks, parse_sampling
from logic.st_import import PNG_SIGNATURE, convert, convert_bytes, extract_png_card


def _card():
    return {
        "spec": "chara_card_v3",
        "name": "测试卡",
        "data": {
            "name": "测试卡",
            "description": "一个测试角色",
            "personality": "沉默",
            "mes_example": "「嗯。」",
            "scenario": "雨夜的旅店。",
            "first_mes": "你推开门，雨水顺着衣角滴落。",
            "system_prompt": "保持简洁。",
            "post_history_instructions": "用第三人称。",
            "alternate_greetings": ["另一个开场。<StatusPlaceHolderImpl/>"],
            "extensions": {
                "depth_prompt": {"prompt": "记住：夜里别出门。", "depth": 4, "role": "system"},
                "regex_scripts": [{"scriptName": "x"}],
                "tavern_helper": {"scripts": [{"name": "MVU"}]},
            },
            "character_book": {
                "entries": [
                    {
                        "id": 0,
                        "keys": ["旅店"],
                        "content": "旅店老板娘是个哑巴。<UpdateVariable>x</UpdateVariable>",
                        "insertion_order": 10,
                        "enabled": True,
                        "constant": False,
                    },
                    {
                        "id": 1,
                        "keys": [],
                        "comment": "常驻",
                        "content": "雨一直没停。",
                        "constant": True,
                        "extensions": {"depth": 2, "role": 0},
                    },
                ]
            },
        },
    }


def _preset():
    return {
        "temperature": 1,
        "top_p": 0.99,
        "top_k": 110,
        "min_p": 0.02,
        "frequency_penalty": 0.2,
        "repetition_penalty": 1.05,
        "prompts": [
            {
                "identifier": "main",
                "name": "主要提示",
                "system_prompt": True,
                "role": "system",
                "content": "你是主持。{{user}}在场。",
            },
            {
                "identifier": "QuickWaaagh",
                "name": "假回合",
                "role": "assistant",
                "injection_depth": 4,
                "content": "<think>KhaosCodex Start!</think>",
            },
            {"identifier": "chatHistory", "name": "对话记录", "marker": True},
        ],
        "prompt_order": [
            {
                "character_id": 100001,
                "order": [
                    {"identifier": "main", "enabled": True},
                    {"identifier": "QuickWaaagh", "enabled": True},
                    {"identifier": "chatHistory", "enabled": True},
                ],
            }
        ],
    }


def _png_with(chara):
    payload = base64.b64encode(json.dumps(chara).encode("utf-8"))
    data = b"chara\x00" + payload
    block = len(data).to_bytes(4, "big") + b"tEXt" + data + b"\x00\x00\x00\x00"
    iend = (0).to_bytes(4, "big") + b"IEND" + b"\x00\x00\x00\x00"
    return PNG_SIGNATURE + block + iend


def test_png_extraction_round_trip():
    assert extract_png_card(_png_with({"name": "x"})) == {"name": "x"}
    assert extract_png_card(b"not a png") is None


def test_convert_card_maps_fields_and_warns():
    config, warnings = convert(_card())
    assert config["characters"][0]["name"] == "测试卡"
    assert "雨夜的旅店" in config["scenario"]
    assert "你推开门" in config["scenario"]
    assert any(block["position"] == "system" for block in config["prompt_blocks"])
    depth_block = next(b for b in config["prompt_blocks"] if b["position"] == "history")
    assert depth_block["depth"] == 4 and depth_block["role"] == "system"
    assert any(entry["depth"] == 2 for entry in config["lorebook"])
    assert "常驻" in [entry["title"] for entry in config["lorebook"]]
    assert any("regex" in w for w in warnings)
    assert any("tavern_helper" in w for w in warnings)
    # The produced config is importable by the project validators.
    parse_cast(config["characters"])
    parse_lorebook(config["lorebook"])
    parse_prompt_blocks(config["prompt_blocks"])


def test_convert_preset_maps_sampling_and_blocks():
    config, _ = convert(_preset())
    assert config["sampling"] == {
        "temperature": 1,
        "top_p": 0.99,
        "top_k": 110,
        "min_p": 0.02,
        "frequency_penalty": 0.2,
        "repetition_penalty": 1.05,
    }
    parse_sampling(config["sampling"])
    system = next(b for b in config["prompt_blocks"] if b["position"] == "system")
    assert system["role"] == "system" and "玩家" in system["content"]
    fake = next(b for b in config["prompt_blocks"] if b["role"] == "assistant")
    assert fake["position"] == "history" and fake["depth"] == 4
    # markers are not imported
    assert all(b["title"] != "对话记录" for b in config["prompt_blocks"])
    parse_prompt_blocks(config["prompt_blocks"])


def test_convert_world_info_and_bytes_json():
    wi = {"entries": {"0": {"keys": ["灯塔"], "content": "灯塔在礁石上。", "enabled": True}}}
    config, _ = convert(wi)
    assert config["lorebook"][0]["keys"] == ["灯塔"]
    parse_lorebook(config["lorebook"])

    config2, _ = convert_bytes(json.dumps(_card()).encode("utf-8"))
    assert config2["characters"][0]["name"] == "测试卡"


def test_convert_bytes_png_and_errors():
    config, _ = convert_bytes(_png_with(_card()))
    assert config["characters"][0]["name"] == "测试卡"
    with pytest.raises(ValueError):
        convert_bytes(b"not a card")
    with pytest.raises(ValueError):
        convert({"unknown": True})


def test_import_endpoint_maps_card_preset_and_rejects_garbage():
    from fastapi.testclient import TestClient

    from api.server import create_app
    from test_engine import FakeResolver

    with TestClient(create_app(FakeResolver)) as client:
        resp = client.post("/st/import", content=json.dumps(_card()).encode("utf-8"))
        assert resp.status_code == 200
        body = resp.json()
        assert body["config"]["characters"][0]["name"] == "测试卡"
        assert isinstance(body["warnings"], list) and body["warnings"]

        resp = client.post("/st/import", content=_png_with(_card()))
        assert resp.status_code == 200

        resp = client.post("/st/import", content=json.dumps(_preset()).encode("utf-8"))
        assert resp.status_code == 200
        assert resp.json()["config"]["sampling"]["top_k"] == 110

        assert client.post("/st/import", content=b"garbage").status_code == 400
        assert client.post("/st/import", content=b"").status_code == 400
