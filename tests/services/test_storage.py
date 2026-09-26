from datetime import UTC

from conic.services.models import ModelCatalogEntry
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
    assert row.created_at.tzinfo is UTC
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


def test_naive_stored_session_time_is_loaded_as_utc(tmp_path):
    storage = make_storage(tmp_path)
    row = storage.get_or_create(channel="discord", native_id="123")
    storage._conn.execute(
        "update sessions set created_at = ? where session_key = ?",
        ["2026-09-19T12:00:00", row.session_key],
    )
    reloaded = storage.get_or_create(channel="discord", native_id="123")
    assert reloaded.created_at.tzinfo is UTC
    assert reloaded.created_at.isoformat() == "2026-09-19T12:00:00+00:00"
    storage.shutdown()


def test_append_and_load_history_round_trip(tmp_path):
    storage = make_storage(tmp_path)
    row = storage.get_or_create(channel="discord", native_id="123")
    handle = storage.handle_for(row)
    handle.append_message({"role": "user", "content": "hi"}, turn_id=1)
    handle.append_message({"role": "assistant", "content": "hello"}, turn_id=1)
    history = handle.load_history()
    assert history == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    storage.shutdown()


def test_load_history_does_not_leak_turn_id_into_message_content(tmp_path):
    storage = make_storage(tmp_path)
    row = storage.get_or_create(channel="discord", native_id="123")
    handle = storage.handle_for(row)
    handle.append_message({"role": "user", "content": "hi"}, turn_id=7)
    history = handle.load_history()
    assert "turn_id" not in history[0]
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
    storage.handle_for(row).append_message({"role": "user", "content": "hi"}, turn_id=1)
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


def test_save_and_list_model_catalog_round_trip(tmp_path):
    storage = make_storage(tmp_path)
    storage.save_model_catalog([
        ModelCatalogEntry(
            slug="deepseek/deepseek-v4-flash-0731",
            vendor="deepseek",
            real_model="deepseek/deepseek-v4-flash-0731",
            name="DeepSeek: DeepSeek V4 Flash 0731",
            description="A sparse mixture-of-experts model.",
            context_length=128000,
            supports_tools=True,
            pricing_prompt=0.00000004,
            pricing_completion=0.00000064,
            input_modalities=["text"],
            output_modalities=["text"],
            supported_parameters=["tools", "reasoning"],
        ),
        ModelCatalogEntry(slug="openai/gpt-audio", context_length=32000, supports_tools=False),
    ])

    entries = storage.list_model_catalog()

    assert [e.slug for e in entries] == ["deepseek/deepseek-v4-flash-0731", "openai/gpt-audio"]
    assert entries[0].vendor == "deepseek"
    assert entries[0].real_model == "deepseek/deepseek-v4-flash-0731"
    assert entries[0].name == "DeepSeek: DeepSeek V4 Flash 0731"
    assert entries[0].description == "A sparse mixture-of-experts model."
    assert entries[0].context_length == 128000
    assert entries[0].supports_tools is True
    assert entries[0].pricing_prompt == 0.00000004
    assert entries[0].pricing_completion == 0.00000064
    assert entries[0].input_modalities == ["text"]
    assert entries[0].output_modalities == ["text"]
    assert entries[0].supported_parameters == ["tools", "reasoning"]
    assert entries[1].supports_tools is False
    assert entries[1].input_modalities == []
    storage.shutdown()


def test_save_model_catalog_replaces_previous_entries(tmp_path):
    storage = make_storage(tmp_path)
    storage.save_model_catalog([ModelCatalogEntry(slug="old/model", context_length=1000)])
    storage.save_model_catalog([ModelCatalogEntry(slug="new/model", context_length=2000)])

    entries = storage.list_model_catalog()

    assert [e.slug for e in entries] == ["new/model"]
    storage.shutdown()


def test_model_catalog_persists_across_reconnect(tmp_path):
    storage = make_storage(tmp_path)
    storage.save_model_catalog([ModelCatalogEntry(slug="deepseek/deepseek-v4-flash-0731", context_length=128000)])
    storage.shutdown()

    reopened = StorageService(
        db_path=str(tmp_path / "conic.duckdb"),
        workspace_root=str(tmp_path / "workspace"),
        default_model="test-model",
    )
    reopened.startup()
    entries = reopened.list_model_catalog()
    assert [e.slug for e in entries] == ["deepseek/deepseek-v4-flash-0731"]
    reopened.shutdown()


def test_get_model_context_length_returns_the_matching_entrys_value(tmp_path):
    storage = make_storage(tmp_path)
    storage.save_model_catalog([
        ModelCatalogEntry(slug="deepseek/deepseek-v4-flash-0731", context_length=1310720),
        ModelCatalogEntry(slug="openai/gpt-audio", context_length=32000),
    ])

    assert storage.get_model_context_length("deepseek/deepseek-v4-flash-0731") == 1310720
    assert storage.get_model_context_length("openai/gpt-audio") == 32000
    storage.shutdown()


def test_get_model_context_length_defaults_to_65535_when_model_not_in_catalog(tmp_path):
    storage = make_storage(tmp_path)
    assert storage.get_model_context_length("some/unknown-model") == 65535
    storage.shutdown()


def test_startup_drops_legacy_model_catalog_table_with_id_column(tmp_path):
    import duckdb

    db_path = tmp_path / "conic.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE TABLE model_catalog (id VARCHAR PRIMARY KEY, context_length INTEGER)")
    conn.execute("INSERT INTO model_catalog VALUES ('old/model', 1000)")
    conn.close()

    storage = make_storage(tmp_path)
    storage.save_model_catalog([ModelCatalogEntry(slug="new/model", vendor="new", real_model="new/model")])

    assert [e.slug for e in storage.list_model_catalog()] == ["new/model"]
    storage.shutdown()


def test_find_model_catalog_entries_matches_slug_or_model_name(tmp_path):
    storage = make_storage(tmp_path)
    storage.save_model_catalog([
        ModelCatalogEntry(slug="deepseek/deepseek-v4-flash", vendor="deepseek"),
        ModelCatalogEntry(slug="deepseek/deepseek-v4-flash-0731", vendor="deepseek"),
        ModelCatalogEntry(slug="~deepseek/deepseek-v4-flash-latest", vendor="deepseek"),
        ModelCatalogEntry(slug="~deepseek/deepseek-flash-latest", vendor="deepseek"),
        ModelCatalogEntry(slug="deepseek/deepseek-r1", vendor="deepseek"),
        ModelCatalogEntry(slug="deepseek/deepseek-r1:free", vendor="deepseek"),
    ])

    def slugs(query):
        return [e.slug for e in storage.find_model_catalog_entries(query)]

    assert slugs("deepseek-v4-flash") == ["deepseek/deepseek-v4-flash"]
    assert slugs("deepseek/deepseek-v4-flash-0731") == ["deepseek/deepseek-v4-flash-0731"]
    assert slugs("deepseek-v4-flash-0731") == ["deepseek/deepseek-v4-flash-0731"]
    assert slugs("deepseek-v4-flash-latest") == ["~deepseek/deepseek-v4-flash-latest"]
    assert slugs("~deepseek/deepseek-flash-latest") == ["~deepseek/deepseek-flash-latest"]
    assert slugs("  DeepSeek-V4-Flash  ") == ["deepseek/deepseek-v4-flash"]
    assert slugs("deepseek-r1") == ["deepseek/deepseek-r1"]
    assert slugs("deepseek-r1:free") == ["deepseek/deepseek-r1:free"]
    assert slugs("nope") == []
    storage.shutdown()
