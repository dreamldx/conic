# Nano-GPT and Venice model backends (short design)

## Intent
Add Nano-GPT and Venice (both OpenAI-compatible) as selectable model backends next to OpenRouter and Experiential. Selected per plugin set with `backend: nano_gpt` / `backend: venice` in `plugins.yaml`. Same shape as the Experiential backend: shared `ChatCompletionModelPlugin` base, model fixed at startup, no runtime switching.

## Goals
- `NanoGptModelPlugin` (`Name = "nano_gpt"`, base URL `https://api.nano-gpt.com/api/v1`) and `VeniceModelPlugin` (`Name = "venice"`, base URL `https://api.venice.ai/api/v1`), each a thin subclass of the base. No `SwitchModelRequestEvent` handler, no catalog prompt section.
- Timeouts identical to OpenRouter (stream 10/15/30s, blocking 30s), inherited from the base.
- `Idempotency-Key` header sent exactly as Experiential does (one uuid per `complete()`, reused across stream-timeout retries). **Neither provider documents `Idempotency-Key`**, and Nano-GPT's docs say its `X-Request-ID` is a correlation id, not an idempotency key. The header is sent for consistency and is harmless if ignored, but **retries have no server-side deduplication guarantee on these two backends**: a timed-out request that was actually processed can be billed twice. This limitation is recorded here (source carries no comments).
- Venice: every request sets `venice_parameters.include_venice_system_prompt = false` through `_extra_body()` so Venice does not inject its own system prompt. The docs do not state the effect of `false` explicitly (inferred from the field name); the test pins the request body, not Venice's behavior.
- Nano-GPT: the configured model id is passed verbatim. Model-name suffixes (`:online`, `:memory`, ...) are a user choice inside `NANO_GPT_MODEL`; the plugin adds none.
- Catalogs: add two providers to the hardcoded `CATALOG_PROVIDERS` (`nano_gpt`, `venice`), each with its own fetcher producing `ModelCatalogEntry` and its own periodic sync; eager startup sync when the provider's slice is empty, exactly as today. Empty fetch or failure keeps existing rows.
  - Nano-GPT: `GET https://api.nano-gpt.com/api/v1/models?detailed=true`, keyless, not paginated. Map `id` to slug, `name`, `description`, `context_length` (null -> skip or default), `pricing.prompt` / `pricing.completion` (USD per million tokens), modalities from `capabilities` flags (`vision`, `video_input`, `audio_input`).
  - Venice: `GET https://api.venice.ai/api/v1/models`, keyless in testing, only entries with `type == "text"`, skipping `model_spec.offline == true`. Map `id`, `model_spec.name`, `model_spec.description`, `model_spec.availableContextTokens` (fall back to `context_length`), `model_spec.pricing.input.usd` / `output.usd` (USD per million tokens), modalities from `model_spec.capabilities` (`supportsVision`, `supportsAudioInput`, `supportsVideoInput`).
  - Unit conversion goes to whatever per-token unit `ModelCatalogEntry` already uses (see the Experiential fetcher).
- Config: `NANO_GPT_API_KEY`, `NANO_GPT_MODEL`, `VENICE_API_KEY`, `VENICE_MODEL`, optional at load, required (`PluginConfigError`) only when a plugin set selects that backend. `registry.backend_model` returns the matching model so the startup log and `session.model` follow the main set.
- Context length: each plugin reads `model_context_length` via a provider-scoped catalog lookup; not found falls back to `DEFAULT_MODEL_CONTEXT_LENGTH`.

## Non-goals
- Runtime model switching; web search / memory / reasoning-effort options beyond what the model string or base already carries.
- Own 429/5xx backoff (OpenAI SDK retries already apply).
- Handling Nano-GPT "decision" models (reject streaming) and `x_nanogpt_pricing` / Venice `cost` response fields.
- Changing OpenRouter or Experiential behavior.

## Approaches
1. Two more thin subclasses of the existing base, two more fetchers, two more `CATALOG_PROVIDERS` entries, two more `BACKEND_BUILDERS` entries. **Chosen** (user: keep consistent with Experiential).
2. Generic "OpenAI-compatible provider" plugin parameterized by name/base URL/headers/body. Rejected for now: three providers do not yet justify a config-driven abstraction, and Venice needs a body hook while the others do not; revisit if a fourth appears.

## Left to the builder
Whether the registry's Experiential builder is generalized into a shared helper for the three keyed backends, the exact `ModelCatalogEntry` field mapping and unit conversion, fetcher injection for tests, handling of entries with missing price/context, tests for: Idempotency-Key stable across retries / differs across calls on both plugins, Venice request body carries `include_venice_system_prompt=false` (blocking and streaming), no switch handler registered, provider-scoped catalog save for both providers, missing-config errors, `backend_model` for both, fetcher filtering (Venice non-text / offline, Nano-GPT null context).
