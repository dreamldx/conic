from conic.core.bus import MessageBus
from conic.types.messages import BuildSystemPrompt
from conic.plugins.context.sections.tooling import ToolingSectionPlugin


async def test_execution_section_adds_guidelines():
    from conic.plugins.context.sections.execution import ExecutionBiasSectionPlugin
    bus = MessageBus()
    plugin = ExecutionBiasSectionPlugin("Act on actionable requests.")
    plugin.register(bus)
    msg = BuildSystemPrompt(sections={})
    result = await bus.chain("build_system_prompt", msg)
    assert "execution" in result.sections
    assert "actionable" in result.sections["execution"]


async def test_runtime_section_contributes_global_only_jinja_placeholders():
    from conic.plugins.context.sections.runtime import RuntimeSectionPlugin
    bus = MessageBus()
    plugin = RuntimeSectionPlugin()
    plugin.register(bus)
    msg = BuildSystemPrompt(sections={})
    result = await bus.chain("build_system_prompt", msg)
    assert "runtime" in result.sections
    assert "{{ global.model }}" in result.sections["runtime"]
    assert "{{ global.platform }}" in result.sections["runtime"]
    assert "{{ global.shell }}" in result.sections["runtime"]
    assert "{{ global.timezone }}" in result.sections["runtime"]
    # Only global-scope placeholders belong here: this section is rendered once
    # and cached by SystemPromptPlugin, so it must not reference session/turn
    # values that change every Step.
    assert "session." not in result.sections["runtime"]
    assert "turn." not in result.sections["runtime"]


async def test_tooling_section_lists_tool_schemas():
    bus = MessageBus()
    schemas = [
        {"function": {"name": "bash", "description": "Run commands", "parameters": {"properties": {"command": {"type": "string"}}}}},
    ]
    plugin = ToolingSectionPlugin(schemas)
    plugin.register(bus)
    msg = BuildSystemPrompt(sections={})
    result = await bus.chain("build_system_prompt", msg)
    assert "tooling" in result.sections
    assert "bash" in result.sections["tooling"]


async def test_tooling_section_empty_schemas_is_noop():
    bus = MessageBus()
    plugin = ToolingSectionPlugin([])
    plugin.register(bus)
    msg = BuildSystemPrompt(sections={})
    result = await bus.chain("build_system_prompt", msg)
    assert "tooling" not in result.sections


async def test_workspace_section_adds_workspace_dir():
    from conic.plugins.context.sections.workspace import WorkspaceSectionPlugin
    bus = MessageBus()
    plugin = WorkspaceSectionPlugin("/tmp/test-ws")
    plugin.register(bus)
    msg = BuildSystemPrompt(sections={})
    result = await bus.chain("build_system_prompt", msg)
    assert "workspace" in result.sections
    assert "/tmp/test-ws" in result.sections["workspace"]
