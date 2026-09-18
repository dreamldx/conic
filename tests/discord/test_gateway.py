import asyncio
from types import SimpleNamespace

from conic.discord.gateway import DiscordGateway
from conic.services.storage import StorageService
from conic.plugins import meta
from conic.types.steering import SteeringItem


class FakePluginManagerRecorder:
    """Mimics the real (async) PluginManager.start_session closely enough for
    gateway tests: builds a real SessionScope with the steering.high mailbox
    already registered (as the real loop plugin's register() would do) and
    chains SessionStartEvent itself, since the real start_session now does
    that internally rather than leaving it to the caller."""

    def __init__(self):
        self.started: list[tuple[str, str, str]] = []

    async def start_session(self, channel, native_id, channel_plugin_factory, reason="new"):
        from conic.core.bus import MessageBus
        from conic.types.session import SessionScope
        from conic.types.messages import SessionStart
        from conic.services.models import Session
        from datetime import datetime, timezone

        self.started.append((channel, native_id, reason))
        channel_plugin_factory()  # exercise the closure like the real PluginManager does
        row = Session(
            session_key=f"{channel}:{native_id}", channel=channel, native_id=native_id,
            workspace_dir="/tmp", model="m", status="active", created_at=datetime.now(timezone.utc),
        )
        bus = MessageBus()
        bus.create_mailbox("steering.high", SteeringItem)
        await bus.chain(meta.SessionStartEvent, SessionStart(reason=reason))
        return SessionScope(bus=bus, row=row)


class FakePluginManagerFixedScope:
    """Ignores channel_plugin_factory and always returns a pre-built scope,
    so a test can attach bus listeners *before* calling the gateway method —
    the real start_session chains session_start internally and returns before
    the test would otherwise get a chance to subscribe."""

    def __init__(self, scope):
        self._scope = scope

    async def start_session(self, channel, native_id, channel_plugin_factory, reason="new"):
        from conic.types.messages import SessionStart

        channel_plugin_factory()
        await self._scope.bus.chain(meta.SessionStartEvent, SessionStart(reason=reason))
        return self._scope


def make_fixed_scope(native_id="333"):
    from conic.core.bus import MessageBus
    from conic.types.session import SessionScope
    from conic.services.models import Session
    from datetime import datetime, timezone

    bus = MessageBus()
    bus.create_mailbox("steering.high", SteeringItem)
    row = Session(
        session_key=f"discord:{native_id}", channel="discord", native_id=native_id,
        workspace_dir="/tmp", model="m", status="active", created_at=datetime.now(timezone.utc),
    )
    return bus, SessionScope(bus=bus, row=row)


def make_storage(tmp_path):
    storage = StorageService(
        db_path=str(tmp_path / "conic.duckdb"),
        workspace_root=str(tmp_path / "workspace"),
        default_model="test-model",
    )
    storage.startup()
    return storage


def make_config():
    return SimpleNamespace(discord_bot_token="t")


async def test_resume_active_sessions_rebuilds_scope_for_each_active_row(tmp_path):
    storage = make_storage(tmp_path)
    storage.get_or_create(channel="discord", native_id="111")
    manager = FakePluginManagerRecorder()
    gateway = DiscordGateway(make_config(), plugin_manager=manager, storage=storage)

    async def fake_fetch_thread(native_id: str):
        return object()

    await gateway.resume_active_sessions(fetch_thread=fake_fetch_thread)

    assert manager.started == [("discord", "111", "resume")]
    assert 111 in gateway._sessions
    storage.shutdown()


async def test_resume_active_sessions_continues_past_a_dead_thread(tmp_path):
    """One row whose thread fetch fails (e.g. deleted/inaccessible thread) must
    not abort resumption of the other still-valid rows, and the failing row's
    status must be set to 'ended' so it doesn't keep poisoning future restarts."""
    storage = make_storage(tmp_path)
    storage.get_or_create(channel="discord", native_id="111")
    storage.get_or_create(channel="discord", native_id="222")
    storage.get_or_create(channel="discord", native_id="333")
    manager = FakePluginManagerRecorder()
    gateway = DiscordGateway(make_config(), plugin_manager=manager, storage=storage)

    async def flaky_fetch_thread(native_id: str):
        if native_id == "222":
            raise RuntimeError("thread deleted")
        return object()

    await gateway.resume_active_sessions(fetch_thread=flaky_fetch_thread)

    assert sorted((c, n) for c, n, _ in manager.started) == [("discord", "111"), ("discord", "333")]
    assert 111 in gateway._sessions
    assert 333 in gateway._sessions
    assert 222 not in gateway._sessions

    reloaded = storage.get_or_create(channel="discord", native_id="222")
    assert reloaded.status == "ended"
    storage.shutdown()


async def test_resume_active_sessions_does_not_mark_ended_when_thread_exists_but_construction_fails(tmp_path):
    """If the thread still exists and fetch succeeds, but start_session itself
    raises (e.g. a plugin factory bug, bad workspace path, transient storage
    error), the session must not be misreported as a dead/deleted thread and
    permanently retired — only the fetch_thread failure path may do that."""
    storage = make_storage(tmp_path)
    storage.get_or_create(channel="discord", native_id="111")
    storage.get_or_create(channel="discord", native_id="222")

    class FailingPluginManager(FakePluginManagerRecorder):
        async def start_session(self, channel, native_id, channel_plugin_factory, reason="new"):
            if native_id == "222":
                raise RuntimeError("plugin factory bug")
            return await super().start_session(channel, native_id, channel_plugin_factory, reason)

    manager = FailingPluginManager()
    gateway = DiscordGateway(make_config(), plugin_manager=manager, storage=storage)

    async def fake_fetch_thread(native_id: str):
        return object()

    await gateway.resume_active_sessions(fetch_thread=fake_fetch_thread)

    assert 111 in gateway._sessions
    assert 222 not in gateway._sessions

    reloaded = storage.get_or_create(channel="discord", native_id="222")
    assert reloaded.status == "active"
    storage.shutdown()


