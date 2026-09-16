from conic.types.messages import BuildDynamicPrompt
from conic.plugins import meta


class DynamicStateSectionPlugin:
    def register(self, bus) -> None:
        bus.on(meta.BuildDynamicPromptEvent, self.contribute)

    async def contribute(self, msg: BuildDynamicPrompt) -> BuildDynamicPrompt:
        msg.sections["state"] = (
            "Current time: {{ turn.now }}\n"
            "Current step: {{ turn.step_count }}\n"
            "Tokens used: {{ session.tokens_used }}\n"
            "Turns so far this session: {{ session.turn_count }}"
        )
        return msg
