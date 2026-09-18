from conic.types.errors import AbortReason, AbortTurn, NoResponderError, DuplicateResponderError
from conic.types.messages import (
    UserInput, TurnStart, TurnEnd, StepStart, BeforeModelCall, ModelRequest,
    ToolCallSpec, ModelResponse, ToolCall, ToolCallResult, AssistantMessage,
    Error, SummarizeRequest, SummarizeResult, MessageUpdate, MessageDeltaUpdate,
)


def test_message_dataclasses_construct():
    spec = ToolCallSpec(id="1", name="bash", args={"command": "ls"})
    assert UserInput(text="hi").text == "hi"
    assert StepStart(step_index=0).step_index == 0
    assert BeforeModelCall(messages=[], tools=[]).tools == []
    assert ModelRequest(messages=[], tools=[]).messages == []
    resp = ModelResponse(text="ok", tool_calls=[spec], raw_message={"role": "assistant"})
    assert resp.tool_calls[0].name == "bash"
    assert ToolCall(call=spec).call is spec
    result = ToolCallResult(output="done")
    assert result.error is None
    assert AssistantMessage(text="hi").text == "hi"
    assert isinstance(Error(exc=ValueError("x")).exc, ValueError)
    req = SummarizeRequest(messages=[], budget_tokens=100)
    assert SummarizeResult(messages=[{"role": "system", "content": "s"}]).messages[0]["role"] == "system"
    TurnStart()
    TurnEnd()
    assert MessageUpdate(text="hi").text == "hi"
    assert MessageDeltaUpdate(text_delta="he").text_delta == "he"
    assert ModelRequest(messages=[], tools=[]).stream_updates is False


def test_error_types_are_exceptions():
    assert issubclass(AbortTurn, Exception)
    assert issubclass(NoResponderError, Exception)
    assert issubclass(DuplicateResponderError, Exception)
    try:
        raise AbortTurn("too many steps")
    except AbortTurn as exc:
        assert "too many steps" in str(exc)


def test_abort_turn_defaults_to_policy_reason_that_does_not_end_the_session():
    exc = AbortTurn("blocked by policy")
    assert exc.reason is AbortReason.POLICY
    assert exc.reason.ends_session is False


def test_abort_turn_user_abort_reason_ends_the_session():
    exc = AbortTurn(reason=AbortReason.USER_ABORT)
    assert exc.reason.ends_session is True


def test_abort_turn_model_timeout_reason_does_not_end_the_session():
    exc = AbortTurn(reason=AbortReason.MODEL_TIMEOUT)
    assert exc.reason.ends_session is False
