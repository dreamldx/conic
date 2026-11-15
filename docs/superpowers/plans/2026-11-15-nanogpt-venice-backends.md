# Nano-GPT and Venice Backends Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `nano_gpt` and `venice` selectable model backends, each with its own catalog provider, mirroring the existing Experiential backend.

**Architecture:** Each backend is a thin subclass of `ChatCompletionModelPlugin` (`plugins/models/base.py`) with fixed model, no switching, `Idempotency-Key` header. Each gets a keyless catalog fetcher producing `ModelCatalogEntry`, a `CatalogProvider` entry, config fields, a registry builder and a `backend_model` branch. Task 1 also extracts the shared sync helper in `catalog.py`.

**Tech Stack:** Python 3.12, `openai.AsyncOpenAI`, aiohttp, DuckDB, pytest, ruff, uv.

**Spec:** `docs/superpowers/specs/2026-11-15-nanogpt-venice-backends-design.md`. Reference implementation to copy the shape from: `src/conic/experiential/catalog.py`, `src/conic/plugins/models/experiential.py`, `_build_experiential_backend` in `src/conic/plugins/registry.py`, `sync_experiential_once` in `src/conic/catalog.py`, and their tests (`tests/experiential/test_catalog.py`, `tests/plugins/models/test_experiential.py`, `tests/test_catalog.py`, `tests/plugins/test_registry.py`, `tests/test_config.py`).

## Global Constraints

- Run `uv run pytest` after every change; do not commit if tests fail. Known unrelated failure: `tests/plugins/test_plugin_config.py::test_repo_default_plugins_yaml_parses_successfully` is caused by the user's uncommitted `config/plugins.yaml` edit (leave it).
- Run `uv run ruff check` on changed files; `uv run ruff check src tests` before committing.
- No comments in source code. SQL only in `src/conic/services/queries.py` (no SQL changes needed here). Bus topics only from `src/conic/plugins/meta.py`.
- Never touch, stage or commit `config/plugins.yaml` or the user's modified docs. Commit with an explicit pathspec (`git commit -m ... -- <files>`), message ending with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
- Pricing in `ModelCatalogEntry` is USD per token: both providers quote USD per million tokens, so divide by `1_000_000`.
- Timeouts, `STREAM_ATTEMPT_TIMEOUTS`/`BLOCKING_TIMEOUT`, and Idempotency-Key semantics come from the base unchanged. Plugins do not register `SwitchModelRequestEvent` or a catalog prompt section.
- Base URLs: Nano-GPT `https://api.nano-gpt.com/api/v1`; Venice `https://api.venice.ai/api/v1`. Plugin `Name` values: `nano_gpt`, `venice`. Provider names in the catalog equal the plugin `Name`.
- Env vars (aliases on `Config`, default `""`): `NANO_GPT_API_KEY`, `NANO_GPT_MODEL`, `VENICE_API_KEY`, `VENICE_MODEL`.

## Review Focus

- Venice entries of `type != "text"` or `model_spec.offline == true` must not enter the catalog.
- Nano-GPT entries with `context_length: null` or missing `pricing` must not crash the fetcher (context 0, price 0.0).
- Empty or failed fetch must leave the existing provider rows intact (covered by the shared sync helper).
- A plugin set selecting a backend without its key/model must raise `PluginConfigError` at build, not at first request.
- Venice request body must carry `venice_parameters.include_venice_system_prompt = false` on both blocking and streaming calls.

---

### Task 1: Nano-GPT backend (and shared sync helper)

**Files:**
- Create: `src/conic/nano_gpt/__init__.py`, `src/conic/nano_gpt/catalog.py`, `src/conic/plugins/models/nano_gpt.py`
- Modify: `src/conic/catalog.py`, `src/conic/config.py`, `src/conic/plugins/registry.py`
- Test: `tests/nano_gpt/__init__.py`, `tests/nano_gpt/test_catalog.py`, `tests/plugins/models/test_nano_gpt.py`, `tests/test_catalog.py`, `tests/plugins/test_registry.py`, `tests/test_config.py`

**Interfaces:**
- Produces:
  - `conic.nano_gpt.catalog`: `NANO_GPT_CATALOG_URL = "https://api.nano-gpt.com/api/v1/models"`, `NANO_GPT_PROVIDER = "nano_gpt"`, `async def fetch_nano_gpt_models(timeout: float = 30, session_factory=aiohttp.ClientSession) -> list[ModelCatalogEntry]` (GET with `params={"detailed": "true"}`).
  - `conic.plugins.models.nano_gpt`: `NANO_GPT_BASE_URL`, `class NanoGptModelPlugin(ChatCompletionModelPlugin)` with `Name = "nano_gpt"`, same constructor signature as `ExperientialModelPlugin`, `_extra_headers` returns `{"Idempotency-Key": key}`.
  - `conic.catalog`: private `async def _sync_provider(storage, provider: str, fetch: Callable[[], Awaitable[list[ModelCatalogEntry]]]) -> bool` (fetch, return False and save nothing when empty or on exception with the existing warning logs, else `storage.save_model_catalog(provider, entries)`, log "synced {} models from {}", return True); `sync_experiential_once` refactored onto it with unchanged behavior; new `sync_nano_gpt_once(storage, config) -> bool` calling `fetch_nano_gpt_models` through it (tests monkeypatch `catalog.fetch_nano_gpt_models`, so look the name up at call time like the experiential one does).
  - `CATALOG_PROVIDERS` becomes `(openrouter, experiential, nano_gpt)`.
  - `Config.nano_gpt_api_key`, `Config.nano_gpt_model`; `registry._build_nano_gpt_backend`, entry in `BACKEND_BUILDERS`; `backend_model("nano_gpt", config)` returns `config.nano_gpt_model`.

