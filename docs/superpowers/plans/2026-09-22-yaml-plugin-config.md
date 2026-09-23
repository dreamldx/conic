# YAML-Driven Plugin Config Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `registry.py`'s hardcoded Python plugin wiring with a declarative `plugins.yaml` that `build_plugin_set()` parses into the exact same `PluginSet` shape used today, and move per-session plugin instantiation out of `core/manager.py` into `registry.py` via a new `PluginSet.instantiate_session` closure.

**Architecture:** A new `conic/plugins/plugin_config.py` module defines a pydantic schema (`PluginSpec`, `PluginsConfig`) and `load_plugins_config()` to parse/validate a YAML file. `registry.py` gets one `name -> builder` dict per plugin category (tools/context sections/context/policy/summarizer/backend); `build_plugin_set()` loads the YAML, looks up each declared plugin by name, and calls its builder. Unknown names or missing API keys for explicitly-declared optional tools raise `PluginConfigError` at startup (fail fast, no silent skip). `PluginSet` keeps its existing six fields unchanged (tests depend on inspecting them directly) and gains a seventh, `instantiate_session`, built as a closure inside `build_plugin_set()` — this avoids a circular import (`registry.py` already imports `PluginSet` from `core/manager.py`; `manager.py` must not import back from `registry.py`).

**Tech Stack:** Python 3.12, pydantic (already a transitive dep via pydantic-settings), PyYAML (already a direct dep per `pyproject.toml`).

**Spec:** `docs/superpowers/specs/2026-09-22-yaml-plugin-config-design.md` (read this first — it has the full YAML schema, error-handling policy, and rationale; this plan implements it task by task).

## Global Constraints

- No dual-path fallback: `build_plugin_set()` has exactly one code path (YAML), never falls back to old hardcoded wiring.
- Fail fast: unknown plugin names and missing API keys for explicitly-declared optional tools raise `PluginConfigError` at startup, never a silent skip or partial load.
- `PluginSet`'s existing six fields (`tool_classes`, `backend`, `context_plugins`, `policy_plugins`, `summarizer`, `loop_factory`) keep their exact current types — do not merge them into the new seventh field.
- All 8 removed `Config` fields (`bash_timeout`, `max_steps_per_turn`, `context_token_budget`, `truncate_keep_last_n`, `web_search_timeout`, `web_fetch_timeout`, `web_fetch_max_chars`, `web_fetch_summary_model`) are consumed *only* in `src/conic/plugins/registry.py` and defined *only* in `src/conic/config.py` — verified via repo-wide grep during spec review, no other call sites.
- Run `uv run pytest -q` and `uv run ruff check src tests scripts` after every task; both must be clean before committing.
- Comments in code: English only (per `CONTRIB.md`).

---

## File Structure

- Create: `src/conic/plugins/plugin_config.py` — YAML schema (`PluginSpec`, `PluginsConfig`, `PluginConfigError`) and `load_plugins_config()`.
- Create: `plugins.yaml` (repo root) — default plugin config, a faithful transcription of today's hardcoded wiring.
- Modify: `src/conic/config.py` — remove 8 fields, add `plugins_config_path`.
- Modify: `src/conic/plugins/registry.py` — full rewrite: builder tables, `BuildContext`, YAML-driven `build_plugin_set()`, `instantiate_session` closure.
- Modify: `src/conic/core/manager.py` — `PluginSet` gains `instantiate_session` field; `PluginManager.start_session()` shrinks to call it.
- Create: `tests/plugins/test_plugin_config.py`
- Modify: `tests/plugins/test_registry.py` — rewritten around YAML input.
- Modify: `tests/test_config.py` — remove assertions on deleted fields, add `plugins_config_path` coverage.
- Modify: `tests/core/test_manager.py` — fake `PluginSet` gains a fake `instantiate_session`.
- Modify: `README.md`, `CONTRIB.md` — document `plugins.yaml`.

---

### Task 1: Plugin config schema module

**Files:**
- Create: `src/conic/plugins/plugin_config.py`
- Test: `tests/plugins/test_plugin_config.py`

**Interfaces:**
- Produces: `PluginConfigError(Exception)`; `PluginSpec(BaseModel)` with fields `name: str`, `params: dict[str, Any] = {}`; `PluginsConfig(BaseModel)` with fields `tools: list[PluginSpec] = []`, `context: list[PluginSpec] = []`, `policy: list[PluginSpec] = []`, `summarizer: str = "default"`, `backend: str = "openrouter"`; `load_plugins_config(path: str | Path) -> PluginsConfig`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/plugins/test_plugin_config.py
import pytest

from conic.plugins.plugin_config import PluginConfigError, PluginsConfig, load_plugins_config


def test_parses_bare_string_and_single_key_mapping_entries(tmp_path):
    yaml_text = """
tools:
  - read_file
  - bash: {timeout: 60}
context: []
policy:
  - permission
  - step_limit: {max_steps: 7}
summarizer: default
backend: openrouter
"""
    path = tmp_path / "plugins.yaml"
    path.write_text(yaml_text, encoding="utf-8")

    cfg = load_plugins_config(path)

    assert cfg.tools[0].name == "read_file"
    assert cfg.tools[0].params == {}
    assert cfg.tools[1].name == "bash"
    assert cfg.tools[1].params == {"timeout": 60}
    assert cfg.policy[0].name == "permission"
    assert cfg.policy[1].name == "step_limit"
    assert cfg.policy[1].params == {"max_steps": 7}
    assert cfg.summarizer == "default"
    assert cfg.backend == "openrouter"


def test_defaults_when_sections_omitted(tmp_path):
    path = tmp_path / "plugins.yaml"
    path.write_text("tools: []\n", encoding="utf-8")

    cfg = load_plugins_config(path)

    assert cfg.tools == []
    assert cfg.context == []
    assert cfg.policy == []
    assert cfg.summarizer == "default"
    assert cfg.backend == "openrouter"


def test_invalid_entry_raises_plugin_config_error(tmp_path):
    path = tmp_path / "plugins.yaml"
    path.write_text("tools:\n  - [not, a, valid, entry]\n", encoding="utf-8")

    with pytest.raises(PluginConfigError):
        load_plugins_config(path)


def test_multi_key_mapping_entry_raises_plugin_config_error(tmp_path):
    path = tmp_path / "plugins.yaml"
    path.write_text("tools:\n  - bash: {timeout: 60}\n    read_file: {}\n", encoding="utf-8")

    with pytest.raises(PluginConfigError):
        load_plugins_config(path)


def test_invalid_yaml_syntax_raises_plugin_config_error(tmp_path):
    path = tmp_path / "plugins.yaml"
    path.write_text("tools: [unclosed\n", encoding="utf-8")

    with pytest.raises(PluginConfigError):
        load_plugins_config(path)


def test_missing_file_raises_plugin_config_error(tmp_path):
    with pytest.raises(PluginConfigError):
        load_plugins_config(tmp_path / "does-not-exist.yaml")


def test_plugins_config_can_be_built_directly_from_a_dict():
    cfg = PluginsConfig.model_validate({
        "tools": ["read_file", {"bash": {"timeout": 60}}],
    })
    assert cfg.tools[0].name == "read_file"
    assert cfg.tools[1].name == "bash"
    assert cfg.tools[1].params == {"timeout": 60}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/plugins/test_plugin_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'conic.plugins.plugin_config'`

- [ ] **Step 3: Write the implementation**

```python
# src/conic/plugins/plugin_config.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/plugins/test_plugin_config.py -v`
Expected: all PASS

- [ ] **Step 5: Lint and commit**

Run: `uv run ruff check src/conic/plugins/plugin_config.py tests/plugins/test_plugin_config.py`

