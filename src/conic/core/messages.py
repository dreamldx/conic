from dataclasses import dataclass


@dataclass
class UserInput:
    text: str


@dataclass
class TurnStart:
    pass


@dataclass
class TurnEnd:
    pass


@dataclass
class StepStart:
    step_index: int


@dataclass
class BeforeModelCall:
    messages: list[dict]
    tools: list[dict]


@dataclass
class ModelRequest:
    messages: list[dict]
    tools: list[dict]


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


@dataclass
class ToolCallResult:
    output: str | None = None
    error: str | None = None


@dataclass
class AssistantMessage:
    text: str


@dataclass
class Error:
    exc: Exception


@dataclass
class SummarizeRequest:
    messages: list[dict]
    budget_tokens: int


@dataclass
class SummarizeResult:
    messages: list[dict]


@dataclass
class BuildSystemPrompt:
    sections: dict[str, str]
