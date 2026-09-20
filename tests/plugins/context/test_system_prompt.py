from conic.core.bus import MessageBus
from conic.plugins.context.sections.identity import IdentitySectionPlugin
from conic.plugins.context.system_prompt import SystemPromptPlugin
from conic.types.messages import BeforeModelCall


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


async def test_renders_jinja2_variables_from_namespaced_scopes():
    bus = MessageBus()
    plugin = SystemPromptPlugin([
        IdentitySectionPlugin("You are running on {{ global.model }} in {{ session.workspace_dir }} at {{ turn.now }}.")
    ])
    plugin.register(bus)
    ctx = BeforeModelCall(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        variables={
            "global": {"model": "gpt-test"},
            "session": {"workspace_dir": "/tmp/ws"},
            "turn": {"now": "2026-09-15T00:00:00+00:00"},
        },
    )
    result = await plugin.apply(ctx)
    assert "You are running on gpt-test in /tmp/ws at 2026-09-15T00:00:00+00:00." in result.messages[0]["content"]


async def test_forwards_variables_on_returned_before_model_call():
    plugin, _bus = make_plugin()
    variables = {"global": {"foo": "bar"}, "session": {}, "turn": {}}
    ctx = BeforeModelCall(messages=[{"role": "user", "content": "hi"}], tools=[], variables=variables)
    result = await plugin.apply(ctx)
    assert result.variables == variables


async def test_system_content_is_rendered_once_and_cached_across_calls():
    """The system message must only be rendered on the first apply() call --
    later Steps (with different turn/session variables, e.g. a different
    turn.now) must reuse the exact same cached content, so the system
    message stays a stable prefix across the whole session (for provider-side
    prompt caching) instead of changing on every Step."""
    bus = MessageBus()
    plugin = SystemPromptPlugin([IdentitySectionPlugin("Model: {{ global.model }}, time: {{ turn.now }}.")])
    plugin.register(bus)

    ctx1 = BeforeModelCall(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        variables={"global": {"model": "gpt-test"}, "session": {}, "turn": {"now": "T1"}},
    )
    result1 = await plugin.apply(ctx1)

    ctx2 = BeforeModelCall(
        messages=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok"}],
        tools=[],
        variables={"global": {"model": "gpt-test"}, "session": {}, "turn": {"now": "T2"}},
    )
    result2 = await plugin.apply(ctx2)

    assert result1.messages[0]["content"] == result2.messages[0]["content"]
    assert "T1" in result2.messages[0]["content"]
    assert "T2" not in result2.messages[0]["content"]


def test_section_order_places_skills_right_after_tooling():
    from conic.plugins.context.system_prompt import SECTION_ORDER
    assert SECTION_ORDER.index("skills") == SECTION_ORDER.index("tooling") + 1
