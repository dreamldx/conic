from datetime import datetime, timezone

from conic.core.bus import MessageBus
from conic.core.gateway import Gateway
from conic.core.session import SessionScope
from conic.services.storage import SessionRow


def test_session_scope_holds_bus_and_row():
    row = SessionRow(
        session_key="discord:1", channel="discord", native_id="1",
        workspace_dir="/tmp/ws", model="m", status="active",
        created_at=datetime.now(timezone.utc),
    )
    bus = MessageBus()
    scope = SessionScope(bus=bus, row=row)
    assert scope.bus is bus
    assert scope.row is row


def test_gateway_protocol_is_satisfied_by_a_minimal_implementation():
    class FakeGateway:
        name = "fake"

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    gw: Gateway = FakeGateway()
    assert gw.name == "fake"
