import asyncio
import time
from typing import Callable

from loguru import logger

from conic.types.messages import (
    AssistantMessage, BuildSystemPrompt, Error, MessageDeltaUpdate, MessageUpdate,
    StepStart, TurnStart, TurnEnd,
)
from conic.plugins import meta

OUTPUT_REQUIREMENTS = (
    "Your reply is posted to a Discord thread. Formatting constraints:\n"
    "- Discord does not render markdown tables (they show as raw pipe-separated "
    "text) -- use short lists or aligned code blocks instead.\n"
    "- Discord only renders headings up to ### -- avoid deeper heading levels.\n"
    "- Your reply streams into the thread live, token by token, editing a "
    "single message as you generate it -- you don't need to chunk it or "
    "announce progress yourself.\n"
    "- Keep replies within 2000 characters -- do not exceed Discord's "
    "single-message limit."
)

DISCORD_MESSAGE_LIMIT = 2000
TYPING_INTERVAL = 8
TYPING_TIMEOUT = 20
STREAM_EDIT_INTERVAL = 1.0
THINKING_TEXT = "🤔 思考中…"


class DiscordThreadPlugin:
    def __init__(self, thread, clock: Callable[[], float] = time.monotonic):
        self._thread = thread
        self._clock = clock
        self._typing_task: asyncio.Task | None = None
        self._stopped = False
        self._status_message = None
        self._buffer = ""
        self._awaiting_first_delta = False
        self._last_edit_time = float("-inf")

    def register(self, bus) -> None:
        bus.on(meta.SessionStopEvent, self.on_session_stop)
        bus.on(meta.TurnStartEvent, self.on_turn_start)
        bus.on(meta.StepStartEvent, self.on_step_start)
        bus.on(meta.MessageUpdateEvent, self.on_message_update)
        bus.on(meta.MessageDeltaUpdateEvent, self.on_message_delta_update)
        bus.on(meta.TurnEndEvent, self.on_turn_end)
        bus.on(meta.ErrorEvent, self.on_error)
        bus.on(meta.AssistantMessageEvent, self.on_assistant_message)
        bus.on(meta.BuildSystemPromptEvent, self.contribute_output_requirements)

    async def contribute_output_requirements(self, msg: BuildSystemPrompt) -> BuildSystemPrompt:
        msg.sections["output"] = OUTPUT_REQUIREMENTS
        return msg

    async def on_session_stop(self, _msg: TurnEnd) -> None:
        self._stopped = True
        self._stop_typing()

    async def on_turn_start(self, _msg: TurnStart) -> None:
        self._ensure_typing()
        self._buffer = THINKING_TEXT
        self._awaiting_first_delta = True
        self._last_edit_time = float("-inf")
        self._status_message = await self._thread.send(self._buffer)

    async def on_step_start(self, _msg: StepStart) -> None:
        self._ensure_typing()

    def _ensure_typing(self) -> None:
        if self._typing_task is not None and not self._typing_task.done():
            return
        self._typing_task = asyncio.create_task(self._keep_typing())

    async def on_message_update(self, msg: MessageUpdate) -> None:
        self._buffer = msg.text
        self._awaiting_first_delta = True
        await self._apply_edit(force=True)

    async def on_message_delta_update(self, msg: MessageDeltaUpdate) -> None:
        force = self._awaiting_first_delta
        if self._awaiting_first_delta:
            self._buffer = ""
            self._awaiting_first_delta = False
        self._buffer += msg.text_delta
        await self._apply_edit(force=force)

    async def _apply_edit(self, force: bool) -> None:
        if self._status_message is None or self._stopped:
            return
        now = self._clock()
        if not force and (now - self._last_edit_time) < STREAM_EDIT_INTERVAL:
            return
        self._last_edit_time = now
        content = self._buffer
        if len(content) > DISCORD_MESSAGE_LIMIT:
            content = "…" + content[-(DISCORD_MESSAGE_LIMIT - 1):]
        try:
            await self._status_message.edit(content=content)
        except Exception as exc:
            logger.warning("failed to live-update responsive message: {}", exc)

    async def on_turn_end(self, _msg: TurnEnd) -> None:
        self._stop_typing()

    async def on_error(self, msg: Error) -> None:
        self._stop_typing()
        await self._finalize(f"⚠️ {msg.exc}")

    async def on_assistant_message(self, msg: AssistantMessage) -> None:
        await self._finalize(msg.text)

    async def _finalize(self, text: str) -> None:
        if self._stopped:
            return
        text = text or "(empty response)"
        if self._status_message is not None:
            try:
                await self._status_message.edit(content=text[:DISCORD_MESSAGE_LIMIT])
            except Exception as exc:
                logger.warning(
                    "failed to edit responsive message into final text, sending a new message: {}", exc
                )
                await self._send(text[:DISCORD_MESSAGE_LIMIT])
            await self._send(text[DISCORD_MESSAGE_LIMIT:])
        else:
            await self._send(text)
        self._status_message = None

    def _stop_typing(self) -> None:
        if self._typing_task is not None and not self._typing_task.done():
            self._typing_task.cancel()
            self._typing_task = None

    async def _keep_typing(self) -> None:
        try:
            async with asyncio.timeout(TYPING_TIMEOUT):
                while True:
                    async with self._thread.typing():
                        await asyncio.sleep(TYPING_INTERVAL)
        except (asyncio.CancelledError, TimeoutError):
            pass

    async def _send(self, text: str) -> None:
        if self._stopped:
            return
        for i in range(0, len(text), DISCORD_MESSAGE_LIMIT):
            await self._thread.send(text[i: i + DISCORD_MESSAGE_LIMIT])
