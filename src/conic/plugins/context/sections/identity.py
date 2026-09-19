from conic.plugins import meta
from conic.types.messages import BuildSystemPrompt


class IdentitySectionPlugin:
    def __init__(self, content: str):
        self._content = content

    def register(self, bus) -> None:
        bus.on_chain(meta.BuildSystemPromptEvent, self.contribute)

    async def contribute(self, msg: BuildSystemPrompt) -> BuildSystemPrompt | None:
        msg.sections["identity"] = self._content
        return msg
