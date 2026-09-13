from conic.core.messages import BeforeModelCall
from conic.plugins.context.system_prompt import SystemPromptPlugin


async def test_injects_system_prompt_when_missing():
    plugin = SystemPromptPlugin(prompt="You are Conic.")
    ctx = BeforeModelCall(messages=[{"role": "user", "content": "hi"}], tools=[])
    result = await plugin.apply(ctx)
    assert result.messages[0] == {"role": "system", "content": "You are Conic."}
    assert result.messages[1] == {"role": "user", "content": "hi"}


async def test_does_not_duplicate_existing_system_prompt():
    plugin = SystemPromptPlugin(prompt="You are Conic.")
    ctx = BeforeModelCall(
        messages=[{"role": "system", "content": "You are Conic."}, {"role": "user", "content": "hi"}],
        tools=[],
    )
    result = await plugin.apply(ctx)
    assert result is None
