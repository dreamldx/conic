import asyncio

from conic.core.messages import AssistantMessage, Error, StepStart, TurnStart, TurnEnd
from conic.plugins import meta

DISCORD_MESSAGE_LIMIT = 2000
TYPING_INTERVAL = 8
TYPING_TIMEOUT = 20


class DiscordThreadPlugin:
    def __init__(self, thread):
        self._thread = thread
        self._typing_task: asyncio.Task | None = None
        self._stopped = False

    def register(self, bus) -> None:
        bus.on(meta.SessionStopEvent, self.on_session_stop)
        bus.on(meta.TurnStartEvent, self.on_turn_start)
        bus.on(meta.StepStartEvent, self.on_step_start)
        bus.on(meta.TurnEndEvent, self.on_turn_end)
        bus.on(meta.ErrorEvent, self.on_error)
        bus.on(meta.AssistantMessageEvent, self.on_assistant_message)

    async def on_session_stop(self, _msg: TurnEnd) -> None:
        self._stopped = True
        self._stop_typing()

    async def on_turn_start(self, _msg: TurnStart) -> None:
        self._ensure_typing()

    async def on_step_start(self, _msg: StepStart) -> None:
        self._ensure_typing()

    def _ensure_typing(self) -> None:
        if self._typing_task is not None and not self._typing_task.done():
            return
        self._typing_task = asyncio.create_task(self._keep_typing())

    async def on_turn_end(self, _msg: TurnEnd) -> None:
        self._stop_typing()

    async def on_error(self, msg: Error) -> None:
        self._stop_typing()
        await self._send(f"⚠️ {msg.exc}")

    async def on_assistant_message(self, msg: AssistantMessage) -> None:
        await self._send(msg.text)

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
