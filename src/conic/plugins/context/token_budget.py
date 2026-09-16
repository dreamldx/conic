from conic.types.messages import BeforeModelCall, BeforeSummarize, SummarizeDone, SummarizeFailed, SummarizeRequest
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
        request = SummarizeRequest(messages=ctx.messages, budget_tokens=self._budget_tokens)
        before = await self._bus.emit(meta.BeforeSummarizeEvent, BeforeSummarize(request=request))
        if before.cancelled:
            return None
        try:
            result = await self._bus.request(meta.SummarizeEvent, before.request)
        except Exception as exc:
            await self._bus.emit(meta.SummarizeFailedEvent, SummarizeFailed(exc=exc))
            raise
        await self._bus.emit(meta.SummarizeDoneEvent, SummarizeDone(result=result))
        return BeforeModelCall(messages=result.messages, tools=ctx.tools, variables=ctx.variables)
