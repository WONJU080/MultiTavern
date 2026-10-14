"""Region graph, items and event boundaries in strict scheduling mode."""

import asyncio
from pathlib import Path

from core.schemas import ActionReview
from logic.engine import GameEngine, restore_engine
from logic.transcript import GameTranscript
from test_engine import FakeResolver, FakeSender, payload

TIME_CONFIG = {
    "enabled": True,
    "start_day": 1,
    "start_minute": 480,
    "max_elapsed_minutes": 600,
    "default_elapsed_minutes": 30,
}
REGIONS = [
    {
        "id": "camp",
        "name": "营地",
        "arrival_text": "你们回到营地。",
        "items": ["key"],
        "links": [{"to": "cave", "requires": "沿山道", "requires_item": "key"}],
    },
    {
        "id": "cave",
        "name": "洞窟",
        "arrival_text": "阴冷的洞窟。",
        "items": ["altar"],
        "links": [{"to": "camp"}],
    },
]
ITEMS = [
    {
        "id": "key",
        "name": "铜钥匙",
        "region": "camp",
        "summary": "一把旧钥匙",
        "investigate_text": "钥匙上刻着洞窟的记号。",
        "clue": True,
        "key_item": True,
    },
    {
        "id": "altar",
        "name": "石祭坛",
        "region": "cave",
        "summary": "一座石祭坛",
        "investigate_text": "祭坛上有一道新鲜的划痕。",
        "clue": True,
    },
]


async def build_world(
    tmp_path: Path, events: list[dict[str, object]] | None = None
) -> tuple[GameEngine, FakeSender, FakeResolver]:
    """Start a strict game with a two-region world."""
    sender = FakeSender()
    resolver = FakeResolver()
    engine = GameEngine(sender, resolver)
    engine.transcript = GameTranscript(tmp_path / "logs")
    await engine.host_join("host", {"name": "Host"})
    await engine.process_payload(
        "host",
        payload(
            "scenario_init",
            scenario="A camp below a cave.",
            characters=[{"name": "Host"}, {"name": "Player"}],
            host_character="Host",
            time_config=TIME_CONFIG,
            regions=REGIONS,
            items=ITEMS,
            world={"start_region": "camp"},
            events=events,
        ),
    )
    await engine.wait_for_inference()
    await engine.player_join("p1", {"name": "Player", "character": "Player"})
    await engine.process_payload("host", payload("start_game"))
    await engine.wait_for_inference()
    return engine, sender, resolver


async def settle(engine: GameEngine) -> None:
    """Wait for any chained boundary jobs to finish."""
    for _ in range(6):
        task = engine.inference_task
        if task is None or task.done():
            return
        await engine.wait_for_inference()


def test_movement_requires_a_link_and_its_item(tmp_path: Path) -> None:
    """A gated link rejects the move until the required item is carried."""

    async def run() -> None:
        engine, sender, resolver = await build_world(tmp_path)
        assert engine.schedule_mode == "strict"
        assert engine.region_of["Host"] == "camp"

        resolver.review_results = [ActionReview(valid=True, move_to="cave", duration_minutes=20)]
        await engine.process_payload("host", payload("action", action="前往洞窟"))
        assert sender.events_of_type("error")[-1].payload["msg"] == "前往该地区需要携带：铜钥匙。"

        engine.item_location["key"] = "Host"
        resolver.review_results = [ActionReview(valid=True, move_to="cave", duration_minutes=20)]
        await engine.process_payload("host", payload("action", action="前往洞窟"))
        await engine.process_payload("p1", payload("hold"))
        await settle(engine)

        assert engine.region_of["Host"] == "cave"
        assert "cave" in engine.visited_regions
        assert engine.game_clock.format() == "Day 1 08:20"
        scenes = sender.events_of_type("scene_text")
        assert scenes and scenes[-1].payload["text"] == "阴冷的洞窟。"

    asyncio.run(run())


def test_pickup_moves_an_item_into_the_inventory(tmp_path: Path) -> None:
    """A pickup action transfers a present item to the acting character."""

    async def run() -> None:
        engine, sender, resolver = await build_world(tmp_path)
        resolver.review_results = [ActionReview(valid=True, take_item="key", duration_minutes=5)]
        await engine.process_payload("host", payload("action", action="拾起钥匙"))
        await engine.process_payload("p1", payload("hold"))
        await settle(engine)
        assert engine.item_location["key"] == "Host"

        await engine.process_payload("host", payload("map_request"))
        states = [
            event
            for client_id, event in sender.events
            if client_id == "host" and event.type == "map_state"
        ]
        assert states
        players = states[-1].payload["players"]
        host = next(item for item in players if item["name"] == "Host")
        assert "铜钥匙" in host["carried"]

    asyncio.run(run())