- [ ] **Step 1: Write failing tests**
  - `tests/nano_gpt/test_catalog.py` (copy the fake session helpers from `tests/experiential/test_catalog.py`):
    - `test_fetch_maps_detailed_model`: one row `{"id": "openai/gpt-x", "name": "GPT X", "description": "d", "context_length": 128000, "capabilities": {"vision": True, "tool_calling": True, "audio_input": False}, "pricing": {"prompt": 2, "completion": 10}}` yields an entry with `slug == real_model == "openai/gpt-x"`, `provider == "nano_gpt"`, `vendor == "openai"`, `name`, `description`, `context_length == 128000`, `supports_tools is True`, `pricing_prompt == 2e-6`, `pricing_completion == 1e-5`, `input_modalities == ["text", "image"]`, `output_modalities == ["text"]`, `"tools"` in `supported_parameters`.
    - `test_fetch_requests_detailed_listing`: captured call has url `NANO_GPT_CATALOG_URL` and `params == {"detailed": "true"}`.
    - `test_fetch_tolerates_null_context_and_missing_pricing`: row with `context_length: None` and no `pricing` gives `context_length == 0`, prices `0.0`.
    - `test_fetch_skips_rows_without_id`, `test_fetch_raises_on_http_error` (error from `raise_for_status` propagates).
  - `tests/plugins/models/test_nano_gpt.py`: copy the Experiential plugin test file with the class swapped and `Name == "nano_gpt"`, covering: Idempotency-Key stable across stream-timeout retries and different across calls, no switch/catalog-section handlers registered, context length from catalog lookup with fallback to `DEFAULT_MODEL_CONTEXT_LENGTH`, default client base URL is `NANO_GPT_BASE_URL`.
  - `tests/test_catalog.py`: add `test_sync_nano_gpt_once_saves_entries_under_nano_gpt_provider`, `..._returns_false_and_saves_nothing_on_failure`, `..._on_empty_fetch` (same shape as the experiential ones); update `test_catalog_providers_...` to `["openrouter", "experiential", "nano_gpt"]`.
  - `tests/test_config.py`: `NANO_GPT_API_KEY`/`NANO_GPT_MODEL` load into `nano_gpt_api_key`/`nano_gpt_model`, default `""`.
  - `tests/plugins/test_registry.py`: nano_gpt backend builds a `NanoGptModelPlugin` with the configured model; missing key and missing model each raise `PluginConfigError` mentioning the env var name; `backend_model("nano_gpt", config)` returns the nano model; catalog lookup is bound to `"nano_gpt"`.

- [ ] **Step 2: Run the new tests, confirm they fail** (`uv run pytest tests/nano_gpt tests/plugins/models/test_nano_gpt.py tests/test_catalog.py tests/test_config.py tests/plugins/test_registry.py -q`): ImportError / assertion failures.

- [ ] **Step 3: Implement `fetch_nano_gpt_models`** in `src/conic/nano_gpt/catalog.py`. One GET (no pagination), `body["data"]`; skip rows without `id`; `vendor` is the part of `id` before the first `/` (lowercased, `""` if none); `input_modalities` starts as `["text"]` plus `"image"` for `capabilities.vision`, `"video"` for `video_input`, `"audio"` for `audio_input`; `output_modalities == ["text"]`; `supports_tools` from `capabilities.tool_calling`; `supported_parameters` includes `"tools"` when supported and `"reasoning"` when `capabilities.reasoning`; `context_length or 0`; pricing per Global Constraints.

- [ ] **Step 4: Implement `NanoGptModelPlugin`** (`plugins/models/nano_gpt.py`) as a copy of the Experiential plugin shape with `NANO_GPT_BASE_URL`.

- [ ] **Step 5: Implement `_sync_provider`, refactor `sync_experiential_once`, add `sync_nano_gpt_once`**, extend `CATALOG_PROVIDERS`.

- [ ] **Step 6: Config, registry builder, `BACKEND_BUILDERS` entry, `backend_model` branch.** Error messages: `"nano_gpt backend requires NANO_GPT_API_KEY to be set"` and `"... NANO_GPT_MODEL ..."`. `backend_model` stays an if-chain over backend names falling through to `openrouter_model`.