async def test_handle_start_command_emits_session_start_with_reason_new():
    from conic.types.messages import SessionStart

    bus, scope = make_fixed_scope()
    starts = []

    async def on_session_start(msg: SessionStart) -> None:
        starts.append(msg.reason)

    bus.on_chain(meta.SessionStartEvent, on_session_start)

    manager = FakePluginManagerFixedScope(scope)
    gateway = DiscordGateway(make_config(), plugin_manager=manager, storage=None)

    async def fake_create_thread():
        class FakeThread:
            id = 333
        return FakeThread()

    async def fake_respond(text: str):
        pass

    await gateway.handle_start_command(create_thread=fake_create_thread, respond=fake_respond)

    assert starts == ["new"]


async def test_resume_active_sessions_emits_session_start_with_reason_resume(tmp_path):
    from conic.types.messages import SessionStart

    storage = make_storage(tmp_path)
    storage.get_or_create(channel="discord", native_id="111")

    bus, scope = make_fixed_scope(native_id="111")
    starts = []

    async def on_session_start(msg: SessionStart) -> None:
        starts.append(msg.reason)

    bus.on_chain(meta.SessionStartEvent, on_session_start)

    manager = FakePluginManagerFixedScope(scope)
    gateway = DiscordGateway(make_config(), plugin_manager=manager, storage=storage)

    async def fake_fetch_thread(native_id: str):
        return object()

    await gateway.resume_active_sessions(fetch_thread=fake_fetch_thread)

    assert starts == ["resume"]
    storage.shutdown()


async def test_handle_stop_command_posts_a_stop_command_and_sets_closing():
    manager = FakePluginManagerRecorder()
    gateway = DiscordGateway(make_config(), plugin_manager=manager, storage=None)
    scope = await manager.start_session("discord", "444", lambda: object())
    gateway._sessions[444] = scope

    async def fake_archive():
        pass

    await gateway.handle_stop_command(thread_id=444, archive=fake_archive)

    posted = await scope.bus.drain("steering.high")
    assert len(posted) == 1
    assert posted[0].is_turn_abort() is True
    assert scope.closing is True


async def test_handle_message_routes_to_known_session_by_enqueueing():
    manager = FakePluginManagerRecorder()
    gateway = DiscordGateway(make_config(), plugin_manager=manager, storage=None)
    scope = await manager.start_session("discord", "222", lambda: object())
    gateway._sessions[222] = scope

    await gateway.handle_message(thread_id=222, text="hello")

    assert await asyncio.wait_for(scope.queue.get(), timeout=1.0) == "hello"


async def test_handle_message_enqueues_multiple_messages_in_order():
    manager = FakePluginManagerRecorder()
    gateway = DiscordGateway(make_config(), plugin_manager=manager, storage=None)
    scope = await manager.start_session("discord", "222", lambda: object())
    gateway._sessions[222] = scope

    await gateway.handle_message(thread_id=222, text="first")
    await gateway.handle_message(thread_id=222, text="second")

    assert await scope.queue.get() == "first"
    assert await scope.queue.get() == "second"


async def test_handle_message_ignores_unknown_thread():
    manager = FakePluginManagerRecorder()
    gateway = DiscordGateway(make_config(), plugin_manager=manager, storage=None)

    await gateway.handle_message(thread_id=999, text="hello")  # must not raise


async def test_handle_start_command_registers_new_session():
    manager = FakePluginManagerRecorder()
    gateway = DiscordGateway(make_config(), plugin_manager=manager, storage=None)

    responses = []

    async def fake_create_thread():
        class FakeThread:
            id = 333
        return FakeThread()

    async def fake_respond(text: str):
        responses.append(text)

    await gateway.handle_start_command(create_thread=fake_create_thread, respond=fake_respond)

    assert [(c, n) for c, n, _ in manager.started] == [("discord", "333")]
    assert 333 in gateway._sessions
    assert responses  # a confirmation was sent


async def test_handle_stop_command_removes_session_from_routing_table():
    manager = FakePluginManagerRecorder()
    gateway = DiscordGateway(make_config(), plugin_manager=manager, storage=None)
    scope = await manager.start_session("discord", "444", lambda: object())
    gateway._sessions[444] = scope

    archived = []

    async def fake_archive():
        archived.append(True)

    await gateway.handle_stop_command(thread_id=444, archive=fake_archive)

    assert 444 not in gateway._sessions
    assert archived == [True]


async def test_handle_stop_command_on_unknown_thread_is_a_noop():
    manager = FakePluginManagerRecorder()
    gateway = DiscordGateway(make_config(), plugin_manager=manager, storage=None)

    async def fake_archive():
        raise AssertionError("should not be called")

    await gateway.handle_stop_command(thread_id=555, archive=fake_archive)  # must not raise
