from datetime import UTC, datetime

from conic.plugins import meta
from conic.types.messages import TurnStart


class TurnVariableUpdaterPlugin:
    def register(self, bus) -> None:
        bus.on_chain(meta.TurnStartEvent, self.contribute)

    async def contribute(self, msg: TurnStart) -> TurnStart:
        msg.variables["turn"]["now"] = datetime.now(UTC).isoformat(timespec="seconds")
        return msg
