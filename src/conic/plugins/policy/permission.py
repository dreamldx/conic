from conic.plugins import Plugin, meta
from conic.types.messages import ToolCall


class PermissionPolicyPlugin(Plugin):
    Name = "permission"
    def register(self, bus) -> None:
        bus.on_chain(meta.ToolCallEvent, self.check)

    async def check(self, ctx: ToolCall) -> None:
        return None
