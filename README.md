# Conic

Conic is a small, pluggable agentic engine that connects an LLM (via
OpenRouter) to a Discord bot, giving it real local tool access — `bash`,
`read_file`, `write_file`, `edit_file`, `web_search` (Tavily), and
`web_fetch` (Firecrawl) — scoped to a per-session workspace directory.
Every session (one per Discord thread) gets its own isolated `MessageBus`
and set of plugin instances, with conversation history persisted to DuckDB
so sessions survive a bot restart. It's intended for small, trusted teams
who want a coding-agent-in-a-thread, not for exposing tool execution to
untrusted public users.

See `docs/superpowers/specs/2026-09-13-conic-agentic-engine-design.md` for
the full architecture. See `docs/superpowers/specs/2026-09-18-web-tools-design.md`
for web tools design.

## Environment variables

Required:

| Variable | Description |
|---|---|
| `PROJECT_ROOT` | Absolute path to the repo/install root (where `prompts/` lives, and `WORKSPACE_ROOT`/`DUCKDB_PATH` default relative to it). |
| `DISCORD_BOT_TOKEN` | Discord bot token. |
| `OPENROUTER_API_KEY` | OpenRouter API key. |

Optional (defaults shown):

| Variable | Default | Description |
|---|---|---|
| `OPENROUTER_MODEL` | `anthropic/claude-sonnet-4.5` | Model used for completions. |
| `OPENROUTER_PROVIDER_BLACKLIST` | unset | Comma-separated OpenRouter provider slugs (e.g. `novita,together`) to exclude via `provider.ignore`. OpenRouter still routes freely among every other provider. |
| `WORKSPACE_ROOT` | `./workspace` | Root directory under which each session's workspace is created. |
| `DUCKDB_PATH` | `./data/conic.duckdb` | Path to the DuckDB persistence file. |
| `MAX_STEPS_PER_TURN` | `25` | Max Steps (model calls) allowed within a single Turn before it's aborted. |
| `CONTEXT_TOKEN_BUDGET` | `50000` | Token threshold that triggers history summarization. |
| `TRUNCATE_KEEP_LAST_N` | `40` | Number of most recent messages the truncator keeps. |
| `BASH_TIMEOUT` | `60` | Seconds before a running `bash` command is killed and reported as an error. |
| `TAVILY_API_KEY` | unset | Tavily API key. If unset, `web_search` is not registered. |
| `FIRECRAWL_API_KEY` | unset | Firecrawl API key. If unset, `web_fetch` is not registered. |
| `WEB_SEARCH_TIMEOUT` | `30` | Seconds before a `web_search` request times out. |
| `WEB_FETCH_TIMEOUT` | `60` | Seconds before a `web_fetch` request times out. |
| `WEB_FETCH_MAX_CHARS` | `15000` | Character budget for inlined fetch results; larger pages are head+tail truncated and the full text spills to workspace. |
| `WEB_FETCH_SUMMARY_MODEL` | unset | OpenRouter model id for two-stage fetch (optional). When set, `web_fetch` exposes a `prompt` parameter that sends the page to this model for targeted answers. |

## Running it

```sh
uv sync
uv run python main.py
```

Before starting the bot, make sure the Discord application has the
**Message Content Intent** enabled in the Developer Portal, and that its
invite/OAuth permissions include creating and managing public threads and
sending messages in threads — see Task 22 in
`docs/superpowers/plans/2026-09-13-conic-agentic-engine.md` for the full
manual verification checklist.
