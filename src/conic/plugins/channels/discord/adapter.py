from conic.core.messages import AssistantMessage, Error
from conic.plugins import meta

DISCORD_MESSAGE_LIMIT = 2000
'''
General Permissions
Administrator
View Audit Log
Manage Server
Manage Roles
Kick Members
Ban Members
Create Instant Invite
Change Nickname
Manage Nicknames
Manage Expressions
Create Expressions
Manage Webhooks
Moderate Members
View Server Insights
View Server Subscription Insights
Text Permissions
Manage Threads
Embed Links
Attach Files
Mention Everyone
Use External Emojis
Use External Stickers
Use Slash Commands
Bypass Slowmode
Voice Permissions
Connect
Speak
Video
Mute Members
Deafen Members
Move Members
Use Voice Activity
Priority Speaker
Request To Speak
Use Embedded Activities
Use Soundboard
Use External Sounds
Set Voice Channel Status
'''

class DiscordThreadPlugin:
    def __init__(self, thread):
        self._thread = thread

    def register(self, bus) -> None:
        bus.on(meta.AssistantMessageEvent, self.on_assistant_message)
        bus.on(meta.ErrorEvent, self.on_error)

    async def on_assistant_message(self, msg: AssistantMessage) -> None:
        await self._send(msg.text)

    async def on_error(self, msg: Error) -> None:
        await self._send(f"⚠️ {msg.exc}")

    async def _send(self, text: str) -> None:
        for i in range(0, len(text), DISCORD_MESSAGE_LIMIT):
            await self._thread.send(text[i: i + DISCORD_MESSAGE_LIMIT])
