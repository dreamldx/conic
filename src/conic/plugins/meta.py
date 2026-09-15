"""All bus topic names, organized by emitting plugin."""

# ── Plugin: DiscordGateway (conic/discord/gateway.py) ────────────────────
# emitted when a non-bot message arrives in a monitored Discord thread
UserInputEvent = "user_input"
# emitted once per session, right after PluginManager.start_session wires
# up its bus (reason: "new" or "resume")
SessionStartEvent = "session_start"
# emitted once per session when it is deliberately ended (reason: "user_stop")
SessionEndEvent = "session_end"

# ── Plugin: ReactLoopPlugin (loops/react_loop.py) ──────────────────────────
# The main orchestrator drives the full ReAct cycle per user turn.

# emitted once at the beginning of each turn
TurnStartEvent = "turn_start"
# emitted at the start of each step within a turn
StepStartEvent = "step_start"
# emitted at the end of each step within a turn (mirrors StepStartEvent)
StepEndEvent = "step_end"
# emitted before each LLM call, carries messages + tool schemas
BeforeModelCallEvent = "before_model_call"
# dispatched as a request to the backend for LLM completion
ModelRequestEvent = "model_request"
# returned from backend, re-emitted for hooks to observe
ModelResponseEvent = "model_response"
# emitted before each tool execution for policy inspection
ToolCallEvent = "before_tool_call"
# emitted right before a tool actually executes, after before_tool_call
# policy checks have run (distinguishes "allowed" from "actually running")
ToolExecutionStartEvent = "tool_execution_start"
# emitted right after a tool finishes executing, carrying the raw result
# before the tool_result observation/mutation chain runs
ToolExecutionEndEvent = "tool_execution_end"
# received from tool plugin, re-emitted for observation
ToolCallResultEvent = "tool_result"
# emitted when the LLM produces a final text-only response
AssistantMessageEvent = "assistant_message"
# emitted on AbortTurn or any unhandled exception
ErrorEvent = "error"
# emitted once at the end of a turn
TurnEndEvent = "turn_end"

# ── Plugin: tool plugins (bash, read_file, write_file, edit_file) ──────────
# all respond to on_request for tool execution
ToolCallRequestEvent = "tool_call"

# ── Plugin: SummarizerPlugin / TokenBudgetPlugin ──────────────────────────
# dispatched when context exceeds token budget; returned as summary result
SummarizeEvent = "summarize"

# ── Plugin: SystemPromptPlugin ────────────────────────────────────────────
# dispatched internally to collect prompt sections from all plugins
BuildSystemPromptEvent = "build_system_prompt"

# ── Plugin: DiscordGateway ────────────────────────────────────────────────
# emitted when the session is stopped, adapter should stop sending
SessionStopEvent = "session_stop"
