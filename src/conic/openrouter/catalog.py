import asyncio

import aiohttp
from loguru import logger

from conic.services.models import ModelCatalogEntry

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
DEFAULT_SYNC_INTERVAL_SECONDS = 3600


async def fetch_openrouter_models(
    api_key: str, timeout: float = 30, session_factory=aiohttp.ClientSession
) -> list[dict]:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    timeout_config = aiohttp.ClientTimeout(total=timeout)
    async with session_factory(timeout=timeout_config) as session, session.get(
        OPENROUTER_MODELS_URL, headers=headers
    ) as response:
        response.raise_for_status()
        body = await response.json()
    return [_parse_model(m) for m in body.get("data", [])]


def _parse_model(m: dict) -> dict:
    pricing = m.get("pricing") or {}
    architecture = m.get("architecture") or {}
    supported_parameters = m.get("supported_parameters") or []
    return {
        "id": m["id"],
        "name": m.get("name") or "",
        "description": m.get("description") or "",
        "context_length": m.get("context_length") or 0,
        "supports_tools": "tools" in supported_parameters,
        "pricing_prompt": float(pricing.get("prompt") or 0),
        "pricing_completion": float(pricing.get("completion") or 0),
        "input_modalities": architecture.get("input_modalities") or [],
        "output_modalities": architecture.get("output_modalities") or [],
        "supported_parameters": supported_parameters,
    }


async def sync_catalog_once(
    storage,
    api_key: str,
    session_factory=aiohttp.ClientSession,
) -> bool:
    """Perform a single sync of the model catalog from OpenRouter.

    Returns True on success, False if the fetch or save failed.
    This is the primitive used by run_periodic_sync and by startup paths
    that need the catalog populated before querying model metadata."""
    try:
        raw_models = await fetch_openrouter_models(api_key, session_factory=session_factory)
        storage.save_model_catalog([ModelCatalogEntry(**m) for m in raw_models])
        logger.info("synced {} models from openrouter", len(raw_models))
        return True
    except Exception as exc:
        logger.warning("failed to sync openrouter model catalog: {}", exc)
        return False


async def run_periodic_sync(
    storage,
    api_key: str,
    interval_seconds: float = DEFAULT_SYNC_INTERVAL_SECONDS,
    session_factory=aiohttp.ClientSession,
    sleep=asyncio.sleep,
) -> None:
    """Sync the model catalog immediately, then again every interval_seconds
    -- runs forever until the enclosing task is cancelled (e.g. on shutdown).
    A failed sync is logged and skipped rather than killing the loop, so one
    bad request doesn't stop future retries."""
    while True:
        await sync_catalog_once(storage, api_key, session_factory=session_factory)
        await sleep(interval_seconds)