```bash
git add src/conic/plugins/plugin_config.py tests/plugins/test_plugin_config.py
git commit -m "feat(plugins): add plugin_config.py YAML schema and loader"
```

---

### Task 2: Config changes

**Files:**
- Modify: `src/conic/config.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `Config.plugins_config_path: str` (resolved default `{project_root}/plugins.yaml`). Removes `Config.bash_timeout`, `Config.max_steps_per_turn`, `Config.context_token_budget`, `Config.truncate_keep_last_n`, `Config.web_search_timeout`, `Config.web_fetch_timeout`, `Config.web_fetch_max_chars`, `Config.web_fetch_summary_model`.

- [ ] **Step 1: Update `tests/test_config.py`**

Remove the 8 deleted fields from `test_load_config_applies_defaults` and `test_load_config_reads_overrides`, and add path-resolution coverage:

```python
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
    assert config.plugins_config_path.startswith("/tmp/conic") or "conic" in config.plugins_config_path


def test_load_config_reads_plugins_config_path_override():
    config = load_config({
        "PROJECT_ROOT": "/tmp/conic", "DISCORD_BOT_TOKEN": "d", "OPENROUTER_API_KEY": "k",
        "PLUGINS_CONFIG_PATH": "/custom/plugins.yaml",
    })
    assert config.plugins_config_path == "/custom/plugins.yaml"
```

Also delete `MAX_STEPS_PER_TURN`/`CONTEXT_TOKEN_BUDGET`/`TRUNCATE_KEEP_LAST_N`/`BASH_TIMEOUT` from `test_load_config_raises_on_invalid_field_type`'s env dict (keep the test itself — it only needs one invalid field, `MAX_STEPS_PER_TURN` was arbitrary; swap it for `WORKSPACE_ROOT` isn't invalid-typeable, so just drop that test's `MAX_STEPS_PER_TURN` line and instead pass an invalid type for a field that still exists, e.g. leave `PROJECT_NAME` as-is and use `OPENROUTER_PROVIDER_BLACKLIST` — actually simplest: this test needs *some* still-existing field with a type it can violate. `openrouter_provider_blacklist` is `str`, hard to violate via env (env vars are always strings). Instead assert on a still-existing numeric-free path: skip this test's need for a bad-int field by deleting it, since no remaining field takes a non-string primitive that env-var coercion can trivially violate except the ones just removed).

```python
def test_load_config_raises_on_invalid_field_type():
    with pytest.raises(ConfigError):
        load_config({
            "PROJECT_ROOT": "/tmp/conic",
            "DISCORD_BOT_TOKEN": "d",
            "OPENROUTER_API_KEY": "k",
            "WEB_FETCH_MAX_CHARS_TYPO_DOES_NOT_MATTER": "ignored",
        })
```

Wait — `extra="ignore"` on `Config.model_config` means unknown keys are silently ignored, so an unknown-key test proves nothing. Replace this test instead with one that violates `bash_timeout`'s replacement: there is no numeric `Config` field left to violate via env string coercion after this task except none exist. **Delete `test_load_config_raises_on_invalid_field_type` entirely** — it tested behavior (`MAX_STEPS_PER_TURN="not_a_number"`) that no longer applies to any `Config` field; there is nothing left in `Config` that takes a non-`str` scalar from `.env`, so this class of failure can't happen anymore. Pydantic's own type-validation machinery is exercised plenty by `PluginsConfig` in Task 1's tests instead.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL (old fields still referenced nowhere now, but `plugins_config_path` tests fail with `AttributeError` / `ValidationError` — `PLUGINS_CONFIG_PATH` alias doesn't exist yet)

- [ ] **Step 3: Edit `src/conic/config.py`**

```python
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
    plugins_config_path: str = Field(default="", alias="PLUGINS_CONFIG_PATH")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    project_name: str = Field(default="Conic", alias="PROJECT_NAME")
    tavily_api_key: str = Field(default="", alias="TAVILY_API_KEY")
    firecrawl_api_key: str = Field(default="", alias="FIRECRAWL_API_KEY")

    @model_validator(mode="after")
    def _resolve_paths(self):
        root = Path(self.project_root)
        if not self.workspace_root:
            object.__setattr__(self, "workspace_root", str(root / "workspace"))
        if not self.duckdb_path:
            object.__setattr__(self, "duckdb_path", str(root / "data" / "conic.duckdb"))
        if not self.plugins_config_path:
            object.__setattr__(self, "plugins_config_path", str(root / "plugins.yaml"))
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: all PASS

Note: this will also break `tests/plugins/test_registry.py` and `tests/test_main.py` (they reference the removed `Config` fields and `build_plugin_set` still uses the old hardcoded implementation) — that's expected and fixed in Tasks 6–7. Don't chase those failures yet; just confirm `test_config.py` itself is green.

- [ ] **Step 5: Lint and commit**

Run: `uv run ruff check src/conic/config.py tests/test_config.py`

```bash
git add src/conic/config.py tests/test_config.py
git commit -m "feat(config): replace 8 plugin-param fields with plugins_config_path"
```

---

### Task 3: Default `plugins.yaml`

**Files:**
- Create: `plugins.yaml` (repo root)
- Test: `tests/plugins/test_plugin_config.py` (add one test)

**Interfaces:**
- Consumes: `load_plugins_config` from Task 1.
- Produces: a `plugins.yaml` file at the repo root that Task 6/7's rewritten `build_plugin_set()` will read by default.

- [ ] **Step 1: Write the failing test**

Append to `tests/plugins/test_plugin_config.py`:

```python
from pathlib import Path


def test_repo_default_plugins_yaml_parses_successfully():
    repo_root = Path(__file__).resolve().parents[2]
    cfg = load_plugins_config(repo_root / "plugins.yaml")

    tool_names = [spec.name for spec in cfg.tools]
    assert tool_names == [
        "bash", "read_file", "write_file", "edit_file", "list_skills", "load_skill",
        "web_search", "web_fetch",
    ]
    context_names = [spec.name for spec in cfg.context]
    assert context_names == ["turn_variables", "system_prompt", "truncator", "token_budget", "extra_prompt"]
    policy_names = [spec.name for spec in cfg.policy]
    assert policy_names == ["permission", "step_limit"]
    assert cfg.summarizer == "default"
    assert cfg.backend == "openrouter"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/plugins/test_plugin_config.py::test_repo_default_plugins_yaml_parses_successfully -v`
