from datetime import datetime, timezone

from conic.types.messages import TurnStart
from conic.plugins import meta


class TurnVariableUpdaterPlugin:
    def register(self, bus) -> None:
        bus.on_chain(meta.TurnStartEvent, self.contribute)

    async def contribute(self, msg: TurnStart) -> TurnStart:
        msg.variables["turn"]["now"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return msg
