from conic.core.messages import BeforeModelCall
from conic.plugins.context.truncator import TruncatorPlugin


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
