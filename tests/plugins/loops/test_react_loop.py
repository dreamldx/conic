import asyncio
from dataclasses import dataclass

import pytest

from conic.core.bus import MessageBus
from conic.types.errors import AbortReason, AbortTurn
from conic.types.messages import (
    AssistantMessage, BeforeModelCall, Error, MessageUpdate, ModelRequest, ModelResponse, SessionEnd,
    StepEnd, StepStart, ToolCall, ToolCallResult, ToolCallSpec, ToolExecutionEnd, ToolExecutionStart,
    TurnEnd, TurnStart,
)
from conic.types.steering import SteeringBackgroundResult, SteeringStopCommand, SteeringUserMessage
from conic.plugins.loops.react_loop import ReactLoopPlugin


class FakeStorageHandle:
    def __init__(self):
        self.messages: list[dict] = []
        self.saved_variables: list[dict] = []

    def append_message(self, message: dict) -> None:
        self.messages.append(message)

    def load_history(self) -> list[dict]:
        return list(self.messages)

    def set_status(self, status: str) -> None:
        pass

    def save_variables(self, variables: dict) -> None:
        self.saved_variables.append(dict(variables))


@dataclass
class FakeToolCall:
    command: str


def make_loop(handle, responses, model_timeout=120):
    bus = MessageBus()
    responses_iter = iter(responses)

    async def fake_model_request(msg: ModelRequest) -> ModelResponse:
        return next(responses_iter)

    bus.on_request("model_request", fake_model_request)

    async def fake_tool_call(call: FakeToolCall) -> ToolCallResult:
        return ToolCallResult(output=f"ran {call.command}")

    bus.on_request("tool_call", fake_tool_call)

    loop = ReactLoopPlugin(
        storage_handle=handle,
        tool_schemas=[{"type": "function", "function": {"name": "bash"}}],
        tool_payload_map={"bash": FakeToolCall},
        model_timeout=model_timeout,
    )
    loop.register(bus)
    return bus, loop


async def test_single_step_turn_with_no_tool_calls_emits_assistant_message():
    handle = FakeStorageHandle()
    responses = [ModelResponse(text="hi there", tool_calls=[], raw_message={"role": "assistant"})]
    bus, loop = make_loop(handle, responses)

    received = []

    async def on_assistant_message(msg: AssistantMessage) -> None:
        received.append(msg.text)

    bus.on_chain("assistant_message", on_assistant_message)

    await loop._run_turn([SteeringUserMessage("hello")], [])

    assert received == ["hi there"]
    assert handle.messages[0] == {"role": "user", "content": "hello"}
    assert handle.messages[-1] == {"role": "assistant", "content": "hi there"}


async def test_multi_step_turn_executes_tool_then_returns_final_answer():
    handle = FakeStorageHandle()
    tool_call = ToolCallSpec(id="call_1", name="bash", args={"command": "ls"})
    responses = [
        ModelResponse(text=None, tool_calls=[tool_call], raw_message={"role": "assistant", "tool_calls": [1]}),
        ModelResponse(text="done", tool_calls=[], raw_message={"role": "assistant"}),
    ]
    bus, loop = make_loop(handle, responses)

    steps = []

    async def on_step_start(msg: StepStart) -> None:
        steps.append(msg.step_index)

    bus.on_chain("step_start", on_step_start)

    await loop._run_turn([SteeringUserMessage("run ls")], [])

    assert steps == [0, 1]
    tool_messages = [m for m in handle.messages if m.get("role") == "tool"]
    assert tool_messages == [{"role": "tool", "tool_call_id": "call_1", "content": "ran ls"}]


async def test_step_end_emitted_with_matching_step_index_on_final_step():
    handle = FakeStorageHandle()
    responses = [ModelResponse(text="hi there", tool_calls=[], raw_message={"role": "assistant"})]
    bus, loop = make_loop(handle, responses)

    ends = []

    async def on_step_end(msg: StepEnd) -> None:
        ends.append(msg.step_index)

    bus.on_chain("step_end", on_step_end)

    await loop._run_turn([SteeringUserMessage("hello")], [])

    assert ends == [0]


