from conic.core.bus import MessageBus
from conic.types.messages import BeforeModelCall
from conic.plugins.context.sections.identity import IdentitySectionPlugin
from conic.plugins.context.system_prompt import SystemPromptPlugin


def make_plugin():
    bus = MessageBus()
    plugin = SystemPromptPlugin([IdentitySectionPlugin("You are Conic, a helpful coding agent.")])
    plugin.register(bus)
    return plugin, bus


async def test_injects_system_prompt_when_missing():
    plugin, _bus = make_plugin()
    ctx = BeforeModelCall(messages=[{"role": "user", "content": "hi"}], tools=[])
    result = await plugin.apply(ctx)
    assert result is not None
    assert result.messages[0]["role"] == "system"
    assert "coding agent" in result.messages[0]["content"]
    assert result.messages[1] == {"role": "user", "content": "hi"}


async def test_does_not_duplicate_existing_system_prompt():
    plugin, _bus = make_plugin()
    ctx = BeforeModelCall(
        messages=[{"role": "system", "content": "already present"}, {"role": "user", "content": "hi"}],
        tools=[],
    )
    result = await plugin.apply(ctx)
    assert result is None


def test_assemble_includes_sections_outside_section_order():
    sections = {"custom_section": "custom content"}
    result = SystemPromptPlugin._assemble(sections)
    assert "custom_section" in result
    assert "custom content" in result


def test_assemble_omits_missing_ordered_sections():
    result = SystemPromptPlugin._assemble({"custom_section": "custom content"})
    assert "identity" not in result
    assert "custom_section" in result
