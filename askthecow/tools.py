"""Read-only tools that the agent uses on a checkout of the branch head.

All paths are confined to the checkout. No tool can write files or run arbitrary commands.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .db import Database
from .gitrepo import GitError, run_git, truncate_patch

MAX_RESULT_CHARS = 30_000
MAX_READ_LINES = 600


class ToolError(Exception):
    pass


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "list_dir",
        "description": (
            "List the files and folders in a directory of the repository (master head). "
            "Use '.' for the repository root. depth 1 to 3."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory relative to the repository root."},
                "depth": {"type": "integer", "description": "How many levels to show (1 to 3). Default 1."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "find_files",
        "description": (
            "Find tracked files by glob pattern on the full path, for example '**/*adc*', "
            "'**/*.ld', 'drivers/**/*.h'. Case-insensitive. Returns at most 300 paths."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read a text file with line numbers. Read at most 600 lines per call; "
            "use start_line and end_line for long files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "description": "First line (1-based). Default 1."},
                "end_line": {"type": "integer", "description": "Last line, inclusive."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_code",
        "description": (
            "Search file contents with an extended regular expression (git grep -E). "
            "Use it to find macros, register names, pin names, functions and constants. "
            "Optional path_glob limits the files, for example '*.c' or 'drivers/'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path_glob": {"type": "string"},
                "ignore_case": {"type": "boolean"},
                "context_lines": {"type": "integer", "description": "Lines of context around each match (0 to 5)."},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
    {
        "name": "git_log",
        "description": (
            "Show the commit history of the branch, newest first. Optional path limits it to a file or "
            "folder. Optional message_grep filters commit messages. Optional code_change finds commits "
            "that added or removed lines matching a regex (git log -G)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "message_grep": {"type": "string"},
                "code_change": {"type": "string"},
                "max_count": {"type": "integer", "description": "Default 20, at most 100."},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "git_show",
        "description": "Show one commit: message, changed files and the patch (shortened when long).",
        "input_schema": {
            "type": "object",
            "properties": {
                "commit": {"type": "string", "description": "Commit hash (short or full)."},
                "path": {"type": "string", "description": "Optional: show only the change to this path."},
            },
            "required": ["commit"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_history",
        "description": (
            "Search the knowledge base of plain-language commit notes made during calibration. "
            "Use it for 'when / why did X change' questions, and to find which commits touched a feature. "
            "Query with keywords (peripheral names, signals, features, file names)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
]


def _clip(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... output cut, {len(text) - limit} more characters. Narrow the request ...]"


@dataclass
class RepoTools:
    root: Path
    db: Database | None = None

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()
        self._handlers: dict[str, Callable[..., str]] = {
            "list_dir": self.list_dir,
            "find_files": self.find_files,
            "read_file": self.read_file,
            "search_code": self.search_code,
            "git_log": self.git_log,
            "git_show": self.git_show,
            "search_history": self.search_history,
        }

    # ----------------------------------------------------------------- dispatch
    def run(self, name: str, args: dict[str, Any]) -> str:
        handler = self._handlers.get(name)
        if handler is None:
            raise ToolError(f"Unknown tool: {name}")
        if not isinstance(args, dict):
            raise ToolError("Tool input must be an object")
        allowed = set(next(t for t in TOOL_DEFINITIONS if t["name"] == name)["input_schema"]["properties"])
        unknown = set(args) - allowed
        if unknown:
            raise ToolError(f"Unknown arguments: {sorted(unknown)}")
        try:
            return _clip(handler(**args))
        except GitError as exc:
            raise ToolError(str(exc)) from exc
        except TypeError as exc:
            raise ToolError(f"Bad arguments: {exc}") from exc

    # ------------------------------------------------------------------ helpers
    def _resolve(self, rel: str) -> Path:
        rel = (rel or ".").strip()
        if rel.startswith("/"):
            rel = rel.lstrip("/")
        target = (self.root / rel).resolve()
        if target != self.root and self.root not in target.parents:
            raise ToolError("Path is outside the repository")
        if ".git" in target.relative_to(self.root).parts:
            raise ToolError("The .git folder is not available")
        return target

    def _git(self, *args: str, ok_codes: tuple[int, ...] = (0,)) -> str:
        return run_git(list(args), cwd=self.root, timeout=120, ok_codes=ok_codes)

    def _tracked_files(self) -> list[str]:
        return [p for p in self._git("ls-files", "-z").split("\0") if p]

    # -------------------------------------------------------------------- tools
    def list_dir(self, path: str = ".", depth: int = 1) -> str:
        base = self._resolve(path)
        if not base.is_dir():
            raise ToolError(f"Not a directory: {path}")
        depth = max(1, min(int(depth or 1), 3))
        lines: list[str] = []

        def walk(d: Path, level: int) -> None:
            try:
                entries = sorted(d.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            except OSError:
                return
            for entry in entries:
                if entry.name == ".git":
                    continue
                rel = entry.relative_to(self.root).as_posix()
                indent = "  " * (level - 1)
                if entry.is_dir():
                    lines.append(f"{indent}{rel}/")
                    if level < depth:
                        walk(entry, level + 1)
                else:
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        size = 0
                    lines.append(f"{indent}{rel}  ({size} bytes)")
                if len(lines) > 1500:
                    return

        walk(base, 1)
        return "\n".join(lines) or "(empty directory)"

    def find_files(self, pattern: str) -> str:
        pat = pattern.strip().lower()
        if not pat:
            raise ToolError("Empty pattern")
        if "/" not in pat and not pat.startswith("**"):
            pat = "**/" + pat
        files = self._tracked_files()
        matches = [f for f in files if fnmatch.fnmatch(f.lower(), pat) or fnmatch.fnmatch("/" + f.lower(), pat)]
        if not matches:
            return "No files match."
        more = f"\n[... {len(matches) - 300} more]" if len(matches) > 300 else ""
        return "\n".join(matches[:300]) + more

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        target = self._resolve(path)
        if not target.is_file():
            raise ToolError(f"Not a file: {path}")
        data = target.read_bytes()
        if b"\0" in data[:8000]:
            return f"{path} is a binary file ({len(data)} bytes)."
        lines = data.decode("utf-8", errors="replace").splitlines()
        total = len(lines)
        start = max(1, int(start_line or 1))
        end = int(end_line) if end_line else start + MAX_READ_LINES - 1
        end = min(end, start + MAX_READ_LINES - 1, total)
        if start > total:
            return f"{path} has only {total} lines."
        width = len(str(end))
        body = "\n".join(f"{i:>{width}}  {lines[i - 1]}" for i in range(start, end + 1))
        footer = f"\n[lines {start}-{end} of {total}]" if (start > 1 or end < total) else ""
        return body + footer

    def search_code(
        self, pattern: str, path_glob: str | None = None, ignore_case: bool = False, context_lines: int = 0
    ) -> str:
        if not pattern:
            raise ToolError("Empty pattern")
        args = ["grep", "-n", "-I", "-E", "--no-color", "--full-name"]
        if ignore_case:
            args.append("-i")
        ctx = max(0, min(int(context_lines or 0), 5))
        if ctx:
            args.append(f"-C{ctx}")
        args += ["-e", pattern, "--"]
        if path_glob:
            glob = path_glob.strip()
            if glob.endswith("/"):
                args.append(glob)
            else:
                args.append(f":(glob,icase)**/{glob}" if "/" not in glob else f":(glob,icase){glob}")
        out = self._git(*args, ok_codes=(0, 1))  # git grep exits 1 when nothing matches
        lines = out.splitlines()
        if not lines:
            return "No matches."
        if len(lines) > 400:
            return "\n".join(lines[:400]) + f"\n[... {len(lines) - 400} more lines. Narrow the pattern or path ...]"
        return out

    def git_log(
        self,
        path: str | None = None,
        message_grep: str | None = None,
        code_change: str | None = None,
        max_count: int = 20,
    ) -> str:
        count = max(1, min(int(max_count or 20), 100))
        args = ["log", f"-n{count}", "--date=short", "--format=%h %ad %an: %s"]
        if message_grep:
            args += ["-i", "-E", f"--grep={message_grep}"]
        if code_change:
            args.append(f"-G{code_change}")
        if path:
            self._resolve(path)
            args += ["--", path]
        out = self._git(*args)
        return out or "No commits found."

    def git_show(self, commit: str, path: str | None = None) -> str:
        if not re.fullmatch(r"[0-9a-fA-F]{4,40}", commit.strip()):
            raise ToolError("commit must be a hash")
        sha = commit.strip()
        header = self._git("show", "-s", "--date=iso", "--format=commit %H%nAuthor: %an%nDate: %ad%n%n%B", sha)
        args = ["show", "--format=", "--stat=120", "--patch", "-M", "--no-color", "--first-parent", "-m", sha]
        if path:
            self._resolve(path)
            args += ["--", path]
        patch = self._git(*args)
        return header + "\n" + truncate_patch(patch, MAX_RESULT_CHARS - len(header) - 200)

    def search_history(self, query: str) -> str:
        if self.db is None:
            return "The knowledge base is not available."
        rows = self.db.search_commit_notes(query, limit=15)
        if not rows:
            return "No commit notes match. Try other keywords, or use git_log."
        out = []
        for r in rows:
            out.append(
                f"{r['sha'][:10]} {r['date'][:10]} {r['author']}: {r['subject']}\n"
                f"  note: {r['note']}\n  files: {r['files'][:300]}\n  topics: {r['topics']}"
            )
        return "\n\n".join(out)
