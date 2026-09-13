from dataclasses import dataclass

import pytest

from conic.core.bus import MessageBus
from conic.core.errors import AbortTurn
from conic.core.messages import (
    AssistantMessage, Error, ModelRequest, ModelResponse, StepStart,
    ToolCall, ToolCallResult, ToolCallSpec, TurnEnd, UserInput,
)
from conic.plugins.loops.react_loop import ReactLoopPlugin


class FakeStorageHandle:
    def __init__(self):
        self.messages: list[dict] = []

    def append_message(self, message: dict) -> None:
        self.messages.append(message)

    def load_history(self) -> list[dict]:
        return list(self.messages)

    def set_status(self, status: str) -> None:
        pass


@dataclass
class FakeToolCall:
    command: str


def make_loop(handle, responses):
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

    bus.on("assistant_message", on_assistant_message)

    await bus.emit("user_input", UserInput(text="hello"))

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

    bus.on("step_start", on_step_start)

    await bus.emit("user_input", UserInput(text="run ls"))

    assert steps == [0, 1]
    tool_messages = [m for m in handle.messages if m.get("role") == "tool"]
    assert tool_messages == [{"role": "tool", "tool_call_id": "call_1", "content": "ran ls"}]


async def test_abort_turn_from_a_hook_emits_error_and_stops_the_loop():
    handle = FakeStorageHandle()
    responses = [ModelResponse(text="unreachable", tool_calls=[], raw_message={})]
    bus, loop = make_loop(handle, responses)

    async def always_abort(msg: StepStart) -> None:
        raise AbortTurn("blocked by policy")

    bus.on("step_start", always_abort)

    errors = []

    async def on_error(msg: Error) -> None:
        errors.append(str(msg.exc))

    bus.on("error", on_error)

    await bus.emit("user_input", UserInput(text="hello"))

    assert errors == ["blocked by policy"]
    assert not any(m.get("role") == "assistant" for m in handle.messages)


async def test_turn_end_emitted_after_final_assistant_message():
    handle = FakeStorageHandle()
    responses = [ModelResponse(text="hi", tool_calls=[], raw_message={})]
    bus, loop = make_loop(handle, responses)

    order = []

    async def on_assistant_message(msg: AssistantMessage) -> None:
        order.append("assistant_message")

    async def on_turn_end(msg: TurnEnd) -> None:
        order.append("turn_end")

    bus.on("assistant_message", on_assistant_message)
    bus.on("turn_end", on_turn_end)

    await bus.emit("user_input", UserInput(text="hello"))

    assert order == ["assistant_message", "turn_end"]


async def test_non_aborttturn_exception_from_model_request_is_reported_as_error():
    """A plain Exception (e.g. an OpenRouter API error) raised while producing a
    model response must be caught and reported as an `error` event, not crash
    the process or propagate out of handle_user_input."""
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

    bus.on("error", on_error)

    # Must not raise out of emit — the loop's own except Exception clause must catch it.
    await bus.emit("user_input", UserInput(text="hello"))

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

    await bus.emit("user_input", UserInput(text="run unknown tool"))

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

    # Register a hook that mutates the call.id to a different value
    async def mutating_hook(tc: ToolCall) -> ToolCall:
        return ToolCall(call=ToolCallSpec(
            id="mutated_call_id",
            name=tc.call.name,
            args=tc.call.args
        ))

    bus.on("before_tool_call", mutating_hook)

    await bus.emit("user_input", UserInput(text="run ls"))

    # The tool-role message should use the ORIGINAL call id, not the mutated one
    tool_messages = [m for m in handle.messages if m.get("role") == "tool"]
    assert tool_messages == [{"role": "tool", "tool_call_id": "original_call_id", "content": "ran ls"}]
