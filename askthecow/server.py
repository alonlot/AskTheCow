"""HTTP server: web chat, JSON ask API, admin endpoints, push webhook, sync loop and channels."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import secrets
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .channels import load_channel, supervise
from .config import Settings
from .service import AskService, Inbound

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
NO_BASIC_AUTH = ("/api/webhook", "/api/ask")  # these have their own secrets


class NewChat(BaseModel):
    title: str = Field(default="New chat", max_length=200)


class RenameChat(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class Ask(BaseModel):
    content: str = Field(min_length=1, max_length=20000)


class AskApi(BaseModel):
    """Body of POST /api/ask. Fields are the same as `service.Inbound`."""

    text: str = Field(min_length=1, max_length=50000)
    channel: str = Field(default="api", max_length=50)
    user: str = Field(default="", max_length=200)
    conversation_id: str | None = None
    thread_id: str | None = Field(default=None, max_length=500)
    title: str | None = Field(default=None, max_length=200)
    instructions: str | None = Field(default=None, max_length=2000)
    meta: dict[str, Any] = Field(default_factory=dict)


def create_app(settings: Settings | None = None, run_background: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        service = AskService(settings)
        app.state.service = service
        app.state.tasks = set()
        background: list[asyncio.Task] = []
        if run_background:
            background.append(asyncio.create_task(service.kb.run_forever()))
            for spec in settings.channels:
                channel = load_channel(spec, service, settings)
                log.info("starting channel %s (%s)", channel.name, spec)
                background.append(asyncio.create_task(supervise(channel)))
        try:
            yield
        finally:
            for t in background + list(app.state.tasks):
                t.cancel()
            service.close()

    app = FastAPI(title="AskTheCow", lifespan=lifespan)

    def spawn(app_: FastAPI, coro: Any) -> asyncio.Task:
        task = asyncio.create_task(coro)
        app_.state.tasks.add(task)
        task.add_done_callback(app_.state.tasks.discard)
        return task

    # ----------------------------------------------------------------- auth
    @app.middleware("http")
    async def basic_auth(request: Request, call_next):
        request.state.user = ""
        if settings.password and request.url.path not in NO_BASIC_AUTH:
            user = _check_basic(request.headers.get("authorization", ""), settings.password)
            if user is None:
                return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="AskTheCow"'})
            request.state.user = user
        return await call_next(request)

    def service_dep(request: Request) -> AskService:
        return request.app.state.service

    def owner_dep(request: Request, x_user: str | None = Header(default=None)) -> str:
        # With a password, the Basic auth user name is the owner. Without, the UI sends X-User.
        return (request.state.user or x_user or "").strip()[:100]

    def admin_dep(x_admin_token: str | None = Header(default=None)) -> None:
        _require_token(settings.admin_token, x_admin_token, "ASKTHECOW_ADMIN_TOKEN")

    def api_dep(x_api_token: str | None = Header(default=None)) -> None:
        _require_token(settings.api_token, x_api_token, "ASKTHECOW_API_TOKEN")

    def own_chat(service: AskService, chat_id: str, owner: str) -> dict:
        chat = service.db.get_chat(chat_id)
        if not chat or chat["owner"] != owner or chat["channel"] != "web":
            raise HTTPException(404, "Chat not found")
        return chat

    # ---------------------------------------------------------------- pages
    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # --------------------------------------------------------------- status
    @app.get("/api/status")
    async def status(service: AskService = Depends(service_dep)) -> dict:
        brief = service.db.latest_brief()
        return {
            "project": settings.project_name,
            "branch": settings.branch,
            "knowledge": {**service.kb.status, "busy": service.kb.busy},
            "commit_notes": service.db.commit_note_count(),
            "digests": service.db.last_digest_id(),
            "brief": None
            if not brief
            else {
                "commit_sha": brief["commit_sha"],
                "kind": brief["kind"],
                "created_at": brief["created_at"],
                "chars": len(brief["content"]),
            },
        }

    @app.get("/api/brief")
    async def get_brief(service: AskService = Depends(service_dep)) -> dict:
        brief = service.db.latest_brief()
        if not brief:
            raise HTTPException(404, "No brief yet. Calibration has not finished.")
        return brief

    # ------------------------------------------------------------- web chat
    @app.get("/api/chats")
    async def list_chats(service: AskService = Depends(service_dep), owner: str = Depends(owner_dep)) -> list[dict]:
        return [c for c in service.db.list_chats(owner) if c["channel"] == "web"]

    @app.post("/api/chats")
    async def new_chat(body: NewChat, service: AskService = Depends(service_dep), owner: str = Depends(owner_dep)) -> dict:
        return service.db.create_chat(owner, body.title, "web")

    @app.get("/api/chats/{chat_id}")
    async def get_chat(chat_id: str, service: AskService = Depends(service_dep), owner: str = Depends(owner_dep)) -> dict:
        chat = own_chat(service, chat_id, owner)
        return {**chat, "messages": service.db.list_messages(chat_id)}

    @app.patch("/api/chats/{chat_id}")
    async def rename_chat(
        chat_id: str, body: RenameChat, service: AskService = Depends(service_dep), owner: str = Depends(owner_dep)
    ) -> dict:
        own_chat(service, chat_id, owner)
        service.db.rename_chat(chat_id, body.title)
        return service.db.get_chat(chat_id)  # type: ignore[return-value]

    @app.delete("/api/chats/{chat_id}")
    async def delete_chat(chat_id: str, service: AskService = Depends(service_dep), owner: str = Depends(owner_dep)) -> dict:
        own_chat(service, chat_id, owner)
        service.db.delete_chat(chat_id)
        return {"ok": True}

    @app.post("/api/chats/{chat_id}/messages")
    async def ask_in_chat(
        chat_id: str,
        body: Ask,
        request: Request,
        service: AskService = Depends(service_dep),
        owner: str = Depends(owner_dep),
    ) -> StreamingResponse:
        own_chat(service, chat_id, owner)
        queue: asyncio.Queue = asyncio.Queue()
        inbound = Inbound(text=body.content, channel="web", user=owner, conversation_id=chat_id)
        # The answer runs as its own task, so it is saved even if the browser disconnects.
        spawn(request.app, service.ask(inbound, on_event=queue.put))

        async def events():
            while True:
                event = await queue.get()
                yield f"data: {json.dumps(event, default=str)}\n\n"
                if event["type"] in ("done", "error"):
                    break

        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    # --------------------------------------------------------- JSON ask API
    @app.post("/api/ask", dependencies=[Depends(api_dep)])
    async def ask_api(body: AskApi, service: AskService = Depends(service_dep)) -> dict:
        """One question in, one answer out. For bridges in other programs (mail, chat tools, scripts)."""
        try:
            out = await service.ask(Inbound(**body.model_dump()))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {
            "text": out.text,
            "conversation_id": out.conversation_id,
            "message_id": out.message_id,
            "commit_sha": out.commit_sha,
            "error": out.error,
        }

    # ---------------------------------------------------------------- admin
    @app.post("/api/admin/sync", dependencies=[Depends(admin_dep)])
    async def admin_sync(service: AskService = Depends(service_dep)) -> dict:
        service.kb.request_sync()
        return {"ok": True, "busy": service.kb.busy}

    @app.post("/api/admin/calibrate", dependencies=[Depends(admin_dep)])
    async def admin_calibrate(request: Request, service: AskService = Depends(service_dep)) -> dict:
        if service.kb.busy:
            raise HTTPException(409, "A knowledge update is running. Try again when it is done.")
        spawn(request.app, _quiet(service.kb.sync(force_full=True)))
        return {"ok": True}

    # -------------------------------------------------------------- webhook
    @app.post("/api/webhook")
    async def webhook(request: Request, service: AskService = Depends(service_dep)) -> JSONResponse:
        """Push webhook (GitHub or GitLab). It only wakes the sync loop; the loop fetches the branch."""
        if not settings.webhook_secret:
            raise HTTPException(404, "Webhook is off. Set ASKTHECOW_WEBHOOK_SECRET.")
        raw = await request.body()
        gh_sig = request.headers.get("x-hub-signature-256", "")
        gl_token = request.headers.get("x-gitlab-token", "")
        if gh_sig:
            expected = "sha256=" + hmac.new(settings.webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
            ok = hmac.compare_digest(gh_sig, expected)
        else:
            ok = bool(gl_token) and hmac.compare_digest(gl_token, settings.webhook_secret)
        if not ok:
            raise HTTPException(403, "Bad signature")
        with contextlib.suppress(ValueError, AttributeError):
            ref = json.loads(raw or b"{}").get("ref")
            if ref and ref != f"refs/heads/{settings.branch}":
                return JSONResponse({"ok": True, "ignored": ref})
        service.kb.request_sync()
        return JSONResponse({"ok": True})

    return app


def _require_token(expected: str, given: str | None, env_name: str) -> None:
    if not expected:
        raise HTTPException(403, f"This endpoint is off. Set {env_name}.")
    if not given or not hmac.compare_digest(given, expected):
        raise HTTPException(403, "Bad token")


async def _quiet(coro: Any) -> None:
    with contextlib.suppress(Exception):
        await coro


def _check_basic(header: str, password: str) -> str | None:
    if not header.lower().startswith("basic "):
        return None
    try:
        user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
    except (ValueError, UnicodeDecodeError):
        return None
    if not secrets.compare_digest(pw.encode(), password.encode()):
        return None
    return user or "user"
