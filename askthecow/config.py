"""Settings. All values come from environment variables (prefix ASKTHECOW_)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(f"ASKTHECOW_{name}", default)


def _int(name: str, default: int) -> int:
    value = _env(name)
    return int(value) if value not in (None, "") else default


def _bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value in (None, ""):
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # Repository. Only this one branch is fetched, calibrated and searched.
    repo_url: str = ""
    branch: str = "master"
    project_name: str = "the firmware"

    # Storage
    data_dir: Path = Path("data")

    # Models. Effort controls thinking depth and token spend.
    model: str = "claude-opus-5-5"
    answer_effort: str = "high"
    calibration_model: str = "claude-opus-5-5"
    digest_effort: str = "medium"
    brief_effort: str = "high"
    use_fallbacks: bool = True

    # Agent limits
    max_agent_turns: int = 40
    max_answer_tokens: int = 32000

    # Calibration
    digest_chunk_chars: int = 120_000
    max_diff_chars_per_commit: int = 8_000
    digest_concurrency: int = 4
    brief_max_chars: int = 60_000

    # Sync with the remote
    poll_interval_seconds: int = 300
    fetch_min_interval_seconds: int = 30
    auto_calibrate: bool = True
    webhook_secret: str = ""

    # HTTP
    host: str = "127.0.0.1"
    port: int = 8000
    password: str = ""  # When set, HTTP Basic auth is required (any user name).
    admin_token: str = ""  # When set, admin endpoints need header X-Admin-Token.

    @property
    def db_path(self) -> Path:
        return self.data_dir / "askthecow.sqlite3"

    @property
    def mirror_path(self) -> Path:
        return self.data_dir / "repo.git"

    @property
    def worktrees_path(self) -> Path:
        return self.data_dir / "worktrees"

    @classmethod
    def from_env(cls) -> "Settings":
        d = cls()
        return cls(
            repo_url=_env("REPO_URL", d.repo_url) or "",
            branch=_env("BRANCH", d.branch) or "master",
            project_name=_env("PROJECT_NAME", d.project_name) or d.project_name,
            data_dir=Path(_env("DATA_DIR", str(d.data_dir)) or "data"),
            model=_env("MODEL", d.model) or d.model,
            answer_effort=_env("ANSWER_EFFORT", d.answer_effort) or d.answer_effort,
            calibration_model=_env("CALIBRATION_MODEL", d.calibration_model) or d.calibration_model,
            digest_effort=_env("DIGEST_EFFORT", d.digest_effort) or d.digest_effort,
            brief_effort=_env("BRIEF_EFFORT", d.brief_effort) or d.brief_effort,
            use_fallbacks=_bool("USE_FALLBACKS", d.use_fallbacks),
            max_agent_turns=_int("MAX_AGENT_TURNS", d.max_agent_turns),
            max_answer_tokens=_int("MAX_ANSWER_TOKENS", d.max_answer_tokens),
            digest_chunk_chars=_int("DIGEST_CHUNK_CHARS", d.digest_chunk_chars),
            max_diff_chars_per_commit=_int("MAX_DIFF_CHARS_PER_COMMIT", d.max_diff_chars_per_commit),
            digest_concurrency=_int("DIGEST_CONCURRENCY", d.digest_concurrency),
            brief_max_chars=_int("BRIEF_MAX_CHARS", d.brief_max_chars),
            poll_interval_seconds=_int("POLL_INTERVAL_SECONDS", d.poll_interval_seconds),
            fetch_min_interval_seconds=_int("FETCH_MIN_INTERVAL_SECONDS", d.fetch_min_interval_seconds),
            auto_calibrate=_bool("AUTO_CALIBRATE", d.auto_calibrate),
            webhook_secret=_env("WEBHOOK_SECRET", "") or "",
            host=_env("HOST", d.host) or d.host,
            port=_int("PORT", d.port),
            password=_env("PASSWORD", "") or "",
            admin_token=_env("ADMIN_TOKEN", "") or "",
        )
