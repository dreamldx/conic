import asyncio
import contextlib
import sys
from functools import partial

from loguru import logger

from conic.catalog import CATALOG_PROVIDERS, CatalogProvider, run_periodic_sync
from conic.config import load_config
from conic.core.manager import PluginManager
from conic.discord.cleanup import run_periodic_cleanup
from conic.discord.gateway import DiscordGateway
from conic.plugins.plugin_config import PluginConfigError, load_plugins_config
from conic.plugins.registry import backend_model, build_plugin_set
from conic.services.storage import StorageService

STARTUP_SYNC_TIMEOUT = 15.0


async def _sync_provider(provider: CatalogProvider, storage: StorageService, config) -> None:
    try:
        await provider.sync(storage, config)
    except Exception as exc:
        logger.warning("startup catalog sync failed for {}: {}", provider.name, exc)


async def _sync_empty_catalogs(
    providers: tuple[CatalogProvider, ...], storage: StorageService, config, timeout: float
) -> None:
    pending = {
        asyncio.create_task(_sync_provider(provider, storage, config)): provider.name
        for provider in providers
        if storage.model_catalog_is_empty(provider.name)
    }
    if not pending:
        return
    _done, unfinished = await asyncio.wait(pending, timeout=timeout)
    for task in unfinished:
        task.cancel()
    if unfinished:
        await asyncio.gather(*unfinished, return_exceptions=True)
        logger.warning(
            "startup catalog sync timed out after {}s for {}; using existing catalog",
            timeout,
            ", ".join(sorted(pending[task] for task in unfinished)),
        )


async def build_app(
    env: dict[str, str] | None = None,
    providers: tuple[CatalogProvider, ...] = CATALOG_PROVIDERS,
    startup_sync_timeout: float = STARTUP_SYNC_TIMEOUT,
) -> tuple[StorageService, DiscordGateway]:
    config = load_config(env)
    logger.remove()
    logger.add(sys.stderr, level=config.log_level)

    main_set = load_plugins_config(config.plugins_config_path).get("main")
    default_model = backend_model(main_set.backend, config) if main_set else config.openrouter_model
    logger.info("starting conic (model={})", default_model)
    storage = StorageService(config.duckdb_path, config.workspace_root, default_model)
    storage.startup()
    logger.info("storage started (db={})", config.duckdb_path)
    await _sync_empty_catalogs(providers, storage, config, startup_sync_timeout)
    plugin_sets = build_plugin_set(config, catalog_lookup=storage.find_model_catalog_entries)
    if "main" not in plugin_sets:
        raise PluginConfigError(f"plugins config at {config.plugins_config_path} must define a 'main' plugin set")
    plugin_manager = PluginManager(storage, plugin_sets)
    gateway = DiscordGateway(config, plugin_manager, storage)
    return storage, gateway


async def main() -> None:
    storage, gateway = await build_app()
    config = load_config()
    catalog_sync_tasks = [
        asyncio.create_task(run_periodic_sync(partial(provider.sync, storage, config)))
        for provider in CATALOG_PROVIDERS
    ]
    cleanup_task = asyncio.create_task(run_periodic_cleanup(gateway.sweep_stale_sessions))
    try:
        await gateway.start()
    finally:
        logger.info("shutting down storage")
        for task in (*catalog_sync_tasks, cleanup_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        storage.shutdown()
