"""All bus topic names, organized by emitting plugin."""

UserInputEvent = "user_input"

TurnStartEvent = "turn_start"
StepStartEvent = "step_start"
BeforeModelCallEvent = "before_model_call"
ModelRequestEvent = "model_request"
ModelResponseEvent = "model_response"
ToolCallEvent = "before_tool_call"
ToolCallResultEvent = "tool_result"
AssistantMessageEvent = "assistant_message"
ErrorEvent = "error"
TurnEndEvent = "turn_end"

ToolCallRequestEvent = "tool_call"

SummarizeEvent = "summarize"

BuildSystemPromptEvent = "build_system_prompt"
