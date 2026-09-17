from conic.entry import build_app


def test_build_app_wires_storage_and_gateway_without_connecting(tmp_path):
    env = {
        "PROJECT_ROOT": str(tmp_path),
        "DISCORD_BOT_TOKEN": "d-token",
        "OPENROUTER_API_KEY": "or-key",
        "DUCKDB_PATH": str(tmp_path / "conic.duckdb"),
        "WORKSPACE_ROOT": str(tmp_path / "workspace"),
    }
    storage, gateway = build_app(env)
    try:
        assert gateway.name == "discord"
        row = storage.get_or_create(channel="discord", native_id="smoke-test")
        assert row.status == "active"
        # app name discovery is wired end-to-end (holder shared with the
        # backend plugin factory) without requiring a live Discord connection
        assert gateway._app_name_holder == {"name": None}
    finally:
        storage.shutdown()