async def test_step_end_emitted_once_per_step_with_matching_index():
    handle = FakeStorageHandle()
    tool_call = ToolCallSpec(id="call_1", name="bash", args={"command": "ls"})
    responses = [
        ModelResponse(text=None, tool_calls=[tool_call], raw_message={"role": "assistant", "tool_calls": [1]}),
        ModelResponse(text="done", tool_calls=[], raw_message={"role": "assistant"}),
    ]
    bus, loop = make_loop(handle, responses)

    ends = []

    async def on_step_end(msg: StepEnd) -> None:
        ends.append(msg.step_index)

    bus.on_chain("step_end", on_step_end)

    await loop._run_turn([SteeringUserMessage("run ls")], [])

    assert ends == [0, 1]


async def test_tool_execution_start_and_end_bracket_the_actual_tool_dispatch():
    handle = FakeStorageHandle()
    tool_call = ToolCallSpec(id="call_1", name="bash", args={"command": "ls"})
    responses = [
        ModelResponse(text=None, tool_calls=[tool_call], raw_message={"role": "assistant", "tool_calls": [1]}),
        ModelResponse(text="done", tool_calls=[], raw_message={"role": "assistant"}),
    ]
    bus, loop = make_loop(handle, responses)

    order = []

    async def on_before_tool_call(msg: ToolCall) -> None:
        order.append("before_tool_call")

    async def on_execution_start(msg: ToolExecutionStart) -> None:
        order.append("tool_execution_start")
        assert msg.call.name == "bash"

    async def on_execution_end(msg: ToolExecutionEnd) -> None:
        order.append("tool_execution_end")
        assert msg.call.name == "bash"
        assert msg.result.output == "ran ls"

    async def on_tool_result(msg: ToolCallResult) -> None:
        order.append("tool_result")

    bus.on_chain("before_tool_call", on_before_tool_call)
    bus.on_chain("tool_execution_start", on_execution_start)
    bus.on_chain("tool_execution_end", on_execution_end)
    bus.on_chain("tool_result", on_tool_result)

    await loop._run_turn([SteeringUserMessage("run ls")], [])

    assert order == ["before_tool_call", "tool_execution_start", "tool_execution_end", "tool_result"]


async def test_tool_execution_end_carries_raw_result_before_tool_result_mutation():
    handle = FakeStorageHandle()
    tool_call = ToolCallSpec(id="call_1", name="bash", args={"command": "ls"})
    responses = [
        ModelResponse(text=None, tool_calls=[tool_call], raw_message={"role": "assistant", "tool_calls": [1]}),
        ModelResponse(text="done", tool_calls=[], raw_message={"role": "assistant"}),
    ]
    bus, loop = make_loop(handle, responses)

    captured = []

    async def on_execution_end(msg: ToolExecutionEnd) -> None:
        captured.append(msg.result.output)

    async def mutate_result(msg: ToolCallResult) -> ToolCallResult:
        return ToolCallResult(output="mutated", error=None)

    bus.on_chain("tool_execution_end", on_execution_end)
    bus.on_chain("tool_result", mutate_result)

    await loop._run_turn([SteeringUserMessage("run ls")], [])

    assert captured == ["ran ls"]
    tool_messages = [m for m in handle.messages if m.get("role") == "tool"]
    assert tool_messages[0]["content"] == "mutated"


async def test_abort_turn_from_a_hook_emits_error_and_stops_the_loop():
    """A policy-reason AbortTurn (the default, used by pre-existing hooks like
    StepLimitPlugin) is swallowed inside _run_turn exactly like before: no
    exception escapes, no TurnEndEvent, just an ErrorEvent."""
    handle = FakeStorageHandle()
    responses = [ModelResponse(text="unreachable", tool_calls=[], raw_message={})]
    bus, loop = make_loop(handle, responses)

    async def always_abort(msg: StepStart) -> None:
        raise AbortTurn("blocked by policy")

    bus.on_chain("step_start", always_abort)

    errors = []

    async def on_error(msg: Error) -> None:
        errors.append(str(msg.exc))

    bus.on_chain("error", on_error)

    await loop._run_turn([SteeringUserMessage("hello")], [])

    assert errors == ["blocked by policy"]
    assert not any(m.get("role") == "assistant" for m in handle.messages)


