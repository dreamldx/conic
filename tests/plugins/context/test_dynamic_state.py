from conic.core.bus import MessageBus
from conic.plugins.context.sections.dynamic_state import DynamicStateSectionPlugin
from conic.types.messages import BuildDynamicPrompt


async def test_dynamic_state_section_contributes_turn_and_session_jinja_placeholders():
    bus = MessageBus()
    plugin = DynamicStateSectionPlugin()
    plugin.register(bus)
    msg = BuildDynamicPrompt(sections={})
    result = await bus.emit("build_dynamic_prompt", msg)
    assert "state" in result.sections
    assert "{{ turn.now }}" in result.sections["state"]
    assert "{{ turn.step_count }}" in result.sections["state"]
    assert "{{ session.tokens_used }}" in result.sections["state"]
    assert "{{ session.turn_count }}" in result.sections["state"]