Expected: FAIL (`plugins.yaml` doesn't exist yet)

- [ ] **Step 3: Write `plugins.yaml`**

```yaml
tools:
  - bash: {timeout: 60}
  - read_file
  - write_file
  - edit_file
  - list_skills
  - load_skill
  - web_search: {timeout: 30}
  - web_fetch: {timeout: 60, max_chars: 15000, summary_model: ""}

context:
  - turn_variables
  - system_prompt:
      sections: [identity, tooling, skills, workspace, runtime, execution]
  - truncator: {keep_last_n: 40}
  - token_budget: {budget_tokens: 50000}
  - extra_prompt:
      sections: [dynamic_state]

policy:
  - permission
  - step_limit: {max_steps: 25}

summarizer: default
backend: openrouter
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/plugins/test_plugin_config.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add plugins.yaml tests/plugins/test_plugin_config.py
git commit -m "feat: add default plugins.yaml"
```

---

### Task 4: `registry.py` — `BuildContext` and tool builders

**Files:**
- Modify: `src/conic/plugins/registry.py`
- Modify: `tests/plugins/test_registry.py` (add new tests alongside the existing, still-passing-against-old-code tests; do not delete old tests yet — that happens in Task 7)

**Interfaces:**
- Consumes: `PluginSpec` from Task 1 (`conic.plugins.plugin_config`).
- Produces: `BuildContext` dataclass (fields `shared_client`, `prompts`); `TOOL_BUILDERS: dict[str, Callable[[Config, dict, BuildContext], type]]`; `_build_tool_classes(specs: list[PluginSpec], config: Config, ctx: BuildContext) -> tuple[type, ...]`. These are additive — `build_plugin_set()` is not yet rewired to use them (Task 7 does that), so the module must still define the *old* `build_plugin_set()` unchanged for now.

- [ ] **Step 1: Write the failing tests**

Add to `tests/plugins/test_registry.py` (new imports at top, new tests at bottom — don't touch existing tests in this task):

```python
from conic.plugins.plugin_config import PluginConfigError, PluginSpec
from conic.plugins.registry import TOOL_BUILDERS, BuildContext, _build_tool_classes


def make_build_context(config):
    from openai import AsyncOpenAI
    client = AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=config.openrouter_api_key)
    return BuildContext(shared_client=client, prompts={})


def test_build_tool_classes_wires_bash_with_yaml_timeout():
    config = make_config()
    ctx = make_build_context(config)
    classes = _build_tool_classes([PluginSpec(name="bash", params={"timeout": 99})], config, ctx)
    assert len(classes) == 1
    assert issubclass(classes[0], BashToolPlugin)
    tool = classes[0](workspace_dir="/tmp/ws")
    assert tool._timeout == 99


def test_build_tool_classes_bash_defaults_timeout_to_60_when_omitted():
    config = make_config()
    ctx = make_build_context(config)
    classes = _build_tool_classes([PluginSpec(name="bash", params={})], config, ctx)
    tool = classes[0](workspace_dir="/tmp/ws")
    assert tool._timeout == 60.0


def test_build_tool_classes_wires_simple_tools_unmodified():
    config = make_config()
    ctx = make_build_context(config)
    classes = _build_tool_classes(
        [PluginSpec(name="read_file"), PluginSpec(name="write_file"), PluginSpec(name="edit_file")],
        config, ctx,
    )
    assert classes == (ReadFileToolPlugin, WriteFileToolPlugin, EditFileToolPlugin)


def test_build_tool_classes_wires_skills_tools_with_project_root():
    config = make_config()
    ctx = make_build_context(config)
    classes = _build_tool_classes(
        [PluginSpec(name="list_skills"), PluginSpec(name="load_skill")], config, ctx,
    )
    list_tool = classes[0](workspace_dir="/tmp/ws")
    load_tool = classes[1](workspace_dir="/tmp/ws")
    assert list_tool._project_root == "/tmp"
    assert load_tool._project_root == "/tmp"


def test_build_tool_classes_unknown_tool_raises_plugin_config_error():
    config = make_config()
    ctx = make_build_context(config)
    with pytest.raises(PluginConfigError, match="unknown tool"):
        _build_tool_classes([PluginSpec(name="does_not_exist")], config, ctx)


def test_build_tool_classes_web_search_without_api_key_raises():
    config = make_config()
    ctx = make_build_context(config)
    with pytest.raises(PluginConfigError, match="TAVILY_API_KEY"):
        _build_tool_classes([PluginSpec(name="web_search")], config, ctx)


def test_build_tool_classes_web_search_with_api_key_wires_timeout():
    config = make_config_with(TAVILY_API_KEY="tv", **{})
    ctx = make_build_context(config)
    classes = _build_tool_classes([PluginSpec(name="web_search", params={"timeout": 11})], config, ctx)
    tool = classes[0](workspace_dir="/tmp/ws")
    assert tool._api_key == "tv"
    assert tool._timeout == 11


def test_build_tool_classes_web_fetch_without_api_key_raises():
    config = make_config()
    ctx = make_build_context(config)
    with pytest.raises(PluginConfigError, match="FIRECRAWL_API_KEY"):
        _build_tool_classes([PluginSpec(name="web_fetch")], config, ctx)


def test_build_tool_classes_web_fetch_with_api_key_and_no_summary_model():
    config = make_config_with(FIRECRAWL_API_KEY="fc")
    ctx = make_build_context(config)
    classes = _build_tool_classes([PluginSpec(name="web_fetch", params={"max_chars": 5000})], config, ctx)
    assert classes[0].schema == build_web_fetch_schema(include_prompt=False)
    tool = classes[0](workspace_dir="/tmp/ws")
    assert tool._api_key == "fc"
    assert tool._max_chars == 5000
    assert tool._summary_client is None


def test_build_tool_classes_web_fetch_summary_model_enables_prompt_and_shared_client():
    config = make_config_with(FIRECRAWL_API_KEY="fc")
    ctx = make_build_context(config)
    classes = _build_tool_classes(
        [PluginSpec(name="web_fetch", params={"summary_model": "fast-model"})], config, ctx,
    )
    assert classes[0].schema == build_web_fetch_schema(include_prompt=True)
    tool = classes[0](workspace_dir="/tmp/ws")
    assert tool._summary_model == "fast-model"
    assert tool._summary_client is ctx.shared_client
```

`make_config_with` in the existing file currently always sets `MAX_STEPS_PER_TURN`/`CONTEXT_TOKEN_BUDGET`/`TRUNCATE_KEEP_LAST_N`/`BASH_TIMEOUT` in its `base` dict (Task 2 removed these `Config` fields) — update `make_config_with`'s `base` dict now to drop those four keys (leave everything else in the existing helper as-is; the *old* `build_plugin_set()` calls elsewhere in this file that use `make_config()`/`make_config_with()` still need to keep passing until Task 7, so don't delete or rewrite the old tests — just fix this one shared helper so `Config(**base)` doesn't choke on now-unknown-but-ignored extra keys, which it wouldn't anyway since `extra="ignore"`, so this edit is optional cleanliness, not a correctness fix — do it anyway for clarity).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/plugins/test_registry.py -k build_tool_classes -v`
Expected: FAIL with `ImportError: cannot import name 'TOOL_BUILDERS'`

- [ ] **Step 3: Add to `src/conic/plugins/registry.py`** (append; do not remove `build_plugin_set` yet)

```python
from collections.abc import Callable
from dataclasses import dataclass

from conic.plugins.plugin_config import PluginConfigError, PluginSpec


@dataclass
class BuildContext:
    shared_client: AsyncOpenAI
    prompts: dict[str, str]


def _build_bash_tool(config: Config, params: dict, ctx: BuildContext) -> type:
    timeout = params.get("timeout", 60.0)

    class ConfiguredBashToolPlugin(BashToolPlugin):
        def __init__(self, workspace_dir: str):
            super().__init__(workspace_dir=workspace_dir, timeout=timeout)

    return ConfiguredBashToolPlugin


def _build_list_skills_tool(config: Config, params: dict, ctx: BuildContext) -> type:
    class ConfiguredListSkillsToolPlugin(ListSkillsToolPlugin):
        def __init__(self, workspace_dir: str):
            super().__init__(workspace_dir=workspace_dir, project_root=config.project_root)

    return ConfiguredListSkillsToolPlugin


def _build_load_skill_tool(config: Config, params: dict, ctx: BuildContext) -> type:
    class ConfiguredLoadSkillToolPlugin(LoadSkillToolPlugin):
        def __init__(self, workspace_dir: str):
            super().__init__(workspace_dir=workspace_dir, project_root=config.project_root)

    return ConfiguredLoadSkillToolPlugin


def _build_web_search_tool(config: Config, params: dict, ctx: BuildContext) -> type:
    if not config.tavily_api_key:
        raise PluginConfigError("web_search requires TAVILY_API_KEY to be set")
    timeout = params.get("timeout", 30.0)

    class ConfiguredWebSearchToolPlugin(WebSearchToolPlugin):
        def __init__(self, workspace_dir: str):
            super().__init__(workspace_dir=workspace_dir, api_key=config.tavily_api_key, timeout=timeout)

    return ConfiguredWebSearchToolPlugin


def _build_web_fetch_tool(config: Config, params: dict, ctx: BuildContext) -> type:
    if not config.firecrawl_api_key:
        raise PluginConfigError("web_fetch requires FIRECRAWL_API_KEY to be set")
    timeout = params.get("timeout", 60.0)
    max_chars = params.get("max_chars", 15000)
    summary_model = params.get("summary_model", "")

    class ConfiguredWebFetchToolPlugin(WebFetchToolPlugin):
        schema = build_web_fetch_schema(include_prompt=bool(summary_model))

        def __init__(self, workspace_dir: str):
            super().__init__(
                workspace_dir=workspace_dir, api_key=config.firecrawl_api_key,
                timeout=timeout, max_chars=max_chars, summary_model=summary_model,
                summary_client=ctx.shared_client if summary_model else None,
            )

    return ConfiguredWebFetchToolPlugin


TOOL_BUILDERS: dict[str, Callable[[Config, dict, BuildContext], type]] = {
    "bash": _build_bash_tool,
    "read_file": lambda config, params, ctx: ReadFileToolPlugin,
    "write_file": lambda config, params, ctx: WriteFileToolPlugin,
    "edit_file": lambda config, params, ctx: EditFileToolPlugin,
    "list_skills": _build_list_skills_tool,
    "load_skill": _build_load_skill_tool,
    "web_search": _build_web_search_tool,
    "web_fetch": _build_web_fetch_tool,
}


def _build_tool_classes(specs: list[PluginSpec], config: Config, ctx: BuildContext) -> tuple[type, ...]:
    classes = []
    for spec in specs:
        builder = TOOL_BUILDERS.get(spec.name)
        if builder is None:
            raise PluginConfigError(f"unknown tool: {spec.name}")
        classes.append(builder(config, spec.params, ctx))
    return tuple(classes)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/plugins/test_registry.py -v`
Expected: all PASS (both the new `_build_tool_classes` tests and the pre-existing `build_plugin_set` tests, since the old function is untouched)

- [ ] **Step 5: Lint and commit**

Run: `uv run ruff check src/conic/plugins/registry.py tests/plugins/test_registry.py`

```bash
git add src/conic/plugins/registry.py tests/plugins/test_registry.py
git commit -m "feat(registry): add BuildContext and tool builder table"
```

---

### Task 5: `registry.py` — section and context builders

**Files:**
- Modify: `src/conic/plugins/registry.py`
- Modify: `tests/plugins/test_registry.py`

**Interfaces:**
- Consumes: `BuildContext`, `PluginSpec` from Tasks 1 and 4.
- Produces: `SECTION_BUILDERS: dict[str, Callable[[Config, dict, BuildContext, str, list[dict]], object]]`; `_build_sections(names: list[str], config: Config, ctx: BuildContext, ws: str, schemas: list[dict]) -> list`; `CONTEXT_BUILDERS: dict[str, Callable[[Config, dict, BuildContext], Callable[[str, list[dict]], object]]]`; `_build_context_plugins(specs: list[PluginSpec], config: Config, ctx: BuildContext) -> tuple`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/plugins/test_registry.py`:

```python
from conic.plugins.context.sections.identity import IdentitySectionPlugin
from conic.plugins.context.sections.runtime import RuntimeSectionPlugin
from conic.plugins.context.sections.skills import SkillsSectionPlugin
from conic.plugins.context.sections.tooling import ToolingSectionPlugin
from conic.plugins.context.sections.workspace import WorkspaceSectionPlugin
from conic.plugins.registry import CONTEXT_BUILDERS, SECTION_BUILDERS, _build_context_plugins, _build_sections


def test_build_sections_resolves_each_name_to_its_plugin_type():
    config = make_config()
    ctx = BuildContext(shared_client=make_build_context(config).shared_client, prompts={"identity": "hi"})
    sections = _build_sections(
        ["identity", "tooling", "skills", "workspace", "runtime", "execution"], config, ctx, "/tmp/ws", [],
    )
    kinds = [type(s) for s in sections]
    assert kinds == [
        IdentitySectionPlugin, ToolingSectionPlugin, SkillsSectionPlugin,
        WorkspaceSectionPlugin, RuntimeSectionPlugin, ExecutionBiasSectionPlugin,
    ]


def test_build_sections_unknown_name_raises():
    config = make_config()
    ctx = make_build_context(config)
    with pytest.raises(PluginConfigError, match="unknown section"):
        _build_sections(["not_a_real_section"], config, ctx, "/tmp/ws", [])


def test_build_context_plugins_wires_configured_values():
    config = make_config()
    ctx = make_build_context(config)
    specs = [
        PluginSpec(name="turn_variables"),
        PluginSpec(name="system_prompt", params={"sections": ["identity"]}),
        PluginSpec(name="truncator", params={"keep_last_n": 9}),
        PluginSpec(name="token_budget", params={"budget_tokens": 123}),
        PluginSpec(name="extra_prompt", params={"sections": ["dynamic_state"]}),
    ]
    factories = _build_context_plugins(specs, config, ctx)
    instances = [factory("/tmp/ws", []) for factory in factories]
    kinds = [type(p) for p in instances]
    assert kinds == [
        TurnVariableUpdaterPlugin, SystemPromptPlugin, TruncatorPlugin, TokenBudgetPlugin, ExtraPromptPlugin,
    ]
    assert instances[2]._keep_last_n == 9
    assert instances[3]._budget_tokens == 123


def test_build_context_plugins_produces_fresh_instances_each_call():
    config = make_config()
    ctx = make_build_context(config)
    factories = _build_context_plugins([PluginSpec(name="turn_variables")], config, ctx)
    assert factories[0]("/tmp/ws", []) is not factories[0]("/tmp/ws", [])


def test_build_context_plugins_unknown_name_raises():
    config = make_config()
    ctx = make_build_context(config)
    with pytest.raises(PluginConfigError, match="unknown context plugin"):
        _build_context_plugins([PluginSpec(name="not_real")], config, ctx)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/plugins/test_registry.py -k "build_sections or build_context_plugins" -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Add to `src/conic/plugins/registry.py`**

```python
SectionBuilder = Callable[[Config, dict, BuildContext, str, list[dict]], object]

SECTION_BUILDERS: dict[str, SectionBuilder] = {
    "identity": lambda config, params, ctx, ws, schemas: IdentitySectionPlugin(ctx.prompts.get("identity", "")),
    "tooling": lambda config, params, ctx, ws, schemas: ToolingSectionPlugin(schemas),
    "skills": lambda config, params, ctx, ws, schemas: SkillsSectionPlugin(ws, config.project_root),
    "workspace": lambda config, params, ctx, ws, schemas: WorkspaceSectionPlugin(ws),
    "runtime": lambda config, params, ctx, ws, schemas: RuntimeSectionPlugin(),
    "execution": lambda config, params, ctx, ws, schemas: ExecutionBiasSectionPlugin(ctx.prompts.get("execution", "")),
}


def _build_sections(
    names: list[str], config: Config, ctx: BuildContext, ws: str, schemas: list[dict]
) -> list:
    sections = []
    for name in names:
        builder = SECTION_BUILDERS.get(name)
        if builder is None:
            raise PluginConfigError(f"unknown section: {name}")
        sections.append(builder(config, {}, ctx, ws, schemas))
    return sections


def _build_turn_variables_context(config: Config, params: dict, ctx: BuildContext):
    return lambda ws, schemas: TurnVariableUpdaterPlugin()


def _build_system_prompt_context(config: Config, params: dict, ctx: BuildContext):
    section_names = params.get("sections", [])
    return lambda ws, schemas: SystemPromptPlugin(_build_sections(section_names, config, ctx, ws, schemas))


def _build_truncator_context(config: Config, params: dict, ctx: BuildContext):
    keep_last_n = params.get("keep_last_n", 40)
    return lambda ws, schemas: TruncatorPlugin(keep_last_n=keep_last_n)


def _build_token_budget_context(config: Config, params: dict, ctx: BuildContext):
    budget_tokens = params.get("budget_tokens", 50000)
    return lambda ws, schemas: TokenBudgetPlugin(budget_tokens=budget_tokens)


def _build_extra_prompt_context(config: Config, params: dict, ctx: BuildContext):
    section_names = params.get("sections", [])
    return lambda ws, schemas: ExtraPromptPlugin(_build_sections(section_names, config, ctx, ws, schemas))


CONTEXT_BUILDERS: dict[str, Callable[[Config, dict, BuildContext], Callable[[str, list[dict]], object]]] = {
    "turn_variables": _build_turn_variables_context,
    "system_prompt": _build_system_prompt_context,
    "truncator": _build_truncator_context,
    "token_budget": _build_token_budget_context,
    "extra_prompt": _build_extra_prompt_context,
}


def _build_context_plugins(
    specs: list[PluginSpec], config: Config, ctx: BuildContext
) -> tuple[Callable[[str, list[dict]], object], ...]:
    factories = []
    for spec in specs:
        builder = CONTEXT_BUILDERS.get(spec.name)
        if builder is None:
            raise PluginConfigError(f"unknown context plugin: {spec.name}")
        factories.append(builder(config, spec.params, ctx))
    return tuple(factories)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/plugins/test_registry.py -v`
Expected: all PASS

- [ ] **Step 5: Lint and commit**

Run: `uv run ruff check src/conic/plugins/registry.py tests/plugins/test_registry.py`

```bash
git add src/conic/plugins/registry.py tests/plugins/test_registry.py
git commit -m "feat(registry): add section and context builder tables"
```

---

### Task 6: `registry.py` — policy, summarizer, backend builders

**Files:**
- Modify: `src/conic/plugins/registry.py`
- Modify: `tests/plugins/test_registry.py`

**Interfaces:**
- Consumes: `BuildContext`, `PluginSpec` from Tasks 1 and 4.
- Produces: `POLICY_BUILDERS`, `_build_policy_plugins(specs, config) -> tuple`; `SUMMARIZER_BUILDERS`, `_build_summarizer(name: str) -> Callable[[], object]`; `BACKEND_BUILDERS`, `_build_backend(name: str, config: Config, ctx: BuildContext, provider_blacklist: list[str]) -> Callable[[str], object]`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/plugins/test_registry.py`:

```python
from conic.plugins.registry import (
    BACKEND_BUILDERS,
    POLICY_BUILDERS,
    SUMMARIZER_BUILDERS,
    _build_backend,
    _build_policy_plugins,
    _build_summarizer,
)


def test_build_policy_plugins_wires_configured_max_steps():
    config = make_config()
    specs = [PluginSpec(name="permission"), PluginSpec(name="step_limit", params={"max_steps": 7})]
    factories = _build_policy_plugins(specs, config)
    instances = [factory() for factory in factories]
    kinds = [type(p) for p in instances]
    assert kinds == [PermissionPolicyPlugin, StepLimitPlugin]
    assert instances[1]._max_steps == 7


def test_build_policy_plugins_produces_fresh_instances():
    config = make_config()
    factories = _build_policy_plugins([PluginSpec(name="permission")], config)
    assert factories[0]() is not factories[0]()


def test_build_policy_plugins_unknown_name_raises():
    config = make_config()
    with pytest.raises(PluginConfigError, match="unknown policy plugin"):
        _build_policy_plugins([PluginSpec(name="not_real")], config)


def test_build_summarizer_default_produces_summarizer_plugin():
    factory = _build_summarizer("default")
    assert isinstance(factory(), SummarizerPlugin)
    assert factory() is not factory()


def test_build_summarizer_unknown_name_raises():
    with pytest.raises(PluginConfigError, match="unknown summarizer"):
        _build_summarizer("not_real")


def test_build_backend_openrouter_wires_model_and_blacklist():
    config = make_config()
    ctx = make_build_context(config)
    backend = _build_backend("openrouter", config, ctx, ["novita"])
    instance = backend("discord:1")
    assert isinstance(instance, OpenRouterModelPlugin)
    assert instance.model == "test-model"
    assert instance._provider_blacklist == ["novita"]
    assert instance._client is ctx.shared_client


def test_build_backend_unknown_name_raises():
    config = make_config()
    ctx = make_build_context(config)
    with pytest.raises(PluginConfigError, match="unknown backend"):
        _build_backend("not_real", config, ctx, [])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/plugins/test_registry.py -k "build_policy_plugins or build_summarizer or build_backend" -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Add to `src/conic/plugins/registry.py`**

```python
def _build_permission_policy(config: Config, params: dict):
    return lambda: PermissionPolicyPlugin()


def _build_step_limit_policy(config: Config, params: dict):
    max_steps = params.get("max_steps", 25)
    return lambda: StepLimitPlugin(max_steps=max_steps)


POLICY_BUILDERS: dict[str, Callable[[Config, dict], Callable[[], object]]] = {
    "permission": _build_permission_policy,
    "step_limit": _build_step_limit_policy,
}


def _build_policy_plugins(specs: list[PluginSpec], config: Config) -> tuple[Callable[[], object], ...]:
    factories = []
    for spec in specs:
        builder = POLICY_BUILDERS.get(spec.name)
        if builder is None:
            raise PluginConfigError(f"unknown policy plugin: {spec.name}")
        factories.append(builder(config, spec.params))
    return tuple(factories)


SUMMARIZER_BUILDERS: dict[str, Callable[[], Callable[[], object]]] = {
    "default": lambda: (lambda: SummarizerPlugin()),
}


def _build_summarizer(name: str) -> Callable[[], object]:
    builder = SUMMARIZER_BUILDERS.get(name)
    if builder is None:
        raise PluginConfigError(f"unknown summarizer: {name}")
    return builder()


def _build_openrouter_backend(config: Config, ctx: BuildContext, provider_blacklist: list[str]):
    def factory(session_key: str):
        return OpenRouterModelPlugin(
            api_key=config.openrouter_api_key, model=config.openrouter_model, client=ctx.shared_client,
            provider_blacklist=provider_blacklist, session_id=session_key, app_name=config.project_name,
        )
    return factory


BACKEND_BUILDERS: dict[str, Callable[[Config, BuildContext, list[str]], Callable[[str], object]]] = {
    "openrouter": _build_openrouter_backend,
}


def _build_backend(
    name: str, config: Config, ctx: BuildContext, provider_blacklist: list[str]
) -> Callable[[str], object]:
    builder = BACKEND_BUILDERS.get(name)
    if builder is None:
        raise PluginConfigError(f"unknown backend: {name}")
    return builder(config, ctx, provider_blacklist)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/plugins/test_registry.py -v`
Expected: all PASS

- [ ] **Step 5: Lint and commit**

Run: `uv run ruff check src/conic/plugins/registry.py tests/plugins/test_registry.py`

```bash
git add src/conic/plugins/registry.py tests/plugins/test_registry.py
git commit -m "feat(registry): add policy, summarizer, and backend builder tables"
```

---

### Task 7: Rewrite `build_plugin_set()` around YAML, delete old hardcoded path, add `instantiate_session`

This is the big swap: delete the old hardcoded `build_plugin_set()` body and every test that exercised it via `Config`-driven behavior, replacing them with YAML-driven equivalents built from Tasks 1–6's pieces. It also adds the `instantiate_session` closure (Task 8's `PluginSet` field doesn't exist yet — that's added in Task 8 in `core/manager.py`; this task builds the closure but `PluginSet(...)` construction here will need updating again in Task 8 once the field exists — for now, skip passing `instantiate_session` into `PluginSet(...)` and leave the closure defined-but-unused, OR do Task 8's `core/manager.py` field-add first as step 0 of this task since the two are tightly coupled. **Do the `core/manager.py` field addition as part of this task's Step 3** to avoid a broken intermediate state — Task 8 then only has to touch `PluginManager.start_session()` and its tests.)

**Files:**
- Modify: `src/conic/plugins/registry.py` — replace `build_plugin_set()`, delete unused imports (`os`, `platform`'s Windows-only bits stay for `_detect_shell`).
- Modify: `src/conic/core/manager.py` — add `instantiate_session` field to `PluginSet` (behavior of `start_session()` itself is unchanged in this task; Task 8 wires it in).
- Modify: `tests/plugins/test_registry.py` — delete every test that exercised the *old* Python-hardcoded `build_plugin_set()` behavior (they test things this task removes: conditional web tool registration via API-key presence, `Config`-driven timeouts/budgets), replace with YAML-driven equivalents.
- Modify: `tests/plugins/test_registry.py` — `_detect_shell` tests are untouched; `make_config` is rewritten to accept `tmp_path` and write a real YAML file (every caller in the file, including Tasks 4–6's tests, updated to match); `make_config_with` is deleted (its 3 remaining callers switch to `make_config(tmp_path, ...)`).

**Interfaces:**
- Consumes: everything from Tasks 1–6.
- Produces: `build_plugin_set(config: Config, global_variables: dict | None = None) -> PluginSet` (new implementation); `PluginSet.instantiate_session: Callable[[MessageBus, str, str, object, dict], object]` field exists (added to the dataclass, but `manager.py`'s `start_session()` body is still unchanged until Task 8).

- [ ] **Step 1: Delete the tests that exercised old `build_plugin_set()` Config-driven behavior**

Remove these test functions entirely from `tests/plugins/test_registry.py` (their behavior is superseded by Tasks 4–6's builder-level tests, which cover the same assertions against the new pieces):
`test_build_plugin_set_wires_the_v1_tools`, `test_build_plugin_set_wires_skill_tools_with_project_root`, `test_build_plugin_set_wires_skills_section_plugin`, `test_build_plugin_set_wires_bash_tool_with_configured_timeout`, `test_build_plugin_set_wires_backend_with_configured_model`, `test_build_plugin_set_wires_backend_with_the_sessions_id_for_sticky_routing`, `test_build_plugin_set_wires_backend_without_blacklist_by_default`, `test_build_plugin_set_parses_configured_provider_blacklist`, `test_build_plugin_set_wires_config_project_name_as_backend_app_name`, `test_build_plugin_set_wires_context_chain_with_configured_values`, `test_build_plugin_set_wires_turn_variable_updater_plugin`, `test_build_plugin_set_wires_policy_plugins_with_configured_max_steps`, `test_build_plugin_set_wires_summarizer`, `test_web_tools_absent_without_api_keys`, `test_web_search_registered_with_tavily_key_only`, `test_web_fetch_registered_with_firecrawl_key_and_raw_schema_by_default`, `test_web_fetch_summary_model_enables_prompt_param_and_shared_client`.

Keep (still meaningful against the new `build_plugin_set()`, just need a `plugins_config_path` pointed at a real YAML file now): `test_build_plugin_set_backend_factory_produces_fresh_instances_sharing_one_client`, `test_build_plugin_set_context_and_policy_factories_produce_fresh_instances`, `test_loop_factory_produces_a_react_loop_plugin`, `test_loop_factory_wires_session_and_global_variables`, `test_loop_factory_seeds_session_variables_from_persisted_values`, `test_build_plugin_set_merges_caller_supplied_global_variables`, `test_build_plugin_set_wires_the_detected_shell_into_global_variables`, `_detect_shell` tests.

- [ ] **Step 2: Update `make_config`/`make_config_with` to write a real YAML file and point `PLUGINS_CONFIG_PATH` at it, and write new end-to-end tests**

```python
# Near the top of tests/plugins/test_registry.py, replace make_config/make_config_with:

DEFAULT_PLUGINS_YAML = """
tools:
  - bash: {timeout: 42}
  - read_file
  - write_file
  - edit_file
  - list_skills
  - load_skill
context:
  - turn_variables
  - system_prompt: {sections: [identity, tooling, skills, workspace, runtime, execution]}
  - truncator: {keep_last_n: 9}
  - token_budget: {budget_tokens: 123}
  - extra_prompt: {sections: [dynamic_state]}
policy:
  - permission
  - step_limit: {max_steps: 7}
summarizer: default
backend: openrouter
"""


def make_config(tmp_path, yaml_text=DEFAULT_PLUGINS_YAML, **extra):
    plugins_yaml = tmp_path / "plugins.yaml"
    plugins_yaml.write_text(yaml_text, encoding="utf-8")
    base = {
        "_env_file": None,
        "PROJECT_ROOT": "/tmp",
        "DISCORD_BOT_TOKEN": "d", "OPENROUTER_API_KEY": "k", "OPENROUTER_MODEL": "test-model",
        "WORKSPACE_ROOT": "./workspace", "DUCKDB_PATH": "./data/conic.duckdb",
        "LOG_LEVEL": "DEBUG",
        "PLUGINS_CONFIG_PATH": str(plugins_yaml),
    }
    base.update(extra)
    return Config(**base)
```

This changes `make_config`'s signature (now requires `tmp_path` as first positional arg) — **every call site of `make_config()` anywhere in this file must become `make_config(tmp_path)`, and every test function that calls it must accept a `tmp_path` parameter**. This is not limited to the "kept" `build_plugin_set`-level tests listed above — it also includes every builder-level test added in Tasks 4, 5, and 6 (`test_build_tool_classes_*`, `test_build_sections_*`, `test_build_context_plugins_*`, `test_build_policy_plugins_*`, `test_build_backend_*`, and `make_build_context`'s own callers), since they all call `make_config()` too. Go through the whole file and add `tmp_path` to every test function signature that calls `make_config`, and pass it through. `make_build_context(config)` itself is unaffected (it takes an already-built `Config`, not raw args) — just make sure every caller of `make_build_context` first gets its `config` from `make_config(tmp_path)`, not the old zero-arg form.

Delete `make_config_with`. Three tests added in Task 4 still call it and must be updated to call `make_config(tmp_path, ...)` instead (same kwarg-based override mechanism, `make_config`'s `**extra` now serves the same purpose):
- `test_build_tool_classes_web_search_with_api_key_wires_timeout`: `config = make_config_with(TAVILY_API_KEY="tv", **{})` -> `config = make_config(tmp_path, TAVILY_API_KEY="tv")`
- `test_build_tool_classes_web_fetch_with_api_key_and_no_summary_model`: `config = make_config_with(FIRECRAWL_API_KEY="fc")` -> `config = make_config(tmp_path, FIRECRAWL_API_KEY="fc")`
- `test_build_tool_classes_web_fetch_summary_model_enables_prompt_and_shared_client`: `config = make_config_with(FIRECRAWL_API_KEY="fc")` -> `config = make_config(tmp_path, FIRECRAWL_API_KEY="fc")`

These three tests only use `config` to build a `BuildContext` and call `_build_tool_classes` directly — they never touch `plugins.yaml`'s content, so pointing `PLUGINS_CONFIG_PATH` at a throwaway file via `make_config`'s YAML-writing side effect is harmless overhead, not a behavior change.

Add new end-to-end tests:

```python
def test_build_plugin_set_end_to_end_matches_yaml(tmp_path):
    config = make_config(tmp_path)
    plugin_set = build_plugin_set(config)

    assert len(plugin_set.tool_classes) == 6
    bash_tool = plugin_set.tool_classes[0](workspace_dir="/tmp/ws")
    assert bash_tool._timeout == 42

    instances = [factory("/tmp/ws", []) for factory in plugin_set.context_plugins]
    assert [type(p) for p in instances] == [
        TurnVariableUpdaterPlugin, SystemPromptPlugin, TruncatorPlugin, TokenBudgetPlugin, ExtraPromptPlugin,
    ]
    assert instances[2]._keep_last_n == 9
    assert instances[3]._budget_tokens == 123

    policy_instances = [factory() for factory in plugin_set.policy_plugins]
    assert [type(p) for p in policy_instances] == [PermissionPolicyPlugin, StepLimitPlugin]
    assert policy_instances[1]._max_steps == 7

    assert isinstance(plugin_set.summarizer(), SummarizerPlugin)
    assert isinstance(plugin_set.backend("discord:1"), OpenRouterModelPlugin)


def test_build_plugin_set_unknown_tool_in_yaml_raises(tmp_path):
    with pytest.raises(PluginConfigError, match="unknown tool"):
        build_plugin_set(make_config(tmp_path, yaml_text="tools: [not_a_real_tool]\n"))


def test_build_plugin_set_web_search_without_key_raises(tmp_path):
    with pytest.raises(PluginConfigError, match="TAVILY_API_KEY"):
        build_plugin_set(make_config(tmp_path, yaml_text="tools: [web_search]\n"))


def test_build_plugin_set_instantiate_session_registers_every_plugin_and_returns_loop(tmp_path):
    from conic.core.bus import MessageBus
    from conic.plugins.loops.react_loop import ReactLoopPlugin

    class FakeHandle:
        def load_history(self):
            return []

    config = make_config(tmp_path)
    plugin_set = build_plugin_set(config)
    bus = MessageBus()

    loop_plugin = plugin_set.instantiate_session(bus, "/tmp/ws", "discord:1", FakeHandle(), {})

    assert isinstance(loop_plugin, ReactLoopPlugin)
    assert loop_plugin._session_variables["workspace_dir"] == "/tmp/ws"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/plugins/test_registry.py -v`
Expected: many FAIL — old `build_plugin_set()` doesn't accept YAML-based config the new tests assume, and `PluginSet` has no `instantiate_session` field yet.

- [ ] **Step 4: Add `instantiate_session` to `PluginSet` in `core/manager.py`**

In `src/conic/core/manager.py`, extend the dataclass (do not change `PluginManager.start_session()`'s body yet — that's Task 8):

```python
from conic.core.bus import MessageBus  # already imported; just noting it's needed for the type hint


@dataclass
class PluginSet:
    tool_classes: tuple[type, ...]
    backend: Callable[[str], object]
    context_plugins: tuple[Callable[[str, list[dict]], object], ...]
    policy_plugins: tuple[Callable[[], object], ...]
    summarizer: Callable[[], object]
    loop_factory: Callable[[object, list[dict], dict[str, type], str, dict], object]
    instantiate_session: Callable[[MessageBus, str, str, object, dict], object]
```

This will break `tests/core/test_manager.py`'s `make_manager()` (missing required field) — expected; fixed in Task 8. Don't chase that failure in this task.

- [ ] **Step 5: Rewrite `build_plugin_set()` in `src/conic/plugins/registry.py`**

Replace the entire old `build_plugin_set()` function (everything from `def build_plugin_set(config: Config, ...` to its closing `)`) with:

```python
from conic.core.bus import infer_payload_type
from conic.plugins.plugin_config import load_plugins_config


def build_plugin_set(config: Config, global_variables: dict | None = None) -> PluginSet:
    prompts = _load_prompts(Path(config.project_root) / "prompts")
    shared_client = AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=config.openrouter_api_key)
    ctx = BuildContext(shared_client=shared_client, prompts=prompts)

    resolved_global_variables = {
        "model": config.openrouter_model,
        "platform": f"{platform.system()} {platform.release()}",
        "shell": _detect_shell(),
        "timezone": "UTC",
        **(global_variables or {}),
    }
    provider_blacklist = [p.strip() for p in config.openrouter_provider_blacklist.split(",") if p.strip()]

    plugins_cfg = load_plugins_config(config.plugins_config_path)

    tool_classes = _build_tool_classes(plugins_cfg.tools, config, ctx)
    context_plugins = _build_context_plugins(plugins_cfg.context, config, ctx)
    policy_plugins = _build_policy_plugins(plugins_cfg.policy, config)
    summarizer = _build_summarizer(plugins_cfg.summarizer)
    backend = _build_backend(plugins_cfg.backend, config, ctx, provider_blacklist)

    def loop_factory(handle, schemas, payload_map, ws, persisted_session_variables):
        return ReactLoopPlugin(
            handle, schemas, payload_map,
            workspace_dir=ws,
            global_variables=resolved_global_variables,
            persisted_session_variables=persisted_session_variables,
        )

    def instantiate_session(bus, workspace_dir, session_key, handle, persisted_session_variables):
        tool_schemas = [cls.schema for cls in tool_classes]
        tool_payload_map = {cls.llm_name: infer_payload_type(cls.execute) for cls in tool_classes}

        for tool_cls in tool_classes:
            tool_cls(workspace_dir=workspace_dir).register(bus)

        backend(session_key).register(bus)
        for ctx_factory in context_plugins:
            ctx_factory(workspace_dir, tool_schemas).register(bus)
        for policy_factory in policy_plugins:
            policy_factory().register(bus)
        summarizer().register(bus)

        loop_plugin = loop_factory(handle, tool_schemas, tool_payload_map, workspace_dir, persisted_session_variables)
        loop_plugin.register(bus)
        return loop_plugin

    return PluginSet(
        tool_classes=tool_classes,
        backend=backend,
        context_plugins=context_plugins,
        policy_plugins=policy_plugins,
        summarizer=summarizer,
        loop_factory=loop_factory,
        instantiate_session=instantiate_session,
    )
