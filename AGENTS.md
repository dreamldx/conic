# AGENTS.md

Agent instructions for working on the Conic project.

## Rules

- See `CONTRIB.md` for coding style and architecture conventions.
- Run `uv run pytest` after every change. Do not commit if tests fail.
- Update or add unit tests for every code change. Key decisions and logic paths must have test coverage, not every code path.
- Bus topic names must use constants from `src/conic/plugins/meta.py`, never hardcoded strings.
- Do not add comments to source code.
- SQL belongs in `src/conic/services/queries.py`, not inline.
- `PROJECT_ROOT` must stay a required env var (`Config.project_root`, no default). Never derive it from `__file__`/cwd/`pyproject.toml` lookups — that breaks silently under a packaged/frozen release or when run from an unexpected cwd/entry point. Every entry point must set or require it explicitly.
