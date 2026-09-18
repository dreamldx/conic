from conic.types.errors import AbortTurn
from conic.types.messages import StepStart
from conic.plugins import meta


class StepLimitPlugin:
    def __init__(self, max_steps: int):
        self._max_steps = max_steps

    def register(self, bus) -> None:
        bus.on_chain(meta.StepStartEvent, self.check)

    async def check(self, msg: StepStart) -> None:
        if msg.step_index >= self._max_steps:
            raise AbortTurn(f"exceeded max steps ({self._max_steps})")
        return None
