import pytest

from conic.core.bus import MessageBus
from conic.core.messages import (
    BeforeModelCall, BeforeSummarize, SummarizeDone, SummarizeFailed,
    SummarizeRequest, SummarizeResult,
)
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


async def test_before_summarize_hook_can_customize_instructions():
    plugin = TokenBudgetPlugin(budget_tokens=1)
    bus = MessageBus()

    captured_requests = []

    async def fake_summarizer(req: SummarizeRequest):
        captured_requests.append(req)
        return SummarizeResult(messages=[{"role": "system", "content": "summary"}])

    async def customize(msg: BeforeSummarize) -> BeforeSummarize:
        msg.request.instructions = "focus on decisions only"
        return msg

    bus.on_request("summarize", fake_summarizer)
    bus.on("before_summarize", customize)
    plugin.register(bus)

    ctx = BeforeModelCall(messages=[{"role": "user", "content": "hi " * 100}], tools=[])
    await plugin.apply(ctx)

    assert captured_requests[0].instructions == "focus on decisions only"


async def test_before_summarize_hook_can_cancel_and_skip_summarization():
    plugin = TokenBudgetPlugin(budget_tokens=1)
    bus = MessageBus()

    called = []

    async def fake_summarizer(req: SummarizeRequest):
        called.append(True)
        return SummarizeResult(messages=[])

    async def cancel(msg: BeforeSummarize) -> BeforeSummarize:
        msg.cancelled = True
        return msg

    bus.on_request("summarize", fake_summarizer)
    bus.on("before_summarize", cancel)
    plugin.register(bus)

    ctx = BeforeModelCall(messages=[{"role": "user", "content": "hi " * 100}], tools=[])
    result = await plugin.apply(ctx)

    assert result is None
    assert called == []


async def test_summarize_done_emitted_with_result_on_success():
    plugin = TokenBudgetPlugin(budget_tokens=1)
    bus = MessageBus()

    summary_result = SummarizeResult(messages=[{"role": "system", "content": "summary"}])

    async def fake_summarizer(req: SummarizeRequest):
        return summary_result

    done = []

    async def on_done(msg: SummarizeDone) -> None:
        done.append(msg.result)

    bus.on_request("summarize", fake_summarizer)
    bus.on("summarize_done", on_done)
    plugin.register(bus)

    ctx = BeforeModelCall(messages=[{"role": "user", "content": "hi " * 100}], tools=[])
    await plugin.apply(ctx)

    assert done == [summary_result]


async def test_summarize_failed_emitted_and_reraised_on_error():
    plugin = TokenBudgetPlugin(budget_tokens=1)
    bus = MessageBus()

    boom = RuntimeError("summarizer backend down")

    async def failing_summarizer(req: SummarizeRequest):
        raise boom

    failed = []

    async def on_failed(msg: SummarizeFailed) -> None:
        failed.append(msg.exc)

    bus.on_request("summarize", failing_summarizer)
    bus.on("summarize_failed", on_failed)
    plugin.register(bus)

    ctx = BeforeModelCall(messages=[{"role": "user", "content": "hi " * 100}], tools=[])
    with pytest.raises(RuntimeError):
        await plugin.apply(ctx)

    assert failed == [boom]
