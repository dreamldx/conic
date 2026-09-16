from conic.core.messagealign import align_cut
from conic.types.messages import BeforeModelCall
from conic.plugins import meta


class TruncatorPlugin:
    def __init__(self, keep_last_n: int):
        self._keep_last_n = keep_last_n

    def register(self, bus) -> None:
        bus.on(meta.BeforeModelCallEvent, self.apply)

    async def apply(self, ctx: BeforeModelCall) -> BeforeModelCall | None:
        non_system = [m for m in ctx.messages if m.get("role") != "system"]
        if len(non_system) <= self._keep_last_n:
            return None
        system = [m for m in ctx.messages if m.get("role") == "system"]
        cut_index = align_cut(non_system, len(non_system) - self._keep_last_n)
        kept = non_system[cut_index:]
        return BeforeModelCall(messages=[*system, *kept], tools=ctx.tools, variables=ctx.variables)
