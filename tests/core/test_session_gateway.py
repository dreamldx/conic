import asyncio
from datetime import UTC, datetime

from conic.core.bus import MessageBus
from conic.core.session_gateway import SessionGatewayPlugin
from conic.plugins import meta
from conic.services.models import Session
from conic.types.messages import Input
from conic.types.session import SessionScope
from conic.types.steering import SteeringItem


def make_scope():
    row = Session(
        session_key="discord:1", channel="discord", native_id="1",
        workspace_dir="/tmp/ws", model="m", status="active",
        created_at=datetime.now(UTC),
    )
    bus = MessageBus()
    bus.create_mailbox("steering.high", SteeringItem)
    return SessionScope(bus=bus, row=row)


async def test_posts_a_steering_user_message_for_a_queued_line():
    scope = make_scope()
    plugin = SessionGatewayPlugin(scope, poll_interval=0.05)
    plugin.register(scope.bus)

    await scope.queue.put("hello")
    task = asyncio.ensure_future(plugin.run())
    await asyncio.sleep(0.02)
    scope.closing = True
    await asyncio.wait_for(task, timeout=1.0)

    posted = await scope.bus.drain("steering.high")
    assert [item.text for item in posted] == ["hello"]


async def test_input_hook_can_transform_text_before_posting():
    scope = make_scope()
    plugin = SessionGatewayPlugin(scope, poll_interval=0.05)
    plugin.register(scope.bus)

    async def upcase(msg: Input) -> Input:
        return Input(text=msg.text.upper())

    scope.bus.on_chain(meta.InputEvent, upcase)

    await scope.queue.put("hello")
    task = asyncio.ensure_future(plugin.run())
    await asyncio.sleep(0.02)
    scope.closing = True
    await asyncio.wait_for(task, timeout=1.0)

    posted = await scope.bus.drain("steering.high")
    assert [item.text for item in posted] == ["HELLO"]


async def test_input_hook_marking_handled_short_circuits_before_posting():
    scope = make_scope()
    plugin = SessionGatewayPlugin(scope, poll_interval=0.05)
    plugin.register(scope.bus)

    async def handle_command(msg: Input) -> Input:
        if msg.text == "!status":
            return Input(text=msg.text, handled=True)
        return msg

    scope.bus.on_chain(meta.InputEvent, handle_command)

    await scope.queue.put("!status")
    task = asyncio.ensure_future(plugin.run())
    await asyncio.sleep(0.02)
    scope.closing = True
    await asyncio.wait_for(task, timeout=1.0)

    posted = await scope.bus.drain("steering.high")
    assert posted == []


async def test_run_exits_promptly_once_closing_is_set_with_no_messages():
    scope = make_scope()
    plugin = SessionGatewayPlugin(scope, poll_interval=0.05)
    plugin.register(scope.bus)

    task = asyncio.ensure_future(plugin.run())
    await asyncio.sleep(0.02)
    assert not task.done()

    scope.closing = True
    await asyncio.wait_for(task, timeout=1.0)


async def test_run_processes_multiple_queued_messages_in_order():
    scope = make_scope()
    plugin = SessionGatewayPlugin(scope, poll_interval=0.05)
    plugin.register(scope.bus)

    await scope.queue.put("first")
    await scope.queue.put("second")
    task = asyncio.ensure_future(plugin.run())
    await asyncio.sleep(0.02)
    scope.closing = True
    await asyncio.wait_for(task, timeout=1.0)

    posted = await scope.bus.drain("steering.high")
    assert [item.text for item in posted] == ["first", "second"]
