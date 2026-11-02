"""HTTP server: chat UI, chat storage, question answering, sync loop and webhook."""

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

from . import prompts
from .agent import make_client, run_agent
from .config import Settings
from .db import Database
from .gitrepo import GitRepo
from .knowledge import KnowledgeBase
from .tools import RepoTools

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
HISTORY_MESSAGES = 20


class Services:
    def __init__(self, settings: Settings, client: Any | None = None):
        self.settings = settings
        self.db = Database(settings.db_path)
        self.repo = GitRepo(
            settings.repo_url,
            settings.branch,
            settings.mirror_path,
            settings.worktrees_path,
            settings.fetch_min_interval_seconds,
        )
        self.client = client or make_client()
        self.kb = KnowledgeBase(settings, self.db, self.repo, self.client)
        self.wake = asyncio.Event()
        self.tasks: set[asyncio.Task] = set()

    def spawn(self, coro: Any) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task


async def sync_loop(svc: Services) -> None:
    """Keep the knowledge base in step with the remote branch."""
    while True:
        with contextlib.suppress(Exception):  # KnowledgeBase.sync logs and records the error
            await svc.kb.sync()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(svc.wake.wait(), timeout=max(10, svc.settings.poll_interval_seconds))
        svc.wake.clear()


class NewChat(BaseModel):
    title: str = Field(default="New chat", max_length=200)


