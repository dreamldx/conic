from typing import Awaitable, Callable

import discord
from discord import app_commands
from loguru import logger

from conic.core.messages import TurnEnd, UserInput
from conic.plugins.channels.discord import DiscordThreadPlugin
from conic.plugins import meta


class DiscordGateway:
    name = "discord"

    def __init__(self, bot_token: str, plugin_manager, storage):
        self._token = bot_token
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
            await self.handle_message(thread_id=message.channel.id, text=message.content)

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
                scope = self._plugin_manager.start_session(
                    channel="discord",
                    native_id=row.native_id,
                    channel_plugin_factory=lambda t=thread: DiscordThreadPlugin(t),
                )
            except Exception:
                logger.warning("failed to resume session {} (thread deleted/missing)", row.session_key)
                self._storage.handle_for(row).set_status("ended")
                continue
            self._sessions[int(row.native_id)] = scope

    async def handle_message(self, thread_id: int, text: str) -> None:
        scope = self._sessions.get(thread_id)
        if scope is None:
            return
        async with scope.lock:
            await scope.bus.emit(meta.UserInputEvent, UserInput(text=text))

    async def handle_start_command(
        self, create_thread: Callable[[], Awaitable[object]], respond: Callable[[str], Awaitable[None]]
    ) -> None:
        thread = await create_thread()
        logger.info("starting new session in thread {}", thread.id)
        scope = self._plugin_manager.start_session(
            channel="discord",
            native_id=str(thread.id),
            channel_plugin_factory=lambda: DiscordThreadPlugin(thread),
        )
        self._sessions[thread.id] = scope
        await respond(f"Started session in thread {thread.id}")

    async def handle_stop_command(self, thread_id: int, archive: Callable[[], Awaitable[None]]) -> None:
        scope = self._sessions.pop(thread_id, None)
        if scope is None:
            logger.warning("stop command for unknown thread {}", thread_id)
            return
        logger.info("stopping session in thread {}", thread_id)
        await scope.bus.emit(meta.SessionStopEvent, TurnEnd())
        self._plugin_manager.stop_session(scope)
        await archive()
