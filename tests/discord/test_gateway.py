import asyncio
from datetime import UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from loguru import logger

from conic.discord.gateway import (
    EMPTY_MENTION_ERROR,
    DiscordGateway,
    strip_bot_mention,
    thread_title,
)
from conic.plugins import meta
from conic.services.storage import StorageService
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
        from datetime import datetime

        from conic.core.bus import MessageBus
        from conic.services.models import Session
        from conic.types.messages import SessionStart
        from conic.types.session import SessionScope

        self.started.append((channel, native_id, reason))
        channel_plugin_factory()  # exercise the closure like the real PluginManager does
        row = Session(
            session_key=f"{channel}:{native_id}", channel=channel, native_id=native_id,
            workspace_dir="/tmp", model="m", status="active", created_at=datetime.now(UTC),
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
    from datetime import datetime

    from conic.core.bus import MessageBus
    from conic.services.models import Session
    from conic.types.session import SessionScope

    bus = MessageBus()
    bus.create_mailbox("steering.high", SteeringItem)
    row = Session(
        session_key=f"discord:{native_id}", channel="discord", native_id=native_id,
        workspace_dir="/tmp", model="m", status="active", created_at=datetime.now(UTC),
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


async def test_resume_active_sessions_logs_the_thread_title(tmp_path):
    storage = make_storage(tmp_path)
    storage.get_or_create(channel="discord", native_id="111")
    manager = FakePluginManagerRecorder()
    gateway = DiscordGateway(make_config(), plugin_manager=manager, storage=storage)

    async def fake_fetch_thread(native_id: str):
        return SimpleNamespace(name="my-session-title", archived=False)

    logged = []
    sink_id = logger.add(lambda msg: logged.append(msg.record["message"]), level="INFO")
    try:
        await gateway.resume_active_sessions(fetch_thread=fake_fetch_thread)
    finally:
        logger.remove(sink_id)

    assert any("my-session-title" in m for m in logged)
    storage.shutdown()


async def test_resume_active_sessions_marks_ended_and_skips_a_thread_archived_remotely(tmp_path):
    """A thread that was archived on Discord's side (e.g. /agent_stop ran and
    archived+locked it, but the process crashed before the background
    join-and-cleanup task got to storage.set_status('ended')) must not be
    silently resumed on restart — fetch_thread succeeding isn't enough proof
    the session is still live; an archived thread means it's already over."""
    from types import SimpleNamespace

    storage = make_storage(tmp_path)
    storage.get_or_create(channel="discord", native_id="111")
    storage.get_or_create(channel="discord", native_id="222")
    manager = FakePluginManagerRecorder()
    gateway = DiscordGateway(make_config(), plugin_manager=manager, storage=storage)

    async def fake_fetch_thread(native_id: str):
        if native_id == "222":
            return SimpleNamespace(archived=True)
        return SimpleNamespace(archived=False)

    await gateway.resume_active_sessions(fetch_thread=fake_fetch_thread)

    assert sorted((c, n) for c, n, _ in manager.started) == [("discord", "111")]
    assert 111 in gateway._sessions
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


async def test_handle_mention_creates_thread_starts_session_and_enqueues_text():
    manager = FakePluginManagerRecorder()
    gateway = DiscordGateway(make_config(), plugin_manager=manager, storage=None)

    async def fake_create_thread():
        return SimpleNamespace(id=444)

    async def fake_reply(error: str):
        raise AssertionError("no error reply expected")

    await gateway.handle_mention(create_thread=fake_create_thread, text="what is 2+2?", reply=fake_reply)

    assert [(c, n, r) for c, n, r in manager.started] == [("discord", "444", "new")]
    assert await asyncio.wait_for(gateway._sessions[444].queue.get(), timeout=1.0) == "what is 2+2?"


async def test_handle_mention_with_empty_text_replies_with_error_and_creates_nothing():
    manager = FakePluginManagerRecorder()
    gateway = DiscordGateway(make_config(), plugin_manager=manager, storage=None)
    created = []
    replies = []

    async def fake_create_thread():
        created.append(1)
        return SimpleNamespace(id=444)

    async def fake_reply(error: str):
        replies.append(error)

    await gateway.handle_mention(create_thread=fake_create_thread, text="", reply=fake_reply)

    assert replies == [EMPTY_MENTION_ERROR]
    assert created == []
    assert manager.started == []


def test_strip_bot_mention_removes_both_mention_forms():
    assert strip_bot_mention("<@42> hello <@!42> world", 42) == "hello  world"
    assert strip_bot_mention("<@43> hi", 42) == "<@43> hi"


def test_thread_title_uses_first_line_truncated_with_fallback():
    assert thread_title("first line\nsecond") == "first line"
    assert len(thread_title("x" * 500)) == 90
    assert thread_title("   ") == "agent-session"


BOT_ID = 42


@pytest.fixture
def bot_gateway(monkeypatch):
    monkeypatch.setattr(discord.Client, "user", SimpleNamespace(id=BOT_ID), raising=False)
    manager = FakePluginManagerRecorder()
    return DiscordGateway(make_config(), plugin_manager=manager, storage=None), manager


def make_thread(thread_id=222):
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    return thread


GUILD = object()


def make_message(channel, content, mentions_bot=True, bot_author=False, guild=GUILD):
    return SimpleNamespace(
        author=SimpleNamespace(bot=bot_author),
        channel=channel,
        content=content,
        guild=guild,
        mentions=[SimpleNamespace(id=BOT_ID)] if mentions_bot else [],
        create_thread=AsyncMock(return_value=make_thread(444)),
    )


def make_channel():
    return SimpleNamespace(id=111, send=AsyncMock())


async def test_on_message_in_thread_strips_the_bot_mention_before_enqueueing(bot_gateway):
    gateway, manager = bot_gateway
    scope = await manager.start_session("discord", "222", lambda: object())
    gateway._sessions[222] = scope

    await gateway._client.on_message(make_message(make_thread(222), f"<@{BOT_ID}> what is 2+2?"))

    assert await asyncio.wait_for(scope.queue.get(), timeout=1.0) == "what is 2+2?"


async def test_on_message_in_thread_with_only_a_mention_enqueues_nothing(bot_gateway):
    gateway, manager = bot_gateway
    scope = await manager.start_session("discord", "222", lambda: object())
    gateway._sessions[222] = scope

    await gateway._client.on_message(make_message(make_thread(222), f"<@{BOT_ID}>"))

    assert scope.queue.empty()


async def test_on_message_ignores_bot_authors(bot_gateway):
    gateway, manager = bot_gateway
    channel = make_channel()
    message = make_message(channel, f"<@{BOT_ID}> hi", bot_author=True)

    await gateway._client.on_message(message)

    message.create_thread.assert_not_called()
    assert manager.started == []


async def test_on_message_mention_in_channel_creates_thread_and_enqueues_text(bot_gateway):
    gateway, manager = bot_gateway
    message = make_message(make_channel(), f"<@{BOT_ID}> summarize this")

    await gateway._client.on_message(message)

    message.create_thread.assert_awaited_once_with(name="summarize this")
    assert [(c, n) for c, n, _ in manager.started] == [("discord", "444")]
    assert await asyncio.wait_for(gateway._sessions[444].queue.get(), timeout=1.0) == "summarize this"


async def test_on_message_in_channel_without_mention_is_ignored(bot_gateway):
    gateway, manager = bot_gateway
    message = make_message(make_channel(), "just chatting", mentions_bot=False)

    await gateway._client.on_message(message)

    message.create_thread.assert_not_called()
    assert manager.started == []


async def test_on_message_mention_in_direct_message_is_ignored(bot_gateway):
    gateway, manager = bot_gateway
    message = make_message(make_channel(), f"<@{BOT_ID}> hi", guild=None)

    await gateway._client.on_message(message)

    message.create_thread.assert_not_called()
    assert manager.started == []


async def test_on_message_empty_mention_in_channel_sends_error_to_channel(bot_gateway):
    gateway, manager = bot_gateway
    channel = make_channel()
    message = make_message(channel, f"<@{BOT_ID}>")

    await gateway._client.on_message(message)

    channel.send.assert_awaited_once_with(EMPTY_MENTION_ERROR)
    message.create_thread.assert_not_called()
    assert manager.started == []


def age_session(storage, session_key, days):
    from datetime import datetime, timedelta

    old = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    storage._conn.execute("UPDATE sessions SET created_at = ? WHERE session_key = ?", [old, session_key])


def cutoff_30_days():
    from datetime import datetime, timedelta

    return datetime.now(UTC) - timedelta(days=30)


async def test_close_stale_sessions_archives_thread_ends_session_and_clears_routing(tmp_path):
    storage = make_storage(tmp_path)
    storage.get_or_create(channel="discord", native_id="111")
    age_session(storage, "discord:111", days=40)
    manager = FakePluginManagerRecorder()
    gateway = DiscordGateway(make_config(), plugin_manager=manager, storage=storage)
    gateway._sessions[111] = await manager.start_session("discord", "111", lambda: object())
    thread = SimpleNamespace(edit=AsyncMock())

    async def fake_fetch_thread(native_id: str):
        return thread

    closed = await gateway.close_stale_sessions(cutoff_30_days(), fake_fetch_thread)

    assert closed == 1
    assert 111 not in gateway._sessions
    thread.edit.assert_awaited_once_with(archived=True, locked=True)
    assert storage.active_sessions(channel="discord") == []
    storage.shutdown()


async def test_close_stale_sessions_leaves_recent_sessions_alone(tmp_path):
    storage = make_storage(tmp_path)
    storage.get_or_create(channel="discord", native_id="111")
    manager = FakePluginManagerRecorder()
    gateway = DiscordGateway(make_config(), plugin_manager=manager, storage=storage)
    gateway._sessions[111] = await manager.start_session("discord", "111", lambda: object())

    async def fake_fetch_thread(native_id: str):
        raise AssertionError("must not fetch")

    closed = await gateway.close_stale_sessions(cutoff_30_days(), fake_fetch_thread)

    assert closed == 0
    assert 111 in gateway._sessions
    assert len(storage.active_sessions(channel="discord")) == 1
    storage.shutdown()


async def test_close_stale_sessions_still_ends_session_when_thread_is_gone(tmp_path):
    storage = make_storage(tmp_path)
    storage.get_or_create(channel="discord", native_id="111")
    age_session(storage, "discord:111", days=40)
    gateway = DiscordGateway(make_config(), plugin_manager=FakePluginManagerRecorder(), storage=storage)

    async def missing_thread(native_id: str):
        raise RuntimeError("thread deleted")

    closed = await gateway.close_stale_sessions(cutoff_30_days(), missing_thread)

    assert closed == 1
    assert storage.active_sessions(channel="discord") == []
    storage.shutdown()


async def test_close_stale_sessions_uses_last_message_time_not_creation_time(tmp_path):
    storage = make_storage(tmp_path)
    row = storage.get_or_create(channel="discord", native_id="111")
    age_session(storage, "discord:111", days=40)
    storage.handle_for(row).append_message({"role": "user", "content": "hi"}, turn_id=1)
    gateway = DiscordGateway(make_config(), plugin_manager=FakePluginManagerRecorder(), storage=storage)

    async def fake_fetch_thread(native_id: str):
        raise AssertionError("must not fetch")

    closed = await gateway.close_stale_sessions(cutoff_30_days(), fake_fetch_thread)

    assert closed == 0
    assert len(storage.active_sessions(channel="discord")) == 1
    storage.shutdown()
