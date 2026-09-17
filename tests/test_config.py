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
    assert config.app_name_holder == {"name": None}
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
    }
    config = load_config(env)
    assert config.openrouter_model == "openai/gpt-4o"
    assert config.openrouter_provider_blacklist == "novita,together"
    assert config.max_steps_per_turn == 10
    assert config.bash_timeout == 15.0


def test_app_name_holder_is_a_fresh_dict_per_config_instance():
    env = {"PROJECT_ROOT": "/tmp/conic", "DISCORD_BOT_TOKEN": "d-token", "OPENROUTER_API_KEY": "or-key"}
    first = load_config(env)
    second = load_config(env)

    assert first.app_name_holder is not second.app_name_holder

    first.app_name_holder["name"] = "Conic"
    assert second.app_name_holder["name"] is None


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
