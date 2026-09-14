from conic.core.bus import MessageBus
from conic.core.messages import AssistantMessage, Error
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
