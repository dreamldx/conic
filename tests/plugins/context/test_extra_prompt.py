from conic.core.bus import MessageBus
from conic.plugins import meta
from conic.plugins.context.extra_prompt import ExtraPromptPlugin
from conic.plugins.context.sections.dynamic_state import DynamicStateSectionPlugin
from conic.plugins.context.token_budget import TokenBudgetPlugin
from conic.plugins.context.truncator import TruncatorPlugin
from conic.types.messages import (
    BeforeModelCall,
    BuildDynamicPrompt,
    SummarizeRequest,
    SummarizeResult,
)


def make_plugin():
    bus = MessageBus()
    plugin = ExtraPromptPlugin([DynamicStateSectionPlugin()])
    plugin.register(bus)
    return plugin, bus


async def test_inserts_new_user_message_before_this_turns_steering_messages():
    """The dynamic content is only produced at Step 0 of a Turn, added as a
    brand-new `user`-role message (never folded into an existing message's
    content -- a live A/B test across deepseek/qwen/glm/claude/openai
    confirmed `user` role is safe here; `system` role made DeepSeek silently
    drop tool_calls in ~80% of multi-turn requests, because DeepSeek's chat
    template collects every system-role message, regardless of its position
    in the array, and renders them all together before the first user
    message -- `user`/`assistant` messages keep their real position).

    It's spliced in BEFORE this turn's own steering-injected messages (the
    last `turn.steering_count` entries of ctx.messages), not appended after
    them, so it reads as context established ahead of the turn's actual
    input rather than a trailing note glued onto it."""
    plugin, _bus = make_plugin()
    ctx = BeforeModelCall(
        messages=[
            {"role": "system", "content": "static system prompt"},
            {"role": "assistant", "content": "previous turn's answer"},
            {"role": "user", "content": "this turn's new input"},
        ],
        tools=[],
        variables={
            "global": {},
            "session": {"tokens_used": 42, "turn_count": 3},
            "turn": {"now": "2026-09-16T00:00:00+00:00", "step_count": 0, "steering_count": 1},
        },
    )
    result = await plugin.apply(ctx)

    assert len(result.messages) == 4
    assert result.messages[0] == ctx.messages[0]
    assert result.messages[1] == ctx.messages[1]
    new = result.messages[2]
    assert new["role"] == "user"
    assert "2026-09-16T00:00:00+00:00" in new["content"]
    assert "42" in new["content"]
    assert "3" in new["content"]
    assert result.messages[3] == ctx.messages[2]


async def test_appends_at_the_end_when_steering_count_is_unknown_or_zero():
    """With no steering_count (or zero), there's nothing this turn's steering
    injected to insert ahead of, so the new message falls back to the end of
    the list."""
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
            "turn": {"now": "2026-09-16T00:00:00+00:00", "step_count": 0},
        },
    )
    result = await plugin.apply(ctx)

    assert len(result.messages) == 3
    assert result.messages[0] == ctx.messages[0]
    assert result.messages[1] == ctx.messages[1]
    new = result.messages[-1]
    assert new["role"] == "user"
    assert "2026-09-16T00:00:00+00:00" in new["content"]
    assert "42" in new["content"]
    assert "3" in new["content"]


async def test_produces_nothing_after_step_zero():
    """Only Step 0 of a Turn gets this content -- later Steps within the same
    Turn (step_count 1, 2, ...) must not repeat it."""
    plugin, _bus = make_plugin()
    ctx = BeforeModelCall(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        variables={
            "global": {},
            "session": {"tokens_used": 42, "turn_count": 3},
            "turn": {"now": "T1", "step_count": 1},
        },
    )
    result = await plugin.apply(ctx)
    assert result is None


async def test_preserves_existing_messages_when_last_one_is_a_tool_reply():
    plugin, _bus = make_plugin()
    ctx = BeforeModelCall(
        messages=[
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "ran ls"},
        ],
        tools=[],
        variables={"global": {}, "session": {}, "turn": {"now": "T1", "step_count": 0}},
    )
    result = await plugin.apply(ctx)

    assert len(result.messages) == 3
    assert result.messages[0] == ctx.messages[0]
    assert result.messages[1] == ctx.messages[1]
    new = result.messages[-1]
    assert new["role"] == "user"
    assert "T1" in new["content"]


async def test_forwards_tools_and_variables_unchanged():
    plugin, _bus = make_plugin()
    ctx = BeforeModelCall(
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "bash"}}],
        variables={"global": {}, "session": {}, "turn": {"step_count": 0}},
    )
    result = await plugin.apply(ctx)

    assert result.tools == ctx.tools
    assert result.variables is ctx.variables


async def test_emit_on_before_model_call_appends_a_new_message():
    _plugin, bus = make_plugin()
    ctx = BeforeModelCall(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        variables={"global": {}, "session": {}, "turn": {"step_count": 0}},
    )
    result = await bus.chain("before_model_call", ctx)
    assert len(result.messages) == 2
    assert result.messages[0] == ctx.messages[0]
    assert result.messages[-1]["role"] == "user"


async def test_no_messages_is_a_noop():
    plugin, _bus = make_plugin()
    ctx = BeforeModelCall(
        messages=[], tools=[], variables={"global": {}, "session": {}, "turn": {"step_count": 0}}
    )
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
        variables={"global": {}, "session": {}, "turn": {"now": "T1", "step_count": 0}},
    )
    result = await plugin.apply(ctx)

    assert "custom T1 content" in result.messages[-1]["content"]


async def test_assemble_wraps_sections_in_xml_style_tags():
    """A distinct, non-markdown delimiter matters here: the state message is
    appended as a standalone user-role message, but it's still good practice
    to keep the content structured rather than a wall of plain text."""
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
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        variables={"global": {}, "session": {}, "turn": {"step_count": 0}},
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

    assert len(result.messages) == 3
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
