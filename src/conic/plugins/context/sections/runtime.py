from conic.plugins import Plugin, meta
from conic.types.messages import BuildSystemPrompt


class RuntimeSectionPlugin(Plugin):
    Name = "runtime"
    def register(self, bus) -> None:
        bus.on_chain(meta.BuildSystemPromptEvent, self.contribute)

    async def contribute(self, msg: BuildSystemPrompt) -> BuildSystemPrompt:
        msg.sections["runtime"] = (
            "Platform: {{ global.platform }}\n"
            "Shell: {{ global.shell }}\n"
            "Model: {{ global.model }}\n"
            "Timezone: {{ global.timezone }}"
        )
        return msg
