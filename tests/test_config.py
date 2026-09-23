import pytest

from conic.config import ConfigError, load_config


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


def test_load_config_reads_overrides():
    env = {
        "PROJECT_ROOT": "/tmp/conic",
        "DISCORD_BOT_TOKEN": "d-token",
        "OPENROUTER_API_KEY": "or-key",
        "OPENROUTER_MODEL": "openai/gpt-4o",
        "OPENROUTER_PROVIDER_BLACKLIST": "novita,together",
        "PROJECT_NAME": "MyAgent",
    }
    config = load_config(env)
    assert config.openrouter_model == "openai/gpt-4o"
    assert config.openrouter_provider_blacklist == "novita,together"
    assert config.project_name == "MyAgent"


def test_load_config_resolves_default_plugins_config_path():
    config = load_config({"PROJECT_ROOT": "/tmp/conic", "DISCORD_BOT_TOKEN": "d", "OPENROUTER_API_KEY": "k"})
    assert config.plugins_config_path.endswith("plugins.yaml")
    assert "conic" in config.plugins_config_path


def test_load_config_reads_plugins_config_path_override():
    config = load_config({
        "PROJECT_ROOT": "/tmp/conic", "DISCORD_BOT_TOKEN": "d", "OPENROUTER_API_KEY": "k",
        "PLUGINS_CONFIG_PATH": "/custom/plugins.yaml",
    })
    assert config.plugins_config_path == "/custom/plugins.yaml"


def test_load_config_raises_when_project_root_missing(monkeypatch):
    # pydantic-settings falls back to the real OS environment for any key not
    # in the dict passed to load_config -- if PROJECT_ROOT happens to be set
    # in the ambient environment (e.g. exported by a shell profile or CI),
    # that value would satisfy the field and this test would never raise.
    monkeypatch.delenv("PROJECT_ROOT", raising=False)
    with pytest.raises(ConfigError, match="PROJECT_ROOT"):
        load_config({"DISCORD_BOT_TOKEN": "d-token", "OPENROUTER_API_KEY": "or-key"})


def test_load_config_raises_when_discord_token_missing(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    with pytest.raises(ConfigError, match="DISCORD_BOT_TOKEN"):
        load_config({"PROJECT_ROOT": "/tmp/conic", "OPENROUTER_API_KEY": "or-key"})


def test_load_config_raises_when_openrouter_key_missing(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        load_config({"PROJECT_ROOT": "/tmp/conic", "DISCORD_BOT_TOKEN": "d-token"})


def test_web_tool_api_keys_default_to_disabled():
    config = load_config(env={
        "PROJECT_ROOT": "/tmp", "DISCORD_BOT_TOKEN": "d", "OPENROUTER_API_KEY": "k",
    })
    assert config.tavily_api_key == ""
    assert config.firecrawl_api_key == ""


def test_web_tool_api_keys_parse_from_env():
    config = load_config(env={
        "PROJECT_ROOT": "/tmp", "DISCORD_BOT_TOKEN": "d", "OPENROUTER_API_KEY": "k",
        "TAVILY_API_KEY": "tv", "FIRECRAWL_API_KEY": "fc",
    })
    assert config.tavily_api_key == "tv"
    assert config.firecrawl_api_key == "fc"
