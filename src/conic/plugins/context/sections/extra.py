from conic.types.messages import BuildSystemPrompt
from conic.plugins import meta


class ExtraPromptPlugin:
    def register(self, bus) -> None:
        bus.on(meta.BuildSystemPromptEvent, self.contribute)

    async def contribute(self, msg: BuildSystemPrompt) -> BuildSystemPrompt:
        msg.sections["extra"] = (
            "Platform: {{ global.platform }}\n"
            "Shell: PowerShell 5.1\n"
            "Model: {{ global.model }}\n"
            "Timezone: {{ global.timezone }}\n"
            "Current time: {{ turn.now }}\n"
            "Current step: {{ turn.step_count }}\n"
            "Tokens used: {{ session.tokens_used }}\n"
            "Turns so far this session: {{ session.turn_count }}"
        )
        return msg
