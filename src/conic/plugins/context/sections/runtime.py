from conic.types.messages import BuildSystemPrompt
from conic.plugins import meta


class RuntimeSectionPlugin:
    def register(self, bus) -> None:
        bus.on(meta.BuildSystemPromptEvent, self.contribute)

    async def contribute(self, msg: BuildSystemPrompt) -> BuildSystemPrompt:
        msg.sections["runtime"] = (
            "Platform: {{ global.platform }}\n"
            "Shell: {{ global.shell }}\n"
            "Model: {{ global.model }}\n"
            "Timezone: {{ global.timezone }}"
        )
        return msg
