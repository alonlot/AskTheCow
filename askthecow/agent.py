"""Agent loop on the Claude API with the read-only repository tools.

The loop is append-only: each response is appended unchanged (thinking and fallback blocks
included), then the tool results. This keeps the prompt cache warm and keeps thinking blocks valid.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import anthropic

from .config import Settings
from .tools import TOOL_DEFINITIONS, RepoTools, ToolError

log = logging.getLogger(__name__)

EventSink = Callable[[dict[str, Any]], Awaitable[None]]

FALLBACK_BETA = "server-side-fallback-2026-07-01"


async def _noop(_: dict[str, Any]) -> None:
    return None


@dataclass
class AgentResult:
    text: str
    stop_reason: str | None
    turns: int
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    served_by: str | None = None


def request_options(settings: Settings, model: str, effort: str) -> dict[str, Any]:
    """Parameters shared by every request this server sends."""
    opts: dict[str, Any] = {"model": model, "output_config": {"effort": effort}}
    if settings.use_fallbacks:
        # On a safety-classifier decline, the API re-runs the request on a recommended model.
        opts["betas"] = [FALLBACK_BETA]
        opts["fallbacks"] = "default"
    return opts


def text_of(message: Any) -> str:
    return "".join(b.text for b in message.content if getattr(b, "type", None) == "text").strip()


def _add_usage(total: dict[str, int], usage: Any) -> None:
    if usage is None:
        return
    for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        value = getattr(usage, key, None)
        if isinstance(value, int):
            total[key] = total.get(key, 0) + value


async def run_agent(
    client: anthropic.AsyncAnthropic,
    settings: Settings,
    *,
    system: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    tools: RepoTools,
    model: str | None = None,
    effort: str | None = None,
    max_turns: int | None = None,
    max_tokens: int | None = None,
    on_event: EventSink = _noop,
) -> AgentResult:
    model = model or settings.model
    effort = effort or settings.answer_effort
    max_turns = max_turns or settings.max_agent_turns
    max_tokens = max_tokens or settings.max_answer_tokens
    messages = list(messages)
    usage: dict[str, int] = {}
    calls: list[dict[str, Any]] = []
    allow_tools = True
    turn = 0
    message: Any = None

    while True:
        turn += 1
        await on_event({"type": "turn", "turn": turn})
        params: dict[str, Any] = {
            **request_options(settings, model, effort),
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "tools": TOOL_DEFINITIONS,
            # Automatic caching: the whole prefix (tools, system with the brief, history) is cached.
            "cache_control": {"type": "ephemeral"},
        }
        if not allow_tools:
            params["tool_choice"] = {"type": "none"}

        async with client.beta.messages.stream(**params) as stream:
            async for event in stream:
                if event.type == "text":
                    await on_event({"type": "text", "text": event.text})
            message = await stream.get_final_message()
        _add_usage(usage, message.usage)

        # Keep the full response, unchanged.
        messages.append({"role": "assistant", "content": message.content})
        stop = message.stop_reason

        if stop == "pause_turn":
            continue
        if stop == "refusal":
            return AgentResult(
                text=text_of(message)
                or "The model declined to answer this question. Rephrase it, or ask the software team.",
                stop_reason=stop,
                turns=turn,
                tool_calls=calls,
                usage=usage,
                served_by=message.model,
            )

        tool_uses = [b for b in message.content if getattr(b, "type", None) == "tool_use"]
        if stop != "tool_use" or not tool_uses:
            text = text_of(message)
            if stop == "max_tokens":
                text += "\n\n_(The answer was cut at the output limit.)_"
            return AgentResult(
                text=text, stop_reason=stop, turns=turn, tool_calls=calls, usage=usage, served_by=message.model
            )

        async def run_one(block: Any) -> tuple[dict[str, Any], dict[str, Any]]:
            await on_event({"type": "tool", "name": block.name, "input": block.input})
            try:
                output = await asyncio.to_thread(tools.run, block.name, dict(block.input or {}))
                is_error = False
            except ToolError as exc:
                output, is_error = f"Error: {exc}", True
            except Exception as exc:  # a tool bug must not stop the answer
                log.exception("tool %s failed", block.name)
                output, is_error = f"Error: {type(exc).__name__}: {exc}", True
            record = {"name": block.name, "input": block.input, "error": is_error, "chars": len(output)}
            await on_event({"type": "tool_done", "name": block.name, "error": is_error, "chars": len(output)})
            result: dict[str, Any] = {"type": "tool_result", "tool_use_id": block.id, "content": output}
            if is_error:
                result["is_error"] = True
            return result, record

        # Tools run in parallel; all results of one response go back in one user message, in order.
        done = await asyncio.gather(*(run_one(b) for b in tool_uses))
        calls.extend(record for _, record in done)
        results: list[dict[str, Any]] = [result for result, _ in done]
        if turn >= max_turns:
            allow_tools = False
            results.append(
                {
                    "type": "text",
                    "text": "Tool budget is used up. Give your best answer now from what you found, "
                    "and say clearly what you could not confirm.",
                }
            )
        messages.append({"role": "user", "content": results})


def make_client() -> anthropic.AsyncAnthropic:
    # Credentials: ANTHROPIC_API_KEY (or another source the SDK resolves).
    return anthropic.AsyncAnthropic(max_retries=4)

