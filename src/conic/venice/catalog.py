import aiohttp

from conic.services.models import ModelCatalogEntry, safe_float

VENICE_CATALOG_URL = "https://api.venice.ai/api/v1/models"
VENICE_PROVIDER = "venice"
TOKENS_PER_MILLION = 1e6


async def fetch_venice_models(
    timeout: float = 30, session_factory=aiohttp.ClientSession
) -> list[ModelCatalogEntry]:
    timeout_config = aiohttp.ClientTimeout(total=timeout)
    async with (
        session_factory(timeout=timeout_config) as session,
        session.get(VENICE_CATALOG_URL) as response,
    ):
        response.raise_for_status()
        body = await response.json()
    rows = body.get("data") or []
    return [e for e in (_parse_row(r) for r in rows) if e is not None]


def _usd_per_token(price: dict | None) -> float:
    return safe_float((price or {}).get("usd")) / TOKENS_PER_MILLION


def _parse_row(row: dict) -> ModelCatalogEntry | None:
    model_id = row.get("id")
    if not model_id or row.get("type") != "text":
        return None
    spec = row.get("model_spec") or {}
    if spec.get("offline"):
        return None
    capabilities = spec.get("capabilities") or {}
    supports_tools = bool(capabilities.get("supportsFunctionCalling"))
    supported = (["tools"] if supports_tools else []) + (
        ["reasoning"] if capabilities.get("supportsReasoning") else []
    )
    modalities = ["text"]
    for capability, modality in (
        ("supportsVision", "image"),
        ("supportsAudioInput", "audio"),
        ("supportsVideoInput", "video"),
    ):
        if capabilities.get(capability):
            modalities.append(modality)
    pricing = spec.get("pricing") or {}
    return ModelCatalogEntry(
        slug=model_id,
        provider=VENICE_PROVIDER,
        vendor=(row.get("owned_by") or "").lower(),
        real_model=model_id,
        name=spec.get("name") or "",
        description=spec.get("description") or "",
        context_length=spec.get("availableContextTokens") or row.get("context_length") or 0,
        supports_tools=supports_tools,
        pricing_prompt=_usd_per_token(pricing.get("input")),
        pricing_completion=_usd_per_token(pricing.get("output")),
        input_modalities=modalities,
        output_modalities=["text"],
        supported_parameters=supported,
    )
