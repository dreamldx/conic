import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from conic.config import Config
from conic.experiential.catalog import EXPERIENTIAL_PROVIDER, fetch_experiential_models
from conic.nano_gpt.catalog import NANO_GPT_PROVIDER, fetch_nano_gpt_models
from conic.openrouter.catalog import MODEL_CATALOG_FILENAME, sync_catalog_once
from conic.services.models import ModelCatalogEntry
from conic.services.storage import StorageService
from conic.venice.catalog import VENICE_PROVIDER, fetch_venice_models

DEFAULT_SYNC_INTERVAL_SECONDS = 3600


@dataclass(frozen=True)
class CatalogProvider:
    name: str
    sync: Callable[[StorageService, Config], Awaitable[bool]]


async def sync_openrouter_once(storage: StorageService, config: Config) -> bool:
    json_path = Path(config.project_root) / "data" / MODEL_CATALOG_FILENAME
    return await sync_catalog_once(storage, config.openrouter_api_key, json_path=json_path)


async def _sync_provider(
    storage: StorageService,
    provider: str,
    fetch: Callable[[], Awaitable[list[ModelCatalogEntry]]],
) -> bool:
    try:
        entries = await fetch()
        if not entries:
            logger.warning("{} model catalog fetch returned no models; keeping existing catalog", provider)
            return False
        storage.save_model_catalog(provider, entries)
        logger.info("synced {} models from {}", len(entries), provider)
        return True
    except Exception as exc:
        logger.warning("failed to sync {} model catalog: {}", provider, exc)
        return False


async def sync_experiential_once(storage: StorageService, config: Config) -> bool:
    return await _sync_provider(storage, EXPERIENTIAL_PROVIDER, lambda: fetch_experiential_models())


async def sync_nano_gpt_once(storage: StorageService, config: Config) -> bool:
    return await _sync_provider(storage, NANO_GPT_PROVIDER, lambda: fetch_nano_gpt_models())


async def sync_venice_once(storage: StorageService, config: Config) -> bool:
    return await _sync_provider(storage, VENICE_PROVIDER, lambda: fetch_venice_models())


CATALOG_PROVIDERS: tuple[CatalogProvider, ...] = (
    CatalogProvider(name="openrouter", sync=sync_openrouter_once),
    CatalogProvider(name=EXPERIENTIAL_PROVIDER, sync=sync_experiential_once),
    CatalogProvider(name=NANO_GPT_PROVIDER, sync=sync_nano_gpt_once),
    CatalogProvider(name=VENICE_PROVIDER, sync=sync_venice_once),
)


async def run_periodic_sync(
    sync: Callable[[], Awaitable[bool]],
    interval_seconds: float = DEFAULT_SYNC_INTERVAL_SECONDS,
    sleep=asyncio.sleep,
) -> None:
    while True:
        await sleep(interval_seconds)
        try:
            await sync()
        except Exception as exc:
            logger.warning("periodic catalog sync failed: {}", exc)
