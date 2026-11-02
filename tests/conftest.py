from __future__ import annotations

import json
import subprocess
from pathlib import Path

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


FAKE_CLAUDE = Path(__file__).parent / "fake_claude.py"


@pytest.fixture
def claude_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Path of the log the fake `claude` writes: one JSON line per call."""
    log = tmp_path / "claude-calls.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    return log


def read_log(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


@pytest.fixture
def settings(tmp_path: Path, upstream: Path, claude_log: Path) -> Settings:
    return Settings(
        claude_bin=str(FAKE_CLAUDE),
        repo_url=str(upstream),
        branch="master",
        project_name="Motor controller",
        data_dir=tmp_path / "data",
        fetch_min_interval_seconds=0,
        digest_chunk_chars=2000,
        digest_concurrency=2,
    )


