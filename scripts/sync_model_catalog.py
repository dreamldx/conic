import asyncio

from conic.config import load_config
from conic.openrouter.catalog import fetch_openrouter_models
from conic.services.models import ModelCatalogEntry
from conic.services.storage import StorageService


async def main() -> None:
    config = load_config()
    raw_models = await fetch_openrouter_models(config.openrouter_api_key)
    entries = [ModelCatalogEntry(**m) for m in raw_models]

    storage = StorageService(config.duckdb_path, config.workspace_root, config.openrouter_model)
    storage.startup()
    try:
        storage.save_model_catalog(entries)
    finally:
        storage.shutdown()

    print(f"saved {len(entries)} models to {config.duckdb_path}")


if __name__ == "__main__":
    asyncio.run(main())
