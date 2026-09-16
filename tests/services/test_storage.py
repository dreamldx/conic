from conic.services.storage import StorageService


def make_storage(tmp_path):
    storage = StorageService(
        db_path=str(tmp_path / "conic.duckdb"),
        workspace_root=str(tmp_path / "workspace"),
        default_model="test-model",
    )
    storage.startup()
    return storage


def test_get_or_create_creates_new_session_with_workspace_dir(tmp_path):
    storage = make_storage(tmp_path)
    row = storage.get_or_create(channel="discord", native_id="123")
    assert row.session_key == "discord:123"
    assert row.channel == "discord"
    assert row.native_id == "123"
    assert row.model == "test-model"
    assert row.status == "active"
    assert row.variables == {}
    assert (tmp_path / "workspace" / "discord" / "123").is_dir()
    storage.shutdown()


def test_save_variables_and_reload_round_trip(tmp_path):
    storage = make_storage(tmp_path)
    row = storage.get_or_create(channel="discord", native_id="123")
    storage.handle_for(row).save_variables({"tokens_used": 42, "workspace_dir": "/tmp/ws"})

    reloaded = storage.get_or_create(channel="discord", native_id="123")
    assert reloaded.variables == {"tokens_used": 42, "workspace_dir": "/tmp/ws"}
    storage.shutdown()


def test_saved_variables_persist_across_reconnect(tmp_path):
    storage = make_storage(tmp_path)
    row = storage.get_or_create(channel="discord", native_id="123")
    storage.handle_for(row).save_variables({"tokens_used": 7})
    storage.shutdown()

    reopened = StorageService(
        db_path=str(tmp_path / "conic.duckdb"),
        workspace_root=str(tmp_path / "workspace"),
        default_model="test-model",
    )
    reopened.startup()
    reloaded_row = reopened.get_or_create(channel="discord", native_id="123")
    assert reloaded_row.variables == {"tokens_used": 7}
    reopened.shutdown()


def test_get_or_create_is_idempotent(tmp_path):
    storage = make_storage(tmp_path)
    first = storage.get_or_create(channel="discord", native_id="123")
    second = storage.get_or_create(channel="discord", native_id="123")
    assert first == second
    storage.shutdown()


def test_append_and_load_history_round_trip(tmp_path):
    storage = make_storage(tmp_path)
    row = storage.get_or_create(channel="discord", native_id="123")
    handle = storage.handle_for(row)
    handle.append_message({"role": "user", "content": "hi"})
    handle.append_message({"role": "assistant", "content": "hello"})
    history = handle.load_history()
    assert history == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    storage.shutdown()


def test_set_status_updates_row(tmp_path):
    storage = make_storage(tmp_path)
    row = storage.get_or_create(channel="discord", native_id="123")
    storage.handle_for(row).set_status("ended")
    reloaded = storage.get_or_create(channel="discord", native_id="123")
    assert reloaded.status == "ended"
    storage.shutdown()


def test_active_sessions_filters_by_channel_and_status(tmp_path):
    storage = make_storage(tmp_path)
    active = storage.get_or_create(channel="discord", native_id="1")
    ended = storage.get_or_create(channel="discord", native_id="2")
    storage.get_or_create(channel="slack", native_id="3")
    storage.handle_for(ended).set_status("ended")

    rows = storage.active_sessions(channel="discord")
    assert [r.native_id for r in rows] == ["1"]
    assert rows[0].session_key == active.session_key
    storage.shutdown()


def test_persists_across_reconnect(tmp_path):
    storage = make_storage(tmp_path)
    row = storage.get_or_create(channel="discord", native_id="123")
    storage.handle_for(row).append_message({"role": "user", "content": "hi"})
    storage.shutdown()

    reopened = StorageService(
        db_path=str(tmp_path / "conic.duckdb"),
        workspace_root=str(tmp_path / "workspace"),
        default_model="test-model",
    )
    reopened.startup()
    reloaded_row = reopened.get_or_create(channel="discord", native_id="123")
    history = reopened.handle_for(reloaded_row).load_history()
    assert history == [{"role": "user", "content": "hi"}]
    reopened.shutdown()
