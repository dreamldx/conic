from conic.core.bus import MessageBus
from conic.plugins.context.summarizer import SummarizerPlugin
from conic.types.messages import ModelRequest, ModelResponse, SummarizeRequest


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


async def test_summarize_does_not_orphan_a_tool_reply_at_the_recent_cut_boundary():
    """A naive last-keep_recent cut landing between a tool_calls assistant
    message and its tool reply must not produce an orphaned `role: "tool"`
    message in the returned messages."""
    plugin = SummarizerPlugin(keep_recent=2)
    bus = MessageBus()

    async def fake_model_request(msg: ModelRequest):
        return ModelResponse(text="summary", tool_calls=[], raw_message={})

    bus.on_request("model_request", fake_model_request)
    plugin.register(bus)

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "0"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "tool result"},
        {"role": "user", "content": "3"},
    ]
    # Naive last-2 cut would take [tool(c1), user(3)] as "recent" -- orphaned tool reply.
    result = await plugin.summarize(SummarizeRequest(messages=messages, budget_tokens=1))

    for i, m in enumerate(result.messages):
        if m.get("role") == "tool":
            assert i > 0 and result.messages[i - 1].get("role") == "assistant" and result.messages[i - 1].get(
                "tool_calls"
            )


async def test_summarize_returns_input_unchanged_when_nothing_to_summarize():
    plugin = SummarizerPlugin(keep_recent=5)
    bus = MessageBus()
    plugin.register(bus)
    messages = [{"role": "user", "content": "only one"}]
    result = await plugin.summarize(SummarizeRequest(messages=messages, budget_tokens=1))
    assert result.messages == messages


async def test_summarize_uses_custom_instructions_when_provided():
    plugin = SummarizerPlugin(keep_recent=1)
    bus = MessageBus()

    captured_prompts = []

    async def fake_model_request(msg: ModelRequest):
        captured_prompts.append(msg.messages)
        return ModelResponse(text="summary", tool_calls=[], raw_message={})

    bus.on_request("model_request", fake_model_request)
    plugin.register(bus)

    messages = [
        {"role": "user", "content": "old"},
        {"role": "user", "content": "recent"},
    ]
    await plugin.summarize(
        SummarizeRequest(messages=messages, budget_tokens=1, instructions="focus on decisions only")
    )

    assert captured_prompts[0][0] == {"role": "system", "content": "focus on decisions only"}
