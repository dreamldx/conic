from conic.plugins.policy.permission import PermissionPolicyPlugin
from conic.types.messages import ToolCall, ToolCallSpec


async def test_v1_allows_every_tool_call():
    plugin = PermissionPolicyPlugin()
    ctx = ToolCall(call=ToolCallSpec(id="1", name="bash", args={"command": "ls"}))
    result = await plugin.check(ctx)
    assert result is None