```

Delete now-unused imports at the top of `registry.py`: `import os` stays (used by `_detect_shell`); remove the old direct class-building code that `build_plugin_set` used to contain inline (the `class ConfiguredBashToolPlugin` etc. that lived *inside* `build_plugin_set` — these are now superseded by the Task 4 module-level `_build_*` functions, delete the inline duplicates). Remove the `tool_classes: list[type] = [...]` / `if config.tavily_api_key: ...` / `if config.firecrawl_api_key: ...` block entirely — it's replaced by `_build_tool_classes(plugins_cfg.tools, ...)`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/plugins/test_registry.py -v`
Expected: all PASS

Run: `uv run pytest tests/core/test_manager.py -v`
Expected: FAIL (missing `instantiate_session` in fake `PluginSet` construction) — expected, fixed in Task 8.

- [ ] **Step 7: Lint and commit**

Run: `uv run ruff check src/conic/plugins/registry.py src/conic/core/manager.py tests/plugins/test_registry.py`

```bash
git add src/conic/plugins/registry.py src/conic/core/manager.py tests/plugins/test_registry.py
git commit -m "feat(registry): rewrite build_plugin_set around YAML, add instantiate_session"
```

---

### Task 8: Wire `instantiate_session` into `PluginManager.start_session()`

**Files:**
- Modify: `src/conic/core/manager.py`
- Modify: `tests/core/test_manager.py`

