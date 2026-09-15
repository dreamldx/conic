import asyncio

import pytest

from conic.core.bus import MessageBus
from conic.core.messages import AssistantMessage, Error, StepStart, TurnStart
from conic.plugins import meta
from conic.plugins.channels.discord import DiscordThreadPlugin


class FakeThread:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)

    def typing(self):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _ctx():
            yield

        return _ctx()


async def test_forwards_assistant_message_to_thread():
    thread = FakeThread()
    plugin = DiscordThreadPlugin(thread)
    bus = MessageBus()
    plugin.register(bus)

    await bus.emit(meta.AssistantMessageEvent, AssistantMessage(text="hello"))

    assert thread.sent == ["hello"]


async def test_forwards_error_to_thread_with_marker():
    thread = FakeThread()
    plugin = DiscordThreadPlugin(thread)
    bus = MessageBus()
    plugin.register(bus)

    await bus.emit(meta.ErrorEvent, Error(exc=ValueError("boom")))

    assert len(thread.sent) == 1
    assert "boom" in thread.sent[0]


async def test_step_start_restarts_typing_after_it_has_timed_out():
    """A turn spanning multiple steps can legitimately run past TYPING_TIMEOUT.
    Once the safety-capped _keep_typing task ends, the next StepStart must
    re-arm a fresh one instead of leaving the indicator dead for the rest of
    the turn."""
    thread = FakeThread()
    plugin = DiscordThreadPlugin(thread)
    bus = MessageBus()
    plugin.register(bus)

    await bus.emit(meta.TurnStartEvent, TurnStart())
    first_task = plugin._typing_task
    assert first_task is not None

    first_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_task
    assert first_task.done()

    await bus.emit(meta.StepStartEvent, StepStart(step_index=1))

    assert plugin._typing_task is not None
    assert plugin._typing_task is not first_task
    assert not plugin._typing_task.done()

    plugin._stop_typing()


async def test_chunks_messages_longer_than_discord_limit():
    thread = FakeThread()
    plugin = DiscordThreadPlugin(thread)
    bus = MessageBus()
    plugin.register(bus)

    long_text = "x" * 4500
    await bus.emit(meta.AssistantMessageEvent, AssistantMessage(text=long_text))

    assert len(thread.sent) == 3
    assert all(len(chunk) <= 2000 for chunk in thread.sent)
    assert "".join(thread.sent) == long_text
