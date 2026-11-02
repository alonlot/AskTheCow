"""The single entry point for questions: `AskService.ask(Inbound) -> Outbound`.

Every channel uses it: the web chat, the CLI, and channels you add later (for example email).
A channel only has to:
  1. turn what it receives into an `Inbound`,
  2. call `await service.ask(inbound)`,
  3. send `outbound.text` back the way the channel works.

The service keeps the conversation history (by `conversation_id`, or by `channel` + `thread_id`),
fetches the latest branch head, runs the `claude -p` agent on a checkout of it, and saves the answer.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from . import prompts
from .claude_cli import ClaudeJob, run_claude
from .config import Settings
from .db import Database
from .gitrepo import GitRepo
from .knowledge import KnowledgeBase

log = logging.getLogger(__name__)

EventSink = Callable[[dict[str, Any]], Awaitable[None]]
HISTORY_MESSAGES = 20
HISTORY_MESSAGE_CHARS = 8000


@dataclass
class Inbound:
    """A question from any channel."""

    text: str
    channel: str = "api"  # "web", "cli", "email", ...
    user: str = ""  # who asked (user name, email address, ...)
    conversation_id: str | None = None  # our id of an existing conversation
    thread_id: str | None = None  # the channel's own conversation key, e.g. an email thread id
    title: str | None = None  # title for a new conversation, e.g. the email subject
    instructions: str | None = None  # extra answer rules for this channel, e.g. "Plain text, no Markdown."
    meta: dict[str, Any] = field(default_factory=dict)  # saved with the question, e.g. message ids


@dataclass
class Outbound:
    """The answer to an `Inbound`."""

    text: str
    conversation_id: str
    message_id: int
    commit_sha: str | None
    error: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


async def _noop(_: dict[str, Any]) -> None:
    return None


class AskService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.db_path)
        self.repo = GitRepo(
            settings.repo_url,
            settings.branch,
            settings.mirror_path,
            settings.worktrees_path,
            settings.fetch_min_interval_seconds,
        )
        self.kb = KnowledgeBase(settings, self.db, self.repo)
        self._slots = asyncio.Semaphore(max(1, settings.max_concurrent_agents))

    def close(self) -> None:
        self.db.close()

    # --------------------------------------------------------------- public
    def conversation_for(self, inbound: Inbound) -> dict:
        """Find the conversation of the question, or make a new one."""
        db = self.db
        if inbound.conversation_id:
            chat = db.get_chat(inbound.conversation_id)
            if chat is None:
                raise KeyError(f"No conversation {inbound.conversation_id}")
            return chat
        if inbound.thread_id:
            chat = db.find_chat(inbound.channel, inbound.thread_id)
            if chat:
                return chat
        first_line = inbound.text.strip().splitlines()[0] if inbound.text.strip() else ""
        title = (inbound.title or first_line or "New chat")[:60]
        return db.create_chat(inbound.user, title, inbound.channel, inbound.thread_id)

    async def ask(self, inbound: Inbound, on_event: EventSink | None = None) -> Outbound:
        """Answer one question. Events (status, tool, text, done, error) go to on_event as they happen."""
        emit = on_event or _noop
        chat = self.conversation_for(inbound)
        history = self.db.list_messages(chat["id"])
        if not history and chat["title"] == "New chat" and inbound.text.strip():
            self.db.rename_chat(chat["id"], inbound.text.strip().splitlines()[0][:60])
        user_msg = self.db.add_message(
            chat["id"], "user", inbound.text, meta={"channel": inbound.channel, "user": inbound.user, **inbound.meta}
        )
        await emit({"type": "user_message", "message": user_msg, "conversation_id": chat["id"]})

        head: str | None = None
        try:
            if self._slots.locked():
                await emit({"type": "status", "text": "Waiting for a free agent"})
            async with self._slots:
                await emit({"type": "status", "text": f"Getting the latest {self.settings.branch}"})
                await asyncio.to_thread(self.repo.ensure)
                head = await asyncio.to_thread(self.repo.fetch)
                brief = self.db.latest_brief()
                if not brief or brief["commit_sha"] != head:
                    self.kb.request_sync()  # new commits: let the knowledge base catch up
                await emit({"type": "status", "text": f"Reading the code at {head[:10]}", "commit": head})
                async with self.repo.acheckout(head) as path:
                    result = await run_claude(self.settings, self._answer_job(inbound, history, head, brief, path), emit)
            if result.is_error:
                raise RuntimeError(f"agent stopped ({result.subtype}): {result.text[:500]}")
            meta = {
                "tool_calls": [{"name": c["name"], "input": c["input"]} for c in result.tool_calls],
                "denials": result.denials,
                "turns": result.num_turns,
                "cost_usd": result.cost_usd,
                "usage": result.usage,
                "model": result.model,
                "brief_sha": brief["commit_sha"] if brief else None,
            }
            msg = self.db.add_message(chat["id"], "assistant", result.text or "(no answer)", head, meta)
            await emit({"type": "done", "message": msg})
            return Outbound(msg["content"], chat["id"], msg["id"], head, False, meta)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("answer failed")
            text = f"Could not answer: {type(exc).__name__}: {exc}"
            msg = self.db.add_message(chat["id"], "assistant", text, head, {"error": True})
            await emit({"type": "error", "error": text, "message": msg})
            return Outbound(text, chat["id"], msg["id"], head, True, msg["meta"])

    # -------------------------------------------------------------- helpers
    def _answer_job(self, inbound: Inbound, history: list[dict], head: str, brief: dict | None, path: Any) -> ClaudeJob:
        s = self.settings
        knowledge = s.knowledge_path.resolve()
        brief_text = prompts.BRIEF_BLOCK.format(sha=brief["commit_sha"], brief=brief["content"]) if brief else prompts.NO_BRIEF
        system = prompts.ANSWER_SYSTEM.format(
            project=s.project_name,
            branch=s.branch,
            knowledge_files=prompts.KNOWLEDGE_FILES.format(knowledge_dir=knowledge, branch=s.branch),
            brief=brief_text,
        )
        return ClaudeJob.read_only_agent(
            prompt=build_question(s.branch, head, inbound, history),
            cwd=path,
            model=s.model,
            effort=s.answer_effort,
            append_system_prompt=system,
            add_dirs=[knowledge] if knowledge.exists() else [],
            max_budget_usd=s.answer_max_budget_usd,
            timeout=s.answer_timeout_seconds,
            stream_text=True,
        )


def build_question(branch: str, head: str, inbound: Inbound, history: list[dict]) -> str:
    turns = []
    for m in history[-HISTORY_MESSAGES:]:
        if m["role"] == "assistant" and m["meta"].get("error"):
            continue
        role = "engineer" if m["role"] == "user" else "assistant"
        turns.append(f'<message role="{role}">\n{m["content"][:HISTORY_MESSAGE_CHARS]}\n</message>')
    history_block = prompts.HISTORY.format(turns="\n".join(turns)) if turns else ""
    instructions = f"\n\n{inbound.instructions}" if inbound.instructions else ""
    return prompts.QUESTION.format(
        history=history_block,
        channel=inbound.channel,
        user=inbound.user or "unknown",
        text=inbound.text,
        branch=branch,
        sha=head,
        instructions=instructions,
    )
