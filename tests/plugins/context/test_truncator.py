from conic.plugins.context.truncator import TruncatorPlugin
from conic.types.messages import BeforeModelCall


async def test_passes_through_when_under_limit():
    plugin = TruncatorPlugin(keep_last_n=5)
    messages = [{"role": "user", "content": str(i)} for i in range(3)]
    result = await plugin.apply(BeforeModelCall(messages=messages, tools=[]))
    assert result is None


async def test_keeps_only_last_n_non_system_messages():
    plugin = TruncatorPlugin(keep_last_n=2)
    messages = [{"role": "user", "content": str(i)} for i in range(5)]
    result = await plugin.apply(BeforeModelCall(messages=messages, tools=[]))
    assert result.messages == [{"role": "user", "content": "3"}, {"role": "user", "content": "4"}]


async def test_naive_cut_that_would_orphan_a_tool_reply_keeps_the_pair_together():
    """A naive last-N cut landing between a tool_calls assistant message and
    its tool reply must not produce an orphaned `role: "tool"` message."""
    plugin = TruncatorPlugin(keep_last_n=2)
    messages = [
        {"role": "user", "content": "0"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "tool result"},
        {"role": "user", "content": "3"},
    ]
    # Naive last-2 cut would take [tool(c1), user(3)] -- an orphaned tool reply.
    result = await plugin.apply(BeforeModelCall(messages=messages, tools=[]))

    kept = result.messages if result is not None else messages
    for i, m in enumerate(kept):
        if m.get("role") == "tool":
            assert i > 0 and kept[i - 1].get("role") == "assistant" and kept[i - 1].get("tool_calls")


async def test_always_keeps_system_messages():
    plugin = TruncatorPlugin(keep_last_n=1)
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "0"},
        {"role": "user", "content": "1"},
        {"role": "user", "content": "2"},
    ]
    result = await plugin.apply(BeforeModelCall(messages=messages, tools=[]))
    assert result.messages == [{"role": "system", "content": "sys"}, {"role": "user", "content": "2"}]


async def test_forwards_variables_when_truncating():
    """ExtraPromptPlugin (registered after TruncatorPlugin in the
    BeforeModelCallEvent chain) reads ctx.variables to render its trailing
    message -- if TruncatorPlugin drops variables when it actually truncates,
    that render crashes with a Jinja2 UndefinedError for the missing scope."""
    plugin = TruncatorPlugin(keep_last_n=2)
    messages = [{"role": "user", "content": str(i)} for i in range(5)]
    variables = {"global": {}, "session": {}, "turn": {"now": "T1"}}
    result = await plugin.apply(BeforeModelCall(messages=messages, tools=[], variables=variables))
    assert result.variables == variables
