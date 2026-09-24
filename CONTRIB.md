# Contributing to Conic

## Code Style

### Python

- Target **Python 3.12+**, use modern syntax (`str | None`, `list[dict]`)
- Package manager: **uv** (`uv sync`, `uv run`)
- Linter: **ruff**. Run `uv run ruff check` on changed Python files after code changes; run `uv run ruff check src tests` before committing code when the full tree is expected to be lint-clean.
- Formatting: keep imports and layout compatible with ruff's reported style. No separate formatter is configured.

### Naming

| Kind | Convention | Example |
|------|-----------|---------|
| Classes | PascalCase | `PluginManager`, `DiscordGateway` |
| Functions / methods | snake_case | `build_app`, `start_session` |
| Constants | UPPER_SNAKE_CASE | `DEFAULT_SYSTEM_PROMPT` |
| Private members | `_underscore` prefix | `self._conn`, `self._storage` |
| Modules | snake_case | `react_loop.py`, `token_budget.py` |

### Type Annotations

All function signatures must have type annotations. Use `| None` for optional, not `Optional[...]`.

```python
def load_config(env: dict[str, str] | None = None) -> Config:
```

### Code Checks

- Tests after every change: `uv run pytest`
- Style check for changed Python files: `uv run ruff check <paths>`
- Full style check for lint-clean code submissions: `uv run ruff check src tests`
- Coverage when needed: `uv run pytest --cov=src/conic --cov-report=term-missing`

### Architecture

- **MessageBus** is the communication backbone — `bus.on_chain()` / `bus.chain()` for chain handlers, `bus.on_request()` / `bus.request()` for request/response, and mailboxes for queued session input
- Bus topic names are centralized in `src/conic/plugins/meta.py` as `*Event` constants
- All plugins register on the bus in their `register(bus)` method
- SQL statements live in `src/conic/services/queries.py` as functions returning `(sql, params)` tuples
- Data models are pydantic `BaseModel` in `src/conic/services/models.py`
- Config is `pydantic_settings.BaseSettings` in `src/conic/config.py`

### Comments

Do not add comments to source code. If a comment is truly necessary (e.g. a
non-obvious workaround or a deployment/template file), write it in **English**
only — Chinese or other non-English comments are not allowed.

### Logging

Use `loguru` — `logger.info()`, `logger.debug()`, `logger.warning()`, `logger.exception()`.

### Error Handling

- Framework-level exceptions in `src/conic/types/errors.py`
- Tool errors returned as `ToolCallResult(error=...)`, not raised
- Context/policy plugins mutate and return payloads; return `None` for no-op
- `ReactLoopPlugin` turn/tool-call boundaries must keep `except Exception` as a last-resort session safety net, with `AbortTurn` handled separately before the catch-all
- Discord runtime boundaries in `DiscordGateway` and the Discord thread channel adapter must keep `except Exception` fallbacks so one bad thread/message edit does not crash the gateway or session
- Other code should catch specific exception types unless it is an explicit process, session, gateway, or external callback boundary

### Date and Time

- All `datetime` values must be timezone-aware.
- Use UTC for stored timestamps, event variables, tests, and default values: `datetime.now(UTC)`.
- Do not use local-time APIs such as `datetime.now()` without a timezone, `datetime.utcnow()`, or `datetime.now().astimezone()` for product logic.
- Serialized timestamps must include an offset, such as `2026-09-19T12:00:00+00:00`.
- If old persisted data lacks timezone information, load it as UTC before exposing it to the rest of the application.
- Use monotonic clocks such as `time.monotonic()` only for elapsed-time measurement, rate limiting, and timeouts.

### Testing

- **pytest** + **pytest-asyncio** + **pytest-cov** (`asyncio_mode = "auto"`)
- Tests mirror source structure under `tests/`
- Test file naming: `test_<module>.py`
- Run: `uv run pytest` or `.venv/Scripts/python.exe -m pytest`
- Coverage: `uv run pytest --cov=src/conic --cov-report=term-missing`

### Skills

- Never install skills into the project directory (no `npx skills add` without `-g`); it creates `.agents/` and `.claude/skills` in the repo
- Install skills globally: `npx skills add <source> -g -y`, which puts them in `~/.agents/skills`

## Project Structure

```
src/conic/
  core/           # MessageBus, PluginManager
  types/          # pure type definitions: messages, errors, Gateway protocol, SessionScope
  discord/        # Discord gateway + adapter
  plugins/
    tools/        # bash, read_file, write_file, edit_file
    models/       # OpenRouter API adapter
    context/      # system prompt assembly, truncation, summarization
      sections/   # per-section prompt contributors
    loops/        # ReAct execution loop
    policy/       # permission, step limit
    meta.py       # bus topic name constants
    registry.py   # plugin wiring, YAML-driven (see config/plugins.yaml)
    plugin_config.py  # plugins.yaml schema (pydantic) and loader
  openrouter/     # OpenRouter model catalog sync (fetch + periodic refresh)
  services/       # DuckDB storage, SQL queries, data models
  config.py       # configuration via pydantic-settings
  entry.py        # application entry point (async build_app)
tests/
  core/
  types/
  discord/
  plugins/
  services/
config/
  plugins.yaml    # declarative plugin configuration (see README)
```
