from conic.core.messages import AssistantMessage, Error

DISCORD_MESSAGE_LIMIT = 2000


class DiscordThreadPlugin:
    def __init__(self, thread):
        self._thread = thread

    def register(self, bus) -> None:
        bus.on("assistant_message", self.on_assistant_message)
        bus.on("error", self.on_error)

    async def on_assistant_message(self, msg: AssistantMessage) -> None:
        await self._send(msg.text)

    async def on_error(self, msg: Error) -> None:
        await self._send(f"⚠️ {msg.exc}")

    async def _send(self, text: str) -> None:
        for i in range(0, len(text), DISCORD_MESSAGE_LIMIT):
            await self._thread.send(text[i: i + DISCORD_MESSAGE_LIMIT])
