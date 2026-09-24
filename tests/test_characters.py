"""Cast creation, character claiming and mid-game join tests."""

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.server import create_app
from core.schemas import RoundResolution
from logic.engine import GameEngine, GameState
from logic.lobby import parse_cast
from logic.rooms import normalize_invite_code
from logic.transcript import GameTranscript
from test_engine import FakeSender, payload
from test_priority_one_lifecycle import ControlledResolver
from test_priority_one_transport import create_room, receive_until

CAST_TWO = [{"name": "Host"}, {"name": "Player"}]
CAST_THREE = [{"name": "Host"}, {"name": "Player"}, {"name": "Free"}]


async def build_engine(tmp_path: Path) -> tuple[GameEngine, FakeSender, ControlledResolver]:
    """Build an engine with the host in the lobby."""
    resolver = ControlledResolver()
    sender = FakeSender()
    engine = GameEngine(sender, resolver)
    engine.transcript = GameTranscript(tmp_path)
    await engine.host_join("host", {"name": "Host"})
    return engine, sender, resolver


async def set_scenario(engine: GameEngine, characters: list[dict], host_character: str) -> None:
    """Submit the host's scenario with the given cast."""
    await engine.process_payload(
        "host",
        payload(
            "scenario_init",
            scenario="A locked gate.",
            characters=characters,
            host_character=host_character,
        ),
    )
    await engine.wait_for_inference()


def test_parse_cast_validates_structure():
    """Cast input is strictly validated."""
    cast = parse_cast([{"name": "金元珠", "description": "剑客"}, {"name": "Ram"}])
    assert [c.name for c in cast] == ["金元珠", "Ram"]
    with pytest.raises(ValueError, match="non-empty list"):
        parse_cast([])
    with pytest.raises(ValueError, match="unique"):
        parse_cast([{"name": "A"}, {"name": "a"}])
    with pytest.raises(ValueError, match="must be an object"):
        parse_cast(["A"])
    with pytest.raises(ValueError, match="must contain 1-40"):
        parse_cast([{"name": ""}])


def test_host_must_claim_one_of_the_cast(tmp_path):
    """The host cannot create a scenario without claiming a character."""

    async def run():
        engine, sender, _ = await build_engine(tmp_path)
        await engine.process_payload(
            "host",
            payload(
                "scenario_init",
                scenario="A gate.",
                characters=CAST_TWO,
                host_character="Stranger",
            ),
        )
        assert "must claim one" in sender.events_of_type("error")[-1].payload["msg"]
        assert engine.state is GameState.SCENARIO_INJECTION
        assert not engine.cast

    asyncio.run(run())


def test_claiming_the_same_character_twice_is_rejected(tmp_path):
    """One character can only be claimed by one player."""

    async def run():
        engine, sender, _ = await build_engine(tmp_path)
        await set_scenario(engine, CAST_THREE, "Host")
        await engine.player_join("p1", {"name": "One", "character": "Player"})
        with pytest.raises(ValueError, match="already been claimed"):
            await engine.player_join("p2", {"name": "Two", "character": "Player"})
        with pytest.raises(ValueError, match="does not exist"):
            await engine.player_join("p2", {"name": "Two", "character": "Ghost"})
        await engine.player_join("p2", {"name": "Two", "character": "Free"})
        assert engine.claims["Free"] == "p2"

    asyncio.run(run())


def test_start_state_receives_cast_and_claims(tmp_path):
    """generate_start_state is called with the full cast and the claim map."""

    async def run():
        engine, sender, resolver = await build_engine(tmp_path)
        received = {}

        async def start_state(cast, claims):
            received["cast"] = [c.name for c in cast]
            received["claims"] = dict(claims)
            return RoundResolution(global_narrative="Everyone gathers.", player_resolutions={})

        resolver.generate_start_state = start_state
        await set_scenario(engine, CAST_THREE, "Host")
        await engine.player_join("p1", {"name": "One", "character": "Player"})
        await engine.process_payload("host", payload("start_game"))
        await engine.wait_for_inference()
        assert received["cast"] == ["Host", "Player", "Free"]
        assert received["claims"] == {"Host": "Host", "Player": "One"}
        await engine.shutdown()

    asyncio.run(run())


