"""Settings. Every field can be set with an environment variable: ASKTHECOW_<FIELD NAME IN CAPITALS>."""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    # Repository. Only this one branch is fetched, calibrated and searched.
    repo_url: str = ""
    branch: str = "master"
    project_name: str = "the firmware"

    # Storage
    data_dir: Path = Path("data")

    # Claude Code CLI. Every agent run is one `claude -p` process.
    claude_bin: str = "claude"
    model: str = "claude-opus-5-5"
    answer_effort: str = "high"
    calibration_model: str = "claude-opus-5-5"
    digest_effort: str = "medium"
    brief_effort: str = "high"
    fallback_model: str = ""  # optional `--fallback-model`, used when the main model is overloaded
    answer_timeout_seconds: int = 900
    brief_timeout_seconds: int = 3600
    digest_timeout_seconds: int = 900
    answer_max_budget_usd: float = 0.0  # 0 means no limit
    max_concurrent_agents: int = 3

    # Calibration
    digest_chunk_chars: int = 120_000
    max_diff_chars_per_commit: int = 8_000
    digest_concurrency: int = 4
    brief_max_chars: int = 60_000
    history_max_chars: int = 400_000
    full_rebrief_every: int = 25

    # Sync with the remote
    poll_interval_seconds: int = 300
    fetch_min_interval_seconds: int = 30
    auto_calibrate: bool = True
    webhook_secret: str = ""

    # Extra input channels started with the server, as "module:ClassName", comma separated.
    channels: list[str] = field(default_factory=list)

    # HTTP
    host: str = "127.0.0.1"
    port: int = 8000
    password: str = ""  # When set, HTTP Basic auth is required. The user name becomes the chat owner.
    admin_token: str = ""  # When set, admin endpoints are on and need header X-Admin-Token.
    api_token: str = ""  # When set, POST /api/ask is on and needs header X-Api-Token.

    @property
    def db_path(self) -> Path:
        return self.data_dir / "askthecow.sqlite3"

    @property
    def mirror_path(self) -> Path:
        return self.data_dir / "repo.git"

    @property
    def worktrees_path(self) -> Path:
        return self.data_dir / "worktrees"

    @property
    def knowledge_path(self) -> Path:
        """Plain files the agents can read: brief.md, history.md, commit-notes.txt."""
        return self.data_dir / "knowledge"

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ
        values = {}
        for f in dataclasses.fields(cls):
            raw = env.get(f"ASKTHECOW_{f.name.upper()}")
            if raw is None or raw == "":
                continue
            default = cls.__dataclass_fields__[f.name].default
            if f.name == "channels":
                values[f.name] = [c.strip() for c in raw.split(",") if c.strip()]
            elif isinstance(default, bool):
                values[f.name] = raw.strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(default, int):
                values[f.name] = int(raw)
            elif isinstance(default, float):
                values[f.name] = float(raw)
            elif isinstance(default, Path):
                values[f.name] = Path(raw)
            else:
                values[f.name] = raw
        return cls(**values)
