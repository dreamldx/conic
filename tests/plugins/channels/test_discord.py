import asyncio

import pytest

from conic.core.bus import MessageBus
from conic.types.messages import (
    AssistantMessage, BuildSystemPrompt, Error, MessageDeltaUpdate, MessageUpdate, StepStart, TurnStart,
)
from conic.plugins import meta
from conic.plugins.channels.discord import DiscordThreadPlugin


class FakeMessage:
    def __init__(self, content: str):
        self.content = content
        self.edits: list[str] = []
        self.fail_next_edits = 0

    async def edit(self, content: str) -> None:
        if self.fail_next_edits > 0:
            self.fail_next_edits -= 1
            raise RuntimeError("simulated discord edit failure")
        self.content = content
        self.edits.append(content)


class FakeThread:
    def __init__(self):
        self.sent: list[str] = []
        self.messages: list[FakeMessage] = []

    async def send(self, text: str) -> FakeMessage:
        self.sent.append(text)
        message = FakeMessage(text)
        self.messages.append(message)
        return message

    def typing(self):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _ctx():
            yield

        return _ctx()


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


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


async def test_turn_start_sends_a_placeholder_message():
    thread = FakeThread()
    plugin = DiscordThreadPlugin(thread)
    bus = MessageBus()
    plugin.register(bus)

    await bus.emit(meta.TurnStartEvent, TurnStart())

    assert thread.sent == ["🤔 思考中…"]


async def test_message_delta_update_appends_to_the_placeholder_and_edits_immediately():
    thread = FakeThread()
    clock = FakeClock()
    plugin = DiscordThreadPlugin(thread, clock=clock)
    bus = MessageBus()
    plugin.register(bus)

    await bus.emit(meta.TurnStartEvent, TurnStart())
    placeholder = thread.messages[0]

    await bus.emit(meta.MessageDeltaUpdateEvent, MessageDeltaUpdate(text_delta="Hel"))
    clock.advance(2.0)
    await bus.emit(meta.MessageDeltaUpdateEvent, MessageDeltaUpdate(text_delta="lo"))

    assert placeholder.edits == ["Hel", "Hello"]


async def test_message_delta_updates_within_the_throttle_window_are_coalesced():
    thread = FakeThread()
    clock = FakeClock()
    plugin = DiscordThreadPlugin(thread, clock=clock)
    bus = MessageBus()
    plugin.register(bus)

    await bus.emit(meta.TurnStartEvent, TurnStart())
    placeholder = thread.messages[0]

    await bus.emit(meta.MessageDeltaUpdateEvent, MessageDeltaUpdate(text_delta="Hel"))
    clock.advance(0.1)
    await bus.emit(meta.MessageDeltaUpdateEvent, MessageDeltaUpdate(text_delta="lo"))

    assert placeholder.edits == ["Hel"]

    clock.advance(1.0)
    await bus.emit(meta.MessageDeltaUpdateEvent, MessageDeltaUpdate(text_delta="!"))

    assert placeholder.edits == ["Hel", "Hello!"]


async def test_message_update_edits_immediately_ignoring_the_throttle():
    thread = FakeThread()
    clock = FakeClock()
    plugin = DiscordThreadPlugin(thread, clock=clock)
    bus = MessageBus()
    plugin.register(bus)

    await bus.emit(meta.TurnStartEvent, TurnStart())
    placeholder = thread.messages[0]

    await bus.emit(meta.MessageDeltaUpdateEvent, MessageDeltaUpdate(text_delta="Hel"))
    await bus.emit(meta.MessageUpdateEvent, MessageUpdate(text="🔧 bash(command='ls')"))

    assert placeholder.edits == ["Hel", "🔧 bash(command='ls')"]


async def test_message_update_then_delta_clears_prior_text_instead_of_appending():
    thread = FakeThread()
    clock = FakeClock()
    plugin = DiscordThreadPlugin(thread, clock=clock)
    bus = MessageBus()
    plugin.register(bus)

    await bus.emit(meta.TurnStartEvent, TurnStart())
    placeholder = thread.messages[0]

    await bus.emit(meta.MessageUpdateEvent, MessageUpdate(text="🔧 bash(command='ls')"))
    await bus.emit(meta.MessageDeltaUpdateEvent, MessageDeltaUpdate(text_delta="Final answer"))

    assert placeholder.edits[-1] == "Final answer"


