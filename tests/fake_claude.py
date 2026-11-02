#!/usr/bin/env python3
"""A stand-in for `claude -p` in tests. It speaks the same stream-json protocol.

Every call is logged (argv, cwd, prompt, appended system prompt) as one JSON line to $FAKE_CLAUDE_LOG.
"""

import json
import os
import re
import sys
from pathlib import Path


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def arg(name):
    if name in sys.argv:
        i = sys.argv.index(name)
        return sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
    return None


def result(text, structured=None, is_error=False, subtype="success"):
    out({
        "type": "result", "subtype": subtype, "is_error": is_error, "result": text,
        "structured_output": structured, "session_id": "fake", "total_cost_usd": 0.01,
        "num_turns": 2, "usage": {"input_tokens": 10, "output_tokens": 5},
    })


def assistant(content):
    out({"type": "assistant", "parent_tool_use_id": None, "message": {"role": "assistant", "content": content}})


def main():
    prompt = sys.stdin.read()
    system_file = arg("--append-system-prompt-file")
    appended = Path(system_file).read_text() if system_file else ""
    log = os.environ.get("FAKE_CLAUDE_LOG")
    if log:
        with open(log, "a") as fh:
            fh.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "prompt": prompt, "appended": appended}) + "\n")

    out({"type": "system", "subtype": "init", "model": arg("--model"), "tools": (arg("--tools") or "").split(",")})

    if arg("--json-schema") is not None:  # commit notes
        shas = re.findall(r"=== commit ([0-9a-f]{40}) ===", prompt)
        data = {
            "summary": f"Group of {len(shas)} commits about ADC and limits.",
            "commits": [{"sha": s, "note": f"Note for {s[:7]}: ADC or limit change.", "topics": ["ADC", "PA0"]} for s in shas],
        }
        result(json.dumps(data), structured=data)
        return

    if "Merge these period summaries" in prompt:
        result("Condensed period summary.")
        return

    if "repository brief" in prompt.lower():  # brief agent
        assistant([{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "git log --oneline"}}])
        kind = "UPDATED" if "Update the repository brief" in prompt else "FULL"
        result(f"# Brief ({kind})\nADC lives in drivers/adc.c.")
        return

    # Answer agent: read the real file in the checkout to prove the working directory is right.
    if "PLEASE_FAIL" in prompt:
        result("Budget limit reached", is_error=True, subtype="error_max_budget_usd")
        return
    line = next((x for x in Path("drivers/adc.c").read_text().splitlines() if "ADC_SAMPLE_RATE_HZ" in x), "?")
    out({"type": "stream_event", "parent_tool_use_id": None, "event": {"type": "message_start"}})
    assistant([{"type": "tool_use", "id": "t1", "name": "Grep", "input": {"pattern": "ADC_SAMPLE_RATE_HZ"}}])
    out({"type": "system", "subtype": "permission_denied", "message": "Permission to use Bash with command touch x has been denied."})
    out({"type": "stream_event", "parent_tool_use_id": None, "event": {"type": "message_start"}})
    answer = f"The ADC rate is set by `{line.strip()}` (`drivers/adc.c:1`)."
    for i in range(0, len(answer), 10):
        out({"type": "stream_event", "parent_tool_use_id": None,
             "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": answer[i:i + 10]}}})
    # Sub-agent text must not leak into the answer stream.
    out({"type": "stream_event", "parent_tool_use_id": "t9",
         "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "SUBAGENT"}}})
    result(answer)


if __name__ == "__main__":
    main()