**Interfaces:**
- Consumes: `PluginSet.instantiate_session` (Task 7).
- Produces: `PluginManager.start_session()` now delegates instantiation to `self._plugin_set.instantiate_session(...)`.

- [ ] **Step 1: Update `tests/core/test_manager.py`'s `make_manager()` to supply a fake `instantiate_session`**

```python
def make_manager(tmp_path):
    from conic.core.bus import infer_payload_type
    from conic.plugins.loops.react_loop import ReactLoopPlugin

    storage = StorageService(
        db_path=str(tmp_path / "conic.duckdb"),
        workspace_root=str(tmp_path / "workspace"),
        default_model="test-model",
    )
    storage.startup()
    shared_client = object()

    tool_classes = (FakeToolPlugin,)
    backend = lambda session_key: FakeBackend(shared_client)
    context_plugins = (FakeContextPlugin,)
    policy_plugins = (FakePolicyPlugin,)
    summarizer = FakeSummarizer

    def loop_factory(handle, schemas, payload_map, ws, session_vars):
        return ReactLoopPlugin(handle, schemas, payload_map, ws, persisted_session_variables=session_vars)

    def instantiate_session(bus, workspace_dir, session_key, handle, persisted_session_variables):
        tool_schemas = [cls.schema for cls in tool_classes]
        tool_payload_map = {cls.llm_name: infer_payload_type(cls.execute) for cls in tool_classes}

        for tool_cls in tool_classes:
            tool_cls(workspace_dir=workspace_dir).register(bus)

        backend(session_key).register(bus)
        for ctx_factory in context_plugins:
            ctx_factory(workspace_dir, tool_schemas).register(bus)
        for policy_factory in policy_plugins:
            policy_factory().register(bus)
        summarizer().register(bus)

        loop_plugin = loop_factory(handle, tool_schemas, tool_payload_map, workspace_dir, persisted_session_variables)
        loop_plugin.register(bus)
        return loop_plugin

    plugin_set = PluginSet(
        tool_classes=tool_classes,
        backend=backend,
        context_plugins=context_plugins,
        policy_plugins=policy_plugins,
        summarizer=summarizer,
        loop_factory=loop_factory,
        instantiate_session=instantiate_session,
    )
    return storage, PluginManager(storage, plugin_set)
```

