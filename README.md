# Conic

Conic is a small, pluggable agentic engine that connects an LLM (via
OpenRouter) to a Discord bot, giving it real local tool access — `bash`,
`read_file`, `write_file`, `edit_file`, `list_skills`/`load_skill`,
`web_search` (Tavily), and `web_fetch` (Firecrawl) — scoped to a
per-session workspace directory.
Every session (one per Discord thread) gets its own isolated `MessageBus`
and set of plugin instances, with conversation history persisted to DuckDB
so sessions survive a bot restart. It's intended for small, trusted teams
who want a coding-agent-in-a-thread, not for exposing tool execution to
untrusted public users.

See `docs/superpowers/specs/2026-09-13-conic-agentic-engine-design.md` for
the full architecture, `docs/superpowers/specs/2026-09-18-web-tools-design.md`
for web tools design, and `docs/superpowers/specs/2026-09-19-conic-skill-system-design.md`
for the skill system.

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

The catalog of available OpenRouter models is fetched from the
[OpenRouter models API](https://openrouter.ai/docs/api-reference/list-available-models),
stored in DuckDB (`model_catalog` table: `slug`, `vendor`, `real_model`,
name, description, context length, pricing, modalities, supported
parameters), and snapshotted to `{PROJECT_ROOT}/data/openrouter_models.json`
on every successful sync. It's synced once at startup if the table is
empty (a fresh database, or the very first run), and every hour after that
via a background task — a non-empty table at startup is assumed fresh
enough and is *not* re-synced immediately, to avoid hitting the OpenRouter
API twice a few seconds apart on every restart.

`OpenRouterModelPlugin` looks up its own model's `context_length` in this
catalog — at construction (covering both new sessions and sessions
resumed after a restart) and again whenever the model is switched — and
exposes it to the prompt as the `model_context_length` global (used by
the context/truncation pipeline). If the catalog can't be queried (e.g.
first-ever startup with no network), it falls back to a default of 65535
tokens; the hourly sync keeps retrying in the background, but note that an
already-running session won't pick up a value corrected by a later sync
except by switching models again.

A session's model can be changed at runtime via the `SwitchModelRequestEvent`
bus request (`SwitchModelRequest(model_id) -> SwitchModelResult(model, error)`).
Matching is case-insensitive against either the full slug or the model
name after the vendor prefix, so `deepseek-v4-flash`,
`deepseek/deepseek-v4-flash-0731` and `~deepseek/deepseek-flash-latest`
all resolve; an alias resolves to the real model it points at. There is
currently no Discord command or tool wired up to send this request.

Use the `find-model` skill (`skills/find-model/`) to search, filter and
sort this catalog — fuzzy name matching, filtering by vendor/price/context/
modality/tools, and listing every value an option accepts.

## Discord behavior

Besides `/agent_start` and `/agent_stop`, @-mentioning the bot in a regular
server channel starts a new session in a fresh public thread, titled from
the first line of the message (truncated to 90 characters, or
`agent-session` if empty); the mention itself is stripped before it's
handed to the model. Mentioning the bot with no other text gets an error
reply in the channel instead of an empty thread. Mentioning the bot inside
an existing session thread just strips the mention and forwards the rest
of the message as usual.

A session whose thread has had no activity (no user or assistant message)
for 30 days is automatically archived, locked, and marked ended by an
hourly sweep — this applies even if the bot was offline when the thread
went quiet.

## Skills

Skills are reusable, task-specific instructions loaded on demand via the
`list_skills`/`load_skill` tools. Discovery scans, lowest to highest
priority, `~/.agents/skills`, `{PROJECT_ROOT}/.agents/skills` and
`{PROJECT_ROOT}/skills`, then the same two paths under the session
workspace directory — matching where
[`npx skills add`](https://github.com/vercel-labs/skills) installs skills
(project-local by default, `~/.agents/skills` with `-g`). See `CONTRIB.md`
for the `description` frontmatter conventions and the project's own
`skills/find-model/` for an example.

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
