"""Narration is checked against the room's authoritative objects."""

import pytest

from core.schemas import RoundResolution
from logic.llm_manager import LLMContextManager, LLMResolutionError


def build_manager() -> LLMContextManager:
    """Return a manager grounded on one item in one region."""
    manager = LLMContextManager()
    manager.llm.max_retries = 1
    manager.set_world_grounding(
        items=[("ledger", "泛黄的账本"), ("red_key", "红色的钥匙")],
        regions=[("study", "书房"), ("hall", "宴厅")],
        corpus="书房里有一本泛黄的账本，缺页上写着灯油未领。红色的钥匙藏在钟楼。",
    )
    return manager


def resolution(narrative: str, **kwargs: object) -> RoundResolution:
    """Build a resolution with a single player outcome."""
    return RoundResolution(
        global_narrative=narrative,
        player_resolutions={"甲": "他记住了眼前的细节。"},
        **kwargs,
    )


def test_invented_object_is_rejected_then_kept_on_the_last_attempt() -> None:
    """A counted object outside the catalogue is corrected once, then tolerated."""
    manager = build_manager()
    invented = resolution("他在门槛缝里抠出一小片折起的灯芯纸。")
    with pytest.raises(LLMResolutionError):
        manager._check_world_grounding(invented, 0)
    manager._check_world_grounding(invented, 1)


def test_a_known_object_may_be_mentioned_even_when_it_is_elsewhere() -> None:
    """Naming a far-away key on a door is legitimate and must not be blocked."""
    manager = build_manager()
    manager._check_world_grounding(resolution("大门上写着：持红色的钥匙者方可入内。"), 0)


def test_an_object_from_the_card_vocabulary_passes() -> None:
    """A prop the scenario established is grounded even without an item id."""
    manager = build_manager()
    manager._check_world_grounding(resolution("他掏出小螺丝刀，拧开钟壳。"), 0)


def test_unknown_region_updates_are_rejected() -> None:
    """region_updates must use region ids that exist in this room."""
    manager = build_manager()
    bad = resolution(
        "走廊的灯闪了一下。",
        region_updates={"attic": "阁楼被打开了。"},
    )
    with pytest.raises(LLMResolutionError):
        manager._check_world_grounding(bad, 0)


def test_mechanism_change_must_be_recorded() -> None:
    """Breaking a lock or mechanism has to leave a region state behind."""
    manager = build_manager()
    with pytest.raises(LLMResolutionError):
        manager._check_world_grounding(
            resolution("椅子里的金属线崩断，楼上钟楼方向传来松脱声响。"), 0
        )
    recorded = resolution(
        "椅子里的金属线崩断，楼上钟楼方向传来松脱声响。",
        region_updates={"hall": "主位椅子塌了，椅面下露出暗格。"},
    )
    manager._check_world_grounding(recorded, 0)


def test_manager_without_a_world_never_blocks() -> None:
    """Rooms without regions or items (pure chat) keep working untouched."""
    manager = LLMContextManager()
    manager._check_world_grounding(resolution("他抠出一枚凭空出现的铜片。"), 0)


def test_an_investigated_clue_may_be_quoted_but_not_before() -> None:
    """Fixed clue text is blocked until the engine records the investigation."""
    manager = build_manager()
    manager.set_world_grounding(
        items=[("ledger", "泛黄的账本")],
        regions=[("study", "书房"), ("hall", "宴厅")],
        corpus="书房里有一本泛黄的账本。",
        clues=[
            (
                "ledger",
                "泛黄的账本",
                "五十年前一页被撕去，断口很新；相邻一页记着一笔『灯油：未领』。",
            )
        ],
    )
    leak = resolution("账本摊开着，相邻一页记着一笔『灯油：未领』。")
    manager.set_round_context(investigated=[])
    with pytest.raises(LLMResolutionError):
        manager._check_world_grounding(leak, 0)
    manager.set_round_context(investigated=["ledger"])
    manager._check_world_grounding(leak, 0)


def test_a_character_cannot_be_narrated_into_another_region() -> None:
    """The outcome must keep a character where the engine placed them."""
    manager = build_manager()
    manager.set_round_context(investigated=[], positions={"甲": "长廊"})
    away = RoundResolution(
        global_narrative="走廊的灯灭了。",
        player_resolutions={"甲": "你在书房里翻开了账本。"},
    )
    with pytest.raises(LLMResolutionError):
        manager._check_world_grounding(away, 0)


def test_standing_at_a_door_is_not_a_teleport() -> None:
    """Being at the door of another region is not being inside it."""
    manager = build_manager()
    manager.set_round_context(investigated=[], positions={"甲": "长廊"})
    manager._check_world_grounding(
        RoundResolution(
            global_narrative="走廊的灯灭了。",
            player_resolutions={"甲": "你在书房门外站了一会儿。"},
        ),
        0,
    )


def test_a_resolved_move_is_allowed() -> None:
    """When the engine already moved the character, the new region is correct."""
    manager = build_manager()
    manager.set_round_context(investigated=[], positions={"甲": "书房"})
    manager._check_world_grounding(
        RoundResolution(
            global_narrative="书房里很冷。",
            player_resolutions={"甲": "你在书房里翻开了账本。"},
        ),
        0,
    )


def test_claiming_arrival_in_another_region_is_rejected() -> None:
    """'你回到宴厅' is an arrival claim and must match the engine position."""
    manager = build_manager()
    manager.set_round_context(investigated=[], positions={"甲": "庄园大门"})
    with pytest.raises(LLMResolutionError):
        manager._check_world_grounding(
            RoundResolution(
                global_narrative="门厅里没有人。",
                player_resolutions={"甲": "你推门回到宴厅，坐下。"},
            ),
            0,
        )
