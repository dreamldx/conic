from conic.core.messages import BuildSystemPrompt
from conic.plugins import meta


class IdentitySectionPlugin:
    def __init__(self, content: str):
        self._content = content

    def register(self, bus) -> None:
        bus.on(meta.BuildSystemPromptEvent, self.contribute)

    async def contribute(self, msg: BuildSystemPrompt) -> BuildSystemPrompt | None:
        msg.sections["identity"] = self._content
        return msg
