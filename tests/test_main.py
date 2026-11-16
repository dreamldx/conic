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


def test_build_app_uses_the_main_backends_model_for_new_sessions(tmp_path):
    env = make_env(tmp_path)
    (tmp_path / "config" / "plugins.yaml").write_text("main: {backend: experiential}\n", encoding="utf-8")
    env["EXPERIENTIAL_LABS_API_KEY"] = "xpl-key"
    env["EXPERIENTIAL_LABS_MODEL"] = "claude-opus-5.5"

    async def _build():
        return await build_app(env, providers=())

    storage, _gateway = asyncio.run(_build())
    try:
        assert storage.get_or_create(channel="discord", native_id="m").model == "claude-opus-5.5"
    finally:
        storage.shutdown()


def test_build_app_uses_openrouter_model_for_new_sessions_with_openrouter_main(tmp_path):
    env = make_env(tmp_path)
    env["OPENROUTER_MODEL"] = "vendor/or-model"

    async def _build():
        return await build_app(env, providers=())

    storage, _gateway = asyncio.run(_build())
    try:
        assert storage.get_or_create(channel="discord", native_id="m").model == "vendor/or-model"
    finally:
        storage.shutdown()


def test_build_app_bounds_startup_sync_and_keeps_fast_results(tmp_path):
    from conic.services.models import ModelCatalogEntry

    env = make_env(tmp_path)

    async def hang(storage, config):
        await asyncio.Event().wait()

    async def fast(storage, config):
        storage.save_model_catalog("fast", [ModelCatalogEntry(slug="a/b", provider="fast")])
        return True

    providers = (CatalogProvider(name="hang", sync=hang), CatalogProvider(name="fast", sync=fast))

    async def _build():
        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await build_app(env, providers=providers, startup_sync_timeout=0.05)
        return result, loop.time() - started

    (storage, _gateway), elapsed = asyncio.run(_build())
    try:
        assert elapsed < 2.0
        assert not storage.model_catalog_is_empty("fast")
        assert storage.model_catalog_is_empty("hang")
    finally:
        storage.shutdown()


def test_build_app_runs_provider_syncs_concurrently(tmp_path):
    env = make_env(tmp_path)
    started = []
    finished = []

    async def _build():
        gate = asyncio.Event()

        def make(name):
            async def sync(storage, config):
                started.append(name)
                if len(started) == 2:
                    gate.set()
                await asyncio.wait_for(gate.wait(), timeout=1.0)
                finished.append(name)
                return True

            return CatalogProvider(name=name, sync=sync)

        return await build_app(env, providers=(make("a"), make("b")))

    storage, _gateway = asyncio.run(_build())
    try:
        assert sorted(started) == ["a", "b"]
        assert sorted(finished) == ["a", "b"]
    finally:
        storage.shutdown()


def test_startup_sync_timeout_is_fifteen_seconds():
    from conic.entry import STARTUP_SYNC_TIMEOUT

    assert STARTUP_SYNC_TIMEOUT == 15.0