- [ ] **Step 7: Run the full suite** (`uv run pytest -q`) and `uv run ruff check src tests`. Expected: all pass except the known `plugins.yaml` test.

- [ ] **Step 8: Commit** `feat(nano_gpt): add Nano-GPT backend and catalog provider` with an explicit pathspec of the files above.

---

### Task 2: Venice backend

**Files:**
- Create: `src/conic/venice/__init__.py`, `src/conic/venice/catalog.py`, `src/conic/plugins/models/venice.py`
- Modify: `src/conic/catalog.py`, `src/conic/config.py`, `src/conic/plugins/registry.py`
- Test: `tests/venice/__init__.py`, `tests/venice/test_catalog.py`, `tests/plugins/models/test_venice.py`, `tests/test_catalog.py`, `tests/plugins/test_registry.py`, `tests/test_config.py`

**Interfaces:**
- Consumes (from Task 1): `_sync_provider(storage, provider, fetch)` in `conic.catalog`; the Nano-GPT files as the template.
- Produces:
  - `conic.venice.catalog`: `VENICE_CATALOG_URL = "https://api.venice.ai/api/v1/models"`, `VENICE_PROVIDER = "venice"`, `async def fetch_venice_models(timeout: float = 30, session_factory=aiohttp.ClientSession) -> list[ModelCatalogEntry]` (plain GET, no params).
  - `conic.plugins.models.venice`: `VENICE_BASE_URL`, `class VeniceModelPlugin(ChatCompletionModelPlugin)` with `Name = "venice"`, `_extra_headers` returns `{"Idempotency-Key": key}`, `_extra_body` returns `{"venice_parameters": {"include_venice_system_prompt": False}}`.
  - `sync_venice_once`, `CATALOG_PROVIDERS == (openrouter, experiential, nano_gpt, venice)`, `Config.venice_api_key` / `venice_model`, `_build_venice_backend`, `backend_model("venice", config)`.

- [ ] **Step 1: Write failing tests**
  - `tests/venice/test_catalog.py`:
    - `test_fetch_maps_text_model`: row `{"id": "llama-x", "type": "text", "owned_by": "venice.ai", "context_length": 100, "model_spec": {"name": "Llama X", "description": "d", "availableContextTokens": 131072, "offline": False, "pricing": {"input": {"usd": 0.5, "diem": 0.5}, "output": {"usd": 2.0, "diem": 2.0}}, "capabilities": {"supportsFunctionCalling": True, "supportsVision": True, "supportsAudioInput": False, "supportsVideoInput": False, "supportsReasoning": True}}}` yields `slug == real_model == "llama-x"`, `provider == "venice"`, `vendor == "venice.ai"`, `name == "Llama X"`, `context_length == 131072` (falls back to top-level `context_length` when `availableContextTokens` is absent), `supports_tools is True`, `pricing_prompt == 5e-7`, `pricing_completion == 2e-6`, `input_modalities == ["text", "image"]`, `output_modalities == ["text"]`, `supported_parameters` containing `"tools"` and `"reasoning"`.
    - `test_fetch_skips_non_text_and_offline_models` (type `image`, and `offline: True`), `test_fetch_tolerates_missing_pricing_and_capabilities`, `test_fetch_requests_models_endpoint_without_params`, `test_fetch_raises_on_http_error`.
  - `tests/plugins/models/test_venice.py`: the Experiential plugin tests with the class swapped, plus `test_every_request_disables_venice_system_prompt`: both the blocking and the streaming call carry `extra_body == {"venice_parameters": {"include_venice_system_prompt": False}}`.
  - `tests/test_catalog.py`: venice sync tests (saved under `"venice"`, failure, empty) and the updated providers-order assertion. `tests/test_config.py`, `tests/plugins/test_registry.py`: same cases as Task 1 for `VENICE_API_KEY` / `VENICE_MODEL`.

- [ ] **Step 2: Run the new tests, confirm they fail.**

- [ ] **Step 3: Implement `fetch_venice_models`** (`body["data"]`, filters per Review Focus, `vendor = owned_by.lower()`, `input_modalities` starting `["text"]` plus `image`/`audio`/`video` from `supportsVision`/`supportsAudioInput`/`supportsVideoInput`, `supports_tools` from `supportsFunctionCalling`, `supported_parameters` with `"tools"` and `"reasoning"` from `supportsReasoning`).

- [ ] **Step 4: Implement `VeniceModelPlugin`.**

- [ ] **Step 5: Implement `sync_venice_once`**, extend `CATALOG_PROVIDERS`, config fields, registry builder (errors name `VENICE_API_KEY` / `VENICE_MODEL`), `BACKEND_BUILDERS`, `backend_model`.

- [ ] **Step 6: Run the full suite and `uv run ruff check src tests`.** Expected: all pass except the known `plugins.yaml` test.

- [ ] **Step 7: Commit** `feat(venice): add Venice backend and catalog provider` with an explicit pathspec.
