from conic.core.messages import BeforeModelCall, SummarizeRequest
from conic.core.tokencount import estimate_tokens


class TokenBudgetPlugin:
    def __init__(self, budget_tokens: int):
        self._budget_tokens = budget_tokens
        self._bus = None

    def register(self, bus) -> None:
        self._bus = bus
        bus.on("before_model_call", self.apply)

    async def apply(self, ctx: BeforeModelCall) -> BeforeModelCall | None:
        if estimate_tokens(ctx.messages) <= self._budget_tokens:
            return None
        result = await self._bus.request(
            "summarize", SummarizeRequest(messages=ctx.messages, budget_tokens=self._budget_tokens)
        )
        return BeforeModelCall(messages=result.messages, tools=ctx.tools)