class RenameChat(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class Ask(BaseModel):
    content: str = Field(min_length=1, max_length=20000)


def history_to_messages(history: list[dict]) -> list[dict]:
    """Earlier turns as plain text. Failed answers are left out."""
    out: list[dict] = []
    for m in history[-HISTORY_MESSAGES:]:
        if m["role"] == "assistant" and m["meta"].get("error"):
            continue
        out.append({"role": m["role"], "content": m["content"]})
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def build_system(settings: Settings, brief: dict | None) -> list[dict]:
    blocks = [
        {"type": "text", "text": prompts.ANSWER_SYSTEM.format(project=settings.project_name, branch=settings.branch)}
    ]
    if brief:
        blocks.append(
            {"type": "text", "text": prompts.BRIEF_HEADER.format(sha=brief["commit_sha"], brief=brief["content"])}
        )
    else:
        blocks.append({"type": "text", "text": prompts.NO_BRIEF})
    return blocks


def create_app(settings: Settings | None = None, client: Any | None = None, run_sync_loop: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = Services(settings, client)
        app.state.svc = svc
        loop_task = asyncio.create_task(sync_loop(svc)) if run_sync_loop else None
        try:
            yield
        finally:
            if loop_task:
                loop_task.cancel()
            for t in list(svc.tasks):
                t.cancel()
            svc.db.close()

    app = FastAPI(title="AskTheCow", lifespan=lifespan)

    # ----------------------------------------------------------------- auth
    @app.middleware("http")
    async def basic_auth(request: Request, call_next):
        request.state.user = ""
        if settings.password and request.url.path != "/api/webhook":
            user = _check_basic(request.headers.get("authorization", ""), settings.password)
            if user is None:
                return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="AskTheCow"'})
            request.state.user = user
        return await call_next(request)

    def svc_dep(request: Request) -> Services:
        return request.app.state.svc

    def owner_dep(request: Request, x_user: str | None = Header(default=None)) -> str:
        # With a password, the Basic auth user name is the owner. Without, the UI sends X-User.
        return (request.state.user or x_user or "").strip()[:100]

    def admin_dep(x_admin_token: str | None = Header(default=None)) -> None:
        if not settings.admin_token:
            raise HTTPException(403, "Admin endpoints are off. Set ASKTHECOW_ADMIN_TOKEN, or use the CLI.")
        if not x_admin_token or not hmac.compare_digest(x_admin_token, settings.admin_token):
            raise HTTPException(403, "Bad admin token")

    def own_chat(svc: Services, chat_id: str, owner: str) -> dict:
        chat = svc.db.get_chat(chat_id)
        if not chat or chat["owner"] != owner:
            raise HTTPException(404, "Chat not found")
        return chat

    # ---------------------------------------------------------------- pages
    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # --------------------------------------------------------------- status
    @app.get("/api/status")
    async def status(svc: Services = Depends(svc_dep)) -> dict:
        brief = svc.db.latest_brief()
        return {
            "project": settings.project_name,
            "branch": settings.branch,
            "knowledge": {**svc.kb.status, "busy": svc.kb.busy},
            "commit_notes": svc.db.commit_note_count(),
            "digests": svc.db.last_digest_id(),
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
    async def get_brief(svc: Services = Depends(svc_dep)) -> dict:
        brief = svc.db.latest_brief()
        if not brief:
            raise HTTPException(404, "No brief yet. Calibration has not finished.")
        return brief

    # ---------------------------------------------------------------- chats
    @app.get("/api/chats")
    async def list_chats(svc: Services = Depends(svc_dep), owner: str = Depends(owner_dep)) -> list[dict]:
        return svc.db.list_chats(owner)

    @app.post("/api/chats")
    async def new_chat(body: NewChat, svc: Services = Depends(svc_dep), owner: str = Depends(owner_dep)) -> dict:
        return svc.db.create_chat(owner, body.title)

    @app.get("/api/chats/{chat_id}")
    async def get_chat(chat_id: str, svc: Services = Depends(svc_dep), owner: str = Depends(owner_dep)) -> dict:
        chat = own_chat(svc, chat_id, owner)
        return {**chat, "messages": svc.db.list_messages(chat_id)}

    @app.patch("/api/chats/{chat_id}")
    async def rename_chat(
        chat_id: str, body: RenameChat, svc: Services = Depends(svc_dep), owner: str = Depends(owner_dep)
    ) -> dict:
        own_chat(svc, chat_id, owner)
        svc.db.rename_chat(chat_id, body.title)
        return svc.db.get_chat(chat_id)  # type: ignore[return-value]

    @app.delete("/api/chats/{chat_id}")
    async def delete_chat(chat_id: str, svc: Services = Depends(svc_dep), owner: str = Depends(owner_dep)) -> dict:
        own_chat(svc, chat_id, owner)
        svc.db.delete_chat(chat_id)
        return {"ok": True}

    @app.post("/api/chats/{chat_id}/messages")
    async def ask(
        chat_id: str, body: Ask, svc: Services = Depends(svc_dep), owner: str = Depends(owner_dep)
    ) -> StreamingResponse:
        chat = own_chat(svc, chat_id, owner)
        history = svc.db.list_messages(chat_id)
        user_msg = svc.db.add_message(chat_id, "user", body.content)
        if not history and chat["title"] == "New chat":
            svc.db.rename_chat(chat_id, body.content.strip().splitlines()[0][:60])
        queue: asyncio.Queue = asyncio.Queue()
        # The answer runs as its own task, so it is saved even if the browser disconnects.
        svc.spawn(answer_question(svc, chat_id, history, body.content, queue))

        async def events():
            yield _sse({"type": "user_message", "message": user_msg})
            while True:
                event = await queue.get()
                yield _sse(event)
                if event["type"] in ("done", "error"):
                    break

        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    # ---------------------------------------------------------------- admin
    @app.post("/api/admin/sync", dependencies=[Depends(admin_dep)])
    async def admin_sync(svc: Services = Depends(svc_dep)) -> dict:
        svc.wake.set()
        return {"ok": True, "busy": svc.kb.busy}

    @app.post("/api/admin/calibrate", dependencies=[Depends(admin_dep)])
    async def admin_calibrate(svc: Services = Depends(svc_dep)) -> dict:
        if svc.kb.busy:
            raise HTTPException(409, "A knowledge update is running. Try again when it is done.")
        svc.spawn(_quiet(svc.kb.sync(force_full=True)))
        return {"ok": True}

    # -------------------------------------------------------------- webhook
    @app.post("/api/webhook")
    async def webhook(request: Request, svc: Services = Depends(svc_dep)) -> JSONResponse:
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
        with contextlib.suppress(ValueError):
            ref = json.loads(raw or b"{}").get("ref")
            if ref and ref != f"refs/heads/{settings.branch}":
                return JSONResponse({"ok": True, "ignored": ref})
        svc.wake.set()
        return JSONResponse({"ok": True})

    return app


async def answer_question(svc: Services, chat_id: str, history: list[dict], question: str, queue: asyncio.Queue) -> None:
    settings = svc.settings
    try:
        await queue.put({"type": "status", "text": f"Getting the latest {settings.branch}"})
        await asyncio.to_thread(svc.repo.ensure)
        head = await asyncio.to_thread(svc.repo.fetch)
        brief = svc.db.latest_brief()
        if not brief or brief["commit_sha"] != head:
            svc.wake.set()  # new commits: let the knowledge base catch up
        system = build_system(settings, brief)
        messages = history_to_messages(history) + [
            {"role": "user", "content": f"{question}\n\n[Asked on {settings.branch} commit {head}]"}
        ]
        await queue.put({"type": "status", "text": f"Reading the code at {head[:10]}", "commit": head})
        async with svc.repo.acheckout(head) as path:
            result = await run_agent(
                svc.client, settings, system=system, messages=messages, tools=RepoTools(path, svc.db), on_event=queue.put
            )
        meta = {
            "tool_calls": result.tool_calls,
            "usage": result.usage,
            "turns": result.turns,
            "stop_reason": result.stop_reason,
            "served_by": result.served_by,
            "brief_sha": brief["commit_sha"] if brief else None,
        }
        msg = svc.db.add_message(chat_id, "assistant", result.text or "(no answer)", head, meta)
        await queue.put({"type": "done", "message": msg})
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.exception("answer failed")
        text = f"Could not answer: {type(exc).__name__}: {exc}"
        msg = svc.db.add_message(chat_id, "assistant", text, None, {"error": True})
        await queue.put({"type": "error", "error": text, "message": msg})


async def _quiet(coro: Any) -> None:
    with contextlib.suppress(Exception):
        await coro


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


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
