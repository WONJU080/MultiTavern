"""FastAPI routes and a WebSocket gateway that routes clients to rooms."""

import asyncio
import hashlib
import hmac
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from time import monotonic
from urllib.parse import parse_qs
from uuid import UUID

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from api.connections import ConnectionManager
from core.config import settings
from core.schemas import ClientPayload, ServerEvent
from logic.llm_manager import LLMContextManager
from logic.rooms import Room, RoomRegistry, format_invite_code

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]

_ROOM_EVENTS = ("create_room", "join_room", "room_info")


def _admin_cookie_valid(request: Request) -> bool:
    """Return whether the request carries a valid admin-panel cookie."""
    admin = settings.server.admin_password
    if admin is None:
        return True
    token = request.cookies.get("anyworld_admin")
    if not token:
        return False
    expected = hashlib.sha256(admin.encode("utf-8")).hexdigest()
    return hmac.compare_digest(token, expected)


def _room_rows(registry: RoomRegistry) -> list[dict[str, str]]:
    """Build display rows for the admin room list."""
    rows = []
    for code, room in sorted(registry.rooms.items(), key=lambda item: item[1].created_at):
        engine = room.engine
        humans = [p.name for p in engine.players.values()] + [
            p.name for p in engine.pending_players.values()
        ]
        rows.append(
            {
                "key": code,
                "code": format_invite_code(code),
                "state": engine.state.name,
                "title": engine.scenario_title or "—",
                "players": ", ".join(humans) or "—",
                "claimed": str(len(engine.claims)),
                "created": (
                    datetime.now() - timedelta(seconds=max(0.0, monotonic() - room.created_at))
                ).strftime("%H:%M:%S"),
                "idle": f"{max(0, int(monotonic() - room.last_active))}s",
            }
        )
    return rows


