import platform
from datetime import datetime, timezone

from conic.types.messages import BuildSystemPrompt
from conic.plugins import meta


class RuntimeSectionPlugin:
    def __init__(self, model: str):
        self._model = model

    def register(self, bus) -> None:
        bus.on(meta.BuildSystemPromptEvent, self.contribute)

    async def contribute(self, msg: BuildSystemPrompt) -> BuildSystemPrompt | None:
        now = datetime.now(timezone.utc)
        msg.sections["runtime"] = (
            f"Platform: {platform.system()} {platform.release()}\n"
            f"Shell: PowerShell 5.1\n"
            f"Model: {self._model}\n"
            f"Current UTC time: {now.isoformat(timespec='seconds')}"
        )
        return msg
