import aiohttp

from conic.services.models import ModelCatalogEntry, safe_float

NANO_GPT_CATALOG_URL = "https://api.nano-gpt.com/api/v1/models"
NANO_GPT_PROVIDER = "nano_gpt"
TOKENS_PER_MILLION = 1e6


async def fetch_nano_gpt_models(
    timeout: float = 30, session_factory=aiohttp.ClientSession
) -> list[ModelCatalogEntry]:
    timeout_config = aiohttp.ClientTimeout(total=timeout)
    async with (
        session_factory(timeout=timeout_config) as session,
        session.get(NANO_GPT_CATALOG_URL, params={"detailed": "true"}) as response,
    ):
        response.raise_for_status()
        body = await response.json()
    rows = body.get("data") or []
    return [e for e in (_parse_row(r) for r in rows) if e is not None]


def _parse_row(row: dict) -> ModelCatalogEntry | None:
    model_id = row.get("id")
    if not model_id:
        return None
    capabilities = row.get("capabilities") or {}
    supports_tools = bool(capabilities.get("tool_calling"))
    supported = (["tools"] if supports_tools else []) + (["reasoning"] if capabilities.get("reasoning") else [])
    modalities = ["text"]
    for capability, modality in (("vision", "image"), ("video_input", "video"), ("audio_input", "audio")):
        if capabilities.get(capability):
            modalities.append(modality)
    pricing = row.get("pricing") or {}
    return ModelCatalogEntry(
        slug=model_id,
        provider=NANO_GPT_PROVIDER,
        vendor=model_id.partition("/")[0].lower() if "/" in model_id else "",
        real_model=model_id,
        name=row.get("name") or "",
        description=row.get("description") or "",
        context_length=row.get("context_length") or 0,
        supports_tools=supports_tools,
        pricing_prompt=safe_float(pricing.get("prompt")) / TOKENS_PER_MILLION,
        pricing_completion=safe_float(pricing.get("completion")) / TOKENS_PER_MILLION,
        input_modalities=modalities,
        output_modalities=["text"],
        supported_parameters=supported,
    )
