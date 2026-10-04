from conic.plugins import Plugin, meta
from conic.types.messages import BuildSystemPrompt


class IdentitySectionPlugin(Plugin):
    Name = "identity"
    def __init__(self, content: str):
        self._content = content

    def register(self, bus) -> None:
        bus.on_chain(meta.BuildSystemPromptEvent, self.contribute)

    async def contribute(self, msg: BuildSystemPrompt) -> BuildSystemPrompt | None:
        msg.sections["identity"] = self._content
        return msg
