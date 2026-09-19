from conic.core.bus import MessageBus
from conic.plugins import meta
from conic.plugins.context.variables import TurnVariableUpdaterPlugin
from conic.types.messages import TurnStart


def make_plugin():
    bus = MessageBus()
    plugin = TurnVariableUpdaterPlugin()
    plugin.register(bus)
    return plugin, bus


async def test_contribute_populates_now_under_turn_scope():
    plugin, _bus = make_plugin()
    msg = TurnStart(variables={"global": {}, "session": {}, "turn": {}})
    result = await plugin.contribute(msg)
    assert "now" in result.variables["turn"]


async def test_contribute_preserves_existing_turn_variables():
    plugin, _bus = make_plugin()
    msg = TurnStart(variables={"global": {}, "session": {}, "turn": {"custom": "value"}})
    result = await plugin.contribute(msg)
    assert result.variables["turn"]["custom"] == "value"
    assert "now" in result.variables["turn"]


async def test_emit_on_turn_start_populates_turn_variables():
    _plugin, bus = make_plugin()
    result = await bus.chain(
        meta.TurnStartEvent, TurnStart(variables={"global": {}, "session": {}, "turn": {}})
    )
    assert "now" in result.variables["turn"]
