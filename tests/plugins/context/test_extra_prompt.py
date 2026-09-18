from conic.core.bus import MessageBus
from conic.plugins import meta
from conic.plugins.context.extra_prompt import ExtraPromptPlugin
from conic.plugins.context.sections.dynamic_state import DynamicStateSectionPlugin
from conic.plugins.context.token_budget import TokenBudgetPlugin
from conic.plugins.context.truncator import TruncatorPlugin
from conic.types.messages import BeforeModelCall, BuildDynamicPrompt, SummarizeRequest, SummarizeResult


def make_plugin():
    bus = MessageBus()
    plugin = ExtraPromptPlugin([DynamicStateSectionPlugin()])
    plugin.register(bus)
    return plugin, bus


async def test_appends_rendered_extra_content_to_the_last_message():
    """The dynamic content must be folded into the content of the last
    existing message, not added as a brand-new message -- inserting a bare
    system-role message after a tool reply (or anywhere mid-conversation)
    breaks the strict role alternation many chat/tool-calling models expect
    and produces garbled output."""
    plugin, _bus = make_plugin()
    ctx = BeforeModelCall(
        messages=[
            {"role": "system", "content": "static system prompt"},
            {"role": "user", "content": "hi"},
        ],
        tools=[],
        variables={
            "global": {},
            "session": {"tokens_used": 42, "turn_count": 3},
            "turn": {"now": "2026-09-16T00:00:00+00:00", "step_count": 1},
        },
    )
    result = await plugin.apply(ctx)

    assert len(result.messages) == 2
    assert result.messages[0] == ctx.messages[0]
    last = result.messages[-1]
    assert last["role"] == "user"
    assert last["content"].startswith("hi")
    assert "2026-09-16T00:00:00+00:00" in last["content"]
    assert "42" in last["content"]
    assert "3" in last["content"]


async def test_preserves_last_message_role_when_it_is_a_tool_reply():
    plugin, _bus = make_plugin()
    ctx = BeforeModelCall(
        messages=[
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "ran ls"},
        ],
        tools=[],
        variables={"global": {}, "session": {}, "turn": {"now": "T1"}},
    )
    result = await plugin.apply(ctx)

    assert len(result.messages) == 2
    last = result.messages[-1]
    assert last["role"] == "tool"
    assert last["tool_call_id"] == "c1"
    assert last["content"].startswith("ran ls")
    assert "T1" in last["content"]


async def test_forwards_tools_and_variables_unchanged():
    plugin, _bus = make_plugin()
    ctx = BeforeModelCall(
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "bash"}}],
        variables={"global": {}, "session": {}, "turn": {}},
    )
    result = await plugin.apply(ctx)

    assert result.tools == ctx.tools
    assert result.variables is ctx.variables


async def test_emit_on_before_model_call_appends_to_last_message():
    plugin, bus = make_plugin()
    ctx = BeforeModelCall(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        variables={"global": {}, "session": {}, "turn": {}},
    )
    result = await bus.chain("before_model_call", ctx)
    assert len(result.messages) == 1
    assert result.messages[0]["role"] == "user"
    assert result.messages[0]["content"].startswith("hi")


async def test_no_messages_is_a_noop():
    plugin, _bus = make_plugin()
    ctx = BeforeModelCall(messages=[], tools=[], variables={"global": {}, "session": {}, "turn": {}})
    result = await plugin.apply(ctx)
    assert result is None


async def test_emits_build_dynamic_prompt_event_to_collect_sections():
    bus = MessageBus()
    plugin = ExtraPromptPlugin([])
    plugin.register(bus)

    async def contribute(msg: BuildDynamicPrompt) -> BuildDynamicPrompt:
        msg.sections["custom"] = "custom {{ turn.now }} content"
        return msg

    bus.on_chain("build_dynamic_prompt", contribute)

    ctx = BeforeModelCall(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        variables={"global": {}, "session": {}, "turn": {"now": "T1"}},
    )
    result = await plugin.apply(ctx)

    assert "custom T1 content" in result.messages[-1]["content"]


async def test_assemble_wraps_sections_in_xml_style_tags():
    """A distinct, non-markdown delimiter matters here: this text gets folded
    into the content of a real conversation message (often a tool result),
    so it must not look like plausible tool stdout the model could confuse
    with the tool's actual output."""
    result = ExtraPromptPlugin._assemble({"a": "content a", "b": "content b"})
    assert result.startswith("<context_state>")
    assert result.endswith("</context_state>")
    assert "<a>\ncontent a\n</a>" in result
    assert "<b>\ncontent b\n</b>" in result


async def test_assemble_empty_sections_is_empty_string():
    assert ExtraPromptPlugin._assemble({}) == ""


async def test_no_section_plugins_is_a_noop():
    plugin = ExtraPromptPlugin([])
    bus = MessageBus()
    plugin.register(bus)
    ctx = BeforeModelCall(
        messages=[{"role": "user", "content": "hi"}], tools=[], variables={"global": {}, "session": {}, "turn": {}}
    )
    result = await plugin.apply(ctx)
    assert result is None


async def test_survives_after_truncation_in_the_real_registry_chain_order():
    """Regression test: registry.py wires TruncatorPlugin and TokenBudgetPlugin
    BEFORE ExtraPromptPlugin on BeforeModelCallEvent. If either of them drops
    ctx.variables when it actually rewrites the messages list, ExtraPromptPlugin
    crashes with a Jinja2 UndefinedError ("turn" is undefined) instead of just
    rendering its content with whatever turn/session state it has."""
    bus = MessageBus()
    TruncatorPlugin(keep_last_n=1).register(bus)
    TokenBudgetPlugin(budget_tokens=10_000).register(bus)
    ExtraPromptPlugin([DynamicStateSectionPlugin()]).register(bus)

    variables = {
        "global": {"model": "test-model"},
        "session": {"tokens_used": 0, "turn_count": 1},
        "turn": {"now": "T1", "step_count": 0},
    }
    ctx = BeforeModelCall(
        messages=[
            {"role": "system", "content": "static system prompt"},
            {"role": "user", "content": "0"},
            {"role": "user", "content": "1"},
            {"role": "user", "content": "2"},
        ],
        tools=[],
        variables=variables,
    )

    result = await bus.chain(meta.BeforeModelCallEvent, ctx)

    assert len(result.messages) == 2
    assert "T1" in result.messages[-1]["content"]


async def test_survives_after_summarization_in_the_real_registry_chain_order():
    bus = MessageBus()
    TruncatorPlugin(keep_last_n=1000).register(bus)
    TokenBudgetPlugin(budget_tokens=1).register(bus)
    ExtraPromptPlugin([DynamicStateSectionPlugin()]).register(bus)

    async def fake_summarizer(req: SummarizeRequest) -> SummarizeResult:
        return SummarizeResult(messages=[{"role": "system", "content": "summary"}])

    bus.on_request(meta.SummarizeEvent, fake_summarizer)

    variables = {
        "global": {"model": "test-model"},
        "session": {"tokens_used": 0, "turn_count": 1},
        "turn": {"now": "T1", "step_count": 0},
    }
    ctx = BeforeModelCall(
        messages=[{"role": "user", "content": "hi " * 100}], tools=[], variables=variables
    )

    result = await bus.chain(meta.BeforeModelCallEvent, ctx)

    assert "T1" in result.messages[-1]["content"]
