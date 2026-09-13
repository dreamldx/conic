from conic.core.bus import MessageBus
from conic.core.messages import BeforeModelCall, SummarizeRequest, SummarizeResult
from conic.plugins.context.token_budget import TokenBudgetPlugin


async def test_passes_through_when_under_budget():
    plugin = TokenBudgetPlugin(budget_tokens=10_000)
    bus = MessageBus()
    plugin.register(bus)
    ctx = BeforeModelCall(messages=[{"role": "user", "content": "hi"}], tools=[])
    result = await plugin.apply(ctx)
    assert result is None


async def test_requests_summary_when_over_budget():
    plugin = TokenBudgetPlugin(budget_tokens=1)
    bus = MessageBus()

    async def fake_summarizer(req: SummarizeRequest):
        return SummarizeResult(messages=[{"role": "system", "content": "summary"}])

    bus.on_request("summarize", fake_summarizer)
    plugin.register(bus)

    ctx = BeforeModelCall(messages=[{"role": "user", "content": "hi " * 100}], tools=["schema"])
    result = await plugin.apply(ctx)
    assert result.messages == [{"role": "system", "content": "summary"}]
    assert result.tools == ["schema"]