def test_investigation_reveals_fixed_text_once(tmp_path: Path) -> None:
    """Investigating a present item emits its fixed text once and records it."""

    async def run() -> None:
        engine, sender, resolver = await build_world(tmp_path)
        engine.region_of["Host"] = "cave"
        engine.visited_regions.add("cave")

        resolver.review_results = [
            ActionReview(valid=True, referenced_item="altar", duration_minutes=10)
        ]
        await engine.process_payload("host", payload("action", action="检查祭坛"))
        await engine.process_payload("p1", payload("hold"))
        await settle(engine)
        assert "altar" in engine.investigated_items
        scenes = sender.events_of_type("scene_text")
        assert scenes[-1].payload["text"] == "祭坛上有一道新鲜的划痕。"

        resolver.review_results = [
            ActionReview(valid=True, referenced_item="altar", duration_minutes=10)
        ]
        await engine.process_payload("host", payload("action", action="再看一次祭坛"))
        await engine.process_payload("p1", payload("hold"))
        await settle(engine)
        repeats = [
            event
            for event in sender.events_of_type("scene_text")
            if event.payload["text"] == "祭坛上有一道新鲜的划痕。"
        ]
        assert len(repeats) == 1

    asyncio.run(run())


def test_region_event_fires_once_on_enter(tmp_path: Path) -> None:
    """A region entry event fires once and announces itself when public."""

    async def run() -> None:
        events = [
            {
                "name": "陷阱触发",
                "region": "cave",
                "when": "enter",
                "description": "脚下石板塌陷。",
                "public": True,
                "once": True,
            }
        ]
        engine, sender, resolver = await build_world(tmp_path, events=events)
        engine.item_location["key"] = "Host"

        resolver.review_results = [ActionReview(valid=True, move_to="cave", duration_minutes=20)]
        await engine.process_payload("host", payload("action", action="前往洞窟"))
        await engine.process_payload("p1", payload("hold"))
        await settle(engine)
        assert "陷阱触发" in engine.fired_events
        region_notes = [
            (client_id, event)
            for client_id, event in sender.events
            if event.type == "system_msg" and "陷阱触发" in event.payload["msg"]
        ]
        assert region_notes
        assert all(client_id == "host" for client_id, _ in region_notes)

        resolver.review_results = [ActionReview(valid=True, move_to="camp", duration_minutes=20)]
        await engine.process_payload("host", payload("action", action="返回营地"))
        await engine.process_payload("p1", payload("hold"))
        await settle(engine)
        resolver.review_results = [ActionReview(valid=True, move_to="cave", duration_minutes=20)]
        await engine.process_payload("host", payload("action", action="再次前往洞窟"))
        await engine.process_payload("p1", payload("hold"))
        await settle(engine)
        announcements = [
            event.payload["msg"]
            for event in sender.events_of_type("system_msg")
            if "陷阱触发" in event.payload["msg"]
        ]
        assert len(announcements) == 1

    asyncio.run(run())


def test_time_event_fires_at_its_boundary_while_all_players_busy(tmp_path: Path) -> None:
    """A due time event becomes its own boundary even when everyone is acting."""

    async def run() -> None:
        events = [
            {
                "name": "钟响",
                "day": 1,
                "minute": 500,
                "description": "钟声传遍山谷。",
                "public": True,
            }
        ]
        engine, sender, resolver = await build_world(tmp_path, events=events)
        for cid, text in (("host", "挖"), ("p1", "搬")):
            resolver.review_results = [ActionReview(valid=True, duration_minutes=60)]
            await engine.process_payload(cid, payload("action", action=text))
        await settle(engine)
        assert "钟响" in engine.fired_events
        assert engine.game_clock.format() == "Day 1 09:00"
        messages = [event.payload["msg"] for event in sender.events_of_type("system_msg")]
        assert any("钟响" in message for message in messages)

    asyncio.run(run())


def test_world_state_survives_restore(tmp_path: Path) -> None:
    """Regions, locations and inventories are persisted and rebuilt."""

    async def run() -> None:
        engine, _, resolver = await build_world(tmp_path)
        resolver.review_results = [ActionReview(valid=True, take_item="key", duration_minutes=5)]
        await engine.process_payload("host", payload("action", action="拾起钥匙"))
        await engine.process_payload("p1", payload("hold"))
        await settle(engine)
        data = engine.to_persistent_dict()
        assert data["world_start_region"] == "camp"
        assert data["item_location"]["key"] == "Host"

        restored = restore_engine(data, FakeSender(), FakeResolver)
        assert set(restored.regions) == {"camp", "cave"}
        assert restored.item_location["key"] == "Host"
        assert restored.region_of["Host"] == "camp"

    asyncio.run(run())


