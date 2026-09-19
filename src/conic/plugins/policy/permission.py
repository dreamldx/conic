from conic.plugins import meta
from conic.types.messages import ToolCall


class PermissionPolicyPlugin:
    def register(self, bus) -> None:
        bus.on_chain(meta.ToolCallEvent, self.check)

    async def check(self, ctx: ToolCall) -> None:
        return None
