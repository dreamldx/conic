import pytest
from pydantic import ValidationError

from conic.config import Config, ConfigError, load_config


def test_load_config_applies_defaults():
    env = {"PROJECT_ROOT": "/tmp/conic", "DISCORD_BOT_TOKEN": "d-token", "OPENROUTER_API_KEY": "or-key"}
    config = load_config(env)
    assert config.discord_bot_token == "d-token"
    assert config.openrouter_api_key == "or-key"
    assert config.openrouter_model == "anthropic/claude-sonnet-4.5"
    assert config.openrouter_provider_blacklist == ""
    assert config.project_name == "Conic"
    assert config.workspace_root.endswith("workspace")
    assert config.duckdb_path.endswith("conic.duckdb")
    assert config.log_level == "INFO"
    assert config.max_steps_per_turn == 25
    assert config.context_token_budget == 50000
    assert config.truncate_keep_last_n == 40
    assert config.bash_timeout == 60.0


def test_load_config_reads_overrides():
    env = {
        "PROJECT_ROOT": "/tmp/conic",
        "DISCORD_BOT_TOKEN": "d-token",
        "OPENROUTER_API_KEY": "or-key",
        "OPENROUTER_MODEL": "openai/gpt-4o",
        "OPENROUTER_PROVIDER_BLACKLIST": "novita,together",
        "MAX_STEPS_PER_TURN": "10",
        "BASH_TIMEOUT": "15",
        "PROJECT_NAME": "MyAgent",
    }
    config = load_config(env)
    assert config.openrouter_model == "openai/gpt-4o"
    assert config.openrouter_provider_blacklist == "novita,together"
    assert config.max_steps_per_turn == 10
    assert config.bash_timeout == 15.0
    assert config.project_name == "MyAgent"


def test_load_config_raises_when_project_root_missing():
    with pytest.raises(ConfigError, match="PROJECT_ROOT"):
        load_config({"DISCORD_BOT_TOKEN": "d-token", "OPENROUTER_API_KEY": "or-key"})


def test_load_config_raises_when_discord_token_missing():
    with pytest.raises(ConfigError, match="DISCORD_BOT_TOKEN"):
        load_config({"PROJECT_ROOT": "/tmp/conic", "OPENROUTER_API_KEY": "or-key"})


def test_load_config_raises_when_openrouter_key_missing():
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        load_config({"PROJECT_ROOT": "/tmp/conic", "DISCORD_BOT_TOKEN": "d-token"})


def test_load_config_raises_on_invalid_field_type():
    with pytest.raises(ConfigError):
        load_config({
            "PROJECT_ROOT": "/tmp/conic",
            "DISCORD_BOT_TOKEN": "d",
            "OPENROUTER_API_KEY": "k",
            "MAX_STEPS_PER_TURN": "not_a_number",
        })


def test_web_tool_settings_default_to_disabled():
    config = load_config(env={
        "PROJECT_ROOT": "/tmp", "DISCORD_BOT_TOKEN": "d", "OPENROUTER_API_KEY": "k",
    })
    assert config.tavily_api_key == ""
    assert config.firecrawl_api_key == ""
    assert config.web_fetch_summary_model == ""
    assert config.web_search_timeout == 30.0
    assert config.web_fetch_timeout == 60.0
    assert config.web_fetch_max_chars == 15000


def test_web_tool_settings_parse_from_env():
    config = load_config(env={
        "PROJECT_ROOT": "/tmp", "DISCORD_BOT_TOKEN": "d", "OPENROUTER_API_KEY": "k",
        "TAVILY_API_KEY": "tv", "FIRECRAWL_API_KEY": "fc",
        "WEB_SEARCH_TIMEOUT": "10", "WEB_FETCH_TIMEOUT": "20",
        "WEB_FETCH_MAX_CHARS": "5000", "WEB_FETCH_SUMMARY_MODEL": "google/gemini-flash",
    })
    assert config.tavily_api_key == "tv"
    assert config.firecrawl_api_key == "fc"
    assert config.web_search_timeout == 10.0
    assert config.web_fetch_timeout == 20.0
    assert config.web_fetch_max_chars == 5000
    assert config.web_fetch_summary_model == "google/gemini-flash"
