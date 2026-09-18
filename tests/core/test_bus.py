import asyncio
from dataclasses import dataclass

import pytest
from loguru import logger

from conic.core.bus import MessageBus, infer_payload_type
from conic.types.errors import (
    DuplicateMailboxError,
    DuplicateResponderError,
    NoResponderError,
    UnknownMailboxError,
)


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


async def test_chain_calls_handlers_in_registration_order():
    bus = MessageBus()
    calls = []

    async def first(msg: Ping):
        calls.append("first")
        return None

    async def second(msg: Ping):
        calls.append("second")
        return None

    bus.on_chain("ping", first)
    bus.on_chain("ping", second)
    await bus.chain("ping", Ping(n=1))
    assert calls == ["first", "second"]


async def test_chain_chains_mutated_payload_to_next_handler():
    bus = MessageBus()

    async def increment(msg: Ping) -> Ping:
        return Ping(n=msg.n + 1)

    async def double(msg: Ping) -> Ping:
        return Ping(n=msg.n * 2)

    bus.on_chain("ping", increment)
    bus.on_chain("ping", double)
    result = await bus.chain("ping", Ping(n=1))
    assert result.n == 4  # (1 + 1) * 2


async def test_chain_keeps_current_payload_when_handler_returns_none():
    bus = MessageBus()

    async def observer(msg: Ping) -> None:
        return None

    bus.on_chain("ping", observer)
    result = await bus.chain("ping", Ping(n=5))
    assert result.n == 5


async def test_chain_only_calls_handlers_matching_payload_type():
    bus = MessageBus()
    seen = []

    async def on_ping(msg: Ping):
        seen.append(("ping", msg.n))

    async def on_pong(msg: Pong):
        seen.append(("pong", msg.n))

    bus.on_chain("event", on_ping)
    bus.on_chain("event", on_pong)
    await bus.chain("event", Ping(n=1))
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


async def test_chain_logs_info_when_a_handler_is_skipped_for_type_mismatch():
    bus = MessageBus()

    async def on_ping(msg: Ping):
        return None

    async def on_pong(msg: Pong):
        return None

    bus.on_chain("event", on_ping)
    bus.on_chain("event", on_pong)

    logged = []
    sink_id = logger.add(lambda msg: logged.append(msg.record["message"]), level="INFO")
    try:
        await bus.chain("event", Ping(n=1))
    finally:
        logger.remove(sink_id)

    assert len(logged) == 1
    assert "chain interrupted" in logged[0]
    assert "event" in logged[0]
    assert "Pong" in logged[0]
    assert "Ping" in logged[0]


async def test_chain_does_not_log_when_all_handlers_match():
    bus = MessageBus()

    async def on_ping(msg: Ping):
        return None

    bus.on_chain("event", on_ping)

    logged = []
    sink_id = logger.add(lambda msg: logged.append(msg.record["message"]), level="INFO")
    try:
        await bus.chain("event", Ping(n=1))
    finally:
        logger.remove(sink_id)

    assert logged == []


# --- mailbox primitives (create_mailbox / post / drain / wait_multiply_mailbox / close) ---


def test_create_mailbox_rejects_duplicate_registration_for_same_name():
    bus = MessageBus()
    bus.create_mailbox("steering.high", Ping)
    with pytest.raises(DuplicateMailboxError):
        bus.create_mailbox("steering.high", Ping)


async def test_post_raises_for_unregistered_mailbox():
    bus = MessageBus()
    with pytest.raises(UnknownMailboxError):
        await bus.post("steering.high", Ping(n=1))


async def test_drain_raises_for_unregistered_mailbox():
    bus = MessageBus()
    with pytest.raises(UnknownMailboxError):
        await bus.drain("steering.high")


async def test_wait_multiply_mailbox_raises_for_unregistered_mailbox():
    bus = MessageBus()
    bus.create_mailbox("steering.high", Ping)
    with pytest.raises(UnknownMailboxError):
        await bus.wait_multiply_mailbox("steering.high", "steering.low")


async def test_post_rejects_mismatched_payload_type():
    bus = MessageBus()
    bus.create_mailbox("steering.high", Ping)
    with pytest.raises(TypeError):
        await bus.post("steering.high", Pong(n=1))


async def test_drain_returns_posted_items_in_order():
    bus = MessageBus()
    bus.create_mailbox("steering.high", Ping)
    await bus.post("steering.high", Ping(n=1))
    await bus.post("steering.high", Ping(n=2))
    result = await bus.drain("steering.high")
    assert result == [Ping(n=1), Ping(n=2)]


async def test_drain_empties_the_mailbox():
    bus = MessageBus()
    bus.create_mailbox("steering.high", Ping)
    await bus.post("steering.high", Ping(n=1))
    await bus.drain("steering.high")
    assert await bus.drain("steering.high") == []


async def test_drain_on_empty_mailbox_returns_empty_list():
    bus = MessageBus()
    bus.create_mailbox("steering.high", Ping)
    assert await bus.drain("steering.high") == []


async def test_wait_multiply_mailbox_returns_immediately_when_mailbox_already_has_items():
    bus = MessageBus()
    bus.create_mailbox("steering.high", Ping)
    bus.create_mailbox("steering.low", Ping)
    await bus.post("steering.high", Ping(n=1))

    await asyncio.wait_for(bus.wait_multiply_mailbox("steering.high", "steering.low"), timeout=0.1)


async def test_wait_multiply_mailbox_blocks_until_matching_mailbox_is_posted_to():
    bus = MessageBus()
    bus.create_mailbox("steering.high", Ping)
    bus.create_mailbox("steering.low", Ping)

    waiter = asyncio.ensure_future(bus.wait_multiply_mailbox("steering.high", "steering.low"))
    await asyncio.sleep(0)
    assert not waiter.done()

    await bus.post("steering.low", Ping(n=1))
    await asyncio.wait_for(waiter, timeout=0.1)


async def test_wait_multiply_mailbox_ignores_posts_to_mailboxes_not_being_waited_on():
    bus = MessageBus()
    bus.create_mailbox("steering.high", Ping)
    bus.create_mailbox("steering.low", Ping)
    bus.create_mailbox("other", Ping)

    waiter = asyncio.ensure_future(bus.wait_multiply_mailbox("steering.high", "steering.low"))
    await asyncio.sleep(0)
    await bus.post("other", Ping(n=1))
    await asyncio.sleep(0)
    assert not waiter.done()

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter


async def test_wait_multiply_mailbox_returns_immediately_after_close():
    bus = MessageBus()
    bus.create_mailbox("steering.high", Ping)
    await bus.close()

    await asyncio.wait_for(bus.wait_multiply_mailbox("steering.high"), timeout=0.1)


async def test_wait_multiply_mailbox_unblocks_when_bus_closes_while_waiting():
    bus = MessageBus()
    bus.create_mailbox("steering.high", Ping)

    waiter = asyncio.ensure_future(bus.wait_multiply_mailbox("steering.high"))
    await asyncio.sleep(0)
    assert not waiter.done()

    await bus.close()
    await asyncio.wait_for(waiter, timeout=0.1)


async def test_post_after_close_is_a_noop_and_logs_warning():
    bus = MessageBus()
    bus.create_mailbox("steering.high", Ping)
    await bus.close()

    logged = []
    sink_id = logger.add(lambda msg: logged.append(msg.record["message"]), level="WARNING")
    try:
        await bus.post("steering.high", Ping(n=1))
    finally:
        logger.remove(sink_id)

    assert await bus.drain("steering.high") == []
    assert len(logged) == 1
    assert "steering.high" in logged[0]
