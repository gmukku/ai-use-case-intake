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

from blueprint.completeness import DEFAULT_CHECKER_MODEL
from blueprint.discovery import DEFAULT_MODEL
from blueprint.orchestrator import DEFAULT_IDLE_TIMEOUT_S

DEFAULT_BUDGET_USD: Final = 3.0


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
        }


def _flag(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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
    )