def test_region_updates_are_applied_and_validated(tmp_path: Path) -> None:
    """AI region-state deltas update known regions; unknown ids are dropped."""

    async def run() -> None:
        engine, _, resolver = await build_world(tmp_path)
        resolver.region_updates = {"cave": "祭坛已经碎裂。", "nowhere": "不存在"}
        resolver.review_results = [ActionReview(valid=True, duration_minutes=10)]
        await engine.process_payload("host", payload("action", action="随手检查"))
        await engine.process_payload("p1", payload("hold"))
        await settle(engine)
        assert engine.region_state == {"cave": "祭坛已经碎裂。"}
        data = engine.to_persistent_dict()
        assert data["region_state"]["cave"] == "祭坛已经碎裂。"
        restored = restore_engine(data, FakeSender(), FakeResolver)
        assert restored.region_state == {"cave": "祭坛已经碎裂。"}

    asyncio.run(run())


def test_npc_time_event_fires_in_its_region(tmp_path: Path) -> None:
    """A region-scoped time event (NPC action) fires at its time and names the actor."""

    async def run() -> None:
        events = [
            {
                "name": "巡逻",
                "day": 1,
                "minute": 500,
                "region": "camp",
                "when": "time",
                "actor": "守卫",
                "description": "守卫沿营地绕行一圈。",
                "public": True,
            }
        ]
        engine, sender, resolver = await build_world(tmp_path, events=events)
        for cid, text in (("host", "挖"), ("p1", "搬")):
            resolver.review_results = [ActionReview(valid=True, duration_minutes=60)]
            await engine.process_payload(cid, payload("action", action=text))
        await settle(engine)
        assert "巡逻" in engine.fired_events
        assert engine.game_clock.format() == "Day 1 09:00"
        messages = [event.payload["msg"] for event in sender.events_of_type("system_msg")]
        assert any("「守卫」" in message and "巡逻" in message for message in messages)

    asyncio.run(run())


def test_region_outcomes_reach_every_player(tmp_path: Path) -> None:
    """Every resolution is party-shared, regardless of who acted or where."""

    async def run() -> None:
        regions = [dict(region) for region in REGIONS]
        sender = FakeSender()
        resolver = FakeResolver()
        engine = GameEngine(sender, resolver)
        engine.transcript = GameTranscript(tmp_path / "logs")
        await engine.host_join("host", {"name": "Host"})
        await engine.process_payload(
            "host",
            payload(
                "scenario_init",
                scenario="A camp below a cave.",
                characters=[{"name": "Host"}, {"name": "Player"}],
                host_character="Host",
                time_config=TIME_CONFIG,
                regions=regions,
                items=ITEMS,
                world={"start_region": "camp"},
            ),
        )
        await engine.wait_for_inference()
        await engine.player_join("p1", {"name": "Player", "character": "Player"})
        await engine.process_payload("host", payload("start_game"))
        await engine.wait_for_inference()

        # Host explores a region the other player has never entered.
        engine.region_of["Host"] = "cave"
        engine.visited_regions.add("cave")
        resolver.review_results = [
            ActionReview(valid=True, referenced_item="altar", duration_minutes=10)
        ]
        await engine.process_payload("host", payload("action", action="检查祭坛"))
        await engine.process_payload("p1", payload("hold"))
        await settle(engine)

        # The outcome is broadcast to the room, not filtered per region.
        updates = [event for _, event in sender.events if event.type == "state_update"]
        assert updates
        assert any("Host" in event.payload["player_resolutions"] for event in updates)
        scenes = [event for _, event in sender.events if event.type == "scene_text"]
        assert scenes

    asyncio.run(run())


def test_holding_does_not_advance_time(tmp_path: Path) -> None:
    """Holding every player keeps the clock put and does not fire future events."""

    async def run() -> None:
        events = [{"name": "晚宴", "day": 2, "minute": 0, "description": "开席", "public": True}]
        engine, _, _ = await build_world(tmp_path, events=events)
        await engine.process_payload("host", payload("hold"))
        await engine.process_payload("p1", payload("hold"))
        assert engine.game_clock.format() == "Day 1 08:00"
        assert "晚宴" not in engine.fired_events
        assert engine.inflight == {}
        assert set(engine.window) == {"host", "p1"}

    asyncio.run(run())


def test_non_carryable_item_cannot_be_taken(tmp_path: Path) -> None:
    """An item marked carryable=false is refused at pickup."""

    async def run() -> None:
        engine, sender, resolver = await build_world(tmp_path)
        engine.items["key"].carryable = False
        resolver.review_results = [ActionReview(valid=True, take_item="key", duration_minutes=5)]
        await engine.process_payload("host", payload("action", action="拾起钥匙"))
        assert sender.events_of_type("error")[-1].payload["msg"] == "这件物品无法携带。"

    asyncio.run(run())


