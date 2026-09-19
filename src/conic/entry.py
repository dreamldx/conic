import sys

from loguru import logger

from conic.config import load_config
from conic.core.manager import PluginManager
from conic.discord.gateway import DiscordGateway
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
    plugin_manager = PluginManager(storage, build_plugin_set(config))
    gateway = DiscordGateway(config, plugin_manager, storage)
    return storage, gateway


async def main() -> None:
    storage, gateway = build_app()
    try:
        await gateway.start()
    finally:
        logger.info("shutting down storage")
        storage.shutdown()