def test_mid_game_join_activates_in_the_next_round(tmp_path):
    """A mid-round joiner is excluded from the current round and acts next round."""

    async def run():
        engine, _, resolver = await build_engine(tmp_path)
        await set_scenario(engine, CAST_TWO + [{"name": "Free"}], "Host")
        await engine.process_payload("host", payload("start_game"))
        await engine.wait_for_inference()
        # Launch round 1 with the host's action only.
        await engine.process_payload("host", payload("action", action="Open the gate"))
        assert engine.state is GameState.AWAITING_LLM
        await engine.player_join("p1", {"name": "One", "character": "Free"})
        assert "p1" in engine.pending_players and "p1" not in engine.players
        await engine.wait_for_inference()
        assert engine.round_counter == 1
        assert "p1" in engine.players and not engine.pending_players
        assert engine.claims["Free"] == "p1"
        # Round 2 must include the new player with a handover note.
        await engine.process_payload("host", payload("action", action="Wait"))
        await engine.process_payload("p1", payload("action", action="Look around"))
        await engine.wait_for_inference()
        assert engine.round_counter == 2
        actions = resolver.received_actions[-1]
        assert set(actions) == {"Host", "Free"}
        assert "no longer DM-controlled" in actions["Free"]
        assert "no longer DM-controlled" not in actions["Host"]
        await engine.shutdown()

    asyncio.run(run())


def test_mid_game_join_with_empty_buffer_is_immediate(tmp_path):
    """A joiner arriving before any action is collected joins the current round."""

    async def run():
        engine, _, resolver = await build_engine(tmp_path)
        await set_scenario(engine, CAST_TWO + [{"name": "Free"}], "Host")
        await engine.process_payload("host", payload("start_game"))
        await engine.wait_for_inference()
        assert engine.state is GameState.ACTIVE_TURN and not engine.round_buffer
        await engine.player_join("p1", {"name": "One", "character": "Free"})
        assert "p1" in engine.players and not engine.pending_players
        await engine.process_payload("host", payload("action", action="Wait"))
        await engine.process_payload("p1", payload("action", action="Look around"))
        await engine.wait_for_inference()
        assert engine.round_counter == 1
        actions = resolver.received_actions[-1]
        assert set(actions) == {"Host", "Free"}
        assert "no longer DM-controlled" in actions["Free"]
        await engine.shutdown()

    asyncio.run(run())


def test_pending_joiner_disconnect_releases_the_claim(tmp_path):
    """A joiner who leaves before activation returns the character to the DM."""

    async def run():
        engine, sender, _ = await build_engine(tmp_path)
        await set_scenario(engine, CAST_TWO + [{"name": "Free"}], "Host")
        await engine.process_payload("host", payload("start_game"))
        await engine.wait_for_inference()
        await engine.process_payload("host", payload("action", action="Open the gate"))
        await engine.player_join("p1", {"name": "One", "character": "Free"})
        assert "Free" in engine.claims
        await engine.handle_disconnect("p1")
        assert "Free" not in engine.claims
        assert "p1" not in engine.pending_players
        # The character can now be claimed by someone else.
        await engine.player_join("p2", {"name": "Two", "character": "Free"})
        assert engine.claims["Free"] == "p2"
        await engine.wait_for_inference()
        await engine.shutdown()

    asyncio.run(run())


def test_room_info_lists_characters_and_claims():
    """Pre-join room_info exposes the cast and its claim status."""
    app = create_app(ControlledResolver)
    host_id, probe_id = str(uuid4()), str(uuid4())
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/{host_id}") as host:
            create_room(host, host_id, "Host")
            code = receive_until(host, "auth_ok")["payload"]["invite_code"]
            host.send_json(
                {
                    "event_type": "scenario_init",
                    "data": {
                        "scenario": "A gate.",
                        "characters": CAST_THREE,
                        "host_character": "Host",
                    },
                }
            )
            receive_until(host, "scenario_ready")
            with client.websocket_connect(f"/ws/{probe_id}") as probe:
                probe.send_json({"event_type": "room_info", "data": {"invite_code": code}})
                info = receive_until(probe, "room_info")["payload"]
                assert info["accepting_new"] is True
                by_name = {c["name"]: c for c in info["characters"]}
                assert by_name["Host"]["claimed_by"] == "Host"
                assert by_name["Player"]["claimed_by"] is None
                assert by_name["Free"]["claimed_by"] is None
                room = app.state.registry.rooms[normalize_invite_code(code)]
                assert room is not None
