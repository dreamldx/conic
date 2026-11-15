import asyncio

from conic.catalog import CatalogProvider
from conic.entry import build_app


def fake_provider(name, calls=None, fail=False):
    async def sync(storage, config):
        if calls is not None:
            calls.append(name)
        if fail:
            raise RuntimeError("boom")
        return True

    return CatalogProvider(name=name, sync=sync)


def make_env(tmp_path):
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "plugins.yaml").write_text("main: {}\n", encoding="utf-8")
    return {
        "PROJECT_ROOT": str(tmp_path),
        "DISCORD_BOT_TOKEN": "d-token",
        "OPENROUTER_API_KEY": "or-key",
        "DUCKDB_PATH": str(tmp_path / "conic.duckdb"),
        "WORKSPACE_ROOT": str(tmp_path / "workspace"),
    }


def test_build_app_wires_storage_and_gateway_without_connecting(tmp_path):
    env = make_env(tmp_path)

    async def _build():
        return await build_app(env, providers=(fake_provider("a"),))

    storage, gateway = asyncio.run(_build())
    try:
        assert gateway.name == "discord"
        row = storage.get_or_create(channel="discord", native_id="smoke-test")
        assert row.status == "active"
    finally:
        storage.shutdown()


def test_build_app_syncs_only_providers_whose_catalog_is_empty(tmp_path):
    from conic.services.models import ModelCatalogEntry
    from conic.services.storage import StorageService

    env = make_env(tmp_path)
    seed = StorageService(env["DUCKDB_PATH"], env["WORKSPACE_ROOT"], "test-model")
    seed.startup()
    seed.save_model_catalog("seeded", [ModelCatalogEntry(slug="x/y", provider="seeded")])
    seed.shutdown()
    calls = []

    async def _build():
        return await build_app(env, providers=(fake_provider("seeded", calls), fake_provider("empty", calls)))

    storage, _gateway = asyncio.run(_build())
    try:
        assert calls == ["empty"]
    finally:
        storage.shutdown()


def test_build_app_boots_when_a_provider_sync_raises(tmp_path):
    env = make_env(tmp_path)
    calls = []

    async def _build():
        return await build_app(env, providers=(fake_provider("bad", calls, fail=True), fake_provider("good", calls)))

    storage, gateway = asyncio.run(_build())
    try:
        assert calls == ["bad", "good"]
        assert gateway.name == "discord"
    finally:
        storage.shutdown()
