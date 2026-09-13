from conic.core.bus import MessageBus
from conic.core.messages import ModelRequest, ModelResponse, SummarizeRequest
from conic.plugins.context.summarizer import SummarizerPlugin


async def test_summarize_keeps_recent_messages_and_replaces_older_ones_with_summary():
    plugin = SummarizerPlugin(keep_recent=1)
    bus = MessageBus()

    async def fake_model_request(msg: ModelRequest):
        return ModelResponse(text="summary of earlier turns", tool_calls=[], raw_message={})

    bus.on_request("model_request", fake_model_request)
    plugin.register(bus)

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old-1"},
        {"role": "assistant", "content": "old-2"},
        {"role": "user", "content": "recent"},
    ]
    result = await plugin.summarize(SummarizeRequest(messages=messages, budget_tokens=1))

    assert result.messages[0] == {"role": "system", "content": "sys"}
    assert "summary of earlier turns" in result.messages[1]["content"]
    assert result.messages[2] == {"role": "user", "content": "recent"}


async def test_summarize_returns_input_unchanged_when_nothing_to_summarize():
    plugin = SummarizerPlugin(keep_recent=5)
    bus = MessageBus()
    plugin.register(bus)
    messages = [{"role": "user", "content": "only one"}]
    result = await plugin.summarize(SummarizeRequest(messages=messages, budget_tokens=1))
    assert result.messages == messages
