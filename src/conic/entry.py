import asyncio
import contextlib
import sys

from loguru import logger

from conic.config import load_config
from conic.core.manager import PluginManager
from conic.discord.gateway import DiscordGateway
from conic.openrouter.catalog import run_periodic_sync, sync_catalog_once
from conic.plugins.plugin_config import PluginConfigError
from conic.plugins.registry import build_plugin_set
from conic.services.storage import StorageService


async def build_app(
    env: dict[str, str] | None = None, sync_once=sync_catalog_once
) -> tuple[StorageService, DiscordGateway]:
    config = load_config(env)
    logger.remove()
    logger.add(sys.stderr, level=config.log_level)

    logger.info("starting conic (model={})", config.openrouter_model)
    storage = StorageService(config.duckdb_path, config.workspace_root, config.openrouter_model)
    storage.startup()
    logger.info("storage started (db={})", config.duckdb_path)
    # Populate the model catalog before reading model metadata, otherwise
    # get_model_context_length falls back to DEFAULT_MODEL_CONTEXT_LENGTH and
    # the wrong value gets baked into the session's global variables.
    await sync_once(storage, config.openrouter_api_key)
    model_context_length = storage.get_model_context_length(config.openrouter_model)
    plugin_sets = build_plugin_set(config, global_variables={"model_context_length": model_context_length})
    if "main" not in plugin_sets:
        raise PluginConfigError(f"plugins config at {config.plugins_config_path} must define a 'main' plugin set")
    plugin_manager = PluginManager(storage, plugin_sets)
    gateway = DiscordGateway(config, plugin_manager, storage)
    return storage, gateway


async def main() -> None:
    storage, gateway = await build_app()
    config = load_config()
    catalog_sync_task = asyncio.create_task(run_periodic_sync(storage, config.openrouter_api_key))
    try:
        await gateway.start()
    finally:
        logger.info("shutting down storage")
        catalog_sync_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await catalog_sync_task
        storage.shutdown()
