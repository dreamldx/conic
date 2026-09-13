import pytest

from conic.config import Config, ConfigError, load_config


def test_load_config_applies_defaults():
    env = {"DISCORD_BOT_TOKEN": "d-token", "OPENROUTER_API_KEY": "or-key"}
    config = load_config(env)
    assert config == Config(
        discord_bot_token="d-token",
        openrouter_api_key="or-key",
        openrouter_model="anthropic/claude-sonnet-4.5",
        workspace_root="./workspace",
        duckdb_path="./data/conic.duckdb",
        max_steps_per_turn=25,
        context_token_budget=50000,
        truncate_keep_last_n=40,
    )


def test_load_config_reads_overrides():
    env = {
        "DISCORD_BOT_TOKEN": "d-token",
        "OPENROUTER_API_KEY": "or-key",
        "OPENROUTER_MODEL": "openai/gpt-4o",
        "MAX_STEPS_PER_TURN": "10",
    }
    config = load_config(env)
    assert config.openrouter_model == "openai/gpt-4o"
    assert config.max_steps_per_turn == 10


def test_load_config_raises_when_discord_token_missing():
    with pytest.raises(ConfigError, match="DISCORD_BOT_TOKEN"):
        load_config({"OPENROUTER_API_KEY": "or-key"})


def test_load_config_raises_when_openrouter_key_missing():
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        load_config({"DISCORD_BOT_TOKEN": "d-token"})
