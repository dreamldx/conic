from conic.types.messages import BuildSystemPrompt
from conic.plugins import meta


class WorkspaceSectionPlugin:
    def __init__(self, workspace_dir: str):
        self._workspace_dir = workspace_dir

    def register(self, bus) -> None:
        bus.on(meta.BuildSystemPromptEvent, self.contribute)

    async def contribute(self, msg: BuildSystemPrompt) -> BuildSystemPrompt | None:
        msg.sections["workspace"] = f"Workspace directory: {self._workspace_dir}"
        return msg
