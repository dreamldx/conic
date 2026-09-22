import asyncio
import contextlib
import sys

from loguru import logger

from conic.config import load_config
from conic.core.manager import PluginManager
from conic.discord.gateway import DiscordGateway
from conic.openrouter.catalog import run_periodic_sync
from conic.plugins.registry import build_plugin_set
from conic.services.storage import StorageService


def build_app(env: dict[str, str] | None = None) -> tuple[StorageService, DiscordGateway]:
    config = load_config(env)
    logger.remove()
    logger.add(sys.stderr, level=config.log_level)

    logger.info("starting conic (model={})", config.openrouter_model)
    storage = StorageService(config.duckdb_path, config.workspace_root, config.openrouter_model)
    storage.startup()
    logger.info("storage started (db={})", config.duckdb_path)
    model_context_length = storage.get_model_context_length(config.openrouter_model)
    plugin_manager = PluginManager(
        storage, build_plugin_set(config, global_variables={"model_context_length": model_context_length})
    )
    gateway = DiscordGateway(config, plugin_manager, storage)
    return storage, gateway


async def main() -> None:
    storage, gateway = build_app()
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