- [ ] **Step 2: Run tests to verify current state**

Run: `uv run pytest tests/core/test_manager.py -v`
Expected: PASS already (this fake `instantiate_session` does exactly what the current inline `start_session()` body does — the test suite doesn't yet observe any difference because `start_session()` hasn't been rewritten). This step just confirms the fake is wired correctly before the production code changes underneath it.

- [ ] **Step 3: Rewrite `PluginManager.start_session()` in `src/conic/core/manager.py`**

```python
async def start_session(
    self,
    channel: str,
    native_id: str,
    channel_plugin_factory: Callable[[], object],
    reason: str = "new",
) -> SessionScope:
    logger.debug("starting session channel={} native_id={}", channel, native_id)
    row = self._storage.get_or_create(channel=channel, native_id=native_id)
    bus = MessageBus()
    scope = SessionScope(bus=bus, row=row)
    handle = self._storage.handle_for(row)

    loop_plugin = self._plugin_set.instantiate_session(
        bus, row.workspace_dir, row.session_key, handle, row.variables
    )

    channel_plugin_factory().register(bus)

    session_gateway = SessionGatewayPlugin(scope)
    session_gateway.register(bus)

    await bus.chain(meta.SessionStartEvent, SessionStart(reason=reason))

    session_end_emitted = False

    async def _mark_session_end_emitted(msg: SessionEnd) -> SessionEnd:
        nonlocal session_end_emitted
        session_end_emitted = True
        return msg

    bus.on_chain(meta.SessionEndEvent, _mark_session_end_emitted)

    scope.tasks = {
        "loop": asyncio.create_task(loop_plugin.run_loop()),
        "gateway": asyncio.create_task(session_gateway.run()),
    }
    asyncio.create_task(self._join_and_cleanup(scope, lambda: session_end_emitted))

    return scope
```

