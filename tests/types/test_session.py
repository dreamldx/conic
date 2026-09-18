import asyncio
from datetime import datetime, timezone

from conic.core.bus import MessageBus
from conic.types.gateway import Gateway
from conic.types.session import SessionScope
from conic.services.models import Session


def make_row(native_id="1"):
    return Session(
        session_key=f"discord:{native_id}", channel="discord", native_id=native_id,
        workspace_dir="/tmp/ws", model="m", status="active",
        created_at=datetime.now(timezone.utc),
    )


def test_session_scope_holds_bus_and_row():
    row = make_row()
    bus = MessageBus()
    scope = SessionScope(bus=bus, row=row)
    assert scope.bus is bus
    assert scope.row is row


def test_session_scope_starts_not_closing():
    scope = SessionScope(bus=MessageBus(), row=make_row())
    assert scope.closing is False


async def test_session_scope_has_an_asyncio_queue():
    scope = SessionScope(bus=MessageBus(), row=make_row())
    assert isinstance(scope.queue, asyncio.Queue)


async def test_separately_constructed_session_scopes_have_distinct_queues():
    scope1 = SessionScope(bus=MessageBus(), row=make_row("1"))
    scope2 = SessionScope(bus=MessageBus(), row=make_row("2"))
    assert scope1.queue is not scope2.queue


def test_session_scope_tasks_defaults_to_empty_dict():
    scope = SessionScope(bus=MessageBus(), row=make_row())
    assert scope.tasks == {}


def test_gateway_protocol_is_satisfied_by_a_minimal_implementation():
    class FakeGateway:
        name = "fake"

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    gw: Gateway = FakeGateway()
    assert gw.name == "fake"
