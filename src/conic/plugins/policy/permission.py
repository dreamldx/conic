from conic.types.messages import ToolCall
from conic.plugins import meta


class PermissionPolicyPlugin:
    def register(self, bus) -> None:
        bus.on_chain(meta.ToolCallEvent, self.check)

    async def check(self, ctx: ToolCall) -> None:
        return None