async def test_abort_turn_from_before_tool_call_hook_stops_the_turn():
    """AbortTurn raised inside the per-tool-call try (e.g. a permission-policy
    denial on before_tool_call) must propagate to the outer handler and abort
    the turn cleanly, not get caught by the generic `except Exception` and
    turned into a tool-result error that lets the loop continue."""
    handle = FakeStorageHandle()
    tool_call = ToolCallSpec(id="call_1", name="bash", args={"command": "rm -rf /"})
    responses = [
        ModelResponse(text=None, tool_calls=[tool_call], raw_message={"role": "assistant", "tool_calls": [1]}),
        ModelResponse(text="unreachable", tool_calls=[], raw_message={}),
    ]
    bus, loop = make_loop(handle, responses)

    async def deny(tc: ToolCall) -> ToolCall:
        raise AbortTurn("denied by policy")

    bus.on_chain("before_tool_call", deny)

    errors = []

    async def on_error(msg: Error) -> None:
        errors.append(str(msg.exc))

    bus.on_chain("error", on_error)

    await loop._run_turn([SteeringUserMessage("run rm -rf /")], [])

    assert errors == ["denied by policy"]
    assert not any(m.get("role") == "tool" for m in handle.messages)
    assert not any(m.get("content") == "unreachable" for m in handle.messages)


async def test_turn_end_emitted_after_final_assistant_message():
    handle = FakeStorageHandle()
    responses = [ModelResponse(text="hi", tool_calls=[], raw_message={})]
    bus, loop = make_loop(handle, responses)

    order = []

    async def on_assistant_message(msg: AssistantMessage) -> None:
        order.append("assistant_message")

    async def on_turn_end(msg: TurnEnd) -> None:
        order.append("turn_end")

    bus.on_chain("assistant_message", on_assistant_message)
    bus.on_chain("turn_end", on_turn_end)

    await loop._run_turn([SteeringUserMessage("hello")], [])

    assert order == ["assistant_message", "turn_end"]


async def test_non_aborttturn_exception_from_model_request_is_reported_as_error():
    """A plain Exception (e.g. an OpenRouter API error) raised while producing a
    model response must be caught and reported as an `error` event, not crash
    the process or propagate out of _run_turn."""
    handle = FakeStorageHandle()
    bus = MessageBus()

    async def failing_model_request(msg: ModelRequest) -> ModelResponse:
        raise ValueError("openrouter blew up")

    bus.on_request("model_request", failing_model_request)

    loop = ReactLoopPlugin(
        storage_handle=handle,
        tool_schemas=[{"type": "function", "function": {"name": "bash"}}],
        tool_payload_map={"bash": FakeToolCall},
    )
    loop.register(bus)

    errors = []

    async def on_error(msg: Error) -> None:
        errors.append(msg.exc)

    bus.on_chain("error", on_error)

    # Must not raise out of _run_turn — its own except Exception clause must catch it.
    await loop._run_turn([SteeringUserMessage("hello")], [])

    assert len(errors) == 1
    assert isinstance(errors[0], ValueError)
    assert str(errors[0]) == "openrouter blew up"


