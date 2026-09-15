from conic.discord.gateway import DiscordGateway
from conic.services.storage import StorageService
from conic.plugins import meta


class FakePluginManagerRecorder:
    def __init__(self):
        self.started: list[tuple[str, str]] = []
        self.stopped: list[object] = []

    def start_session(self, channel, native_id, channel_plugin_factory):
        from conic.core.bus import MessageBus
        from conic.core.session import SessionScope
        from conic.services.models import Session
        from datetime import datetime, timezone

        self.started.append((channel, native_id))
        channel_plugin_factory()  # exercise the closure like the real PluginManager does
        row = Session(
            session_key=f"{channel}:{native_id}", channel=channel, native_id=native_id,
            workspace_dir="/tmp", model="m", status="active", created_at=datetime.now(timezone.utc),
        )
        return SessionScope(bus=MessageBus(), row=row)

    def stop_session(self, scope):
        self.stopped.append(scope)


class FakePluginManagerFixedScope:
    """Ignores channel_plugin_factory and always returns a pre-built scope,
    so a test can attach bus listeners *before* calling the gateway method —
    gateway emits session_start internally and returns before the test would
    otherwise get a chance to subscribe."""

    def __init__(self, scope):
        self._scope = scope
        self.stopped: list[object] = []

    def start_session(self, channel, native_id, channel_plugin_factory):
        channel_plugin_factory()
        return self._scope

    def stop_session(self, scope):
        self.stopped.append(scope)


def make_fixed_scope(native_id="333"):
    from conic.core.bus import MessageBus
    from conic.core.session import SessionScope
    from conic.services.models import Session
    from datetime import datetime, timezone

    bus = MessageBus()
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


async def test_resume_active_sessions_rebuilds_scope_for_each_active_row(tmp_path):
    storage = make_storage(tmp_path)
    storage.get_or_create(channel="discord", native_id="111")
    manager = FakePluginManagerRecorder()
    gateway = DiscordGateway(bot_token="t", plugin_manager=manager, storage=storage)

    async def fake_fetch_thread(native_id: str):
        return object()

    await gateway.resume_active_sessions(fetch_thread=fake_fetch_thread)

    assert manager.started == [("discord", "111")]
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
    gateway = DiscordGateway(bot_token="t", plugin_manager=manager, storage=storage)

    async def flaky_fetch_thread(native_id: str):
        if native_id == "222":
            raise RuntimeError("thread deleted")
        return object()

    await gateway.resume_active_sessions(fetch_thread=flaky_fetch_thread)

    assert sorted(manager.started) == [("discord", "111"), ("discord", "333")]
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
        def start_session(self, channel, native_id, channel_plugin_factory):
            if native_id == "222":
                raise RuntimeError("plugin factory bug")
            return super().start_session(channel, native_id, channel_plugin_factory)

    manager = FailingPluginManager()
    gateway = DiscordGateway(bot_token="t", plugin_manager=manager, storage=storage)

    async def fake_fetch_thread(native_id: str):
        return object()

    await gateway.resume_active_sessions(fetch_thread=fake_fetch_thread)

    assert 111 in gateway._sessions
    assert 222 not in gateway._sessions

    reloaded = storage.get_or_create(channel="discord", native_id="222")
    assert reloaded.status == "active"
    storage.shutdown()


async def test_handle_start_command_emits_session_start_with_reason_new():
    from conic.core.messages import SessionStart

    bus, scope = make_fixed_scope()
    starts = []

    async def on_session_start(msg: SessionStart) -> None:
        starts.append(msg.reason)

    bus.on(meta.SessionStartEvent, on_session_start)

    manager = FakePluginManagerFixedScope(scope)
    gateway = DiscordGateway(bot_token="t", plugin_manager=manager, storage=None)

    async def fake_create_thread():
        class FakeThread:
            id = 333
        return FakeThread()

    async def fake_respond(text: str):
        pass

    await gateway.handle_start_command(create_thread=fake_create_thread, respond=fake_respond)

    assert starts == ["new"]


async def test_resume_active_sessions_emits_session_start_with_reason_resume(tmp_path):
    from conic.core.messages import SessionStart

    storage = make_storage(tmp_path)
    storage.get_or_create(channel="discord", native_id="111")

    bus, scope = make_fixed_scope(native_id="111")
    starts = []

    async def on_session_start(msg: SessionStart) -> None:
        starts.append(msg.reason)

    bus.on(meta.SessionStartEvent, on_session_start)

    manager = FakePluginManagerFixedScope(scope)
    gateway = DiscordGateway(bot_token="t", plugin_manager=manager, storage=storage)

    async def fake_fetch_thread(native_id: str):
        return object()

    await gateway.resume_active_sessions(fetch_thread=fake_fetch_thread)

    assert starts == ["resume"]
    storage.shutdown()