def test_map_shows_paths_clues_and_packs(tmp_path: Path) -> None:
    """The shared map exposes reachable/locked paths, clue flags and packs."""

    async def run() -> None:
        engine, sender, _ = await build_world(tmp_path)
        engine.visited_regions.add("cave")
        engine.region_of["Host"] = "cave"

        async def snapshot() -> dict:
            await engine.process_payload("host", payload("map_request"))
            states = [
                event
                for client_id, event in sender.events
                if client_id == "host" and event.type == "map_state"
            ]
            return states[-1].payload

        state = await snapshot()
        regions = {region["id"]: region for region in state["regions"]}
        assert set(regions) == {"camp", "cave"}
        # Cave is locked from camp until the party carries the key.
        assert regions["camp"]["reachable"] == []
        assert regions["camp"]["locked"] == [{"id": "cave", "name": "洞窟"}]
        assert regions["cave"]["reachable"] == [{"id": "camp", "name": "营地"}]
        altar = next(item for item in regions["cave"]["items"] if item["id"] == "altar")
        assert altar["clue"] is True

        engine.item_location["key"] = "Host"
        state = await snapshot()
        regions = {region["id"]: region for region in state["regions"]}
        assert regions["camp"]["reachable"] == [{"id": "cave", "name": "洞窟"}]
        assert regions["camp"]["locked"] == []
        host = next(player for player in state["players"] if player["name"] == "Host")
        assert "铜钥匙" in host["carried"]

    asyncio.run(run())


def test_codex_lists_discovered_items_and_fired_events(tmp_path: Path) -> None:
    """The codex shows discovered items (origin + blurb) and fired events (blurb)."""

    async def run() -> None:
        events = [
            {
                "name": "陷阱触发",
                "region": "cave",
                "when": "enter",
                "description": "脚下石板塌陷。",
                "public": True,
                "once": True,
            }
        ]
        engine, sender, _ = await build_world(tmp_path, events=events)
        engine.item_location["key"] = "Host"
        engine.investigated_items.add("altar")
        engine.fired_events.add("陷阱触发")

        await engine.process_payload("host", payload("codex_request"))
        states = [
            event
            for client_id, event in sender.events
            if client_id == "host" and event.type == "codex_state"
        ]
        data = states[-1].payload
        ids = {item["id"] for item in data["items"]}
        assert {"key", "altar"} <= ids
        key = next(item for item in data["items"] if item["id"] == "key")
        assert key["origin"] == "营地"
        assert key["here"] is True
        altar = next(item for item in data["items"] if item["id"] == "altar")
        assert altar["origin"] == "洞窟"
        assert altar["investigated"] is True
        assert [event["name"] for event in data["events"]] == ["陷阱触发"]
        assert data["events"][0]["description"] == "脚下石板塌陷。"

    asyncio.run(run())


def test_key_items_are_marked_for_the_dm_and_the_players(tmp_path: Path) -> None:
    """`key_item` reaches the DM prompt and the player-facing payloads."""

    async def run() -> None:
        engine, sender, _ = await build_world(tmp_path)

        # The DM prompt marks the host-declared key item so it is not treated as scenery.
        context = engine._world_context_locked("Host")
        assert "铜钥匙" in context and "关键物品" in context
        # A non-key item in the same region is not marked.
        assert "石祭坛" not in context

        # The codex payload carries the flag through to the client. (The map lists only
        # items still lying in a region, so a taken item moves to the pack instead.)
        await engine.process_payload("host", payload("codex_request"))
        codex = sender.events_of_type("codex_state")[-1].payload
        by_id = {item["id"]: item for item in codex["items"]}
        # The key item is in the visited start region, so it is discovered and flagged.
        assert by_id["key"]["key_item"] is True
        # The cave has not been visited, so its altar is not in the codex yet.
        assert "altar" not in by_id
        engine.visited_regions.add("cave")
        await engine.process_payload("host", payload("codex_request"))
        codex = sender.events_of_type("codex_state")[-1].payload
        by_id = {item["id"]: item for item in codex["items"]}
        assert by_id["altar"]["key_item"] is False

        await engine.shutdown()

    asyncio.run(run())


def test_key_item_flag_survives_persistence(tmp_path: Path) -> None:
    """A restored room keeps the host's key-item marking."""

    async def run() -> None:
        engine, sender, _ = await build_world(tmp_path)
        restored = restore_engine(engine.to_persistent_dict(), sender, FakeResolver)
        assert restored.items["key"].key_item is True
        assert restored.items["altar"].key_item is False
        assert "关键物品" in restored._world_context_locked("Host")
        await engine.shutdown()
        await restored.shutdown()

    asyncio.run(run())
