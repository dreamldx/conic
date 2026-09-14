# AGENTS.md

Agent instructions for working on the Conic project.

## Rules

- See `CONTRIB.md` for coding style and architecture conventions.
- Run `uv run pytest` after every change. Do not commit if tests fail.
- Bus topic names must use constants from `src/conic/plugins/meta.py`, never hardcoded strings.
- Do not add comments to source code.
- SQL belongs in `src/conic/services/queries.py`, not inline.