async def test_unknown_tool_name_does_not_corrupt_history_and_continues_next_step():
    """If the model names a tool not present in the payload map (KeyError), the
    tool_calls assistant message must still get a matching tool-role reply so
    the persisted history stays structurally valid, and the loop must continue
    to the next Step rather than crashing."""
    handle = FakeStorageHandle()
    tool_call = ToolCallSpec(id="call_1", name="not_a_real_tool", args={})
    responses = [
        ModelResponse(text=None, tool_calls=[tool_call], raw_message={"role": "assistant", "tool_calls": [1]}),
        ModelResponse(text="done", tool_calls=[], raw_message={"role": "assistant"}),
    ]
    bus, loop = make_loop(handle, responses)

    await loop._run_turn([SteeringUserMessage("run unknown tool")], [])

    tool_messages = [m for m in handle.messages if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "call_1"
    assert tool_messages[0]["content"].startswith("Error: ")

    # The loop must have continued to the second step and produced a final assistant message.
    assert handle.messages[-1] == {"role": "assistant", "content": "done"}


async def test_tool_call_id_preserved_when_hook_mutates_call_id():
    """Test that tool-role message uses original call.id even if before_tool_call hook mutates it."""
    handle = FakeStorageHandle()
    tool_call = ToolCallSpec(id="original_call_id", name="bash", args={"command": "ls"})
    responses = [
        ModelResponse(text=None, tool_calls=[tool_call], raw_message={"role": "assistant", "tool_calls": [1]}),
        ModelResponse(text="done", tool_calls=[], raw_message={"role": "assistant"}),
    ]
    bus, loop = make_loop(handle, responses)

    async def mutating_hook(tc: ToolCall) -> ToolCall:
        return ToolCall(call=ToolCallSpec(
            id="mutated_call_id",
            name=tc.call.name,
            args=tc.call.args
        ))

    bus.on_chain("before_tool_call", mutating_hook)

    await loop._run_turn([SteeringUserMessage("run ls")], [])

    tool_messages = [m for m in handle.messages if m.get("role") == "tool"]
    assert tool_messages == [{"role": "tool", "tool_call_id": "original_call_id", "content": "ran ls"}]


async def test_message_update_fires_only_for_tool_status_not_a_per_step_thinking_reset():
    handle = FakeStorageHandle()
    tool_call = ToolCallSpec(id="call_1", name="bash", args={"command": "ls"})
    responses = [
        ModelResponse(text=None, tool_calls=[tool_call], raw_message={"role": "assistant", "tool_calls": [1]}),
        ModelResponse(text="done", tool_calls=[], raw_message={"role": "assistant"}),
    ]
    bus, loop = make_loop(handle, responses)

    updates = []

    async def on_update(msg: MessageUpdate) -> None:
        updates.append(msg.text)

    bus.on_chain("message_update", on_update)

    await loop._run_turn([SteeringUserMessage("run ls")], [])

    assert updates == ["🔧 bash(command='ls')"]


async def test_model_request_opts_into_streaming():
    handle = FakeStorageHandle()
    bus = MessageBus()
    captured = []

    async def fake_model_request(msg: ModelRequest) -> ModelResponse:
        captured.append(msg.stream_updates)
        return ModelResponse(text="hi", tool_calls=[], raw_message={})

    bus.on_request("model_request", fake_model_request)

    loop = ReactLoopPlugin(
        storage_handle=handle,
        tool_schemas=[{"type": "function", "function": {"name": "bash"}}],
        tool_payload_map={"bash": FakeToolCall},
    )
    loop.register(bus)

    await loop._run_turn([SteeringUserMessage("hello")], [])

    assert captured == [True]


async def test_variables_dict_is_shared_across_turn_scoped_events():
    handle = FakeStorageHandle()
    responses = [ModelResponse(text="hi", tool_calls=[], raw_message={})]
    bus, loop = make_loop(handle, responses)

    async def contribute(msg: TurnStart) -> TurnStart:
        msg.variables["turn"]["injected"] = "value"
        return msg

    seen = []

    async def observe(msg: BeforeModelCall) -> None:
        seen.append(msg.variables["turn"].get("injected"))

    bus.on_chain("turn_start", contribute)
    bus.on_chain("before_model_call", observe)

    await loop._run_turn([SteeringUserMessage("hello")], [])

    assert seen == ["value"]


async def test_turn_step_count_tracks_current_step_index():
    handle = FakeStorageHandle()
    tool_call = ToolCallSpec(id="call_1", name="bash", args={"command": "ls"})
    responses = [
        ModelResponse(text=None, tool_calls=[tool_call], raw_message={"role": "assistant", "tool_calls": [1]}),
        ModelResponse(text="done", tool_calls=[], raw_message={"role": "assistant"}),
    ]
    bus, loop = make_loop(handle, responses)

    seen_step_counts = []

    async def observe(msg: BeforeModelCall) -> None:
        seen_step_counts.append(msg.variables["turn"]["step_count"])

    bus.on_chain("before_model_call", observe)

    await loop._run_turn([SteeringUserMessage("run ls")], [])

    assert seen_step_counts == [0, 1]


async def test_variables_has_global_session_turn_scopes():
    handle = FakeStorageHandle()
    responses = [ModelResponse(text="hi", tool_calls=[], raw_message={})]
    bus = MessageBus()
    responses_iter = iter(responses)

    async def fake_model_request(msg: ModelRequest) -> ModelResponse:
        return next(responses_iter)

    bus.on_request("model_request", fake_model_request)

    loop = ReactLoopPlugin(
        storage_handle=handle,
        tool_schemas=[{"type": "function", "function": {"name": "bash"}}],
        tool_payload_map={"bash": FakeToolCall},
        workspace_dir="/tmp/ws",
        global_variables={"model": "gpt-test"},
    )
    loop.register(bus)

    seen = []

    async def observe(msg: BeforeModelCall) -> None:
        seen.append(msg.variables)

    bus.on_chain("before_model_call", observe)

    await loop._run_turn([SteeringUserMessage("hello")], [])

    assert seen[0]["global"] == {"model": "gpt-test"}
    assert seen[0]["session"] == {"workspace_dir": "/tmp/ws", "tokens_used": 0, "turn_count": 1}
    assert seen[0]["turn"] == {"step_count": 0}


async def test_session_variables_seeded_from_persisted_values():
    handle = FakeStorageHandle()
    responses = [ModelResponse(text="hi", tool_calls=[], raw_message={})]
    bus, _responses_iter = MessageBus(), iter(responses)

    async def fake_model_request(msg: ModelRequest) -> ModelResponse:
        return next(_responses_iter)

    bus.on_request("model_request", fake_model_request)

    loop = ReactLoopPlugin(
        storage_handle=handle,
        tool_schemas=[{"type": "function", "function": {"name": "bash"}}],
        tool_payload_map={"bash": FakeToolCall},
        workspace_dir="/tmp/ws",
        persisted_session_variables={"tokens_used": 250},
    )
    loop.register(bus)

    await loop._run_turn([SteeringUserMessage("hello")], [])

    assert loop._session_variables == {"workspace_dir": "/tmp/ws", "tokens_used": 250, "turn_count": 1}


async def test_session_variables_persisted_after_successful_turn():
    handle = FakeStorageHandle()
    responses = [ModelResponse(text="hi", tool_calls=[], raw_message={})]
    bus, loop = make_loop(handle, responses)

    await loop._run_turn([SteeringUserMessage("hello")], [])

    assert handle.saved_variables == [{"workspace_dir": "", "tokens_used": 0, "turn_count": 1}]


async def test_session_variables_persisted_after_abort_turn():
    handle = FakeStorageHandle()
    responses = [ModelResponse(text="unreachable", tool_calls=[], raw_message={})]
    bus, loop = make_loop(handle, responses)

    async def always_abort(msg: StepStart) -> None:
        raise AbortTurn("blocked by policy")

    bus.on_chain("step_start", always_abort)

    await loop._run_turn([SteeringUserMessage("hello")], [])

    assert handle.saved_variables == [{"workspace_dir": "", "tokens_used": 0, "turn_count": 1}]


async def test_session_variables_persisted_after_generic_exception():
    handle = FakeStorageHandle()
    bus = MessageBus()

    async def failing_model_request(msg: ModelRequest) -> ModelResponse:
        raise ValueError("openrouter blew up")

    bus.on_request("model_request", failing_model_request)

    loop = ReactLoopPlugin(
        storage_handle=handle,
        tool_schemas=[{"type": "function", "function": {"name": "bash"}}],
        tool_payload_map={"bash": FakeToolCall},
    )
    loop.register(bus)

    await loop._run_turn([SteeringUserMessage("hello")], [])

    assert handle.saved_variables == [{"workspace_dir": "", "tokens_used": 0, "turn_count": 1}]


async def test_session_variables_reflect_mutations_made_during_the_turn():
    handle = FakeStorageHandle()
    responses = [ModelResponse(text="hi", tool_calls=[], raw_message={})]
    bus, loop = make_loop(handle, responses)

    async def bump_tokens(msg: BeforeModelCall) -> None:
        msg.variables["session"]["tokens_used"] += 42

    bus.on_chain("before_model_call", bump_tokens)

    await loop._run_turn([SteeringUserMessage("hello")], [])

    assert handle.saved_variables == [{"workspace_dir": "", "tokens_used": 42, "turn_count": 1}]


async def test_turn_count_increments_across_multiple_turns_in_the_same_session():
    handle = FakeStorageHandle()
    responses = [
        ModelResponse(text="hi", tool_calls=[], raw_message={}),
        ModelResponse(text="hi again", tool_calls=[], raw_message={}),
        ModelResponse(text="hi a third time", tool_calls=[], raw_message={}),
    ]
    bus, loop = make_loop(handle, responses)

    seen_turn_counts = []

    async def observe(msg: BeforeModelCall) -> None:
        seen_turn_counts.append(msg.variables["session"]["turn_count"])

    bus.on_chain("before_model_call", observe)

    await loop._run_turn([SteeringUserMessage("hello")], [])
    await loop._run_turn([SteeringUserMessage("hello again")], [])
    await loop._run_turn([SteeringUserMessage("hello a third time")], [])

    assert seen_turn_counts == [1, 2, 3]
    assert loop._session_variables["turn_count"] == 3
    assert handle.saved_variables[-1]["turn_count"] == 3


async def test_turn_count_seeded_from_persisted_value():
    handle = FakeStorageHandle()
    responses = [ModelResponse(text="hi", tool_calls=[], raw_message={})]
    bus = MessageBus()
    responses_iter = iter(responses)

    async def fake_model_request(msg: ModelRequest) -> ModelResponse:
        return next(responses_iter)

    bus.on_request("model_request", fake_model_request)

    loop = ReactLoopPlugin(
        storage_handle=handle,
        tool_schemas=[{"type": "function", "function": {"name": "bash"}}],
        tool_payload_map={"bash": FakeToolCall},
        persisted_session_variables={"turn_count": 9},
    )
    loop.register(bus)

    await loop._run_turn([SteeringUserMessage("hello")], [])

    assert loop._session_variables["turn_count"] == 10


# --- checkpoint / steering behavior (new) ---


async def test_turn_start_injects_the_full_high_and_low_batch_into_history():
    handle = FakeStorageHandle()
    responses = [ModelResponse(text="hi", tool_calls=[], raw_message={})]
    bus, loop = make_loop(handle, responses)

    await loop._run_turn(
        [SteeringUserMessage("hello")],
        [SteeringBackgroundResult(task_id="t1", exit_code=0, output="build ok")],
    )

    assert handle.messages[0] == {"role": "user", "content": "hello"}
    assert "t1" in handle.messages[1]["content"]
    assert handle.messages[1]["role"] == "user"


async def test_checkpoint_high_message_with_no_tool_calls_forces_another_step():
    """A steering.high message that arrives exactly when the model returns a
    final (no tool_calls) answer must not be dropped: the assistant's answer
    is appended, the high item is injected, and the loop takes one more step
    instead of ending the turn."""
    handle = FakeStorageHandle()
    responses = [
        ModelResponse(text="first answer", tool_calls=[], raw_message={}),
        ModelResponse(text="second answer", tool_calls=[], raw_message={}),
    ]
    bus, loop = make_loop(handle, responses)

    async def inject_on_first_response(msg: ModelResponse) -> ModelResponse:
        if not any(m.get("content") == "injected mid-turn" for m in handle.messages):
            await bus.post("steering.high", SteeringUserMessage("injected mid-turn"))
        return msg

    bus.on_chain("model_response", inject_on_first_response)

    steps = []

    async def on_step_start(msg: StepStart) -> None:
        steps.append(msg.step_index)

    bus.on_chain("step_start", on_step_start)

    await loop._run_turn([SteeringUserMessage("hello")], [])

    assert steps == [0, 1]
    assert {"role": "assistant", "content": "first answer"} in handle.messages
    assert {"role": "user", "content": "injected mid-turn"} in handle.messages
    assert handle.messages[-1] == {"role": "assistant", "content": "second answer"}


async def test_checkpoint_abort_after_final_answer_keeps_the_answer_and_ends_turn():
    """A SteeringStopCommand posted while the model is producing a final (no
    tool_calls) answer no longer gets checked until after that answer is
    appended to history — the model's completed answer isn't thrown away just
    because a stop arrived at the same moment. TurnEndEvent still fires,
    ErrorEvent does not, and AbortTurn(reason=USER_ABORT) propagates out for
    run_loop to handle."""
    handle = FakeStorageHandle()
    responses = [ModelResponse(text="final answer", tool_calls=[], raw_message={})]
    bus, loop = make_loop(handle, responses)

    async def inject_stop(msg: ModelResponse) -> ModelResponse:
        await bus.post("steering.high", SteeringStopCommand())
        return msg

    bus.on_chain("model_response", inject_stop)

    errors = []
    turn_ends = []

    async def on_error(msg: Error) -> None:
        errors.append(msg)

    async def on_turn_end(msg: TurnEnd) -> None:
        turn_ends.append(msg)

    bus.on_chain("error", on_error)
    bus.on_chain("turn_end", on_turn_end)

    with pytest.raises(AbortTurn) as exc_info:
        await loop._run_turn([SteeringUserMessage("hello")], [])

    assert exc_info.value.reason is AbortReason.USER_ABORT
    assert errors == []
    assert len(turn_ends) == 1
    assert {"role": "assistant", "content": "final answer"} in handle.messages


async def test_checkpoint_abort_only_takes_effect_after_all_tools_in_the_step_finish():
    """A SteeringStopCommand posted while the first of two tool calls in the
    same step is running must NOT cut off the second tool call — the
    checkpoint only drains steering.high once, after the whole step's
    tool_calls have all completed (and all have matching tool_result entries
    in history), so the abort is only noticed then."""
    handle = FakeStorageHandle()
    call_1 = ToolCallSpec(id="call_1", name="bash", args={"command": "one"})
    call_2 = ToolCallSpec(id="call_2", name="bash", args={"command": "two"})
    responses = [
        ModelResponse(
            text=None, tool_calls=[call_1, call_2],
            raw_message={"role": "assistant", "tool_calls": [1, 2]},
        ),
    ]
    bus = MessageBus()
    responses_iter = iter(responses)

    async def fake_model_request(msg: ModelRequest) -> ModelResponse:
        return next(responses_iter)

    bus.on_request("model_request", fake_model_request)

    executed = []

    async def fake_tool_call(call: FakeToolCall) -> ToolCallResult:
        executed.append(call.command)
        if call.command == "one":
            await bus.post("steering.high", SteeringStopCommand())
        return ToolCallResult(output=f"ran {call.command}")

    bus.on_request("tool_call", fake_tool_call)

    loop = ReactLoopPlugin(
        storage_handle=handle,
        tool_schemas=[{"type": "function", "function": {"name": "bash"}}],
        tool_payload_map={"bash": FakeToolCall},
    )
    loop.register(bus)

    turn_ends = []

    async def on_turn_end(msg: TurnEnd) -> None:
        turn_ends.append(msg)

    bus.on_chain("turn_end", on_turn_end)

    with pytest.raises(AbortTurn) as exc_info:
        await loop._run_turn([SteeringUserMessage("run both")], [])

    assert exc_info.value.reason is AbortReason.USER_ABORT
    assert executed == ["one", "two"]
    assert len(turn_ends) == 1
    tool_messages = [m for m in handle.messages if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["call_1", "call_2"]


async def test_checkpoint_high_message_is_injected_without_aborting_remaining_tools():
    handle = FakeStorageHandle()
    call_1 = ToolCallSpec(id="call_1", name="bash", args={"command": "one"})
    call_2 = ToolCallSpec(id="call_2", name="bash", args={"command": "two"})
    responses = [
        ModelResponse(
            text=None, tool_calls=[call_1, call_2],
            raw_message={"role": "assistant", "tool_calls": [1, 2]},
        ),
        ModelResponse(text="done", tool_calls=[], raw_message={"role": "assistant"}),
    ]
    bus = MessageBus()
    responses_iter = iter(responses)

    async def fake_model_request(msg: ModelRequest) -> ModelResponse:
        return next(responses_iter)

    bus.on_request("model_request", fake_model_request)

    executed = []

    async def fake_tool_call(call: FakeToolCall) -> ToolCallResult:
        executed.append(call.command)
        if call.command == "one":
            await bus.post("steering.high", SteeringUserMessage("also check three"))
        return ToolCallResult(output=f"ran {call.command}")

    bus.on_request("tool_call", fake_tool_call)

    loop = ReactLoopPlugin(
        storage_handle=handle,
        tool_schemas=[{"type": "function", "function": {"name": "bash"}}],
        tool_payload_map={"bash": FakeToolCall},
    )
    loop.register(bus)

    await loop._run_turn([SteeringUserMessage("run both")], [])

    assert executed == ["one", "two"]
    assert {"role": "user", "content": "also check three"} in handle.messages
    assert handle.messages[-1] == {"role": "assistant", "content": "done"}


async def test_model_timeout_raises_abort_turn_and_emits_error_and_turn_end():
    handle = FakeStorageHandle()
    bus = MessageBus()

    async def slow_model_request(msg: ModelRequest) -> ModelResponse:
        await asyncio.sleep(10)
        return ModelResponse(text="too slow", tool_calls=[], raw_message={})

    bus.on_request("model_request", slow_model_request)

    loop = ReactLoopPlugin(
        storage_handle=handle,
        tool_schemas=[{"type": "function", "function": {"name": "bash"}}],
        tool_payload_map={"bash": FakeToolCall},
        model_timeout=0.01,
    )
    loop.register(bus)

    errors = []
    turn_ends = []

    async def on_error(msg: Error) -> None:
        errors.append(msg)

    async def on_turn_end(msg: TurnEnd) -> None:
        turn_ends.append(msg)

    bus.on_chain("error", on_error)
    bus.on_chain("turn_end", on_turn_end)

    with pytest.raises(AbortTurn) as exc_info:
        await loop._run_turn([SteeringUserMessage("hello")], [])

    assert exc_info.value.reason is AbortReason.MODEL_TIMEOUT
    assert len(errors) == 1
    assert len(turn_ends) == 1
    assert not any(m.get("role") == "assistant" for m in handle.messages)


async def test_turn_end_drains_steering_low_posted_during_the_turn():
    handle = FakeStorageHandle()
    responses = [ModelResponse(text="hi", tool_calls=[], raw_message={})]
    bus, loop = make_loop(handle, responses)

    async def post_low_result(msg: ModelResponse) -> ModelResponse:
        await bus.post(
            "steering.low", SteeringBackgroundResult(task_id="bg1", exit_code=0, output="finished")
        )
        return msg

    bus.on_chain("model_response", post_low_result)

    await loop._run_turn([SteeringUserMessage("hello")], [])

    assert any("bg1" in m.get("content", "") for m in handle.messages)


# --- run_loop (new) ---


async def test_run_loop_idle_abort_finalizes_without_starting_a_turn():
    handle = FakeStorageHandle()
    bus, loop = make_loop(handle, [])

    session_ends = []
    turn_starts = []

    async def on_session_end(msg: SessionEnd) -> None:
        session_ends.append(msg)

    async def on_turn_start(msg: TurnStart) -> None:
        turn_starts.append(msg)

    bus.on_chain("session_end", on_session_end)
    bus.on_chain("turn_start", on_turn_start)

    await bus.post("steering.high", SteeringStopCommand())

    await asyncio.wait_for(loop.run_loop(), timeout=1.0)

    assert len(session_ends) == 1
    assert turn_starts == []


async def test_run_loop_processes_a_message_then_ends_on_a_later_stop_command():
    handle = FakeStorageHandle()
    responses = [ModelResponse(text="hi", tool_calls=[], raw_message={})]
    bus, loop = make_loop(handle, responses)

    session_ends = []
    assistant_messages = []

    async def on_session_end(msg: SessionEnd) -> None:
        session_ends.append(msg)

    async def on_assistant_message(msg: AssistantMessage) -> None:
        assistant_messages.append(msg.text)

    bus.on_chain("session_end", on_session_end)
    bus.on_chain("assistant_message", on_assistant_message)

    await bus.post("steering.high", SteeringUserMessage("hello"))

    async def post_stop_after_first_turn(msg: TurnEnd) -> TurnEnd:
        await bus.post("steering.high", SteeringStopCommand())
        return msg

    bus.on_chain("turn_end", post_stop_after_first_turn)

    await asyncio.wait_for(loop.run_loop(), timeout=1.0)

    assert assistant_messages == ["hi"]
    assert len(session_ends) == 1


async def test_run_loop_drops_high_batch_that_contains_both_message_and_stop():
    """Per the design's abort semantics, any abort in a drained batch discards
    the rest of that same batch — a user message posted in the same wake as
    /agent_stop never starts a turn."""
    handle = FakeStorageHandle()
    bus, loop = make_loop(handle, [])

    turn_starts = []

    async def on_turn_start(msg: TurnStart) -> None:
        turn_starts.append(msg)

    bus.on_chain("turn_start", on_turn_start)

    await bus.post("steering.high", SteeringUserMessage("hello"))
    await bus.post("steering.high", SteeringStopCommand())

    await asyncio.wait_for(loop.run_loop(), timeout=1.0)

    assert turn_starts == []
