"""HTTP asset-serving smoke tests."""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api.server import app


def test_index_and_static_assets_are_served() -> None:
    """Serve the index page and static assets."""
    with TestClient(app) as client:
        index = client.get("/")
        script = client.get("/static/js/app.js")
        stylesheet = client.get("/static/css/style.css")

    assert index.status_code == 200
    assert "login-modal" in index.text
    assert script.status_code == 200
    assert "WebSocket" in script.text
    assert stylesheet.status_code == 200
    assert "grid-template-areas" in stylesheet.text


@pytest.mark.parametrize("path", ["/create", "/join", "/rooms"])
def test_landing_routes_serve_the_same_entry_page(path: str) -> None:
    """Each landing route is deep-linkable and serves the SPA shell."""
    with TestClient(app) as client:
        response = client.get(path)
    assert response.status_code == 200
    assert "MultiTavern" in response.text
    assert "login-modal" in response.text


def test_llm_model_scan_requires_key() -> None:
    """Reject a model-scan request without an API key before any network call."""
    with TestClient(app) as client:
        response = client.post("/api/llm/models", json={})
    assert response.status_code == 400
    assert "API Key" in response.json()["error"]


def test_llm_model_scan_returns_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """Return the model ids reported by the resolver to the create form."""

    async def fake_models(api_key: object, endpoint: object = None) -> list[str]:
        assert api_key == "sk-test"
        assert endpoint == "https://models.test/v1"
        return ["alpha", "beta"]

    monkeypatch.setattr("api.server.list_available_models", fake_models)
    with TestClient(app) as client:
        response = client.post(
            "/api/llm/models",
            json={"api_key": "sk-test", "endpoint": "https://models.test/v1"},
        )
    assert response.status_code == 200
    assert response.json() == {"models": ["alpha", "beta"]}
    assert response.headers["cache-control"] == "no-store"


def test_websocket_rejects_malformed_client_id() -> None:
    """Reject a non-UUID client id on the WebSocket."""
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws/not-a-uuid"):
                pass

    assert exc_info.value.code == 1008