Remove the now-unused `infer_payload_type` import from the top of `core/manager.py` (`from conic.core.bus import MessageBus, infer_payload_type` -> `from conic.core.bus import MessageBus`) — it's only used inside `instantiate_session` closures now, which live in `registry.py`/test fakes, not in `manager.py` itself.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_manager.py -v`
Expected: all PASS (identical behavior, now routed through `instantiate_session`)

- [ ] **Step 5: Lint and commit**

Run: `uv run ruff check src/conic/core/manager.py tests/core/test_manager.py`

```bash
git add src/conic/core/manager.py tests/core/test_manager.py
git commit -m "refactor(manager): delegate session plugin instantiation to PluginSet.instantiate_session"
```

---

### Task 9: Full suite check, docs, spec status update

**Files:**
- Modify: `README.md`
- Modify: `CONTRIB.md`
- Modify: `docs/superpowers/specs/2026-09-22-yaml-plugin-config-design.md` (status line)
- Modify: `docs/superpowers/specs/2026-09-13-conic-agentic-engine-design.md` (section 15 status)

**Interfaces:**
- Consumes: nothing new (docs-only + full-suite verification).
- Produces: nothing new (no code interfaces).

- [ ] **Step 1: Run the full suite and lint across the whole repo**

Run: `uv run pytest -q`
Expected: all PASS, no leftover failures from earlier tasks' "expected to fail, fixed later" notes.

