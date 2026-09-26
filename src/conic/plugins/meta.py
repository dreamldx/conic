"""All bus topic names, organized by emitting plugin."""

# ── Session lifecycle (core/session_gateway.py, core/manager.py) ──────────
# emitted by SessionGatewayPlugin right before a message becomes a
# SteeringUserMessage post to steering.high, so hooks can transform the text
# or mark it handled to keep it from reaching the session's loop entirely
InputEvent = "input"
# emitted by PluginManager.start_session once per session, right after it
# wires up the bus (reason: "new" or "resume", passed in by the gateway)
SessionStartEvent = "session_start"
# emitted once per session when it ends: by ReactLoopPlugin._finalize_session
# (reason "agent_stop") or by PluginManager._join_and_cleanup as a fallback
# (reason "unexpected_exit") — the lifecycle notification plugins should hook
# for cleanup/archival and to mute themselves
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
# emitted before SummarizeEvent; hooks may customize instructions or cancel
BeforeSummarizeEvent = "before_summarize"
# dispatched when context exceeds token budget; returned as summary result
SummarizeEvent = "summarize"
# emitted after a successful summarize, carrying the SummarizeResult
SummarizeDoneEvent = "summarize_done"
# emitted when summarize raises, before the exception propagates
SummarizeFailedEvent = "summarize_failed"

# ── Plugin: SystemPromptPlugin ────────────────────────────────────────────
# dispatched internally to collect prompt sections from all plugins
BuildSystemPromptEvent = "build_system_prompt"

# ── Plugin: ExtraPromptPlugin ──────────────────────────────────────────────
# dispatched on every BeforeModelCallEvent to collect session/turn-scoped
# prompt sections that change every Step, appended as a trailing message
# instead of the cached, session-stable system message (see BuildSystemPromptEvent)
BuildDynamicPromptEvent = "build_dynamic_prompt"

# ── Plugin: ReactLoopPlugin / OpenRouterModelPlugin ─────────────────────
# emitted by ReactLoopPlugin to wholesale-replace the current live status
# text (e.g. "thinking", a tool-status line)
MessageUpdateEvent = "message_update"
# emitted by OpenRouterModelPlugin per streaming chunk when
# ModelRequest.stream_updates is True; carries only visible text, never
# tool-call argument fragments
MessageDeltaUpdateEvent = "message_delta_update"

# dispatched as a request to OpenRouterModelPlugin to change the model used by
# the session; the responder looks the model id up in the synced catalog and
# answers with a SwitchModelResult (error set when the id is unknown)
SwitchModelRequestEvent = "switch_model_request"
