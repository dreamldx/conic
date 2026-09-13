import os
from dataclasses import dataclass


class ConfigError(Exception):
    pass


@dataclass
class Config:
    discord_bot_token: str
    openrouter_api_key: str
    openrouter_model: str
    workspace_root: str
    duckdb_path: str
    max_steps_per_turn: int
    context_token_budget: int
    truncate_keep_last_n: int


def load_config(env: dict[str, str] | None = None) -> Config:
    env = env if env is not None else os.environ

    def required(key: str) -> str:
        value = env.get(key)
        if not value:
            raise ConfigError(f"missing required environment variable: {key}")
        return value

    return Config(
        discord_bot_token=required("DISCORD_BOT_TOKEN"),
        openrouter_api_key=required("OPENROUTER_API_KEY"),
        openrouter_model=env.get("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.5"),
        workspace_root=env.get("WORKSPACE_ROOT", "./workspace"),
        duckdb_path=env.get("DUCKDB_PATH", "./data/conic.duckdb"),
        max_steps_per_turn=int(env.get("MAX_STEPS_PER_TURN", "25")),
        context_token_budget=int(env.get("CONTEXT_TOKEN_BUDGET", "50000")),
        truncate_keep_last_n=int(env.get("TRUNCATE_KEEP_LAST_N", "40")),
    )
