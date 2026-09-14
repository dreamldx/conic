from conic.core.messages import BuildSystemPrompt
from conic.plugins import meta


class ExecutionBiasSectionPlugin:
    def register(self, bus) -> None:
        bus.on(meta.BuildSystemPromptEvent, self.contribute)

    async def contribute(self, msg: BuildSystemPrompt) -> BuildSystemPrompt | None:
        msg.sections["execution"] = (
            "- Act on actionable requests immediately; continue until done or blocked."
            "- Verify results before claiming completion."
            "- If a tool fails, try an alternative approach before giving up."
            "- Check mutable state (files, process output) live; don't assume."
            "- Handle errors gracefully and report them clearly."
        )
        return msg
