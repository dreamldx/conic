from dataclasses import dataclass

import pytest

from conic.core.bus import MessageBus, infer_payload_type
from conic.types.errors import DuplicateResponderError, NoResponderError


@dataclass
class Ping:
    n: int


@dataclass
class Pong:
    n: int


def test_infer_payload_type_from_handler_annotation():
    async def handler(msg: Ping) -> None:
        return None

    assert infer_payload_type(handler) is Ping


def test_infer_payload_type_ignores_self():
    class Handler:
        async def handle(self, msg: Ping) -> None:
            return None

    assert infer_payload_type(Handler.handle) is Ping


async def test_emit_calls_handlers_in_registration_order():
    bus = MessageBus()
    calls = []

    async def first(msg: Ping):
        calls.append("first")
        return None

    async def second(msg: Ping):
        calls.append("second")
        return None

    bus.on("ping", first)
    bus.on("ping", second)
    await bus.emit("ping", Ping(n=1))
    assert calls == ["first", "second"]


async def test_emit_chains_mutated_payload_to_next_handler():
    bus = MessageBus()

    async def increment(msg: Ping) -> Ping:
        return Ping(n=msg.n + 1)

    async def double(msg: Ping) -> Ping:
        return Ping(n=msg.n * 2)

    bus.on("ping", increment)
    bus.on("ping", double)
    result = await bus.emit("ping", Ping(n=1))
    assert result.n == 4  # (1 + 1) * 2


async def test_emit_keeps_current_payload_when_handler_returns_none():
    bus = MessageBus()

    async def observer(msg: Ping) -> None:
        return None

    bus.on("ping", observer)
    result = await bus.emit("ping", Ping(n=5))
    assert result.n == 5


async def test_emit_only_calls_handlers_matching_payload_type():
    bus = MessageBus()
    seen = []

    async def on_ping(msg: Ping):
        seen.append(("ping", msg.n))

    async def on_pong(msg: Pong):
        seen.append(("pong", msg.n))

    bus.on("event", on_ping)
    bus.on("event", on_pong)
    await bus.emit("event", Ping(n=1))
    assert seen == [("ping", 1)]


async def test_request_returns_single_responder_result():
    bus = MessageBus()

    async def responder(msg: Ping) -> Pong:
        return Pong(n=msg.n + 100)

    bus.on_request("ask", responder)
    result = await bus.request("ask", Ping(n=1))
    assert result == Pong(n=101)


def test_on_request_rejects_duplicate_responder_for_same_topic_and_type():
    bus = MessageBus()

    async def responder_a(msg: Ping) -> Pong:
        return Pong(n=1)

    async def responder_b(msg: Ping) -> Pong:
        return Pong(n=2)

    bus.on_request("ask", responder_a)
    with pytest.raises(DuplicateResponderError):
        bus.on_request("ask", responder_b)


def test_on_request_allows_same_topic_different_payload_type():
    bus = MessageBus()

    async def responder_a(msg: Ping) -> Pong:
        return Pong(n=1)

    async def responder_b(msg: Pong) -> Ping:
        return Ping(n=2)

    bus.on_request("ask", responder_a)
    bus.on_request("ask", responder_b)  # must not raise


async def test_request_raises_when_no_responder_matches():
    bus = MessageBus()
    with pytest.raises(NoResponderError):
        await bus.request("ask", Ping(n=1))
