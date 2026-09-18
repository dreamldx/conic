import asyncio

from conic.plugins import meta
from conic.types.messages import Input
from conic.types.session import SessionScope
from conic.types.steering import SteeringUserMessage

DEFAULT_POLL_INTERVAL = 0.5


class SessionGatewayPlugin:
    """Channel-agnostic per-session coroutine: drains scope.queue (fed by the
    channel-specific process-singleton gateway's on_message), runs the
    InputEvent interception chain, and posts unhandled text as a
    SteeringUserMessage into steering.high.

    Polls instead of blocking forever on queue.get() because asyncio gives no
    way to wake a coroutine parked in an await from the outside except by
    putting something into what it's waiting on — so scope.closing can only
    be noticed on a timeout tick.
    """

    def __init__(self, scope: SessionScope, poll_interval: float = DEFAULT_POLL_INTERVAL):
        self._scope = scope
        self._poll_interval = poll_interval
        self._bus = None

    def register(self, bus) -> None:
        self._bus = bus

    async def run(self) -> None:
        bus = self._bus
        queue = self._scope.queue
        while True:
            try:
                text = await asyncio.wait_for(queue.get(), timeout=self._poll_interval)
            except TimeoutError:
                text = None
            if self._scope.closing:
                return
            if text is None:
                continue
            ctx = await bus.chain(meta.InputEvent, Input(text=text))
            if ctx.handled:
                continue
            await bus.post("steering.high", SteeringUserMessage(ctx.text))
