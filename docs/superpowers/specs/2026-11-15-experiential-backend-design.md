# Experiential Labs model backend (short design)

## Intent
Add Experiential Labs (OpenAI-compatible gateway, `https://api.experientiallabs.ai/v1`) as a selectable model backend, next to OpenRouter. Selected per plugin set with `backend: experiential` in `plugins.yaml`.

## Goals
- `ExperientialModelPlugin` (`Name = "experiential"`): model fixed at startup from `EXPERIENTIAL_LABS_MODEL`; no runtime switching (does not register `SwitchModelRequestEvent`, no catalog prompt section).
- `Idempotency-Key` header: one uuid per `complete()` call, reused across stream-timeout retries of that call, new for the next call.
- Timeouts identical to OpenRouter (stream 10/15/30s, blocking 30s).
- Catalog in DuckDB gets a `provider` column (`openrouter`, `experiential`, ...). Primary key becomes `(provider, slug)`; the table is rebuilt on migration (it is a re-syncable cache). `save_model_catalog` replaces only the given provider's rows; lookups take a provider filter. Existing rows migrate to `provider = 'openrouter'` if cheap, otherwise dropped and re-synced.
- Provider list is hardcoded in code (a `CATALOG_PROVIDERS` tuple/registry in `conic/catalog/`-style module, e.g. `("openrouter", "experiential")`), not an env var. Adding a provider means adding a fetcher entry there. Catalog loading stays eager exactly as today: `entry.py` syncs every listed provider at startup (when its table slice is empty) and re-syncs periodically; no lazy loading. Each provider has its own fetcher producing `ModelCatalogEntry`. The existing OpenRouter fetch request is left untouched. Experiential fetcher: keyless `GET /api/models`, mapping `model.slug`, `display_name`, `context_window`, `description`, modalities, `supported_params`, providers' pricing.
- `OpenRouterModelPlugin` lookups filter `provider = 'openrouter'` (so Experiential slugs never appear in `switch_model`). `ExperientialModelPlugin` reads `model_context_length` via a `provider = 'experiential'` lookup; not found falls back to `DEFAULT_MODEL_CONTEXT_LENGTH`.
- Config: `EXPERIENTIAL_LABS_API_KEY`, `EXPERIENTIAL_LABS_MODEL`, optional at load; required (PluginConfigError) only when a plugin set selects this backend.

## Non-goals
- Runtime model switching for the Experiential plugin; changing the existing OpenRouter catalog request.
- Own 429/5xx backoff (OpenAI SDK retries already apply).
- `provider.ignore`, OpenRouter referer/title headers.

## Approaches
1. Standalone copy of the plugin — rejected (duplicates ~100 lines of streaming/timeout logic).
2. **Chosen:** extract a shared base (`plugins/models/base.py`: `_complete_blocking`, `_complete_streaming`, `_stream_once`, usage recording) with hook methods `_extra_body()`, `_extra_headers(idempotency_key)`; `OpenRouterModelPlugin` and `ExperientialModelPlugin` subclass it. OpenRouter behavior must stay unchanged (existing tests are the guard).

## Left to the builder
Base class name, how the base threads the idempotency key into headers, where the hardcoded provider registry lives, per-provider fetcher (injected for tests), the migration SQL in `queries.py`, registry wiring (`BACKEND_BUILDERS`), tests for: key stable across retries / differs across calls, no switch handler registered, provider-scoped save/find (saving one provider keeps the other's rows), context length from catalog and fallback, missing-config error.
