import pytest

from blueprint.settings import DEFAULT_BUDGET_USD, SettingsError, load_settings

API = {"ANTHROPIC_API_KEY": "sk-ant-test"}


class TestAuth:
    def test_api_key_path(self) -> None:
        s = load_settings(API)
        assert s.auth_mode == "api_key"
        assert s.model == "claude-opus-5"
        assert s.max_budget_usd == DEFAULT_BUDGET_USD
        assert not s.web_search_enabled

    def test_no_credentials_is_a_plain_error(self) -> None:
        with pytest.raises(SettingsError, match="set ANTHROPIC_API_KEY"):
            load_settings({})

    def test_blank_key_counts_as_missing(self) -> None:
        with pytest.raises(SettingsError):
            load_settings({"ANTHROPIC_API_KEY": "   "})

    def test_bedrock_requires_region_and_credentials(self) -> None:
        with pytest.raises(SettingsError, match="AWS_REGION"):
            load_settings({"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_PROFILE": "dev"})
        with pytest.raises(SettingsError, match="AWS credentials"):
            load_settings({"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_REGION": "us-east-1"})
        s = load_settings(
            {"CLAUDE_CODE_USE_BEDROCK": "true", "AWS_REGION": "us-east-1", "AWS_PROFILE": "dev"}
        )
        assert s.auth_mode == "bedrock"

    def test_bedrock_takes_precedence_over_api_key(self) -> None:
        s = load_settings(
            {**API, "CLAUDE_CODE_USE_BEDROCK": "1", "AWS_REGION": "x", "AWS_ACCESS_KEY_ID": "y"}
        )
        assert s.auth_mode == "bedrock"


class TestTunables:
    def test_overrides(self) -> None:
        s = load_settings(
            {
                **API,
                "BLUEPRINT_MODEL": "claude-sonnet-5",
                "BLUEPRINT_MAX_BUDGET_USD": "1.5",
                "BLUEPRINT_IDLE_TIMEOUT_S": "30",
            }
        )
        assert s.model == "claude-sonnet-5"
        assert s.max_budget_usd == 1.5
        assert s.idle_timeout_s == 30

    @pytest.mark.parametrize("bad", ["abc", "0", "-1"])
    def test_bad_budget_rejected(self, bad: str) -> None:
        with pytest.raises(SettingsError, match="BLUEPRINT_MAX_BUDGET_USD"):
            load_settings({**API, "BLUEPRINT_MAX_BUDGET_USD": bad})


class TestWebSearch:
    def test_enabled_requires_key(self) -> None:
        with pytest.raises(SettingsError, match="TAVILY_API_KEY"):
            load_settings({**API, "BLUEPRINT_WEB_SEARCH": "1"})

    def test_enabled_with_key(self) -> None:
        s = load_settings({**API, "BLUEPRINT_WEB_SEARCH": "yes", "TAVILY_API_KEY": "tvly-x"})
        assert s.web_search_enabled and s.tavily_api_key == "tvly-x"

    def test_key_present_but_disabled_is_fine(self) -> None:
        s = load_settings({**API, "TAVILY_API_KEY": "tvly-x"})
        assert not s.web_search_enabled


class TestNoSecretLeak:
    def test_to_dict_never_contains_secret_values(self) -> None:
        s = load_settings({**API, "TAVILY_API_KEY": "tvly-secret"})
        flat = str(s.to_dict())
        assert "sk-ant-test" not in flat and "tvly-secret" not in flat
        assert s.to_dict()["tavily_api_key_set"] is True
