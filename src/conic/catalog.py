import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from conic.config import Config
from conic.experiential.catalog import EXPERIENTIAL_PROVIDER, fetch_experiential_models
from conic.openrouter.catalog import MODEL_CATALOG_FILENAME, sync_catalog_once
from conic.services.storage import StorageService

DEFAULT_SYNC_INTERVAL_SECONDS = 3600


@dataclass(frozen=True)
class CatalogProvider:
    name: str
    sync: Callable[[StorageService, Config], Awaitable[bool]]


async def sync_openrouter_once(storage: StorageService, config: Config) -> bool:
    json_path = Path(config.project_root) / "data" / MODEL_CATALOG_FILENAME
    return await sync_catalog_once(storage, config.openrouter_api_key, json_path=json_path)


async def sync_experiential_once(storage: StorageService, config: Config) -> bool:
    try:
        entries = await fetch_experiential_models()
        if not entries:
            logger.warning("experiential model catalog fetch returned no models; keeping existing catalog")
            return False
        storage.save_model_catalog(EXPERIENTIAL_PROVIDER, entries)
        logger.info("synced {} models from experiential", len(entries))
        return True
    except Exception as exc:
        logger.warning("failed to sync experiential model catalog: {}", exc)
        return False


CATALOG_PROVIDERS: tuple[CatalogProvider, ...] = (
    CatalogProvider(name="openrouter", sync=sync_openrouter_once),
    CatalogProvider(name=EXPERIENTIAL_PROVIDER, sync=sync_experiential_once),
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