async def test_handle_stop_command_emits_session_end_with_reason_user_stop():
    from conic.core.messages import SessionEnd

    manager = FakePluginManagerRecorder()
    gateway = DiscordGateway(bot_token="t", plugin_manager=manager, storage=None)
    scope = manager.start_session("discord", "444", lambda: object())
    gateway._sessions[444] = scope

    ends = []

    async def on_session_end(msg: SessionEnd) -> None:
        ends.append(msg.reason)

    scope.bus.on(meta.SessionEndEvent, on_session_end)

    async def fake_archive():
        pass

    await gateway.handle_stop_command(thread_id=444, archive=fake_archive)

    assert ends == ["user_stop"]


async def test_handle_message_routes_to_known_session():
    manager = FakePluginManagerRecorder()
    gateway = DiscordGateway(bot_token="t", plugin_manager=manager, storage=None)
    scope = manager.start_session("discord", "222", lambda: object())
    gateway._sessions[222] = scope

    received = []
    from conic.core.messages import UserInput

    async def on_user_input(msg: UserInput) -> None:
        received.append(msg.text)

    scope.bus.on("user_input", on_user_input)

    await gateway.handle_message(thread_id=222, text="hello")

    assert received == ["hello"]


async def test_handle_message_serializes_concurrent_messages_for_the_same_thread():
    """Two concurrent handle_message calls for the same thread must not
    interleave: the second turn must only start after the first's handler
    has fully completed."""
    import asyncio

    manager = FakePluginManagerRecorder()
    gateway = DiscordGateway(bot_token="t", plugin_manager=manager, storage=None)
    scope = manager.start_session("discord", "222", lambda: object())
    gateway._sessions[222] = scope

    from conic.core.messages import UserInput

    events = []

    async def on_user_input(msg: UserInput) -> None:
        events.append(("start", msg.text))
        if msg.text == "first":
            await asyncio.sleep(0.05)
        events.append(("end", msg.text))

    scope.bus.on("user_input", on_user_input)

    await asyncio.gather(
        gateway.handle_message(thread_id=222, text="first"),
        gateway.handle_message(thread_id=222, text="second"),
    )

    assert events == [
        ("start", "first"),
        ("end", "first"),
        ("start", "second"),
        ("end", "second"),
    ]


async def test_handle_message_ignores_unknown_thread():
    manager = FakePluginManagerRecorder()
    gateway = DiscordGateway(bot_token="t", plugin_manager=manager, storage=None)

    await gateway.handle_message(thread_id=999, text="hello")  # must not raise


async def test_handle_start_command_registers_new_session():
    manager = FakePluginManagerRecorder()
    gateway = DiscordGateway(bot_token="t", plugin_manager=manager, storage=None)

    responses = []

    async def fake_create_thread():
        class FakeThread:
            id = 333
        return FakeThread()

    async def fake_respond(text: str):
        responses.append(text)

    await gateway.handle_start_command(create_thread=fake_create_thread, respond=fake_respond)

    assert manager.started == [("discord", "333")]
    assert 333 in gateway._sessions
    assert responses  # a confirmation was sent


async def test_handle_stop_command_removes_session_and_calls_stop_session():
    manager = FakePluginManagerRecorder()
    gateway = DiscordGateway(bot_token="t", plugin_manager=manager, storage=None)
    scope = manager.start_session("discord", "444", lambda: object())
    gateway._sessions[444] = scope

    archived = []

    async def fake_archive():
        archived.append(True)

    await gateway.handle_stop_command(thread_id=444, archive=fake_archive)

    assert 444 not in gateway._sessions
    assert manager.stopped == [scope]
    assert archived == [True]


async def test_handle_stop_command_waits_for_an_in_flight_turn_to_finish():
    """/agent_stop must not archive/stop the session while a turn triggered by
    handle_message is still running under the same scope.lock — it must wait
    for the lock instead of racing the in-flight turn."""
    import asyncio

    manager = FakePluginManagerRecorder()
    gateway = DiscordGateway(bot_token="t", plugin_manager=manager, storage=None)
    scope = manager.start_session("discord", "444", lambda: object())
    gateway._sessions[444] = scope

    from conic.core.messages import UserInput

    events = []

    async def on_user_input(msg: UserInput) -> None:
        events.append("turn_start")
        await asyncio.sleep(0.05)
        events.append("turn_end")

    scope.bus.on("user_input", on_user_input)

    async def fake_archive():
        events.append("archived")

    await asyncio.gather(
        gateway.handle_message(thread_id=444, text="hello"),
        gateway.handle_stop_command(thread_id=444, archive=fake_archive),
    )

    assert events == ["turn_start", "turn_end", "archived"]


async def test_handle_stop_command_on_unknown_thread_is_a_noop():
    manager = FakePluginManagerRecorder()
    gateway = DiscordGateway(bot_token="t", plugin_manager=manager, storage=None)

    async def fake_archive():
        raise AssertionError("should not be called")

    await gateway.handle_stop_command(thread_id=555, archive=fake_archive)  # must not raise
    assert manager.stopped == []
