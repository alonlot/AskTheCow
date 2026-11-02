"""Run Claude Code in headless mode (`claude -p`) as a subprocess.

One function, `run_claude(job)`, is used for every agent run in this project:
answers, commit notes, history condensing and the repository brief.

The prompt goes to stdin. The output is read as `--output-format stream-json`, and the events are
turned into small dicts for the caller (text deltas, tool calls, permission denials).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import Settings

log = logging.getLogger(__name__)

EventSink = Callable[[dict[str, Any]], Awaitable[None]]

# Read-only tool set for agents that look at the repository.
READ_ONLY_TOOLS = ["Read", "Grep", "Glob", "Bash"]
READ_ONLY_ALLOWED = [
    "Read",
    "Grep",
    "Glob",
    "Bash(git log:*)",
    "Bash(git show:*)",
    "Bash(git blame:*)",
    "Bash(git diff:*)",
    "Bash(git ls-files:*)",
    "Bash(git ls-tree:*)",
    "Bash(git shortlog:*)",
    "Bash(git rev-parse:*)",
    "Bash(git describe:*)",
]
# Options of the allowed git commands that write files or read outside the checkout.
READ_ONLY_DENIED = [
    "Bash(git * --output*)",
    "Bash(git * --ext-diff*)",
    "Bash(git * --no-index*)",
    "Edit",
    "Write",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
]


class ClaudeError(RuntimeError):
    pass


@dataclass
class ClaudeJob:
    prompt: str
    cwd: Path
    model: str
    effort: str | None = None
    system_prompt: str | None = None  # replaces the default Claude Code system prompt
    append_system_prompt: str | None = None  # added to the default system prompt
    tools: list[str] = field(default_factory=list)  # empty: no tools at all
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    add_dirs: list[Path] = field(default_factory=list)
    json_schema: dict | None = None
    max_budget_usd: float = 0.0
    timeout: float = 900
    stream_text: bool = False  # emit text deltas (--include-partial-messages)

    @classmethod
    def read_only_agent(cls, **kw: Any) -> "ClaudeJob":
        return cls(tools=list(READ_ONLY_TOOLS), allowed_tools=list(READ_ONLY_ALLOWED),
                   disallowed_tools=list(READ_ONLY_DENIED), **kw)


@dataclass
class ClaudeResult:
    text: str
    is_error: bool
    subtype: str | None = None
    structured: Any = None
    session_id: str | None = None
    cost_usd: float | None = None
    num_turns: int | None = None
    usage: dict = field(default_factory=dict)
    tool_calls: list[dict] = field(default_factory=list)
    denials: list[str] = field(default_factory=list)
    model: str | None = None


def build_command(settings: Settings, job: ClaudeJob, system_file: Path | None) -> list[str]:
    cmd = [
        settings.claude_bin,
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--model", job.model,
        "--permission-mode", "dontAsk",  # anything not allowed is denied, nothing waits for a person
        "--restricted",  # ignore settings files, keep file tools inside the working dirs
        "--strict-mcp-config",  # no MCP servers
        "--no-session-persistence",
        "--tools", ",".join(job.tools) if job.tools else "",
    ]
    if job.stream_text:
        cmd.append("--include-partial-messages")
    if job.effort:
        cmd += ["--effort", job.effort]
    if settings.fallback_model:
        cmd += ["--fallback-model", settings.fallback_model]
    if job.system_prompt is not None:
        cmd += ["--system-prompt", job.system_prompt]
    if system_file is not None:
        cmd += ["--append-system-prompt-file", str(system_file)]
    if job.allowed_tools:
        cmd += ["--allowedTools", *job.allowed_tools]
    if job.disallowed_tools:
        cmd += ["--disallowedTools", *job.disallowed_tools]
    for d in job.add_dirs:
        cmd += ["--add-dir", str(d)]
    if job.json_schema is not None:
        cmd += ["--json-schema", json.dumps(job.json_schema)]
    if job.max_budget_usd and job.max_budget_usd > 0:
        cmd += ["--max-budget-usd", str(job.max_budget_usd)]
    return cmd


async def _noop(_: dict[str, Any]) -> None:
    return None


async def run_claude(settings: Settings, job: ClaudeJob, on_event: EventSink | None = None) -> ClaudeResult:
    """Run one `claude -p` process and return its final result."""
    emit = on_event or _noop
    system_file: Path | None = None
    if job.append_system_prompt:
        # A file, because the brief can be longer than one command line argument may be.
        fd, name = tempfile.mkstemp(prefix="askthecow-system-", suffix=".md")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(job.append_system_prompt)
        system_file = Path(name)
    cmd = build_command(settings, job, system_file)
    env = {**os.environ, "DISABLE_AUTOUPDATER": "1"}
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(job.cwd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=64 * 1024 * 1024,  # one stream-json line can be large
        )
    except FileNotFoundError as exc:
        if system_file:
            system_file.unlink(missing_ok=True)
        raise ClaudeError(f"Claude Code CLI not found: {settings.claude_bin}. Install it, or set ASKTHECOW_CLAUDE_BIN.") from exc

    stderr_task = asyncio.create_task(proc.stderr.read())  # type: ignore[union-attr]
    try:
        async def feed() -> None:
            assert proc.stdin is not None
            proc.stdin.write(job.prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()

        feeder = asyncio.create_task(feed())
        result = await asyncio.wait_for(_read_events(proc, emit), timeout=job.timeout)
        await feeder
        await proc.wait()
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise ClaudeError(f"claude -p did not finish in {job.timeout:.0f} seconds") from exc
    except asyncio.CancelledError:
        proc.kill()
        raise
    finally:
        if system_file:
            system_file.unlink(missing_ok=True)

    stderr = (await stderr_task).decode("utf-8", errors="replace").strip()
    if result is None:
        raise ClaudeError(f"claude -p ended without a result (exit code {proc.returncode}). {stderr[-2000:]}")
    return result


async def _read_events(proc: asyncio.subprocess.Process, emit: EventSink) -> ClaudeResult | None:
    assert proc.stdout is not None
    calls: list[dict] = []
    denials: list[str] = []
    model: str | None = None
    result: ClaudeResult | None = None
    async for raw in proc.stdout:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            log.debug("not JSON from claude: %s", line[:200])
            continue
        etype = ev.get("type")
        top_level = ev.get("parent_tool_use_id") is None
        if etype == "system" and ev.get("subtype") == "init":
            model = ev.get("model")
        elif etype == "stream_event" and top_level:
            inner = ev.get("event") or {}
            if inner.get("type") == "message_start":
                await emit({"type": "turn"})
            elif inner.get("type") == "content_block_delta" and (inner.get("delta") or {}).get("type") == "text_delta":
                await emit({"type": "text", "text": inner["delta"].get("text", "")})
        elif etype == "assistant" and top_level:
            for block in (ev.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use":
                    call = {"name": block.get("name"), "input": block.get("input") or {}}
                    calls.append(call)
                    await emit({"type": "tool", **call})
        elif etype == "system" and ev.get("subtype") == "permission_denied":
            denials.append(ev.get("message") or "")
        elif etype == "result":
            result = ClaudeResult(
                text=(ev.get("result") or "").strip(),
                is_error=bool(ev.get("is_error")),
                subtype=ev.get("subtype"),
                structured=ev.get("structured_output"),
                session_id=ev.get("session_id"),
                cost_usd=ev.get("total_cost_usd"),
                num_turns=ev.get("num_turns"),
                usage={k: v for k, v in (ev.get("usage") or {}).items() if isinstance(v, int)},
                tool_calls=calls,
                denials=denials,
                model=model,
            )
    return result
