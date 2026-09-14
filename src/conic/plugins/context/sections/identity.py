from conic.core.messages import BuildSystemPrompt
from conic.plugins import meta


class IdentitySectionPlugin:
    def __init__(self, name: str = "Conic"):
        self._name = name

    def register(self, bus) -> None:
        bus.on(meta.BuildSystemPromptEvent, self.contribute)

    async def contribute(self, msg: BuildSystemPrompt) -> BuildSystemPrompt | None:
        msg.sections["identity"] = (
            f"You are {self._name}, a helpful coding agent. "
            f"You have access to tools scoped to this session's workspace directory."
        )
        return msg
