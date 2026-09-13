from conic.core.messages import BeforeModelCall


class SystemPromptPlugin:
    def __init__(self, prompt: str):
        self._prompt = prompt

    def register(self, bus) -> None:
        bus.on("before_model_call", self.apply)

    async def apply(self, ctx: BeforeModelCall) -> BeforeModelCall | None:
        if ctx.messages and ctx.messages[0].get("role") == "system":
            return None
        return BeforeModelCall(
            messages=[{"role": "system", "content": self._prompt}, *ctx.messages],
            tools=ctx.tools,
        )
