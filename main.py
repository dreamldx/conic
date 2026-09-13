import asyncio

from conic.config import load_config
from conic.core.manager import PluginManager
from conic.plugins.channels.discord.gateway import DiscordGateway
from conic.plugins.registry import build_plugin_set
from conic.services.storage import StorageService


def build_app(env: dict[str, str] | None = None) -> tuple[StorageService, DiscordGateway]:
    config = load_config(env)
    storage = StorageService(config.duckdb_path, config.workspace_root, config.openrouter_model)
    storage.startup()
    plugin_manager = PluginManager(storage, build_plugin_set(config))
    gateway = DiscordGateway(config.discord_bot_token, plugin_manager, storage)
    return storage, gateway


async def main() -> None:
    storage, gateway = build_app()
    try:
        await gateway.start()
    finally:
        storage.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
