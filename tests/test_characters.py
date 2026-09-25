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


def test_host_claim_is_optional(tmp_path):
    """The host may observe without claiming, but a bad claim is rejected."""

    async def run():
        engine, sender, _ = await build_engine(tmp_path)
        # A claim that does not name a cast member is rejected.
        await engine.process_payload(
            "host",
            payload(
                "scenario_init",
                scenario="A gate.",
                characters=CAST_TWO,
                host_character="Stranger",
            ),
        )
        assert "does not exist" in sender.events_of_type("error")[-1].payload["msg"]
        assert engine.state is GameState.SCENARIO_INJECTION
        assert not engine.cast
        # An empty claim makes the host a pure observer.
        await engine.process_payload(
            "host",
            payload(
                "scenario_init",
                scenario="A gate.",
                characters=CAST_TWO,
                host_character="",
            ),
        )
        await engine.wait_for_inference()
        assert engine.state is GameState.AWAITING_PLAYERS
        assert engine.players["host"].character_name is None
        assert engine.claims == {}
        await engine.process_payload("host", payload("start_game"))
        await engine.wait_for_inference()
        assert engine.state is GameState.ACTIVE_TURN
        assert engine.active_player_id is None
        # A player joining now claims a character and becomes active.
        await engine.player_join("p1", {"name": "One", "character": "Player"})
        assert engine.active_player_id == "p1"
        await engine.shutdown()

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
        assert engine.claims["Free"] == "Two"

    asyncio.run(run())


def test_start_state_receives_cast_and_claims(tmp_path):
    """generate_start_state is called with the full cast and the claim map."""

    async def run():
        engine, sender, resolver = await build_engine(tmp_path)
        received = {}

        async def start_state(cast, claims, current_time=""):
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
        assert engine.claims["Free"] == "One"
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
        assert engine.claims["Free"] == "Two"
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


def test_player_can_join_as_a_spectator(tmp_path):
    """A player may join without a character and watch without acting."""

    async def run():
        engine, sender, _ = await build_engine(tmp_path)
        await set_scenario(engine, CAST_THREE, "Host")
        await engine.player_join("p1", {"name": "One", "character": ""})
        assert engine.players["p1"].character_name is None
        assert engine.claims == {"Host": "Host"}
        await engine.process_payload("host", payload("start_game"))
        await engine.wait_for_inference()
        assert engine.active_player_id == "host"
        # The spectator can chat but never acts.
        await engine.process_payload("p1", payload("chat", message="Watching"))
        assert sender.events_of_type("chat_echo")[-1].payload["chat"] == "Watching"
        assert engine.players["p1"].character_name is None
        await engine.shutdown()

    asyncio.run(run())


def test_spectator_reconnects_without_a_character(tmp_path):
    """A spectator can reconnect with an empty character claim."""

    async def run():
        engine, _, _ = await build_engine(tmp_path)
        await set_scenario(engine, CAST_TWO, "Host")
        await engine.player_join("p1", {"name": "One", "character": ""})
        token = engine.players["p1"].reconnect_token
        await engine.handle_disconnect("p1")
        await engine.player_join("p1", {"name": "One", "character": "", "reconnect_token": token})
        assert engine.players["p1"].character_name is None
        assert engine.players["p1"].is_connected
        await engine.shutdown()

    asyncio.run(run())


def test_spectator_joins_mid_game_without_pending(tmp_path):
    """A spectator may join during resolution without waiting a round."""

    async def run():
        engine, _, _ = await build_engine(tmp_path)
        await set_scenario(engine, CAST_TWO + [{"name": "Free"}], "Host")
        await engine.process_payload("host", payload("start_game"))
        await engine.wait_for_inference()
        await engine.process_payload("host", payload("action", action="Open the gate"))
        assert engine.state is GameState.AWAITING_LLM
        await engine.player_join("p1", {"name": "One", "character": ""})
        assert "p1" in engine.players and "p1" not in engine.pending_players
        assert engine.players["p1"].character_name is None
        await engine.wait_for_inference()
        await engine.shutdown()

    asyncio.run(run())


def test_spectators_do_not_count_toward_skip_votes(tmp_path):
    """Spectators are excluded from the skip-vote electorate."""

    async def run():
        engine, sender, _ = await build_engine(tmp_path)
        await set_scenario(engine, CAST_TWO + [{"name": "Free"}], "Host")
        await engine.player_join("p1", {"name": "One", "character": "Player"})
        await engine.player_join("obs", {"name": "Watcher", "character": ""})
        await engine.process_payload("host", payload("start_game"))
        await engine.wait_for_inference()
        assert engine.active_player_id == "host"
        await engine.process_payload("obs", payload("skip_vote"))
        assert sender.events_of_type("skip_vote")[-1].payload["needed"] == 1
        assert "host" not in engine.round_buffer
        await engine.shutdown()

    asyncio.run(run())


def test_returning_player_reclaims_their_character_seat(tmp_path):
    """A name can leave and later re-claim the same character seat."""

    async def run():
        engine, _, _ = await build_engine(tmp_path)
        await set_scenario(engine, CAST_THREE, "Host")
        await engine.player_join("p1", {"name": "One", "character": "Player"})
        await engine.process_payload("host", payload("start_game"))
        await engine.wait_for_inference()
        assert engine.claims["Player"] == "One"
        await engine.handle_disconnect("p1")
        assert not engine.players["p1"].is_connected
        assert engine.claims["Player"] == "One"  # seat retained after leaving
        await engine.player_join("p1new", {"name": "One", "character": "Player"})
        assert engine.claims["Player"] == "One"
        assert engine.players["p1new"].character_name == "Player"
        assert "p1" not in engine.players
        await engine.shutdown()

    asyncio.run(run())


def test_name_can_switch_to_spectating_while_keeping_its_character(tmp_path):
    """A name may switch to spectating while its character seat stays reserved."""

    async def run():
        engine, _, _ = await build_engine(tmp_path)
        await set_scenario(engine, CAST_TWO, "Host")
        await engine.player_join("p1", {"name": "One", "character": "Player"})
        assert engine.claims["Player"] == "One"
        await engine.handle_disconnect("p1")
        await engine.player_join("p1new", {"name": "One", "character": ""})
        assert engine.players["p1new"].character_name is None
        assert engine.claims["Player"] == "One"  # character seat retained
        await engine.shutdown()

    asyncio.run(run())


def test_a_new_name_on_a_reused_client_id_is_a_fresh_join(tmp_path):
    """A different name on a reused client id is a new player, not a reconnect."""

    async def run():
        engine, _, _ = await build_engine(tmp_path)
        await set_scenario(engine, CAST_TWO, "Host")
        await engine.handle_disconnect("host")
        await engine.player_join("host", {"name": "Alice", "character": "Player"})
        assert engine.players["host"].name == "Alice"
        assert engine.players["host"].character_name == "Player"
        assert engine.claims["Player"] == "Alice"
        await engine.shutdown()

    asyncio.run(run())
