from conic.core.bus import MessageBus
from conic.core.messages import BuildSystemPrompt
from conic.plugins.context.sections.tooling import ToolingSectionPlugin


async def test_execution_section_adds_guidelines():
    from conic.plugins.context.sections.execution import ExecutionBiasSectionPlugin
    bus = MessageBus()
    plugin = ExecutionBiasSectionPlugin("Act on actionable requests.")
    plugin.register(bus)
    msg = BuildSystemPrompt(sections={})
    result = await bus.emit("build_system_prompt", msg)
    assert "execution" in result.sections
    assert "actionable" in result.sections["execution"]


async def test_runtime_section_includes_model_and_platform():
    from conic.plugins.context.sections.runtime import RuntimeSectionPlugin
    bus = MessageBus()
    plugin = RuntimeSectionPlugin(model="test-model")
    plugin.register(bus)
    msg = BuildSystemPrompt(sections={})
    result = await bus.emit("build_system_prompt", msg)
    assert "runtime" in result.sections
    assert "test-model" in result.sections["runtime"]


async def test_tooling_section_lists_tool_schemas():
    bus = MessageBus()
    schemas = [
        {"function": {"name": "bash", "description": "Run commands", "parameters": {"properties": {"command": {"type": "string"}}}}},
    ]
    plugin = ToolingSectionPlugin(schemas)
    plugin.register(bus)
    msg = BuildSystemPrompt(sections={})
    result = await bus.emit("build_system_prompt", msg)
    assert "tooling" in result.sections
    assert "bash" in result.sections["tooling"]


async def test_tooling_section_empty_schemas_is_noop():
    bus = MessageBus()
    plugin = ToolingSectionPlugin([])
    plugin.register(bus)
    msg = BuildSystemPrompt(sections={})
    result = await bus.emit("build_system_prompt", msg)
    assert "tooling" not in result.sections


async def test_workspace_section_adds_workspace_dir():
    from conic.plugins.context.sections.workspace import WorkspaceSectionPlugin
    bus = MessageBus()
    plugin = WorkspaceSectionPlugin("/tmp/test-ws")
    plugin.register(bus)
    msg = BuildSystemPrompt(sections={})
    result = await bus.emit("build_system_prompt", msg)
    assert "workspace" in result.sections
    assert "/tmp/test-ws" in result.sections["workspace"]