def create_app(resolver_factory=LLMContextManager) -> FastAPI:
    """Build the FastAPI app with its lifespan, routes and WebSocket gateway."""

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        """Manage the pending gateway, room registry and sweep on startup/shutdown."""
        manager = ConnectionManager()
        registry = RoomRegistry(resolver_factory)
        application.state.manager = manager
        application.state.registry = registry
        registry.start_sweep()
        try:
            yield
        finally:
            await registry.close_all()
            await manager.close()

    application = FastAPI(title="Anyworld", lifespan=lifespan)
    application.mount("/static", StaticFiles(directory=PROJECT_ROOT / "static"), name="static")
    templates = Jinja2Templates(directory=PROJECT_ROOT / "templates")

    @application.get("/", response_class=HTMLResponse)
    async def get_index(request: Request) -> HTMLResponse:
        """Serve the main HTML page."""
        return templates.TemplateResponse(request=request, name="index.html")

    @application.get("/admin/rooms", response_class=HTMLResponse)
    async def admin_rooms(request: Request) -> HTMLResponse:
        """Serve the operator room overview, guarded by the admin password."""
        if not _admin_cookie_valid(request):
            error = "Incorrect password." if "bad" in request.query_params else ""
            return templates.TemplateResponse(request, "admin_login.html", {"error": error})
        return templates.TemplateResponse(
            request,
            "admin_rooms.html",
            {"rooms": _room_rows(request.app.state.registry)},
        )

    @application.post("/admin/login")
    async def admin_login(request: Request) -> RedirectResponse:
        """Validate the admin password and set the panel cookie."""
        admin = settings.server.admin_password
        body = await request.body()
        supplied = parse_qs(body.decode("utf-8")).get("password", [""])[0]
        if admin is None or hmac.compare_digest(supplied, admin):
            response = RedirectResponse("/admin/rooms", status_code=303)
            response.set_cookie(
                "anyworld_admin",
                hashlib.sha256((admin or "").encode("utf-8")).hexdigest(),
                httponly=True,
            )
            return response
        return RedirectResponse("/admin/rooms?bad=1", status_code=303)

    @application.post("/admin/rooms/{code}/close")
    async def admin_close_room(code: str, request: Request) -> RedirectResponse:
        """Close a room from the operator panel."""
        if not _admin_cookie_valid(request):
            return RedirectResponse("/admin/rooms", status_code=303)
        registry = request.app.state.registry
        room = registry.rooms.get(code)
        if room is not None:
            await registry.shutdown_room(room, "The room was closed by the server operator.")
        return RedirectResponse("/admin/rooms", status_code=303)

    @application.websocket("/ws/{client_id}")
    async def websocket_endpoint(websocket: WebSocket, client_id: str) -> None:
        """Authenticate a client into a room and route its messages to that room."""
        try:
            if str(UUID(client_id)) != client_id:
                raise ValueError("Noncanonical UUID")
        except (ValueError, AttributeError):
            await websocket.close(code=1008, reason="client_id must be a canonical UUID")
            return
        manager = websocket.app.state.manager
        registry = websocket.app.state.registry
        if not await manager.connect(client_id, websocket):
            return
        deadline = asyncio.get_running_loop().time() + settings.server.auth_timeout_seconds
        attempts = 0
        authenticated = False
        room: Room | None = None
        active_manager: ConnectionManager = manager

        try:
            while True:
                try:
                    if not authenticated:
                        attempts += 1
                        async with asyncio.timeout_at(deadline):
                            raw_payload = await websocket.receive_json()
                    else:
                        raw_payload = await websocket.receive_json()
                    payload = ClientPayload.model_validate(raw_payload)
                    # A player removed by a skip vote is no longer part of the room's
                    # domain state; their socket re-enters the join flow.
                    if authenticated and (
                        client_id not in room.engine.players
                        and client_id not in room.engine.pending_players
                    ):
                        authenticated = False
                    if not authenticated:
                        if payload.event_type not in _ROOM_EVENTS:
                            raise ValueError("Create or join a room before sending game messages.")
                        if payload.event_type == "room_info":
                            info_room = registry.find_room(payload.data.get("invite_code"))
                            await active_manager.send_socket(
                                websocket,
                                ServerEvent(
                                    type="room_info",
                                    payload=info_room.engine.room_info_payload(),
                                ),
                            )
                            continue
                        if payload.event_type == "create_room":
                            room = registry.create_room(client_id, payload.data)
                            previous = []
                            try:
                                await room.engine.host_join(
                                    client_id,
                                    payload.data,
                                    activate=lambda: previous.append(
                                        room.connections.promote(client_id, websocket)
                                    ),
                                )
                            except ValueError:
                                if not previous:
                                    registry.discard(room.code)
                                else:
                                    active_manager = room.connections
                                    await registry.remove_room(
                                        room.code, "The room could not be created.", notify=False
                                    )
                                raise
                            active_manager = room.connections
                        else:
                            room = registry.find_room(payload.data.get("invite_code"))
                            previous = []
                            await room.engine.player_join(
                                client_id,
                                payload.data,
                                activate=lambda: previous.append(
                                    room.connections.promote(client_id, websocket)
                                ),
                            )
                            active_manager = room.connections
                        registry.touch(room)
                        authenticated = True
                        manager.release(websocket)
                        if previous and previous[0] is not None and previous[0] is not websocket:
                            await active_manager.close_socket(previous[0])
                    elif not room.connections.owns(client_id, websocket):
                        break
                    elif payload.event_type in _ROOM_EVENTS:
                        raise ValueError("This socket is already in a room.")
                    else:
                        registry.touch(room)
                        await room.engine.process_payload(
                            client_id,
                            payload,
                            authorize=lambda: room.connections.owns(client_id, websocket),
                        )
                except (ValidationError, ValueError) as exc:
                    message = (
                        "Invalid message schema." if isinstance(exc, ValidationError) else str(exc)
                    )
                    await active_manager.send_socket(
                        websocket, ServerEvent(type="error", payload={"msg": message})
                    )
                    if not authenticated and attempts >= settings.server.max_auth_attempts:
                        await manager.close_socket(websocket, code=1008)
                        break
        except TimeoutError:
            await manager.close_socket(websocket, code=1008)
        except (WebSocketDisconnect, RuntimeError):
            LOGGER.info("WebSocket disconnected")
        finally:
            if room is not None:
                # Hold the engine state lock across removal/marking to avoid a new
                # authenticated replacement being marked disconnected by an older socket.
                async with room.engine.lock:
                    removed = await room.connections.disconnect(client_id, websocket)
                    if removed:
                        version = room.engine.connection_version_of(client_id)
                    else:
                        version = None
                if removed:
                    await room.engine.handle_disconnect(
                        client_id,
                        expected_version=version,
                        still_disconnected=lambda: client_id
                        not in room.connections.active_connections,
                    )
                    registry.touch(room)
            else:
                await manager.disconnect(client_id, websocket)

    return application


app = create_app()