async def test_assistant_message_finalizes_by_editing_the_placeholder():
    thread = FakeThread()
    plugin = DiscordThreadPlugin(thread)
    bus = MessageBus()
    plugin.register(bus)

    await bus.emit(meta.TurnStartEvent, TurnStart())
    placeholder = thread.messages[0]

    await bus.emit(meta.AssistantMessageEvent, AssistantMessage(text="the final answer"))

    assert placeholder.edits[-1] == "the final answer"
    assert len(thread.sent) == 1


async def test_error_finalizes_by_editing_the_placeholder():
    thread = FakeThread()
    plugin = DiscordThreadPlugin(thread)
    bus = MessageBus()
    plugin.register(bus)

    await bus.emit(meta.TurnStartEvent, TurnStart())
    placeholder = thread.messages[0]

    await bus.emit(meta.ErrorEvent, Error(exc=ValueError("boom")))

    assert "boom" in placeholder.edits[-1]
    assert len(thread.sent) == 1


async def test_finalize_edits_first_chunk_and_sends_the_overflow():
    thread = FakeThread()
    plugin = DiscordThreadPlugin(thread)
    bus = MessageBus()
    plugin.register(bus)

    await bus.emit(meta.TurnStartEvent, TurnStart())
    long_text = "x" * 2500

    await bus.emit(meta.AssistantMessageEvent, AssistantMessage(text=long_text))

    assert thread.sent[0] == "🤔 思考中…"
    assert thread.messages[0].edits[-1] == "x" * 2000
    assert thread.sent[1] == "x" * 500


async def test_streaming_preview_truncates_to_the_last_2000_chars():
    thread = FakeThread()
    clock = FakeClock()
    plugin = DiscordThreadPlugin(thread, clock=clock)
    bus = MessageBus()
    plugin.register(bus)

    await bus.emit(meta.TurnStartEvent, TurnStart())
    placeholder = thread.messages[0]

    for i in range(3):
        clock.advance(2.0)
        await bus.emit(meta.MessageDeltaUpdateEvent, MessageDeltaUpdate(text_delta="a" * 1000))

    last_edit = placeholder.edits[-1]
    assert len(last_edit) == 2000
    assert last_edit.startswith("…")
    assert last_edit.endswith("a" * 1999)


async def test_apply_edit_swallows_a_live_preview_edit_failure():
    thread = FakeThread()
    plugin = DiscordThreadPlugin(thread)
    bus = MessageBus()
    plugin.register(bus)

    await bus.emit(meta.TurnStartEvent, TurnStart())
    placeholder = thread.messages[0]
    placeholder.fail_next_edits = 1

    await bus.emit(meta.MessageDeltaUpdateEvent, MessageDeltaUpdate(text_delta="Hel"))  # must not raise

    assert placeholder.edits == []


async def test_finalize_falls_back_to_sending_a_new_message_when_the_edit_fails():
    thread = FakeThread()
    plugin = DiscordThreadPlugin(thread)
    bus = MessageBus()
    plugin.register(bus)

    await bus.emit(meta.TurnStartEvent, TurnStart())
    placeholder = thread.messages[0]
    placeholder.fail_next_edits = 1

    await bus.emit(meta.AssistantMessageEvent, AssistantMessage(text="the final answer"))

    assert placeholder.edits == []
    assert thread.sent[-1] == "the final answer"


async def test_finalize_substitutes_a_placeholder_for_empty_assistant_text():
    thread = FakeThread()
    plugin = DiscordThreadPlugin(thread)
    bus = MessageBus()
    plugin.register(bus)

    await bus.emit(meta.TurnStartEvent, TurnStart())
    placeholder = thread.messages[0]

    await bus.emit(meta.AssistantMessageEvent, AssistantMessage(text=""))

    assert placeholder.edits[-1] == "(empty response)"


async def test_contributes_an_output_requirements_section_to_the_system_prompt():
    thread = FakeThread()
    plugin = DiscordThreadPlugin(thread)
    bus = MessageBus()
    plugin.register(bus)

    result = await bus.emit(meta.BuildSystemPromptEvent, BuildSystemPrompt(sections={}))

    assert "output" in result.sections
    assert "table" in result.sections["output"]
    assert "###" in result.sections["output"]
    assert "2000" in result.sections["output"]


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
