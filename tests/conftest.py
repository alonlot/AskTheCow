from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from askthecow.config import Settings


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "Dana",
            "GIT_AUTHOR_EMAIL": "dana@example.com",
            "GIT_COMMITTER_NAME": "Dana",
            "GIT_COMMITTER_EMAIL": "dana@example.com",
            "HOME": str(cwd),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    ).stdout


def commit_file(repo: Path, path: str, content: str, message: str) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    git(repo, "add", path)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    """A small firmware repo with a master branch and a feature branch."""
    repo = tmp_path / "upstream"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "master")
    commit_file(repo, "README.md", "# Motor controller firmware\nSTM32G4, bare metal.\n", "Initial commit")
    commit_file(
        repo,
        "drivers/adc.c",
        '#include "adc.h"\n#define ADC_SAMPLE_RATE_HZ 1000\nvoid adc_init(void) {\n  /* PA0 = motor current */\n}\n',
        "Add ADC driver, 1 kHz sampling on PA0",
    )
    commit_file(
        repo,
        "config/limits.h",
        "#define OVERCURRENT_LIMIT_MA 2500\n#define OVERCURRENT_DEBOUNCE_MS 5\n",
        "Add over-current limits",
    )
    git(repo, "checkout", "-q", "-b", "feature/secret")
    commit_file(repo, "secret.txt", "not on master\n", "Feature branch work")
    git(repo, "checkout", "-q", "master")
    return repo


@pytest.fixture
def settings(tmp_path: Path, upstream: Path) -> Settings:
    return Settings(
        repo_url=str(upstream),
        branch="master",
        project_name="Motor controller",
        data_dir=tmp_path / "data",
        fetch_min_interval_seconds=0,
        digest_chunk_chars=2000,
        digest_concurrency=2,
    )


# --------------------------------------------------------------- fake Claude


def block(**kw: Any) -> SimpleNamespace:
    return SimpleNamespace(**kw)


def message(content: list, stop_reason: str) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        model="claude-opus-5-5",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0),
    )


class FakeStream:
    def __init__(self, msg: SimpleNamespace):
        self.msg = msg

    async def __aenter__(self) -> "FakeStream":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def __aiter__(self):
        async def gen():
            for b in self.msg.content:
                if b.type == "text":
                    yield SimpleNamespace(type="text", text=b.text)

        return gen()

    async def get_final_message(self) -> SimpleNamespace:
        return self.msg


class FakeMessages:
    def __init__(self, owner: "FakeClient"):
        self.owner = owner

    def stream(self, **params: Any) -> FakeStream:
        self.owner.calls.append(params)
        return FakeStream(self.owner.respond(params))


class FakeClient:
    """Answers like the model would, enough to drive every code path."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.beta = SimpleNamespace(messages=FakeMessages(self))

    @staticmethod
    def _last_user_text(params: dict) -> str:
        content = params["messages"][-1]["content"]
        if isinstance(content, str):
            return content
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict))

    def respond(self, params: dict) -> SimpleNamespace:
        system = params["system"] if isinstance(params["system"], str) else " ".join(b["text"] for b in params["system"])
        msgs = params["messages"]
        first = msgs[0]["content"] if isinstance(msgs[0]["content"], str) else ""
        has_tool_results = any(
            isinstance(m["content"], list) and any(isinstance(c, dict) and c.get("type") == "tool_result" for c in m["content"])
            for m in msgs
        )

        if "output_config" in params and "format" in params["output_config"]:  # digest
            shas = re.findall(r"=== commit ([0-9a-f]{40}) ===", first)
            data = {
                "summary": f"Group of {len(shas)} commits about ADC and limits.",
                "commits": [{"sha": s, "note": f"Note for {s[:7]}: ADC or limit change.", "topics": ["ADC", "PA0"]} for s in shas],
            }
            return message([block(type="text", text=json.dumps(data))], "end_turn")

        if "Merge these period summaries" in first:
            return message([block(type="text", text="Condensed period summary.")], "end_turn")

        if "repository brief" in first.lower() and "senior embedded" in system:  # brief agent
            if not has_tool_results:
                return message(
                    [block(type="tool_use", id="tu_b1", name="list_dir", input={"path": ".", "depth": 2})], "tool_use"
                )
            kind = "UPDATED" if "Update the repository brief" in first else "FULL"
            return message([block(type="text", text=f"# Brief ({kind})\nADC lives in drivers/adc.c.")], "end_turn")

        # question agent
        if not has_tool_results:
            return message(
                [
                    block(type="thinking", thinking="", signature="sig"),
                    block(type="tool_use", id="tu_q1", name="search_code", input={"pattern": "ADC_SAMPLE_RATE_HZ"}),
                    block(type="tool_use", id="tu_q2", name="read_file", input={"path": "../../etc/passwd"}),
                ],
                "tool_use",
            )
        return message([block(type="text", text="The ADC samples at **1 kHz** (`drivers/adc.c:2`).")], "end_turn")
