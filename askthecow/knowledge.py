"""Calibration, incremental updates and the sync loop. All model work runs through `claude -p`.

Calibration (first run, or on request):
  1. Read every commit of the branch, oldest first, in groups that fit a size budget.
  2. For each group, `claude -p` (no tools, JSON schema output) writes a plain-language note per
     commit and a group summary.
  3. A `claude -p` agent with read-only tools on a checkout of the branch head writes the
     repository brief from the summaries and the current code.

Incremental update (each time new commits arrive on the remote branch):
  steps 1 and 2 for the new commits only, then the agent updates the brief.

The knowledge is in SQLite, and is also written as plain files in data/knowledge/ that the
answer agent reads with its normal file tools: brief.md, history.md, commit-notes.txt.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from pathlib import Path
from typing import Any

from . import prompts
from .claude_cli import ClaudeJob, run_claude
from .config import Settings
from .db import Database
from .gitrepo import GitError, GitRepo

log = logging.getLogger(__name__)

MAX_BODY_CHARS = 3000
CONDENSE_GROUP_CHARS = 100_000


def _one_line(text: str) -> str:
    return " ".join(str(text).replace("|", "/").split())


class KnowledgeBase:
    def __init__(self, settings: Settings, db: Database, repo: GitRepo):
        self.settings = settings
        self.db = db
        self.repo = repo
        self.lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self.status: dict[str, Any] = {"phase": "idle", "commits_todo": 0, "commits_done": 0, "last_error": None}

    # ------------------------------------------------------------------ public
    @property
    def busy(self) -> bool:
        return self.lock.locked()

    def request_sync(self) -> None:
        """Ask the sync loop to run now (new commits seen, webhook, admin request)."""
        self.wake.set()

    async def run_forever(self) -> None:
        """Keep the knowledge base in step with the remote branch."""
        while True:
            with contextlib.suppress(Exception):  # sync() logs and records the error
                await self.sync()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.wake.wait(), timeout=max(10, self.settings.poll_interval_seconds))
            self.wake.clear()

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
                self.export_files()
                self.status["last_error"] = None
            except Exception as exc:
                log.exception("knowledge sync failed")
                self.status["last_error"] = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                self.status["last_sync_at"] = time.time()
                self._phase("idle")

    def export_files(self) -> None:
        """Write the knowledge as plain files for the agents."""
        out = self.settings.knowledge_path
        out.mkdir(parents=True, exist_ok=True)
        notes = self.db.list_commit_notes()
        lines = [
            " | ".join(
                [
                    n["sha"][:12],
                    n["date"][:10],
                    _one_line(n["author"]),
                    _one_line(n["subject"]),
                    _one_line(n["note"]),
                    "topics: " + _one_line(n["topics"]),
                    "files: " + _one_line(n["files"])[:600],
                ]
            )
            for n in notes
        ]
        digests = sorted(self.db.list_digests(), key=lambda d: (d["first_date"], d["id"]))
        brief = self.db.latest_brief()
        _write_atomic(out / "commit-notes.txt", "\n".join(lines) + ("\n" if lines else ""))
        _write_atomic(out / "history.md", "# History of the branch\n\n" + "\n\n".join(self._digest_text(d) for d in digests) + "\n")
        _write_atomic(out / "brief.md", brief["content"] + "\n" if brief else "No brief yet.\n")

    # ---------------------------------------------------------------- phases
    def _phase(self, name: str) -> None:
        self.status["phase"] = name
        log.info("knowledge phase: %s", name)

    async def _calibrate(self, head: str) -> None:
        await self._digest_missing(head)
        self.export_files()
        await self._write_full_brief(head)
        self.db.set_state("incremental_updates", "0")

    async def _update(self, head: str, brief: dict) -> None:
        before = self.db.last_digest_id()
        await self._digest_missing(head)
        self.export_files()
        count = int(self.db.get_state("incremental_updates", "0") or 0)
        if count + 1 >= self.settings.full_rebrief_every:
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
        position = {s: i for i, s in enumerate(shas)}
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
            item["seq"] = position[meta["sha"]]
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

    def _text_job(self, prompt: str, *, system: str, schema: dict | None = None) -> ClaudeJob:
        """A `claude -p` run with no tools: all input is in the prompt."""
        return ClaudeJob(
            prompt=prompt,
            cwd=self.settings.data_dir.resolve(),
            model=self.settings.calibration_model,
            effort=self.settings.digest_effort,
            system_prompt=system,
            json_schema=schema,
            timeout=self.settings.digest_timeout_seconds,
        )

    async def _digest_group(self, group: list[dict]) -> None:
        system = prompts.DIGEST_SYSTEM.format(project=self.settings.project_name, branch=self.settings.branch)
        content = "\n".join(item["text"] for item in group)
        result = await run_claude(self.settings, self._text_job(content, system=system, schema=prompts.DIGEST_SCHEMA))

        data = result.structured
        if data is None and result.text:
            with contextlib.suppress(json.JSONDecodeError):
                data = json.loads(result.text)
        if result.is_error or not isinstance(data, dict):
            # Do not save: these commits stay "unknown" and the next sync tries again.
            raise RuntimeError(
                f"commit notes failed for {group[0]['sha'][:10]}..{group[-1]['sha'][:10]} "
                f"({result.subtype}): {result.text[:300]}"
            )
        summary = data.get("summary") or "(no summary)"
        by_sha = {str(c["sha"]).lower(): c for c in data.get("commits", []) if isinstance(c, dict) and c.get("sha")}

        notes = []
        for item in group:
            found = by_sha.get(item["sha"]) or next(
                (v for k, v in by_sha.items() if len(k) >= 7 and item["sha"].startswith(k)), None
            )
            notes.append(
                {
                    "sha": item["sha"],
                    "seq": item.get("seq", 0),
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
        history = await self._condense([self._digest_text(d) for d in digests])
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
        limit = self.settings.history_max_chars
        while sum(len(p) for p in parts) > limit and len(parts) > 1:
            self._phase("condensing history")
            keep_from = max(1, len(parts) * 2 // 3)  # keep the newest third as it is
            old, recent = parts[:keep_from], parts[keep_from:]
            groups: list[list[str]] = [[]]
            for p in old:
                if groups[-1] and sum(len(x) for x in groups[-1]) + len(p) > CONDENSE_GROUP_CHARS:
                    groups.append([])
                groups[-1].append(p)
            condensed = await asyncio.gather(*(self._condense_group(g) for g in groups))
            if len(condensed) >= len(old):  # nothing got shorter; stop to avoid a loop
                return "\n\n".join(parts)[-limit:]
            parts = list(condensed) + recent
        return "\n\n".join(parts)

    async def _condense_group(self, group: list[str]) -> str:
        prompt = prompts.CONDENSE.format(project=self.settings.project_name, parts="\n\n".join(group))
        system = prompts.DIGEST_SYSTEM.format(project=self.settings.project_name, branch=self.settings.branch)
        result = await run_claude(self.settings, self._text_job(prompt, system=system))
        return result.text if result.text and not result.is_error else "\n\n".join(group)[:5000]

    def _changed_files(self, old: str, new: str) -> str:
        try:
            self.repo.git("merge-base", "--is-ancestor", old, new)
        except GitError:
            return "(the branch history was rewritten; compare the brief with the code)"
        return self.repo.git("diff", "--stat=150", "--stat-count=300", old, new)[-20000:]

    async def _run_brief_agent(self, head: str, task: str) -> str:
        self._phase("writing brief")
        knowledge = self.settings.knowledge_path.resolve()
        system = prompts.BRIEF_SYSTEM.format(
            project=self.settings.project_name,
            branch=self.settings.branch,
            knowledge_files=prompts.KNOWLEDGE_FILES.format(knowledge_dir=knowledge, branch=self.settings.branch),
        )
        async with self.repo.acheckout(head) as path:
            result = await run_claude(
                self.settings,
                ClaudeJob.read_only_agent(
                    prompt=task,
                    cwd=path,
                    model=self.settings.calibration_model,
                    effort=self.settings.brief_effort,
                    append_system_prompt=system,
                    add_dirs=[knowledge],
                    timeout=self.settings.brief_timeout_seconds,
                ),
            )
        if result.is_error or not result.text:
            raise RuntimeError(f"brief agent returned no brief ({result.subtype}): {result.text[:300]}")
        return result.text


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
