"""Operator admin panel tests: password guard, room list and room closing."""

from uuid import uuid4

from fastapi.testclient import TestClient

from api.server import create_app
from core.config import settings
from logic.rooms import normalize_invite_code
from test_engine import FakeResolver
from test_priority_one_transport import create_room, receive_until


def test_admin_panel_requires_password_and_lists_and_closes_rooms():
    """The admin panel lists running rooms and can close them."""
    settings.server.admin_password = "secret-admin"
    app = create_app(FakeResolver)
    host_id = str(uuid4())
    with TestClient(app) as client:
        response = client.get("/admin/rooms")
        assert response.status_code == 200
        assert "密码" in response.text
        response = client.post(
            "/admin/login", data={"password": "secret-admin"}, follow_redirects=False
        )
        assert response.status_code == 303
        client.cookies.set("anyworld_admin", response.cookies["anyworld_admin"])
        response = client.get("/admin/rooms")
        assert "运行中的房间" in response.text
        with client.websocket_connect(f"/ws/{host_id}") as host:
            create_room(host, host_id, "Host", admin_password="secret-admin")
            code = receive_until(host, "auth_ok")["payload"]["invite_code"]
        response = client.get("/admin/rooms")
        assert code in response.text
        assert "Host" in response.text
        client.post(f"/admin/rooms/{normalize_invite_code(code)}/close")
        assert normalize_invite_code(code) not in app.state.registry.rooms
        response = client.get("/admin/rooms")
        assert "当前没有运行中的房间" in response.text


def test_admin_panel_rejects_wrong_password():
    """A wrong password never grants access to the panel."""
    settings.server.admin_password = "secret-admin"
    with TestClient(create_app(FakeResolver)) as client:
        client.post("/admin/login", data={"password": "wrong"})
        response = client.get("/admin/rooms")
        assert "密码" in response.text
        assert "运行中的房间" not in response.text


def test_admin_panel_is_open_when_no_password_is_configured():
    """Without an admin password the panel is directly viewable."""
    settings.server.admin_password = None
    with TestClient(create_app(FakeResolver)) as client:
        response = client.get("/admin/rooms")
        assert "运行中的房间" in response.text


def test_admin_panel_links_to_room_transcripts():
    """A live room with a started game exposes its story record to the operator."""
    settings.server.admin_password = "secret-admin"
    app = create_app(FakeResolver)
    host_id = str(uuid4())
    with TestClient(app) as client:
        response = client.post(
            "/admin/login", data={"password": "secret-admin"}, follow_redirects=False
        )
        client.cookies.set("anyworld_admin", response.cookies["anyworld_admin"])
        with client.websocket_connect(f"/ws/{host_id}") as host:
            create_room(host, host_id, "Host", admin_password="secret-admin")
            code = receive_until(host, "auth_ok")["payload"]["invite_code"]
            host.send_json(
                {
                    "event_type": "scenario_init",
                    "data": {
                        "scenario": "A gate.",
                        "characters": [{"name": "Host"}],
                        "host_character": "Host",
                    },
                }
            )
            receive_until(host, "scenario_ready")
            host.send_json({"event_type": "start_game", "data": {}})
            receive_until(host, "turn_directive")
        response = client.get("/admin/rooms")
        assert "查看记录" in response.text
        record = client.get(f"/admin/rooms/{normalize_invite_code(code)}/record")
        assert record.status_code == 200
        assert "Opening scenario" in record.text
        assert "Host stand at a gate." in record.text


def test_admin_record_requires_password_and_reports_missing_rooms():
    """The record route is guarded and answers cleanly for unknown rooms."""
    settings.server.admin_password = "secret-admin"
    with TestClient(create_app(FakeResolver)) as client:
        response = client.get("/admin/rooms/ABC123/record", follow_redirects=False)
        assert response.status_code == 303
        login = client.post(
            "/admin/login", data={"password": "secret-admin"}, follow_redirects=False
        )
        client.cookies.set("anyworld_admin", login.cookies["anyworld_admin"])
        response = client.get("/admin/rooms/ABC123/record")
        assert response.status_code == 404
        assert "没有剧情记录" in response.text
