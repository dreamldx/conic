import asyncio

from conic.entry import build_app


async def _noop_sync_once(storage, api_key, session_factory=None, json_path=None):
    return False


def test_build_app_wires_storage_and_gateway_without_connecting(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "plugins.yaml").write_text("main: {}\n", encoding="utf-8")
    env = {
        "PROJECT_ROOT": str(tmp_path),
        "DISCORD_BOT_TOKEN": "d-token",
        "OPENROUTER_API_KEY": "or-key",
        "DUCKDB_PATH": str(tmp_path / "conic.duckdb"),
        "WORKSPACE_ROOT": str(tmp_path / "workspace"),
    }

    async def _build():
        return await build_app(env, sync_once=_noop_sync_once)

    storage, gateway = asyncio.run(_build())
    try:
        assert gateway.name == "discord"
        row = storage.get_or_create(channel="discord", native_id="smoke-test")
        assert row.status == "active"
    finally:
        storage.shutdown()
