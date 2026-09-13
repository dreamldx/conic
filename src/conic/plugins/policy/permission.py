from conic.core.messages import ToolCall


class PermissionPolicyPlugin:
    def register(self, bus) -> None:
        bus.on("before_tool_call", self.check)

    async def check(self, ctx: ToolCall) -> None:
        return None
