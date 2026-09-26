from dataclasses import dataclass, field


@dataclass
class UserInput:
    text: str


@dataclass
class Input:
    text: str
    handled: bool = False


@dataclass
class SessionStart:
    reason: str


@dataclass
class SessionEnd:
    reason: str


@dataclass
class TurnStart:
    variables: dict = field(default_factory=dict)


@dataclass
class TurnEnd:
    variables: dict = field(default_factory=dict)


@dataclass
class StepStart:
    step_index: int
    variables: dict = field(default_factory=dict)


@dataclass
class StepEnd:
    step_index: int
    variables: dict = field(default_factory=dict)


@dataclass
class BeforeModelCall:
    messages: list[dict]
    tools: list[dict]
    variables: dict = field(default_factory=dict)


@dataclass
class ModelRequest:
    messages: list[dict]
    tools: list[dict]
    stream_updates: bool = False
    variables: dict = field(default_factory=dict)


@dataclass
class SwitchModelRequest:
    model_id: str


@dataclass
class SwitchModelResult:
    model: str | None = None
    error: str | None = None


@dataclass
class MessageUpdate:
    text: str


@dataclass
class MessageDeltaUpdate:
    text_delta: str


@dataclass
class ToolCallSpec:
    id: str
    name: str
    args: dict


@dataclass
class ModelResponse:
    text: str | None
    tool_calls: list[ToolCallSpec]
    raw_message: dict


@dataclass
class ToolCall:
    call: ToolCallSpec
    variables: dict = field(default_factory=dict)


@dataclass
class ToolCallResult:
    output: str | None = None
    error: str | None = None


@dataclass
class ToolExecutionStart:
    call: ToolCallSpec
    variables: dict = field(default_factory=dict)


@dataclass
class ToolExecutionEnd:
    call: ToolCallSpec
    result: ToolCallResult
    variables: dict = field(default_factory=dict)


@dataclass
class AssistantMessage:
    text: str
    variables: dict = field(default_factory=dict)


@dataclass
class Error:
    exc: Exception
    variables: dict = field(default_factory=dict)


@dataclass
class SummarizeRequest:
    messages: list[dict]
    budget_tokens: int
    instructions: str | None = None


@dataclass
class SummarizeResult:
    messages: list[dict]


@dataclass
class BeforeSummarize:
    request: SummarizeRequest
    cancelled: bool = False


@dataclass
class SummarizeDone:
    result: SummarizeResult


@dataclass
class SummarizeFailed:
    exc: Exception


@dataclass
class BuildSystemPrompt:
    sections: dict[str, str]


@dataclass
class BuildDynamicPrompt:
    sections: dict[str, str]