Run: `uv run ruff check src tests scripts`
Expected: `All checks passed!`

If anything fails, fix it now before touching docs — do not proceed with a red suite.

- [ ] **Step 2: Add a "Plugin configuration" section to `README.md`**

Append after the existing "Model catalog" section:

```markdown
## Plugin configuration

Which tools, context-pipeline stages, and policy checks are wired into every
session is declared in `plugins.yaml` (path configurable via
`PLUGINS_CONFIG_PATH`, defaults to `{PROJECT_ROOT}/plugins.yaml`). Secrets
(API keys, tokens) stay in `.env`; `plugins.yaml` only declares plugin
names, order, and non-sensitive parameters (timeouts, token budgets, step
limits). An unknown plugin name, or a tool declared without its required
API key configured in `.env` (e.g. `web_search` without `TAVILY_API_KEY`),
fails at startup with a clear error rather than silently skipping the
plugin.
```

- [ ] **Step 3: Update `CONTRIB.md`'s project layout listing**

Find the `src/conic/` tree listing (near the `openrouter/` line added previously) and add:

```
    plugins/
      plugin_config.py  # YAML plugin config schema and loader
      registry.py   # plugin wiring, now YAML-driven (see plugins.yaml)
```

Add a line near the repo-root file listing noting `plugins.yaml  # declarative plugin configuration (see README)`.

- [ ] **Step 4: Flip the new spec's status line**

In `docs/superpowers/specs/2026-09-22-yaml-plugin-config-design.md`, change:
```
状态：设计中，未实现
```
to:
```
状态：已实现
```

- [ ] **Step 5: Update the main engine design doc's section 15**

In `docs/superpowers/specs/2026-09-13-conic-agentic-engine-design.md`, change the section 15 heading and opening line from "尚未实现，仅记录设计讨论" to something like:

```markdown
## 15. YAML 驱动的插件配置（已实现，详见独立 spec）

这一节曾经记录的是设计讨论阶段的内容；已经实现，完整设计和实现细节见
`docs/superpowers/specs/2026-09-22-yaml-plugin-config-design.md` 和
`docs/superpowers/plans/2026-09-22-yaml-plugin-config.md`。这里只留一句
指路，不重复维护两份内容。
```

Delete the rest of section 15's body (the YAML schema draft, builder mechanism description, etc.) since it now lives in the dedicated spec file — this doc's convention (established throughout this file) is one authoritative description per topic, not two copies that can drift.

- [ ] **Step 6: Final full-suite check and commit**

Run: `uv run pytest -q && uv run ruff check src tests scripts`
Expected: all PASS, `All checks passed!`

```bash
git add README.md CONTRIB.md docs/superpowers/specs/2026-09-22-yaml-plugin-config-design.md docs/superpowers/specs/2026-09-13-conic-agentic-engine-design.md
git commit -m "docs: document plugins.yaml, mark YAML plugin config design as implemented"
```
