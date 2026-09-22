from conic.plugins import meta
from conic.types.messages import BuildDynamicPrompt


class DynamicStateSectionPlugin:
    def register(self, bus) -> None:
        bus.on_chain(meta.BuildDynamicPromptEvent, self.contribute)

    async def contribute(self, msg: BuildDynamicPrompt) -> BuildDynamicPrompt:
        msg.sections["state"] = (
            "Current time: {{ turn.now }}\n"
            "Current step: {{ turn.step_count }}\n"
            "Current turns: {{ session.turn_count }}\n"
            "Session total tokens used: {{ session.tokens_used }} tokens\n"
            "Context window used: {{ session.context_usage }} tokens\n"
            "Model context window: {{ global.model_context_length }} tokens"
        )
        return msg
