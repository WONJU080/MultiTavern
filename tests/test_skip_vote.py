"""Skip-vote mechanics: unanimous votes skip and remove the active player."""

import asyncio
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from api.server import create_app
from logic.engine import GameEngine, SKIP_ACTION
from logic.transcript import GameTranscript
from test_engine import FakeSender, payload
from test_priority_one_lifecycle import ControlledResolver
from test_priority_one_transport import create_room, join_room, receive_until

CAST_THREE = [{"name": "Host"}, {"name": "Player"}, {"name": "Free"}]


async def setup_game(
    tmp_path: Path,
) -> tuple[GameEngine, FakeSender, ControlledResolver]:
    """Build a started three-character game with the host as active player."""
    resolver = ControlledResolver()
    sender = FakeSender()
    engine = GameEngine(sender, resolver)
    engine.transcript = GameTranscript(tmp_path)
    await engine.host_join("host", {"name": "Host"})
    await engine.process_payload(
        "host",
        payload(
            "scenario_init",
            scenario="A locked gate.",
            characters=CAST_THREE,
            host_character="Host",
        ),
    )
    await engine.wait_for_inference()
    await engine.player_join("p1", {"name": "One", "character": "Player"})
    await engine.player_join("p2", {"name": "Two", "character": "Free"})
    await engine.process_payload("host", payload("start_game"))
    await engine.wait_for_inference()
    return engine, sender, resolver


def test_self_vote_is_rejected(tmp_path):
    """The active player cannot vote to skip themselves."""

    async def run():
        engine, sender, _ = await setup_game(tmp_path)
        assert engine.active_player_id == "host"
        await engine.process_payload("host", payload("skip_vote"))
        assert "cannot vote to skip yourself" in sender.events_of_type("error")[-1].payload["msg"]
        assert not engine.round_buffer

    asyncio.run(run())


def test_vote_outside_active_turn_is_rejected(tmp_path):
    """Votes are only accepted while a turn is active."""

    async def run():
        engine, sender, resolver = await setup_game(tmp_path)
        await engine.process_payload("host", payload("action", action="Open the gate"))
        await engine.process_payload("p1", payload("action", action="Wait"))
        await engine.process_payload("p2", payload("action", action="Wait"))
        assert engine.state.name == "AWAITING_LLM"
        await engine.process_payload("p1", payload("skip_vote"))
        assert (
            "only allowed during an active turn"
            in sender.events_of_type("error")[-1].payload["msg"]
        )
        assert not sender.events_of_type("skip_vote")
        await engine.wait_for_inference()

    asyncio.run(run())


def test_partial_votes_do_not_skip(tmp_path):
    """A skip requires every eligible online player to agree."""

    async def run():
        engine, sender, _ = await setup_game(tmp_path)
        await engine.process_payload("p1", payload("skip_vote"))
        event = sender.events_of_type("skip_vote")[-1].payload
        assert event == {"target": "Host", "voter": "One", "votes": 1, "needed": 2}
        assert "host" not in engine.round_buffer
        assert engine.active_player_id == "host"
        assert not engine.players["host"].skip_pending

    asyncio.run(run())


def test_unanimous_vote_skips_and_removes_the_player_at_commit(tmp_path):
    """All online players agreeing skips the turn and removes the player."""

    async def run():
        engine, sender, resolver = await setup_game(tmp_path)
        await engine.process_payload("p1", payload("skip_vote"))
        await engine.process_payload("p2", payload("skip_vote"))
        assert engine.round_buffer["host"] == SKIP_ACTION
        assert engine.players["host"].skip_pending
        assert engine.active_player_id == "p1"
        await engine.process_payload("p1", payload("action", action="Wait"))
        await engine.process_payload("p2", payload("action", action="Wait"))
        await engine.wait_for_inference()
        assert engine.round_counter == 1
        actions = resolver.received_actions[-1]
        assert set(actions) == {"Host", "Player", "Free"}
        assert "skipped by a unanimous vote" in actions["Host"]
        assert "Host" not in engine.players and "host" not in engine.players
        assert "Host" not in engine.claims
        assert engine.join_order == ["p1", "p2"]
        removed = sender.events_of_type("removed")[-1]
        removed_targets = [e for _, e in sender.events if e.type == "removed"]
        assert len(removed_targets) == 1
        assert "unanimous skip vote" in removed.payload["msg"]
        assert "Rejoin" in removed.payload["msg"]
        assert engine.active_player_id == "p1"
        await engine.shutdown()

    asyncio.run(run())


def test_skipped_host_rejoin_restores_host_powers(tmp_path):
    """A skipped host who rejoins keeps host powers for end/retry."""

    async def run():
        engine, _, _ = await setup_game(tmp_path)
        await engine.process_payload("p1", payload("skip_vote"))
        await engine.process_payload("p2", payload("skip_vote"))
        await engine.process_payload("p1", payload("action", action="Wait"))
        await engine.process_payload("p2", payload("action", action="Wait"))
        await engine.wait_for_inference()
        assert "host" not in engine.players
        await engine.player_join("host", {"name": "Host", "character": "Host"})
        assert engine.players["host"].is_host
        assert engine.claims["Host"] == "Host"
        await engine.shutdown()

    asyncio.run(run())


def test_skipped_player_can_rejoin_on_the_same_socket():
    """End-to-end: the removed player re-enters the join flow on their socket."""
    app = create_app(ControlledResolver)
    host_id, p1_id, p2_id = str(uuid4()), str(uuid4()), str(uuid4())
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
            with (
                client.websocket_connect(f"/ws/{p1_id}") as p1,
                client.websocket_connect(f"/ws/{p2_id}") as p2,
            ):
                join_room(p1, p1_id, code, "One", "Player")
                receive_until(p1, "auth_ok")
                join_room(p2, p2_id, code, "Two", "Free")
                receive_until(p2, "auth_ok")
                host.send_json({"event_type": "start_game", "data": {}})
                receive_until(host, "turn_directive")
                p1.send_json({"event_type": "skip_vote", "data": {}})
                receive_until(p1, "skip_vote")
                p2.send_json({"event_type": "skip_vote", "data": {}})
                receive_until(p2, "skip_vote")
                # The round still needs the remaining actions before the removal commits.
                p1.send_json({"event_type": "action", "data": {"action": "Wait"}})
                receive_until(p1, "turn_directive")
                p2.send_json({"event_type": "action", "data": {"action": "Wait"}})
                receive_until(host, "removed")
                # The removed host rejoins on the same socket without a token.
                host.send_json(
                    {
                        "event_type": "join_room",
                        "data": {"name": "Host", "invite_code": code, "character": "Host"},
                    }
                )
                auth_ok = receive_until(host, "auth_ok")["payload"]
                assert auth_ok["is_host"] is True
                assert auth_ok["character"] == "Host"
