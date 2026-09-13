from conic.plugins.channels.discord.gateway import DiscordGateway
from conic.services.storage import StorageService


class FakePluginManagerRecorder:
    def __init__(self):
        self.started: list[tuple[str, str]] = []
        self.stopped: list[object] = []

    def start_session(self, channel, native_id, channel_plugin_factory):
        from conic.core.bus import MessageBus
        from conic.core.session import SessionScope
        from conic.services.storage import SessionRow
        from datetime import datetime, timezone

        self.started.append((channel, native_id))
        channel_plugin_factory()  # exercise the closure like the real PluginManager does
        row = SessionRow(
            session_key=f"{channel}:{native_id}", channel=channel, native_id=native_id,
            workspace_dir="/tmp", model="m", status="active", created_at=datetime.now(timezone.utc),
        )
        return SessionScope(bus=MessageBus(), row=row)

    def stop_session(self, scope):
        self.stopped.append(scope)


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


async def test_handle_stop_command_on_unknown_thread_is_a_noop():
    manager = FakePluginManagerRecorder()
    gateway = DiscordGateway(bot_token="t", plugin_manager=manager, storage=None)

    async def fake_archive():
        raise AssertionError("should not be called")

    await gateway.handle_stop_command(thread_id=555, archive=fake_archive)  # must not raise
    assert manager.stopped == []
