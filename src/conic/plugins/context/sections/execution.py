from conic.types.messages import BuildSystemPrompt
from conic.plugins import meta


class ExecutionBiasSectionPlugin:
    def __init__(self, content: str):
        self._content = content

    def register(self, bus) -> None:
        bus.on_chain(meta.BuildSystemPromptEvent, self.contribute)

    async def contribute(self, msg: BuildSystemPrompt) -> BuildSystemPrompt | None:
        msg.sections["execution"] = self._content
        return msg
