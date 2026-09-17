from pathlib import Path

from pydantic import Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(Exception):
    pass


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    project_root: str = Field(alias="PROJECT_ROOT")
    discord_bot_token: str = Field(alias="DISCORD_BOT_TOKEN")
    openrouter_api_key: str = Field(alias="OPENROUTER_API_KEY")
    openrouter_model: str = Field(default="anthropic/claude-sonnet-4.5", alias="OPENROUTER_MODEL")
    openrouter_provider_blacklist: str = Field(default="", alias="OPENROUTER_PROVIDER_BLACKLIST")
    workspace_root: str = Field(default="", alias="WORKSPACE_ROOT")
    duckdb_path: str = Field(default="", alias="DUCKDB_PATH")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    max_steps_per_turn: int = Field(default=25, ge=1, alias="MAX_STEPS_PER_TURN")
    context_token_budget: int = Field(default=50000, ge=1, alias="CONTEXT_TOKEN_BUDGET")
    truncate_keep_last_n: int = Field(default=40, ge=1, alias="TRUNCATE_KEEP_LAST_N")
    bash_timeout: float = Field(default=60.0, ge=1, alias="BASH_TIMEOUT")
    app_name_holder: dict = Field(default_factory=lambda: {"name": None})

    @model_validator(mode="after")
    def _resolve_paths(self):
        root = Path(self.project_root)
        if not self.workspace_root:
            object.__setattr__(self, "workspace_root", str(root / "workspace"))
        if not self.duckdb_path:
            object.__setattr__(self, "duckdb_path", str(root / "data" / "conic.duckdb"))
        return self


def _raise_config_error(exc: ValidationError) -> None:
    missing = [e["loc"][0] for e in exc.errors() if e["type"] == "missing"]
    if missing:
        raise ConfigError(f"missing required environment variable: {missing[0]}") from exc
    raise ConfigError(str(exc)) from exc


def load_config(env: dict[str, str] | None = None) -> Config:
    try:
        if env is not None:
            return Config(_env_file=None, **env)
        return Config()
    except ValidationError as exc:
        _raise_config_error(exc)
