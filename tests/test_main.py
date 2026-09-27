import asyncio

from conic.entry import build_app


async def _noop_sync_once(storage, api_key, session_factory=None, json_path=None):
    return False


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
        return await build_app(env, sync_once=_noop_sync_once)

    storage, gateway = asyncio.run(_build())
    try:
        assert gateway.name == "discord"
        row = storage.get_or_create(channel="discord", native_id="smoke-test")
        assert row.status == "active"
    finally:
        storage.shutdown()


def test_build_app_syncs_once_when_the_catalog_is_empty(tmp_path):
    env = make_env(tmp_path)
    calls = []

    async def _tracking_sync_once(storage, api_key, session_factory=None, json_path=None):
        calls.append(1)
        return False

    async def _build():
        return await build_app(env, sync_once=_tracking_sync_once)

    storage, _gateway = asyncio.run(_build())
    try:
        assert calls == [1]
    finally:
        storage.shutdown()


def test_build_app_skips_the_sync_when_the_catalog_already_has_entries(tmp_path):
    from conic.services.models import ModelCatalogEntry
    from conic.services.storage import StorageService

    env = make_env(tmp_path)
    seed = StorageService(env["DUCKDB_PATH"], env["WORKSPACE_ROOT"], "test-model")
    seed.startup()
    seed.save_model_catalog([ModelCatalogEntry(slug="openai/gpt-audio")])
    seed.shutdown()
    calls = []

    async def _tracking_sync_once(storage, api_key, session_factory=None, json_path=None):
        calls.append(1)
        return False

    async def _build():
        return await build_app(env, sync_once=_tracking_sync_once)

    storage, _gateway = asyncio.run(_build())
    try:
        assert calls == []
    finally:
        storage.shutdown()
