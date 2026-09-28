"""Validated runtime configuration, read once from the environment.

Every secret and tunable enters the program here and nowhere else. Missing required values
fail at startup with a plain message instead of surfacing later as an opaque subprocess
error. Secret *values* are never logged, printed, or included in ``to_dict``.

Two auth paths (mutually exclusive by precedence, matching the SDK):
    - Bedrock: ``CLAUDE_CODE_USE_BEDROCK=1`` plus AWS credentials and region.
    - Direct API: ``ANTHROPIC_API_KEY``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from blueprint.builder import DEFAULT_BUILD_BUDGET_USD
from blueprint.completeness import DEFAULT_CHECKER_MODEL
from blueprint.discovery import DEFAULT_MODEL
from blueprint.orchestrator import DEFAULT_IDLE_TIMEOUT_S

DEFAULT_BUDGET_USD: Final = 3.0
DEFAULT_SESSION_IDLE_S: Final = 900.0
DEFAULT_CORS_ORIGINS: Final = "http://localhost:3000"


class SettingsError(ValueError):
    """Configuration is missing or invalid. The message says what to set."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the application reads from the environment, validated."""

    auth_mode: str
    """``"bedrock"`` or ``"api_key"``."""
    model: str
    classifier_model: str
    max_budget_usd: float
    idle_timeout_s: float
    web_search_enabled: bool
    tavily_api_key: str | None
    sop_grounding: bool
    completeness_check: bool
    checker_model: str
    build_budget_usd: float
    admin_token: str | None
    """Bearer token for reviewer/admin/build endpoints; ``None`` leaves them open (local dev)."""
    session_idle_s: float
    cors_origins: tuple[str, ...]
    store_path: str
    """SQLite file for runs and the audit trail; ``""`` keeps the JSON files."""
    redact_stored_text: bool
    """Strip identifiers from free text on the way into the store. See redaction.py."""
    retention_days: int
    """Purge runs older than this. 0 keeps everything, and is the default on purpose."""

    def to_dict(self) -> dict[str, Any]:
        """Loggable view: presence of secrets, never their values."""
        return {
            "auth_mode": self.auth_mode,
            "model": self.model,
            "classifier_model": self.classifier_model,
            "max_budget_usd": self.max_budget_usd,
            "idle_timeout_s": self.idle_timeout_s,
            "web_search_enabled": self.web_search_enabled,
            "tavily_api_key_set": self.tavily_api_key is not None,
            "sop_grounding": self.sop_grounding,
            "completeness_check": self.completeness_check,
            "checker_model": self.checker_model,
            "build_budget_usd": self.build_budget_usd,
            "admin_token_set": self.admin_token is not None,
            "session_idle_s": self.session_idle_s,
            "cors_origins": list(self.cors_origins),
            "store": self.store_path or "files",
            "redact_stored_text": self.redact_stored_text,
            "retention_days": self.retention_days,
        }


def _flag(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(env: Mapping[str, str], name: str, default: int, *, minimum: int = 0) -> int:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SettingsError(f"{name} must be a whole number, got {raw!r}") from exc
    if value < minimum:
        raise SettingsError(f"{name} must be at least {minimum}, got {value}")
    return value


def _float(env: Mapping[str, str], name: str, default: float, *, minimum: float) -> float:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise SettingsError(f"{name} must be a number, got {raw!r}") from exc
    if value < minimum:
        raise SettingsError(f"{name} must be >= {minimum}, got {value}")
    return value


def _secret(env: Mapping[str, str], name: str) -> str | None:
    raw = env.get(name)
    return raw.strip() if raw and raw.strip() else None


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Read and validate configuration.

    Args:
        env: Mapping to read from; defaults to ``os.environ``. Injected for tests.

    Raises:
        SettingsError: with a message naming the variable to set.
    """
    env = os.environ if env is None else env

    if _flag(env, "CLAUDE_CODE_USE_BEDROCK"):
        auth_mode = "bedrock"
        if not _secret(env, "AWS_REGION") and not _secret(env, "AWS_DEFAULT_REGION"):
            raise SettingsError("CLAUDE_CODE_USE_BEDROCK=1 requires AWS_REGION")
        has_creds = any(
            _secret(env, k)
            for k in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_BEARER_TOKEN_BEDROCK")
        )
        if not has_creds:
            raise SettingsError(
                "CLAUDE_CODE_USE_BEDROCK=1 requires AWS credentials: set AWS_PROFILE, "
                "AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, or AWS_BEARER_TOKEN_BEDROCK"
            )
    elif _secret(env, "ANTHROPIC_API_KEY"):
        auth_mode = "api_key"
    else:
        raise SettingsError(
            "no model credentials: set ANTHROPIC_API_KEY, or CLAUDE_CODE_USE_BEDROCK=1 with "
            "AWS credentials (see .env.example)"
        )

    web_search_enabled = _flag(env, "BLUEPRINT_WEB_SEARCH")
    tavily_api_key = _secret(env, "TAVILY_API_KEY")
    if web_search_enabled and tavily_api_key is None:
        raise SettingsError("BLUEPRINT_WEB_SEARCH=1 requires TAVILY_API_KEY")

    return Settings(
        auth_mode=auth_mode,
        model=env.get("BLUEPRINT_MODEL", "").strip() or DEFAULT_MODEL,
        classifier_model=env.get("BLUEPRINT_CLASSIFIER_MODEL", "").strip() or DEFAULT_MODEL,
        max_budget_usd=_float(env, "BLUEPRINT_MAX_BUDGET_USD", DEFAULT_BUDGET_USD, minimum=0.01),
        idle_timeout_s=_float(env, "BLUEPRINT_IDLE_TIMEOUT_S", DEFAULT_IDLE_TIMEOUT_S, minimum=1),
        web_search_enabled=web_search_enabled,
        tavily_api_key=tavily_api_key,
        sop_grounding=_flag(env, "BLUEPRINT_SOP_GROUNDING", default=True),
        completeness_check=_flag(env, "BLUEPRINT_COMPLETENESS_CHECK", default=True),
        checker_model=env.get("BLUEPRINT_CHECKER_MODEL", "").strip() or DEFAULT_CHECKER_MODEL,
        build_budget_usd=_float(
            env, "BLUEPRINT_BUILD_BUDGET_USD", DEFAULT_BUILD_BUDGET_USD, minimum=0.1
        ),
        admin_token=_secret(env, "BLUEPRINT_ADMIN_TOKEN"),
        session_idle_s=_float(env, "BLUEPRINT_SESSION_IDLE_S", DEFAULT_SESSION_IDLE_S, minimum=30),
        cors_origins=tuple(
            o.strip()
            for o in env.get("BLUEPRINT_CORS_ORIGINS", DEFAULT_CORS_ORIGINS).split(",")
            if o.strip()
        ),
        store_path=env.get("BLUEPRINT_STORE_PATH", "").strip(),
        redact_stored_text=_flag(env, "BLUEPRINT_REDACT", default=True),
        retention_days=_int(env, "BLUEPRINT_RETENTION_DAYS", default=0),
    )
