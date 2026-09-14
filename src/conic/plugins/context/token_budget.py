from conic.core.messages import BeforeModelCall, SummarizeRequest
from conic.core.tokencount import estimate_tokens
from conic.plugins import meta


class TokenBudgetPlugin:
    def __init__(self, budget_tokens: int):
        self._budget_tokens = budget_tokens
        self._bus = None

    def register(self, bus) -> None:
        self._bus = bus
        bus.on(meta.BeforeModelCallEvent, self.apply)

    async def apply(self, ctx: BeforeModelCall) -> BeforeModelCall | None:
        if estimate_tokens(ctx.messages) <= self._budget_tokens:
            return None
        result = await self._bus.request(
            meta.SummarizeEvent, SummarizeRequest(messages=ctx.messages, budget_tokens=self._budget_tokens)
        )
        return BeforeModelCall(messages=result.messages, tools=ctx.tools)
