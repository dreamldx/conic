# Experiential Labs Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an Experiential Labs model backend (no runtime model switching, `Idempotency-Key` per call) and make the DuckDB model catalog provider-aware.

**Architecture:** Extract OpenRouterModelPlugin's request/stream/timeout logic into a shared base; `OpenRouterModelPlugin` and `ExperientialModelPlugin` subclass it. `model_catalog` gets a `provider` column (PK `(provider, slug)`); a hardcoded provider registry drives eager startup sync plus periodic re-sync per provider.

**Tech Stack:** Python 3.12, openai `AsyncOpenAI`, aiohttp, DuckDB, pytest, uv, ruff.

**Spec:** `docs/superpowers/specs/2026-11-15-experiential-backend-design.md`

## Global Constraints

- Run `uv run pytest` after every change; do not commit if tests fail. Run `uv run ruff check` on changed Python files.
- No comments in source code. SQL only in `src/conic/services/queries.py`. Bus topic names only from `src/conic/plugins/meta.py`.
- `PROJECT_ROOT` stays a required env var (do not touch it).
- Env names are `EXPERIENTIAL_LABS_API_KEY` and `EXPERIENTIAL_LABS_MODEL` (already in the user's `.env`), no defaults, required only when a plugin set selects `backend: experiential`.
- Experiential base URL `https://api.experientiallabs.ai/v1`; catalog URL `https://api.experientiallabs.ai/api/models` (keyless, paginated: query params `limit` and `offset`; response keys `models`, `total`, `limit`, `offset`).
- Timeouts identical to OpenRouter: stream `(10.0, 15.0, 30.0)`, blocking `30.0`.
- Idempotency header name: `Idempotency-Key`. OpenRouter behavior (headers, body, tests) must not change.
- `config/plugins.yaml` already has `backend: experiential` for `main` (user edit, uncommitted); leave it and do not stage it unless the user asks.
- Update or add unit tests for every change; existing tests may be adapted only where a signature changes.

## Review Focus

- Experiential catalog has 972 models over several pages (default page size 50): all pages must be fetched, not just the first.
- Same slug under two providers must not collide or overwrite each other.
- Experiential model with null/missing `context_window` or pricing: entry gets 0 values and the plugin falls back to `DEFAULT_MODEL_CONTEXT_LENGTH`.
- Experiential catalog fetch failing at startup must not stop the app from booting.
- Existing DuckDB file with the old `model_catalog` (no `provider` column) must migrate on startup, not crash.
- Selecting `backend: experiential` without the env vars: clear `PluginConfigError`, not an error from deep inside.
- Stream-timeout retry within one `complete()` reuses the same `Idempotency-Key`; the next `complete()` uses a new one.

---

### Task 1: Provider-aware catalog storage

**Files:**
- Modify: `src/conic/services/models.py` (`ModelCatalogEntry`)
- Modify: `src/conic/services/queries.py` (catalog SQL, ~lines 106-196)
- Modify: `src/conic/services/storage.py` (startup migration, catalog methods)
- Modify: `src/conic/openrouter/catalog.py` (`sync_catalog_once` save call), `src/conic/entry.py`, `src/conic/plugins/registry.py`
- Test: `tests/services/test_storage.py`, `tests/openrouter/test_catalog.py`, `tests/test_main.py`, `tests/plugins/test_registry.py`

**Interfaces:**
- Produces: `ModelCatalogEntry.provider: str = "openrouter"`; `StorageService.save_model_catalog(provider: str, entries: list[ModelCatalogEntry]) -> None` (replaces only that provider's rows); `list_model_catalog(provider: str | None = None)`; `find_model_catalog_entries(provider: str, query: str) -> list[ModelCatalogEntry]`; `get_model_context_length(provider: str, slug: str) -> int`; `model_catalog_is_empty(provider: str) -> bool`. `build_plugin_set(..., catalog_lookup: Callable[[str, str], list[ModelCatalogEntry]] | None)` where the callable takes `(provider, query)`; `BuildContext.catalog_lookup` has the same type.

- [ ] **Step 1: Write failing tests** in `tests/services/test_storage.py`:
  - `test_same_slug_under_two_providers_does_not_collide`: save `ModelCatalogEntry(slug="x", provider="openrouter", context_length=1)` and `(slug="x", provider="experiential", context_length=2)`; `find_model_catalog_entries("experiential", "x")` returns one entry with `context_length == 2` and provider `"experiential"`.
  - `test_save_model_catalog_only_replaces_the_given_provider`: save openrouter `a`, experiential `b`, then openrouter `[c]`; `list_model_catalog()` slugs are `{"b", "c"}`.
  - `test_model_catalog_is_empty_is_per_provider`: after saving only openrouter, `model_catalog_is_empty("openrouter") is False` and `model_catalog_is_empty("experiential") is True`.
  - `test_startup_rebuilds_model_catalog_table_without_provider_column`: create the old table (current schema, no `provider`) with one row before `startup()`; afterwards `model_catalog_is_empty("openrouter") is True` and a save/find round trip works.
  - Adapt existing catalog tests in this file to the new signatures.
- [ ] **Step 2: Run** `uv run pytest tests/services/test_storage.py -v` — expect FAIL.
- [ ] **Step 3: Implement.** Add the `provider` field to `ModelCatalogEntry`. In `queries.py`: `provider VARCHAR` column with `PRIMARY KEY (provider, slug)` in `create_model_catalog_table_sql`; replace `model_catalog_has_legacy_id_column_sql` with `model_catalog_is_outdated_sql` (true when `model_catalog` exists in `information_schema.tables` and has no `provider` column, which also covers the legacy `id` table); `drop_model_catalog_sql` becomes `DROP TABLE IF EXISTS`; `clear_model_catalog_sql(provider)` deletes `WHERE provider = ?`; insert/list/find/context-length/is-empty SQL gain the provider column or filter. Update `_catalog_entry_from_row` for the new column. The existing legacy-id test must still pass.
- [ ] **Step 4: Adapt callers.** `sync_catalog_once` calls `storage.save_model_catalog("openrouter", entries)`; `entry.py` uses `storage.model_catalog_is_empty("openrouter")` and passes `catalog_lookup=storage.find_model_catalog_entries`; in `registry.py` change the `catalog_lookup` types and bind `functools.partial(ctx.catalog_lookup, "openrouter")` when constructing `OpenRouterModelPlugin` (guard `None`). Fix `FakeStorage.save_model_catalog(self, provider, entries)` in `tests/openrouter/test_catalog.py`, the seed calls in `tests/test_main.py`, and any registry test passing `catalog_lookup`.
- [ ] **Step 5: Run** `uv run pytest` and `uv run ruff check` on changed files — expect all pass.
- [ ] **Step 6: Commit** `feat(storage): add provider column to model_catalog`.

---

### Task 2: Experiential catalog fetcher

**Files:**
- Create: `src/conic/experiential/__init__.py` (empty), `src/conic/experiential/catalog.py`
- Test: `tests/experiential/__init__.py`, `tests/experiential/test_catalog.py`

**Interfaces:**
- Consumes: `ModelCatalogEntry` (Task 1).
- Produces: `EXPERIENTIAL_CATALOG_URL = "https://api.experientiallabs.ai/api/models"`, `EXPERIENTIAL_PROVIDER = "experiential"`, `async def fetch_experiential_models(timeout: float = 30, session_factory=aiohttp.ClientSession) -> list[ModelCatalogEntry]`.

- [ ] **Step 1: Write failing tests** (reuse the fake `session_factory` pattern from `tests/openrouter/test_catalog.py`, extended so each GET returns the next canned page and records `params`):
  - `test_fetch_maps_a_model_to_a_catalog_entry`: page `{"models":[{"model":{"slug":"claude-opus-5.5","display_name":"Claude Opus 5.5","description":"d","context_window":1000000,"maker":"Anthropic","input_modalities":["text","image"],"output_modalities":["text"],"supported_params":{"tools":true,"reasoning":true,"stop":false},"status":"active"},"providers":[{"input_nano_usd_per_million":4000000000,"output_nano_usd_per_million":20000000000}]}],"total":1,"limit":500,"offset":0}` gives one entry with `provider="experiential"`, `slug == real_model == "claude-opus-5.5"`, `vendor="anthropic"`, `name="Claude Opus 5.5"`, `context_length=1000000`, `supports_tools=True`, `supported_parameters=["tools","reasoning"]`, `pricing_prompt == pytest.approx(4e-6)`, `pricing_completion == pytest.approx(2e-5)` (nano-USD per million tokens divided by 1e15 gives USD per token).
  - `test_fetch_follows_pagination_until_total_is_reached`: two pages (`total=3`, 2 + 1 models) give 3 entries; the second request has `offset == 2`.
  - `test_fetch_tolerates_missing_context_window_pricing_and_providers`: `context_window: None`, `providers: []` gives `context_length == 0` and prices `0.0`.
  - `test_fetch_skips_models_that_are_not_active`.
  - `test_fetch_raises_on_http_error`.
- [ ] **Step 2: Run** `uv run pytest tests/experiential -v` — expect FAIL (module missing).
- [ ] **Step 3: Implement** `fetch_experiential_models` with page size 500, looping `GET` with `limit`/`offset` until `offset >= total` or a page is empty. Pricing comes from the first provider entry that has the price (missing gives `0.0`). Vendor is `maker.lower()` or `""`.
- [ ] **Step 4: Run** `uv run pytest tests/experiential -v` and ruff — expect PASS.
- [ ] **Step 5: Commit** `feat(experiential): fetch model catalog`.

---

### Task 3: Extract shared model plugin base

**Files:**
- Create: `src/conic/plugins/models/base.py`
- Modify: `src/conic/plugins/models/openrouter.py`
- Test: `tests/plugins/models/test_openrouter.py` (must pass unchanged apart from import or patch paths), `tests/plugins/models/test_base.py`

**Interfaces:**
- Produces: `class ChatCompletionModelPlugin(Plugin)` (`Name = "chat_completion_base"`) in `base.py`, with `STREAM_ATTEMPT_TIMEOUTS` and `BLOCKING_TIMEOUT` constants (re-exported from `openrouter.py`), constructor `(model: str, client: AsyncOpenAI, session_id: str | None = None, catalog_lookup=None, global_variables: dict | None = None, stream_timeouts=STREAM_ATTEMPT_TIMEOUTS, blocking_timeout=BLOCKING_TIMEOUT)`, `register(bus)` wiring only `meta.ModelRequestEvent -> complete`, `async complete(msg)`, overridable hooks `_extra_body() -> dict | None` (default `None`) and `_extra_headers(idempotency_key: str) -> dict` (default `{}`), plus the shared `_resolve_catalog_entry`, `_reload_model_context_length`, `_record_usage`, `_complete_blocking`, `_complete_streaming`, `_stream_once`. Each `complete()` call generates one key (`uuid.uuid4().hex`) and threads it through blocking, streaming and retries. Log messages use `self.Name` instead of the literal `"openrouter"`.

- [ ] **Step 1: Write failing test** in `tests/plugins/models/test_base.py`: a minimal subclass (`Name = "fake_chat"`) overriding `_extra_headers` to return `{"Idempotency-Key": key}`; with a fake client modelled on `FakeCompletions` in `test_openrouter.py`, `test_complete_calls_hooks_with_one_key_per_call` asserts two `complete()` calls send two different keys, and a streaming call whose first attempt times out then succeeds sends the same key on both attempts (model the timeout fakes on the existing stream-retry tests).
- [ ] **Step 2: Run** the new test — expect FAIL.
- [ ] **Step 3: Move the shared logic** from `OpenRouterModelPlugin` into the base (everything except `switch_model`, `contribute_model_catalog_info`, provider blacklist and referer/title headers). `OpenRouterModelPlugin(ChatCompletionModelPlugin)` keeps its constructor signature (`api_key`, `provider_blacklist`, `app_name`, ...) and default client, overrides `_extra_body`, `_extra_headers(idempotency_key)` (ignores the key) and `register` (calls `super().register(bus)`, then adds the switch and catalog-section handlers).
- [ ] **Step 4: Run** `uv run pytest tests/plugins/models -v`, then `uv run pytest` — all existing OpenRouter tests pass.
- [ ] **Step 5: Commit** `refactor(models): extract ChatCompletionModelPlugin base`.

---

### Task 4: ExperientialModelPlugin and registry/config wiring

**Files:**
- Create: `src/conic/plugins/models/experiential.py`
- Modify: `src/conic/config.py`, `src/conic/plugins/registry.py`
- Test: `tests/plugins/models/test_experiential.py`, `tests/test_config.py`, `tests/plugins/test_registry.py`

**Interfaces:**
- Consumes: `ChatCompletionModelPlugin` (Task 3); `BuildContext.catalog_lookup(provider, query)` (Task 1).
- Produces: `EXPERIENTIAL_BASE_URL = "https://api.experientiallabs.ai/v1"`; `class ExperientialModelPlugin(ChatCompletionModelPlugin)` with `Name = "experiential"` and constructor `(api_key: str, model: str, client: AsyncOpenAI | None = None, session_id: str | None = None, catalog_lookup=None, global_variables: dict | None = None, stream_timeouts=STREAM_ATTEMPT_TIMEOUTS, blocking_timeout=BLOCKING_TIMEOUT)`; `Config.experiential_api_key: str = Field(default="", alias="EXPERIENTIAL_LABS_API_KEY")`; `Config.experiential_model: str = Field(default="", alias="EXPERIENTIAL_LABS_MODEL")`.

- [ ] **Step 1: Write failing tests**
  - `test_experiential.py`: `test_register_does_not_handle_switch_model_or_catalog_section` (after `register(bus)`, no handler is registered for `meta.SwitchModelRequestEvent` or `meta.BuildSystemPromptEvent`); `test_blocking_request_sends_idempotency_key_header` (non-empty, differs between two calls); `test_streaming_retry_reuses_the_same_idempotency_key`; `test_default_timeouts_match_openrouter`; `test_context_length_comes_from_catalog_lookup_and_falls_back_to_default` (lookup returns an entry with `context_length=1000000` gives `global_variables["model_context_length"] == 1000000`; lookup returning `[]` gives `DEFAULT_MODEL_CONTEXT_LENGTH`).
  - `test_registry.py`: `test_experiential_backend_requires_api_key_and_model` (set with `backend: experiential` and a missing key or model raises `PluginConfigError` naming `EXPERIENTIAL_LABS_API_KEY` or `EXPERIENTIAL_LABS_MODEL`); `test_experiential_set_uses_its_own_model_in_global_variables` (the experiential set's `global_variables["model"] == EXPERIENTIAL_LABS_MODEL` while an `openrouter` set keeps `OPENROUTER_MODEL`).
  - `test_config.py`: the new fields default to `""` and read from the aliases.
- [ ] **Step 2: Run** the new tests — expect FAIL.
- [ ] **Step 3: Implement** `ExperientialModelPlugin` (default client `AsyncOpenAI(base_url=EXPERIENTIAL_BASE_URL, api_key=api_key)`; `_extra_headers(key)` returns `{"Idempotency-Key": key}`; the base `register` is used as is). In `registry.py` add `_build_experiential_backend` to `BACKEND_BUILDERS` (raise `PluginConfigError` if key or model is empty; bind `functools.partial(ctx.catalog_lookup, "experiential")`; own client per set), and in `_build_one_plugin_set` give each set its own copy of `resolved_global_variables` whose `"model"` is the backend's model (`config.experiential_model` for `experiential`, `config.openrouter_model` otherwise).
- [ ] **Step 4: Run** `uv run pytest` and ruff on changed files — expect PASS.
- [ ] **Step 5: Commit** `feat(models): add Experiential Labs backend`.

---

### Task 5: Provider registry, startup sync and periodic sync

**Files:**
- Create: `src/conic/catalog.py`
- Modify: `src/conic/entry.py`, `src/conic/openrouter/catalog.py` (remove `run_periodic_sync` and `DEFAULT_SYNC_INTERVAL_SECONDS` once moved)
- Test: `tests/test_catalog.py` (new; port the four `run_periodic_sync` tests), `tests/openrouter/test_catalog.py`, `tests/test_main.py`

**Interfaces:**
- Consumes: `sync_catalog_once` (OpenRouter), `fetch_experiential_models` (Task 2), `StorageService` provider methods (Task 1).
- Produces: `@dataclass(frozen=True) class CatalogProvider: name: str; sync: Callable[[StorageService, Config], Awaitable[bool]]`; `CATALOG_PROVIDERS: tuple[CatalogProvider, ...]` hardcoded as `openrouter`, then `experiential`; `DEFAULT_SYNC_INTERVAL_SECONDS = 3600`; `async def sync_experiential_once(storage: StorageService, config: Config) -> bool` (fetch, then `storage.save_model_catalog("experiential", entries)`; any exception is logged and returns `False`); `async def run_periodic_sync(sync: Callable[[], Awaitable[bool]], interval_seconds: float = DEFAULT_SYNC_INTERVAL_SECONDS, sleep=asyncio.sleep) -> None` (sleep first, then sync, keep looping after a failed sync, same semantics as today's loop).

- [ ] **Step 1: Write failing tests** in `tests/test_catalog.py`: the four ported periodic tests adapted to the zero-arg `sync` callable (no sync before the first sleep; syncs after each sleep; re-syncs every interval; keeps looping after `sync` raises); `test_sync_experiential_once_saves_entries_under_experiential_provider`; `test_sync_experiential_once_returns_false_and_saves_nothing_on_failure`; `test_catalog_providers_are_openrouter_then_experiential`. In `tests/test_main.py`: `test_build_app_syncs_only_providers_whose_catalog_is_empty` (inject `build_app(env, providers=...)` with two fake providers, one pre-seeded) and `test_build_app_boots_when_a_provider_sync_raises`. Adapt the existing `sync_once=` based tests in `tests/test_main.py` to the new parameter.
- [ ] **Step 2: Run** — expect FAIL.
- [ ] **Step 3: Implement.** The openrouter `CatalogProvider` wraps `sync_catalog_once(storage, config.openrouter_api_key, json_path=<data/openrouter_models.json>)`. `build_app(env=None, providers=CATALOG_PROVIDERS)`: for each provider with `model_catalog_is_empty(provider.name)`, `await provider.sync(storage, config)`; a raised exception is logged and boot continues. `main()` starts one `asyncio.create_task(run_periodic_sync(partial(provider.sync, storage, config)))` per provider and cancels them all on shutdown with the cleanup task. Remove the unused loop from `openrouter/catalog.py` together with its tests.
- [ ] **Step 4: Run** `uv run pytest` and `uv run ruff check src tests` — expect PASS.
- [ ] **Step 5: Manual smoke:** with the real `.env`, `uv run python -c` calling `fetch_experiential_models()` prints a count above 50 and shows `claude-opus-5.5` with `context_length == 1000000`.
- [ ] **Step 6: Commit** `feat(catalog): sync every registered provider`.

---

## Self-review

- Spec coverage: shared base, Idempotency-Key reuse and rotation, same timeouts, no switching (T3, T4); provider column, PK, per-provider save, lookup filters, migration (T1); hardcoded provider list, eager and periodic sync, OpenRouter fetch untouched (T2, T5); config required only when selected (T4); context length from catalog with fallback (T4).
- Deviations from the spec text: env vars are `EXPERIENTIAL_LABS_*` to match the user's `.env` (spec updated); the catalog fetch is paginated; the old table is dropped and re-synced rather than migrated in place; `session.model` default and the startup log line still use `OPENROUTER_MODEL`.
