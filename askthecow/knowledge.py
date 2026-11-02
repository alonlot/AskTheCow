"""Calibration and incremental update of the knowledge base.

Calibration (first run, or on request):
  1. Read every commit of the branch, oldest first, in groups that fit a size budget.
  2. For each group, the model writes a plain-language note per commit and a group summary.
     Notes go into a full-text index that the question agent can search.
  3. An agent with read-only tools on the branch head writes the repository brief from the
     summaries and the current code. The brief goes into the system prompt of every question.

Incremental update (each time new commits arrive on the remote branch):
  steps 1 and 2 for the new commits only, then the agent updates the brief.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import anthropic

from . import prompts
from .agent import request_options, run_agent, text_of
from .config import Settings
from .db import Database
from .gitrepo import GitError, GitRepo
from .tools import RepoTools

log = logging.getLogger(__name__)

MAX_BODY_CHARS = 3000
HISTORY_MAX_CHARS = 400_000
CONDENSE_GROUP_CHARS = 100_000
FULL_REBRIEF_EVERY = 25  # incremental updates before the brief is written again from scratch


class KnowledgeBase:
    def __init__(self, settings: Settings, db: Database, repo: GitRepo, client: anthropic.AsyncAnthropic):
        self.settings = settings
        self.db = db
        self.repo = repo
        self.client = client
        self.lock = asyncio.Lock()
        self.status: dict[str, Any] = {"phase": "idle", "commits_todo": 0, "commits_done": 0, "last_error": None}

    # ------------------------------------------------------------------ public
    @property
    def busy(self) -> bool:
        return self.lock.locked()

    async def sync(self, force_full: bool = False) -> None:
        """Fetch the branch and bring the knowledge base up to date with its head."""
        async with self.lock:
            try:
                self._phase("fetching")
                await asyncio.to_thread(self.repo.ensure)
                head = await asyncio.to_thread(self.repo.fetch, True)
                self.status["head"] = head
                brief = self.db.latest_brief()
                if force_full:
                    await self._calibrate(head)
                elif brief is None:
                    if self.settings.auto_calibrate:
                        await self._calibrate(head)
                elif brief["commit_sha"] != head:
                    await self._update(head, brief)
                self.status["last_error"] = None
            except Exception as exc:
                log.exception("knowledge sync failed")
                self.status["last_error"] = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                self.status["last_sync_at"] = time.time()
                self._phase("idle")

    # ---------------------------------------------------------------- phases
    def _phase(self, name: str) -> None:
        self.status["phase"] = name
        log.info("knowledge phase: %s", name)

    async def _calibrate(self, head: str) -> None:
        await self._digest_missing(head)
        await self._write_full_brief(head)
        self.db.set_state("incremental_updates", "0")

    async def _update(self, head: str, brief: dict) -> None:
        before = self.db.last_digest_id()
        await self._digest_missing(head)
        count = int(self.db.get_state("incremental_updates", "0") or 0)
        if count + 1 >= FULL_REBRIEF_EVERY:
            await self._write_full_brief(head)
            self.db.set_state("incremental_updates", "0")
            return
        new = self.db.list_digests(since_id=before)
        history = "\n\n".join(self._digest_text(d) for d in new) or "(no new commits, the branch head moved)"
        changed = await asyncio.to_thread(self._changed_files, brief["commit_sha"], head)
        task = prompts.BRIEF_TASK_UPDATE.format(
            project=self.settings.project_name,
            branch=self.settings.branch,
            sha=head,
            old_sha=brief["commit_sha"],
            brief=brief["content"],
            history=history,
            changed=changed,
            max_chars=self.settings.brief_max_chars,
        )
        content = await self._run_brief_agent(head, task)
        self.db.save_brief(head, "incremental", content)
        self.db.set_state("incremental_updates", str(count + 1))

    # --------------------------------------------------------------- digests
    async def _digest_missing(self, head: str) -> None:
        shas = await asyncio.to_thread(self.repo.rev_list, head)
        known = self.db.known_commit_shas()
        todo = [s for s in shas if s not in known]
        self.status.update(commits_todo=len(todo), commits_done=0)
        if not todo:
            return
        self._phase("digesting")
        metas = await asyncio.to_thread(self.repo.commit_meta, todo)
        sem = asyncio.Semaphore(max(1, self.settings.digest_concurrency))
        tasks: list[asyncio.Task] = []
        chunk: list[dict] = []
        size = 0

        async def flush(group: list[dict]) -> None:
            async with sem:
                await self._digest_group(group)
                self.status["commits_done"] += len(group)

        for meta in metas:
            item = await asyncio.to_thread(self._commit_item, meta)
            if chunk and size + len(item["text"]) > self.settings.digest_chunk_chars:
                await sem.acquire()  # back-pressure: do not read far ahead of the workers
                sem.release()
                tasks.append(asyncio.create_task(flush(chunk)))
                chunk, size = [], 0
            chunk.append(item)
            size += len(item["text"])
        if chunk:
            tasks.append(asyncio.create_task(flush(chunk)))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            raise errors[0]

    def _commit_item(self, meta: dict) -> dict:
        merge = meta["parents"] > 1
        try:
            patch = self.repo.commit_patch(meta["sha"], self.settings.max_diff_chars_per_commit, merge=merge)
            files = self.repo.commit_files(meta["sha"])
        except GitError as exc:
            patch, files = f"(patch not available: {exc})", []
        body = meta["body"][:MAX_BODY_CHARS]
        text = (
            f"=== commit {meta['sha']} ===\n"
            f"Author: {meta['author']}\nDate: {meta['date']}\n"
            f"{'Merge commit (diffstat against first parent only)' if merge else ''}\n"
            f"Subject: {meta['subject']}\n{body}\n\n{patch}\n"
        )
        return {**meta, "files": files, "text": text}

    async def _digest_group(self, group: list[dict]) -> None:
        opts = request_options(self.settings, self.settings.calibration_model, self.settings.digest_effort)
        opts["output_config"] = {**opts["output_config"], "format": {"type": "json_schema", "schema": prompts.DIGEST_SCHEMA}}
        system = prompts.DIGEST_SYSTEM.format(project=self.settings.project_name, branch=self.settings.branch)
        content = "\n".join(item["text"] for item in group)
        async with self.client.beta.messages.stream(
            **opts,
            max_tokens=32000,
            system=system,
            messages=[{"role": "user", "content": content}],
        ) as stream:
            message = await stream.get_final_message()

        summary, by_sha = "(this group was not summarized)", {}
        if message.stop_reason == "refusal":
            log.warning("digest declined for %s..%s", group[0]["sha"][:10], group[-1]["sha"][:10])
        else:
            try:
                data = json.loads(text_of(message))
                summary = data.get("summary") or summary
                for c in data.get("commits", []):
                    if isinstance(c, dict) and c.get("sha"):
                        by_sha[str(c["sha"]).lower()] = c
            except (json.JSONDecodeError, AttributeError):
                log.warning("digest output was not valid JSON (stop_reason=%s)", message.stop_reason)

        notes = []
        for item in group:
            found = by_sha.get(item["sha"]) or next(
                (v for k, v in by_sha.items() if len(k) >= 7 and item["sha"].startswith(k)), None
            )
            notes.append(
                {
                    "sha": item["sha"],
                    "author": item["author"],
                    "date": item["date"],
                    "subject": item["subject"],
                    "note": (found or {}).get("note") or item["subject"],
                    "files": " ".join(item["files"]),
                    "topics": " ".join(str(t) for t in (found or {}).get("topics", [])),
                }
            )
        self.db.save_digest(
            {
                "first_sha": group[0]["sha"],
                "last_sha": group[-1]["sha"],
                "first_date": group[0]["date"],
                "last_date": group[-1]["date"],
                "commit_count": len(group),
                "summary": summary,
            },
            notes,
        )

    # ----------------------------------------------------------------- brief
    @staticmethod
    def _digest_text(d: dict) -> str:
        return f"### {d['first_date'][:10]} to {d['last_date'][:10]} ({d['commit_count']} commits)\n{d['summary']}"

    async def _write_full_brief(self, head: str) -> None:
        digests = sorted(self.db.list_digests(), key=lambda d: (d["first_date"], d["id"]))
        parts = [self._digest_text(d) for d in digests]
        history = await self._condense(parts)
        task = prompts.BRIEF_TASK_FULL.format(
            project=self.settings.project_name,
            branch=self.settings.branch,
            sha=head,
            history=history,
            max_chars=self.settings.brief_max_chars,
        )
        content = await self._run_brief_agent(head, task)
        self.db.save_brief(head, "full", content)

    async def _condense(self, parts: list[str]) -> str:
        """Make the history fit: merge the oldest groups into shorter period summaries until it fits."""
        while sum(len(p) for p in parts) > HISTORY_MAX_CHARS and len(parts) > 1:
            self._phase("condensing history")
            # Keep the newest third as it is; condense the rest in groups.
            keep_from = max(1, len(parts) * 2 // 3)
            old, recent = parts[:keep_from], parts[keep_from:]
            groups: list[list[str]] = [[]]
            for p in old:
                if groups[-1] and sum(len(x) for x in groups[-1]) + len(p) > CONDENSE_GROUP_CHARS:
                    groups.append([])
                groups[-1].append(p)
            condensed = await asyncio.gather(*(self._condense_group(g) for g in groups))
            if len(condensed) >= len(old):  # nothing got shorter; stop to avoid a loop
                return "\n\n".join(parts)[-HISTORY_MAX_CHARS:]
            parts = list(condensed) + recent
        return "\n\n".join(parts)

    async def _condense_group(self, group: list[str]) -> str:
        opts = request_options(self.settings, self.settings.calibration_model, self.settings.digest_effort)
        async with self.client.beta.messages.stream(
            **opts,
            max_tokens=16000,
            system=prompts.DIGEST_SYSTEM.format(project=self.settings.project_name, branch=self.settings.branch),
            messages=[
                {
                    "role": "user",
                    "content": "Merge these period summaries into one summary of at most about 500 words. "
                    "Keep hardware-relevant changes, values and decisions. Start with the date range.\n\n"
                    + "\n\n".join(group),
                }
            ],
        ) as stream:
            message = await stream.get_final_message()
        return text_of(message) or "\n\n".join(group)[:5000]

    def _changed_files(self, old: str, new: str) -> str:
        try:
            self.repo.git("merge-base", "--is-ancestor", old, new)
        except GitError:
            return "(the branch history was rewritten; compare the brief with the code)"
        out = self.repo.git("diff", "--stat=150", "--stat-count=300", old, new)
        return out[-20000:]

    async def _run_brief_agent(self, head: str, task: str) -> str:
        self._phase("writing brief")
        system = [
            {
                "type": "text",
                "text": prompts.BRIEF_SYSTEM.format(project=self.settings.project_name, branch=self.settings.branch),
            }
        ]
        async with self.repo.acheckout(head) as path:
            result = await run_agent(
                self.client,
                self.settings,
                system=system,
                messages=[{"role": "user", "content": task}],
                tools=RepoTools(path, self.db),
                model=self.settings.calibration_model,
                effort=self.settings.brief_effort,
                max_turns=60,
                max_tokens=64000,
            )
        content = result.text.strip()
        if not content or result.stop_reason == "refusal":
            raise RuntimeError(f"brief agent returned no brief (stop_reason={result.stop_reason})")
        return content
