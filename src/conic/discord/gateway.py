import re
from collections.abc import Awaitable, Callable

import discord
from discord import app_commands
from loguru import logger

from conic.config import Config
from conic.plugins.channels.discord import DiscordThreadPlugin
from conic.types.steering import SteeringStopCommand

THREAD_TITLE_LIMIT = 90
DEFAULT_THREAD_TITLE = "agent-session"


def strip_bot_mention(content: str, bot_id: int) -> str:
    return re.sub(rf"<@!?{bot_id}>", "", content).strip()


def thread_title(text: str) -> str:
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    return first_line[:THREAD_TITLE_LIMIT] or DEFAULT_THREAD_TITLE


class DiscordGateway:
    name = "discord"

    def __init__(self, config: Config, plugin_manager, storage):
        self._token = config.discord_bot_token
        self._plugin_manager = plugin_manager
        self._storage = storage
        self._sessions: dict[int, object] = {}

        intents = discord.Intents.default()
        intents.message_content = True
        self._client = discord.Client(intents=intents)
        self._tree = app_commands.CommandTree(self._client)
        self._register_discord_wiring()

    def _register_discord_wiring(self) -> None:
        @self._tree.command(name="agent_start", description="Start a new agent session in a thread")
        @app_commands.describe(title="Thread title for the session")
        async def agent_start(interaction: discord.Interaction, title: str = "agent-session") -> None:
            async def create_thread():
                return await interaction.channel.create_thread(
                    name=title, type=discord.ChannelType.public_thread
                )

            async def respond(text: str) -> None:
                await interaction.response.send_message(text, ephemeral=True)

            await self.handle_start_command(create_thread=create_thread, respond=respond)

        @self._tree.command(name="agent_stop", description="Stop the agent session in this thread")
        async def agent_stop(interaction: discord.Interaction) -> None:
            async def archive() -> None:
                await interaction.channel.edit(archived=True, locked=True)

            await self.handle_stop_command(thread_id=interaction.channel.id, archive=archive)
            await interaction.response.send_message("Session stopped.", ephemeral=True)

        @self._client.event
        async def on_ready() -> None:
            logger.info("discord gateway connected, syncing commands")
            await self._tree.sync()
            logger.info("discord commands synced, resuming active sessions")

            async def fetch_thread(native_id: str):
                return await self._client.fetch_channel(int(native_id))

            await self.resume_active_sessions(fetch_thread=fetch_thread)

        @self._client.event
        async def on_message(message: discord.Message) -> None:
            if message.author.bot:
                return
            if isinstance(message.channel, discord.Thread):
                await self.handle_message(thread_id=message.channel.id, text=message.content)
                return
            bot_user = self._client.user
            if bot_user is None or message.guild is None or bot_user not in message.mentions:
                return
            text = strip_bot_mention(message.content, bot_user.id)

            async def create_thread():
                return await message.create_thread(name=thread_title(text))

            await self.handle_mention(create_thread=create_thread, text=text)

    async def start(self) -> None:
        await self._client.start(self._token)

    async def stop(self) -> None:
        await self._client.close()

    async def resume_active_sessions(self, fetch_thread: Callable[[str], Awaitable[object]]) -> None:
        active = self._storage.active_sessions(channel="discord")
        logger.info("resuming {} active sessions", len(active))
        for row in active:
            try:
                thread = await fetch_thread(row.native_id)
            except Exception:
                logger.warning("failed to resume session {} (thread deleted/missing)", row.session_key)
                self._storage.handle_for(row).set_status("ended")
                continue
            title = getattr(thread, "name", "?")
            if getattr(thread, "archived", False):
                # The thread was archived (almost certainly by /agent_stop's
                # own archive() call) but the row never got marked "ended" —
                # most likely the process died between that archive() and the
                # background join-and-cleanup task reaching storage.set_status.
                # Fetch succeeding is not proof the session is still live.
                logger.info(
                    "session {} title={!r} thread is archived remotely, marking ended",
                    row.session_key, title,
                )
                self._storage.handle_for(row).set_status("ended")
                continue
            try:
                scope = await self._plugin_manager.start_session(
                    channel="discord",
                    native_id=row.native_id,
                    channel_plugin_factory=lambda t=thread: DiscordThreadPlugin(t),
                    reason="resume",
                )
            except Exception:
                logger.exception(
                    "failed to construct session {} title={!r} despite thread existing",
                    row.session_key, title,
                )
                continue
            self._sessions[int(row.native_id)] = scope
            logger.info("resumed session {} title={!r}", row.session_key, title)

    async def handle_message(self, thread_id: int, text: str) -> None:
        scope = self._sessions.get(thread_id)
        if scope is None:
            return
        await scope.queue.put(text)

    async def _start_thread_session(self, thread):
        logger.info("starting new session in thread {}", thread.id)
        scope = await self._plugin_manager.start_session(
            channel="discord",
            native_id=str(thread.id),
            channel_plugin_factory=lambda: DiscordThreadPlugin(thread),
            reason="new",
        )
        self._sessions[thread.id] = scope
        return scope

    async def handle_start_command(
        self, create_thread: Callable[[], Awaitable[object]], respond: Callable[[str], Awaitable[None]]
    ) -> None:
        thread = await create_thread()
        await self._start_thread_session(thread)
        await respond(f"Started session in thread {thread.id}")

    async def handle_mention(self, create_thread: Callable[[], Awaitable[object]], text: str) -> None:
        if not text:
            return
        thread = await create_thread()
        scope = await self._start_thread_session(thread)
        await scope.queue.put(text)

    async def handle_stop_command(self, thread_id: int, archive: Callable[[], Awaitable[None]]) -> None:
        scope = self._sessions.get(thread_id)
        if scope is None:
            logger.warning("stop command for unknown thread {}", thread_id)
            return
        logger.info("stopping session in thread {}", thread_id)
        await scope.bus.post("steering.high", SteeringStopCommand())
        scope.closing = True
        self._sessions.pop(thread_id, None)
        await archive()
