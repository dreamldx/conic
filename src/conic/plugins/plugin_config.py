from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError, field_validator


class PluginConfigError(Exception):
    pass


class PluginSpec(BaseModel):
    name: str
    params: dict[str, Any] = {}


def _normalize_entry(entry: Any) -> PluginSpec:
    if isinstance(entry, PluginSpec):
        return entry
    if isinstance(entry, str):
        return PluginSpec(name=entry, params={})
    if isinstance(entry, dict) and len(entry) == 1:
        (name, params), = entry.items()
        return PluginSpec(name=name, params=params or {})
    raise PluginConfigError(f"invalid plugin entry: {entry!r}")


class PluginsConfig(BaseModel):
    tools: list[PluginSpec] = []
    context: list[PluginSpec] = []
    policy: list[PluginSpec] = []
    summarizer: str = "default"
    backend: str = "openrouter"

    @field_validator("tools", "context", "policy", mode="before")
    @classmethod
    def _normalize_list(cls, value: Any) -> Any:
        if value is None:
            return []
        return [_normalize_entry(entry) for entry in value]


def load_plugins_config(path: str | Path) -> PluginsConfig:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise PluginConfigError(f"cannot read plugins config at {path}: {exc}") from exc

    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise PluginConfigError(f"invalid YAML in {path}: {exc}") from exc

    try:
        return PluginsConfig.model_validate(raw)
    except (ValidationError, PluginConfigError) as exc:
        raise PluginConfigError(f"invalid plugins config in {path}: {exc}") from exc
