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
| `PLUGINS_CONFIG_PATH` | `{PROJECT_ROOT}/config/plugins.yaml` | Path to the declarative plugin config file -- see "Plugin configuration" below. |
| `TAVILY_API_KEY` | unset | Tavily API key. Required only if `web_search` is declared in `plugins.yaml`. |
| `FIRECRAWL_API_KEY` | unset | Firecrawl API key. Required only if `web_fetch` is declared in `plugins.yaml`. |

Per-tool timeouts, token budgets, step limits, and which tools/context
stages/policies exist at all are no longer environment variables -- they're
declared in `plugins.yaml` (see below).

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

## Model catalog

On every startup (and then hourly via a background task), the catalog of
available OpenRouter models is fetched from the
[OpenRouter models API](https://openrouter.ai/docs/api-reference/list-available-models)
and stored in DuckDB. The model running the bot
(`OPENROUTER_MODEL`) is looked up in that catalog to resolve its advertised
`context_length`, which is exposed to sessions as the
`model_context_length` global (used by the context/truncation pipeline).

Because the catalog is fetched before the bot connects, this value is
correct from the first session — it does not rely on a pre-seeded database.
If the catalog fetch fails at startup (e.g. no network), the lookup falls
back to a default of 65535 tokens, so the bot still starts; the hourly sync
keeps retrying in the background.

## Plugin configuration

Which tools, context-pipeline stages, and policy checks are wired into every
session is declared in `config/plugins.yaml` (path configurable via
`PLUGINS_CONFIG_PATH`, defaults to `{PROJECT_ROOT}/config/plugins.yaml`).
The file is a dict of named plugin sets -- each top-level key (e.g. `main`,
`agent`) maps to its own independent `tools`/`context`/`policy`/
`summarizer`/`backend`/`loop` configuration. `build_plugin_set()` builds
every named set in the file and `PluginManager` holds the whole dict;
`PluginManager.start_session()` takes an optional `plugin_set_name`
(defaults to `"main"`) to pick which one a given session runs. The
Discord gateway always starts sessions with the default, so it always
runs `main` (startup fails with a clear error if the file has no `main`
entry). Nothing currently starts a session with a different name --
the `agent` set exists so a caller can opt into it later (e.g. a
sub-agent with a different tool/model configuration).

Secrets (API keys, tokens) stay in `.env`; `plugins.yaml` only declares
plugin names, order, and non-sensitive parameters (timeouts, token
budgets, step limits). An unknown plugin name, or a tool declared without
its required API key configured in `.env` (e.g. `web_search` without
`TAVILY_API_KEY`), fails at startup with a clear error rather than
silently skipping the plugin. See the repo's `config/plugins.yaml` for
the default configuration and
`docs/superpowers/specs/2026-09-22-yaml-plugin-config-design.md` for the
full schema and design.
