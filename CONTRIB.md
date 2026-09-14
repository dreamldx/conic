# Contributing to Conic

## Code Style

### Python

- Target **Python 3.12+**, use modern syntax (`str | None`, `list[dict]`)
- Package manager: **uv** (`uv sync`, `uv run`)
- Formatter/linter: follow existing style, no formatter configured yet

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

### Architecture

- **MessageBus** is the communication backbone — `bus.on()` for event handlers, `bus.on_request()` for request/response
- Bus topic names are centralized in `src/conic/plugins/meta.py` as `*Event` constants
- All plugins register on the bus in their `register(bus)` method
- SQL statements live in `src/conic/services/queries.py` as functions returning `(sql, params)` tuples
- Data models are pydantic `BaseModel` in `src/conic/services/models.py`
- Config is `pydantic_settings.BaseSettings` in `src/conic/config.py`

### Comments

Do not add comments to source code.

### Logging

Use `loguru` — `logger.info()`, `logger.debug()`, `logger.warning()`, `logger.exception()`.

### Error Handling

- Framework-level exceptions in `src/conic/core/errors.py`
- Tool errors returned as `ToolCallResult(error=...)`, not raised
- Context/policy plugins mutate and return payloads; return `None` for no-op

### Testing

- **pytest** + **pytest-asyncio** + **pytest-cov** (`asyncio_mode = "auto"`)
- Tests mirror source structure under `tests/`
- Test file naming: `test_<module>.py`
- Run: `uv run pytest` or `.venv/Scripts/python.exe -m pytest`
- Coverage: `uv run pytest --cov=src/conic --cov-report=term-missing`

## Project Structure

```
src/conic/
  core/           # MessageBus, SessionScope, PluginManager, messages
  discord/        # Discord gateway + adapter
  plugins/
    tools/        # bash, read_file, write_file, edit_file
    backends/     # OpenRouter API adapter
    context/      # system prompt assembly, truncation, summarization
      sections/   # per-section prompt contributors
    loops/        # ReAct execution loop
    policy/       # permission, step limit
    meta.py       # bus topic name constants
    registry.py   # plugin wiring
  services/       # DuckDB storage, SQL queries, data models
  config.py       # configuration via pydantic-settings
  entry.py        # application entry point
tests/
  core/
  discord/
  plugins/
  services/
```
