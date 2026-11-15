import aiohttp

from conic.services.models import ModelCatalogEntry

EXPERIENTIAL_CATALOG_URL = "https://api.experientiallabs.ai/api/models"
EXPERIENTIAL_PROVIDER = "experiential"
PAGE_SIZE = 500
NANO_USD_PER_MILLION_TO_USD_PER_TOKEN = 1e15


async def fetch_experiential_models(
    timeout: float = 30, session_factory=aiohttp.ClientSession
) -> list[ModelCatalogEntry]:
    timeout_config = aiohttp.ClientTimeout(total=timeout)
    entries: list[ModelCatalogEntry] = []
    offset = 0
    async with session_factory(timeout=timeout_config) as session:
        while True:
            async with session.get(
                EXPERIENTIAL_CATALOG_URL, params={"limit": PAGE_SIZE, "offset": offset}
            ) as response:
                response.raise_for_status()
                body = await response.json()
            rows = body.get("models") or []
            if not rows:
                break
            entries.extend(e for e in (_parse_row(r) for r in rows) if e is not None)
            offset += len(rows)
            total = body.get("total")
            if total is not None and offset >= total:
                break
    return entries


def _parse_row(row: dict) -> ModelCatalogEntry | None:
    model = row.get("model") or {}
    if model.get("status") != "active" or not model.get("slug"):
        return None
    supported = [k for k, v in (model.get("supported_params") or {}).items() if v]
    prompt, completion = _pricing(row.get("providers") or [])
    slug = model["slug"]
    return ModelCatalogEntry(
        slug=slug,
        provider=EXPERIENTIAL_PROVIDER,
        vendor=(model.get("maker") or "").lower(),
        real_model=slug,
        name=model.get("display_name") or "",
        description=model.get("description") or "",
        context_length=model.get("context_window") or 0,
        supports_tools="tools" in supported,
        pricing_prompt=prompt,
        pricing_completion=completion,
        input_modalities=model.get("input_modalities") or [],
        output_modalities=model.get("output_modalities") or [],
        supported_parameters=supported,
    )


def _pricing(providers: list[dict]) -> tuple[float, float]:
    for p in providers:
        prompt = p.get("input_nano_usd_per_million")
        completion = p.get("output_nano_usd_per_million")
        if prompt is not None or completion is not None:
            return (
                float(prompt or 0) / NANO_USD_PER_MILLION_TO_USD_PER_TOKEN,
                float(completion or 0) / NANO_USD_PER_MILLION_TO_USD_PER_TOKEN,
            )
    return 0.0, 0.0
