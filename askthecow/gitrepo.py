"""Git access. Only one branch (master by default) is fetched from the remote.

Layout under data_dir:
  repo.git/          bare clone that tracks only refs/heads/<branch>
  worktrees/<sha>/   read-only checkouts used by the question agent
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import AsyncIterator, Iterator

log = logging.getLogger(__name__)

# Paths that make diffs long but tell an engineer nothing.
NOISY_PATHSPECS = [
    ":(exclude,glob)**/*.hex",
    ":(exclude,glob)**/*.bin",
    ":(exclude,glob)**/*.elf",
    ":(exclude,glob)**/*.map",
    ":(exclude,glob)**/*.lst",
    ":(exclude,glob)**/*.srec",
    ":(exclude,glob)**/*.lock",
    ":(exclude,glob)**/package-lock.json",
    ":(exclude,glob)**/*.min.js",
]

FIELD_SEP = "\x1f"
RECORD_SEP = "\x1e"


class GitError(RuntimeError):
    pass


def run_git(
    args: list[str],
    cwd: Path | None = None,
    git_dir: Path | None = None,
    timeout: int = 600,
    ok_codes: tuple[int, ...] = (0,),
) -> str:
    cmd = ["git"]
    if git_dir is not None:
        cmd.append(f"--git-dir={git_dir}")
    cmd += args
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        timeout=timeout,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"},
    )
    if proc.returncode not in ok_codes:
        raise GitError(f"git {' '.join(args[:3])} failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout.decode("utf-8", errors="replace")


def truncate_patch(patch: str, budget: int) -> str:
    """Shorten a patch so that every changed file keeps some lines."""
    if len(patch) <= budget:
        return patch
    parts = patch.split("\ndiff --git ")
    head, files = parts[0], ["diff --git " + p for p in parts[1:]]
    if not files:
        return patch[:budget] + f"\n[... {len(patch) - budget} more characters cut ...]"
    per_file = max(400, (budget - len(head)) // len(files))
    out = [head]
    used = len(head)
    for i, f in enumerate(files):
        if used >= budget:
            out.append(f"[... {len(files) - i} more changed files not shown ...]")
            break
        piece = f if len(f) <= per_file else f[:per_file] + f"\n[... {len(f) - per_file} characters of this file cut ...]"
        out.append(piece)
        used += len(piece)
    return "\n".join(out)


class GitRepo:
    def __init__(self, url: str, branch: str, mirror_path: Path, worktrees_path: Path, fetch_min_interval: int = 30):
        self.url = url
        self.branch = branch
        self.git_dir = Path(mirror_path)
        self.worktrees_path = Path(worktrees_path)
        self.fetch_min_interval = fetch_min_interval
        self._lock = threading.RLock()
        self._last_fetch = 0.0
        self._refcount: dict[str, int] = {}

    @property
    def ref(self) -> str:
        return f"refs/heads/{self.branch}"

    def git(self, *args: str, timeout: int = 600) -> str:
        return run_git(list(args), git_dir=self.git_dir, timeout=timeout)

    # ------------------------------------------------------------ remote sync
    def ensure(self) -> None:
        """Create the bare clone on first use. It fetches only the configured branch."""
        with self._lock:
            if not (self.git_dir / "HEAD").exists():
                if not self.url:
                    raise GitError("ASKTHECOW_REPO_URL is not set")
                self.git_dir.parent.mkdir(parents=True, exist_ok=True)
                run_git(["init", "--bare", str(self.git_dir)])
                self.git("remote", "add", "origin", self.url)
            # Always rewrite the refspec so that no other branch is ever fetched.
            if self.url:
                self.git("config", "remote.origin.url", self.url)
            self.git("config", "--replace-all", "remote.origin.fetch", f"+{self.ref}:{self.ref}")
            self.git("symbolic-ref", "HEAD", self.ref)
            self.worktrees_path.mkdir(parents=True, exist_ok=True)

    def fetch(self, force: bool = False) -> str:
        """Fetch the branch from the remote. Returns the head commit after the fetch."""
        with self._lock:
            now = time.time()
            if force or now - self._last_fetch >= self.fetch_min_interval or not self.has_head():
                self.git("fetch", "--prune", "--no-tags", "origin", timeout=1800)
                self._last_fetch = time.time()
            return self.head()

    def has_head(self) -> bool:
        try:
            self.git("rev-parse", "--verify", "--quiet", self.ref)
            return True
        except GitError:
            return False

    def head(self) -> str:
        return self.git("rev-parse", self.ref).strip()

    # ---------------------------------------------------------------- history
    def rev_list(self, tip: str | None = None) -> list[str]:
        """All commits reachable from the branch tip, oldest first."""
        out = self.git("rev-list", "--reverse", "--topo-order", tip or self.ref)
        return [line for line in out.splitlines() if line]

    def commit_meta(self, shas: list[str]) -> list[dict]:
        """Author, date, subject, body and parent count for many commits."""
        result: list[dict] = []
        for i in range(0, len(shas), 500):
            batch = shas[i : i + 500]
            fmt = FIELD_SEP.join(["%H", "%an", "%aI", "%P", "%s", "%b"]) + RECORD_SEP
            out = self.git("show", "-s", f"--format={fmt}", *batch)
            for rec in out.split(RECORD_SEP):
                rec = rec.strip("\n")
                if not rec:
                    continue
                sha, author, date, parents, subject, body = (rec.split(FIELD_SEP) + [""] * 6)[:6]
                result.append(
                    {
                        "sha": sha,
                        "author": author,
                        "date": date,
                        "parents": len(parents.split()),
                        "subject": subject,
                        "body": body.strip(),
                    }
                )
        return result

    def commit_files(self, sha: str) -> list[str]:
        out = self.git("show", "--format=", "--name-only", "--first-parent", "-m", sha)
        return [line for line in out.splitlines() if line]

    def commit_patch(self, sha: str, budget: int, merge: bool = False) -> str:
        """Diffstat plus a patch shortened to the budget. For merges, only the stat vs the first parent."""
        if merge:
            return self.git("diff", "--stat=120", f"{sha}^1", sha)
        out = self.git(
            "show", "--format=", "--stat=120", "--patch", "-M", "--no-color", sha, "--", ".", *NOISY_PATHSPECS
        )
        return truncate_patch(out, budget)

    # -------------------------------------------------------------- worktrees
    def acquire(self, sha: str) -> Path:
        """Get a checkout of the commit. Shared between concurrent users of the same commit."""
        with self._lock:
            path = self.worktrees_path / sha
            if not (path / ".git").exists():
                if path.exists():
                    shutil.rmtree(path)
                self.git("worktree", "prune")
                self.git("worktree", "add", "--detach", "--force", str(path), sha)
            self._refcount[sha] = self._refcount.get(sha, 0) + 1
            return path

    def release(self, sha: str) -> None:
        with self._lock:
            self._refcount[sha] = max(0, self._refcount.get(sha, 0) - 1)
            self.cleanup_worktrees()

    @contextlib.contextmanager
    def checkout(self, sha: str) -> Iterator[Path]:
        path = self.acquire(sha)
        try:
            yield path
        finally:
            self.release(sha)

    @contextlib.asynccontextmanager
    async def acheckout(self, sha: str) -> AsyncIterator[Path]:
        path = await asyncio.to_thread(self.acquire, sha)
        try:
            yield path
        finally:
            await asyncio.to_thread(self.release, sha)

    def cleanup_worktrees(self) -> None:
        """Remove checkouts that nobody uses and that are not the branch head."""
        with self._lock:
            if not self.worktrees_path.exists():
                return
            try:
                keep = {self.head()}
            except GitError:
                keep = set()
            keep |= {s for s, n in self._refcount.items() if n > 0}
            for child in self.worktrees_path.iterdir():
                if child.name in keep:
                    continue
                try:
                    self.git("worktree", "remove", "--force", str(child))
                except GitError:
                    shutil.rmtree(child, ignore_errors=True)
                self._refcount.pop(child.name, None)
            with contextlib.suppress(GitError):
                self.git("worktree", "prune")
