"""Runtime settings, read from environment variables (never from code or the database).

Secrets (ANTHROPIC_API_KEY, COMPOSIO_API_KEY, OVERLORD_ADMIN_TOKEN) are only ever read from the environment;
they are never written to the database, logs or API responses.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("OVERLORD_DATA", "./overlord-data")).resolve())
    whisper_model: str = field(default_factory=lambda: os.environ.get("OVERLORD_WHISPER_MODEL", "small"))
    whisper_compute: str = field(default_factory=lambda: os.environ.get("OVERLORD_WHISPER_COMPUTE", "int8"))
    llm_model: str = field(default_factory=lambda: os.environ.get("OVERLORD_LLM_MODEL", "claude-opus-5-5"))
    llm_enabled: bool = field(default_factory=lambda: _bool("OVERLORD_LLM", True))  # needs an Anthropic credential
    composio_user_id: str | None = field(default_factory=lambda: os.environ.get("COMPOSIO_USER_ID"))
    composio_account_id: str | None = field(default_factory=lambda: os.environ.get("COMPOSIO_YOUTUBE_ACCOUNT_ID"))
    max_workers: int = field(default_factory=lambda: int(os.environ.get("OVERLORD_WORKERS", "1")))
    job_timeout_s: int = field(default_factory=lambda: int(os.environ.get("OVERLORD_JOB_TIMEOUT", str(6 * 3600))))
    max_attempts: int = field(default_factory=lambda: int(os.environ.get("OVERLORD_MAX_ATTEMPTS", "3")))
    admin_token: str | None = field(default_factory=lambda: os.environ.get("OVERLORD_ADMIN_TOKEN"))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "overlord.db"

    def job_dir(self, job_id: str) -> Path:
        p = self.data_dir / "jobs" / job_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def has_llm(self) -> bool:
        return self.llm_enabled and bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))

    def has_composio(self) -> bool:
        return bool(os.environ.get("COMPOSIO_API_KEY"))


def settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s
