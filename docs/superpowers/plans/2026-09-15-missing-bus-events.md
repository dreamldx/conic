# Missing Bus Events Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the four highest-priority missing bus events identified in `docs/Improvement.md` section 2 — session lifecycle (`session_start`/`session_end`), Step/tool execution symmetry (`step_end`/`tool_execution_start`/`tool_execution_end`), summarize hooks (`before_summarize`/`summarize_done`/`summarize_failed`), and input interception (`input`) — so third-party plugins can observe and (where noted) intervene in these points without editing `react_loop.py` or `gateway.py`.

**Architecture:** Every new event follows the existing MessageBus conventions already used throughout the codebase: plain notifications use `bus.emit(topic, payload)` with a dataclass payload (chain-emit; a handler may return a mutated payload or `None` for "no change"); nothing here needs `bus.request` (request/response) since none of these are answered by exactly one plugin. Session lifecycle events are emitted by `DiscordGateway` itself (not `PluginManager`) right after `start_session()` returns / right before `stop_session()` is called — this keeps `PluginManager.start_session`/`stop_session` synchronous and untouched (no ripple into `tests/core/test_manager.py`), since the gateway is the caller that actually knows the `reason` ("new" vs "resume" vs "user_stop") and already awaits other bus emits at those call sites.

**Tech Stack:** Python 3.12, pytest + pytest-asyncio, the existing hand-rolled `conic.core.bus.MessageBus`.

**Spec:** `docs/Improvement.md` (section "二、Conic 缺失的事件", items 1/2/4/5, and section "四、建议落地顺序")

## Global Constraints

- Bus topic names must use constants from `src/conic/plugins/meta.py`, never hardcoded strings (AGENTS.md).
- Do not add comments to source code, EXCEPT `meta.py`, which already documents every topic with a one-line comment — new topics follow that existing per-file convention (it is a reference/documentation file, not logic).
- Update or add unit tests for every code change (AGENTS.md) — every task below is test-first.
- Run `uv run pytest` after every task; do not move to the next task if it fails.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/conic/plugins/meta.py` | Modify — add 8 new topic constants |
| `src/conic/core/messages.py` | Modify — add 9 new dataclasses, add `instructions` field to `SummarizeRequest` |
| `src/conic/discord/gateway.py` | Modify — emit `session_start`/`session_end`, add `input` interception in `handle_message` |
| `src/conic/plugins/loops/react_loop.py` | Modify — emit `step_end`, `tool_execution_start`, `tool_execution_end` |
| `src/conic/plugins/context/token_budget.py` | Modify — emit `before_summarize` (with cancel), `summarize_done`, `summarize_failed` |
| `src/conic/plugins/context/summarizer.py` | Modify — honor `SummarizeRequest.instructions` when set |
| `tests/discord/test_gateway.py` | Modify — add session lifecycle + input interception tests |
| `tests/plugins/loops/test_react_loop.py` | Modify — add step_end / tool_execution_start/end tests |
| `tests/plugins/context/test_token_budget.py` | Modify — add before_summarize/summarize_done/summarize_failed tests |
| `tests/plugins/context/test_summarizer.py` | Modify — add custom-instructions test |

No new files. `PluginManager` (`src/conic/core/manager.py`) and its tests are **not** touched.

---

### Task 1: Session lifecycle — `session_start` (reason: new/resume) + `session_end` (reason: user_stop)

**Files:**
- Modify: `src/conic/plugins/meta.py`
- Modify: `src/conic/core/messages.py`
- Modify: `src/conic/discord/gateway.py`
- Test: `tests/discord/test_gateway.py`

**Interfaces:**
- Produces: `meta.SessionStartEvent = "session_start"`, `meta.SessionEndEvent = "session_end"`; `messages.SessionStart(reason: str)`, `messages.SessionEnd(reason: str)`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/discord/test_gateway.py`. First add `from conic.plugins import meta` to the top imports (it isn't imported yet), and this helper right after the `FakePluginManagerRecorder` class:

```python
class FakePluginManagerFixedScope:
    """Ignores channel_plugin_factory and always returns a pre-built scope,
    so a test can attach bus listeners *before* calling the gateway method —
    gateway emits session_start internally and returns before the test would
    otherwise get a chance to subscribe."""

    def __init__(self, scope):
        self._scope = scope
        self.stopped: list[object] = []

    def start_session(self, channel, native_id, channel_plugin_factory):
        channel_plugin_factory()
        return self._scope

    def stop_session(self, scope):
        self.stopped.append(scope)


def make_fixed_scope(native_id="333"):
    from conic.core.bus import MessageBus
    from conic.core.session import SessionScope
    from conic.services.models import Session
    from datetime import datetime, timezone

    bus = MessageBus()
    row = Session(
        session_key=f"discord:{native_id}", channel="discord", native_id=native_id,
        workspace_dir="/tmp", model="m", status="active", created_at=datetime.now(timezone.utc),
    )
    return bus, SessionScope(bus=bus, row=row)
```

Then add these three tests (anywhere after the helpers, e.g. before `test_handle_message_routes_to_known_session`):

```python
async def test_handle_start_command_emits_session_start_with_reason_new():
    from conic.core.messages import SessionStart

    bus, scope = make_fixed_scope()
    starts = []

    async def on_session_start(msg: SessionStart) -> None:
        starts.append(msg.reason)

    bus.on(meta.SessionStartEvent, on_session_start)

    manager = FakePluginManagerFixedScope(scope)
    gateway = DiscordGateway(bot_token="t", plugin_manager=manager, storage=None)

    async def fake_create_thread():
        class FakeThread:
            id = 333
        return FakeThread()

    async def fake_respond(text: str):
        pass

    await gateway.handle_start_command(create_thread=fake_create_thread, respond=fake_respond)

    assert starts == ["new"]


async def test_resume_active_sessions_emits_session_start_with_reason_resume(tmp_path):
    from conic.core.messages import SessionStart

    storage = make_storage(tmp_path)
    storage.get_or_create(channel="discord", native_id="111")

    bus, scope = make_fixed_scope(native_id="111")
    starts = []

    async def on_session_start(msg: SessionStart) -> None:
        starts.append(msg.reason)

    bus.on(meta.SessionStartEvent, on_session_start)

    manager = FakePluginManagerFixedScope(scope)
    gateway = DiscordGateway(bot_token="t", plugin_manager=manager, storage=storage)

    async def fake_fetch_thread(native_id: str):
        return object()

    await gateway.resume_active_sessions(fetch_thread=fake_fetch_thread)

    assert starts == ["resume"]
    storage.shutdown()


async def test_handle_stop_command_emits_session_end_with_reason_user_stop():
    from conic.core.messages import SessionEnd

    manager = FakePluginManagerRecorder()
    gateway = DiscordGateway(bot_token="t", plugin_manager=manager, storage=None)
    scope = manager.start_session("discord", "444", lambda: object())
    gateway._sessions[444] = scope

    ends = []

    async def on_session_end(msg: SessionEnd) -> None:
        ends.append(msg.reason)

    scope.bus.on(meta.SessionEndEvent, on_session_end)

    async def fake_archive():
        pass

    await gateway.handle_stop_command(thread_id=444, archive=fake_archive)

    assert ends == ["user_stop"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/discord/test_gateway.py -k "session_start or session_end" -v`
Expected: FAIL — `AttributeError: module 'conic.plugins.meta' has no attribute 'SessionStartEvent'` (and `ImportError` for `SessionStart`/`SessionEnd` once meta resolves).

- [ ] **Step 3: Add the topic constants**

In `src/conic/plugins/meta.py`, add right after `UserInputEvent = "user_input"` (still inside the `# ── Plugin: DiscordGateway` section):

```python
# emitted once per session, right after PluginManager.start_session wires
# up its bus (reason: "new" or "resume")
SessionStartEvent = "session_start"
# emitted once per session when it is deliberately ended (reason: "user_stop")
SessionEndEvent = "session_end"
```

- [ ] **Step 4: Add the dataclasses**

In `src/conic/core/messages.py`, add right after the `UserInput` dataclass:

```python
@dataclass
class SessionStart:
    reason: str


@dataclass
class SessionEnd:
    reason: str
```

- [ ] **Step 5: Emit from DiscordGateway**

In `src/conic/discord/gateway.py`:

Change the import line:
```python
from conic.core.messages import SessionEnd, SessionStart, TurnEnd, UserInput
```

In `resume_active_sessions`, change:
```python
            try:
                scope = self._plugin_manager.start_session(
                    channel="discord",
                    native_id=row.native_id,
                    channel_plugin_factory=lambda t=thread: DiscordThreadPlugin(t),
                )
            except Exception:
```
to:
```python
            try:
                scope = self._plugin_manager.start_session(
                    channel="discord",
                    native_id=row.native_id,
                    channel_plugin_factory=lambda t=thread: DiscordThreadPlugin(t),
                )
                await scope.bus.emit(meta.SessionStartEvent, SessionStart(reason="resume"))
            except Exception:
```

In `handle_start_command`, change:
```python
        scope = self._plugin_manager.start_session(
            channel="discord",
            native_id=str(thread.id),
            channel_plugin_factory=lambda: DiscordThreadPlugin(thread),
        )
        self._sessions[thread.id] = scope
```
to:
```python
        scope = self._plugin_manager.start_session(
            channel="discord",
            native_id=str(thread.id),
            channel_plugin_factory=lambda: DiscordThreadPlugin(thread),
        )
        await scope.bus.emit(meta.SessionStartEvent, SessionStart(reason="new"))
        self._sessions[thread.id] = scope
```

In `handle_stop_command`, change:
```python
        async with scope.lock:
            await scope.bus.emit(meta.SessionStopEvent, TurnEnd())
            self._plugin_manager.stop_session(scope)
        await archive()
```
to:
```python
        async with scope.lock:
            await scope.bus.emit(meta.SessionStopEvent, TurnEnd())
            await scope.bus.emit(meta.SessionEndEvent, SessionEnd(reason="user_stop"))
            self._plugin_manager.stop_session(scope)
        await archive()
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/discord/test_gateway.py -v`
Expected: all PASS (including the pre-existing tests in this file — nothing else should regress).

- [ ] **Step 7: Commit**

```bash
git add src/conic/plugins/meta.py src/conic/core/messages.py src/conic/discord/gateway.py tests/discord/test_gateway.py
git commit -m "feat: emit session_start/session_end lifecycle events from DiscordGateway"
```

---

### Task 2: Step/tool execution symmetry — `step_end` + `tool_execution_start`/`tool_execution_end`

**Files:**
- Modify: `src/conic/plugins/meta.py`
- Modify: `src/conic/core/messages.py`
- Modify: `src/conic/plugins/loops/react_loop.py`
- Test: `tests/plugins/loops/test_react_loop.py`

**Interfaces:**
- Produces: `meta.StepEndEvent = "step_end"`, `meta.ToolExecutionStartEvent = "tool_execution_start"`, `meta.ToolExecutionEndEvent = "tool_execution_end"`; `messages.StepEnd(step_index: int)`, `messages.ToolExecutionStart(call: ToolCallSpec)`, `messages.ToolExecutionEnd(call: ToolCallSpec, result: ToolCallResult)`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/plugins/loops/test_react_loop.py`. Extend the existing import line:

```python
from conic.core.messages import (
    AssistantMessage, Error, ModelRequest, ModelResponse, StepEnd, StepStart,
    ToolCall, ToolCallResult, ToolCallSpec, ToolExecutionEnd, ToolExecutionStart,
    TurnEnd, UserInput,
)
```

(`ToolCallSpec` was already imported; keep the merged single import block alphabetized as above — replace the existing `from conic.core.messages import (...)` block entirely with this one.)

Add these tests (e.g. after `test_multi_step_turn_executes_tool_then_returns_final_answer`):

```python
async def test_step_end_emitted_with_matching_step_index_on_final_step():
    handle = FakeStorageHandle()
    responses = [ModelResponse(text="hi there", tool_calls=[], raw_message={"role": "assistant"})]
    bus, loop = make_loop(handle, responses)

    ends = []

    async def on_step_end(msg: StepEnd) -> None:
        ends.append(msg.step_index)

    bus.on("step_end", on_step_end)

    await bus.emit("user_input", UserInput(text="hello"))

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

    bus.on("step_end", on_step_end)

    await bus.emit("user_input", UserInput(text="run ls"))

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

    bus.on("before_tool_call", on_before_tool_call)
    bus.on("tool_execution_start", on_execution_start)
    bus.on("tool_execution_end", on_execution_end)
    bus.on("tool_result", on_tool_result)

    await bus.emit("user_input", UserInput(text="run ls"))

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

    bus.on("tool_execution_end", on_execution_end)
    bus.on("tool_result", mutate_result)

    await bus.emit("user_input", UserInput(text="run ls"))

    assert captured == ["ran ls"]
    tool_messages = [m for m in handle.messages if m.get("role") == "tool"]
    assert tool_messages[0]["content"] == "mutated"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/plugins/loops/test_react_loop.py -v`
Expected: FAIL — `ImportError: cannot import name 'StepEnd'` (and similar for `ToolExecutionStart`/`ToolExecutionEnd`).

- [ ] **Step 3: Add the topic constants**

In `src/conic/plugins/meta.py`, in the `# ── Plugin: ReactLoopPlugin` section, change:
```python
# emitted at the start of each step within a turn
StepStartEvent = "step_start"
# emitted before each LLM call, carries messages + tool schemas
BeforeModelCallEvent = "before_model_call"
```
to:
```python
# emitted at the start of each step within a turn
StepStartEvent = "step_start"
# emitted at the end of each step within a turn (mirrors StepStartEvent)
StepEndEvent = "step_end"
# emitted before each LLM call, carries messages + tool schemas
BeforeModelCallEvent = "before_model_call"
```

And change:
```python
# emitted before each tool execution for policy inspection
ToolCallEvent = "before_tool_call"
# received from tool plugin, re-emitted for observation
ToolCallResultEvent = "tool_result"
```
to:
```python
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
```

- [ ] **Step 4: Add the dataclasses**

In `src/conic/core/messages.py`, add right after `StepStart`:
```python
@dataclass
class StepEnd:
    step_index: int
```

And add right after `ToolCallResult`:
```python
@dataclass
class ToolExecutionStart:
    call: ToolCallSpec


@dataclass
class ToolExecutionEnd:
    call: ToolCallSpec
    result: ToolCallResult
```

- [ ] **Step 5: Emit from ReactLoopPlugin**

In `src/conic/plugins/loops/react_loop.py`, update the import line to also pull in `StepEnd`, `ToolExecutionEnd`, `ToolExecutionStart`:
```python
from conic.core.messages import (
    AssistantMessage, BeforeModelCall, Error, ModelRequest, ModelResponse,
    StepEnd, StepStart, ToolCall, ToolCallResult, ToolExecutionEnd,
    ToolExecutionStart, TurnEnd, TurnStart, UserInput,
)
```

Replace the whole `while True:` body with (this captures `this_step` before the increment so `StepEnd` reports the same index as the `StepStart` it closes, and brackets the actual dispatch with the two new tool-execution events):

```python
            while True:
                this_step = step_index
                await bus.emit(meta.StepStartEvent, StepStart(step_index=this_step))
                step_index += 1
                history = self._storage.load_history()
                ctx = await bus.emit(
                    meta.BeforeModelCallEvent, BeforeModelCall(messages=history, tools=self._tool_schemas)
                )
                response: ModelResponse = await bus.request(
                    meta.ModelRequestEvent, ModelRequest(messages=ctx.messages, tools=ctx.tools)
                )
                response = await bus.emit(meta.ModelResponseEvent, response)
                if not response.tool_calls:
                    out = await bus.emit(meta.AssistantMessageEvent, AssistantMessage(text=response.text or ""))
                    self._storage.append_message({"role": "assistant", "content": out.text})
                    await bus.emit(meta.StepEndEvent, StepEnd(step_index=this_step))
                    break
                self._storage.append_message(response.raw_message)
                for call in response.tool_calls:
                    original_id = call.id
                    try:
                        call_ctx = await bus.emit(meta.ToolCallEvent, ToolCall(call=call))
                        payload_cls = self._tool_payload_map[call_ctx.call.name]
                        payload = payload_cls(**call_ctx.call.args)
                        await bus.emit(meta.ToolExecutionStartEvent, ToolExecutionStart(call=call_ctx.call))
                        result: ToolCallResult = await bus.request(meta.ToolCallRequestEvent, payload)
                        await bus.emit(
                            meta.ToolExecutionEndEvent, ToolExecutionEnd(call=call_ctx.call, result=result)
                        )
                        result = await bus.emit(meta.ToolCallResultEvent, result)
                        content = result.output if result.error is None else f"Error: {result.error}"
                    except AbortTurn:
                        raise
                    except Exception as exc:
                        logger.warning("tool call failed name={} error={}", call.name, exc)
                        content = f"Error: {exc}"
                    self._storage.append_message(
                        {"role": "tool", "tool_call_id": original_id, "content": content}
                    )
                await bus.emit(meta.StepEndEvent, StepEnd(step_index=this_step))
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/plugins/loops/test_react_loop.py -v`
Expected: all PASS (all pre-existing tests in this file too).

- [ ] **Step 7: Commit**

```bash
git add src/conic/plugins/meta.py src/conic/core/messages.py src/conic/plugins/loops/react_loop.py tests/plugins/loops/test_react_loop.py
git commit -m "feat: emit step_end and tool_execution_start/end from ReactLoopPlugin"
```

---

### Task 3: Summarize hooks — `before_summarize` (cancel/customize) + `summarize_done` + `summarize_failed`

**Files:**
- Modify: `src/conic/plugins/meta.py`
- Modify: `src/conic/core/messages.py`
- Modify: `src/conic/plugins/context/token_budget.py`
- Modify: `src/conic/plugins/context/summarizer.py`
- Test: `tests/plugins/context/test_token_budget.py`
- Test: `tests/plugins/context/test_summarizer.py`

**Interfaces:**
- Produces: `meta.BeforeSummarizeEvent = "before_summarize"`, `meta.SummarizeDoneEvent = "summarize_done"`, `meta.SummarizeFailedEvent = "summarize_failed"`; `messages.BeforeSummarize(request: SummarizeRequest, cancelled: bool = False)`, `messages.SummarizeDone(result: SummarizeResult)`, `messages.SummarizeFailed(exc: Exception)`; `messages.SummarizeRequest` gains `instructions: str | None = None`.
- Consumes: existing `messages.SummarizeRequest`, `messages.SummarizeResult` (Task adds a field to the former; must stay keyword-compatible with all existing call sites, which already construct it via keyword args).

- [ ] **Step 1: Write the failing tests**

Replace the top import line of `tests/plugins/context/test_token_budget.py` with:

```python
import pytest

from conic.core.bus import MessageBus
from conic.core.messages import (
    BeforeModelCall, BeforeSummarize, SummarizeDone, SummarizeFailed,
    SummarizeRequest, SummarizeResult,
)
from conic.plugins.context.token_budget import TokenBudgetPlugin
```

Append these tests:

```python
async def test_before_summarize_hook_can_customize_instructions():
    plugin = TokenBudgetPlugin(budget_tokens=1)
    bus = MessageBus()

    captured_requests = []

    async def fake_summarizer(req: SummarizeRequest):
        captured_requests.append(req)
        return SummarizeResult(messages=[{"role": "system", "content": "summary"}])

    async def customize(msg: BeforeSummarize) -> BeforeSummarize:
        msg.request.instructions = "focus on decisions only"
        return msg

    bus.on_request("summarize", fake_summarizer)
    bus.on("before_summarize", customize)
    plugin.register(bus)

    ctx = BeforeModelCall(messages=[{"role": "user", "content": "hi " * 100}], tools=[])
    await plugin.apply(ctx)

    assert captured_requests[0].instructions == "focus on decisions only"


async def test_before_summarize_hook_can_cancel_and_skip_summarization():
    plugin = TokenBudgetPlugin(budget_tokens=1)
    bus = MessageBus()

    called = []

    async def fake_summarizer(req: SummarizeRequest):
        called.append(True)
        return SummarizeResult(messages=[])

    async def cancel(msg: BeforeSummarize) -> BeforeSummarize:
        msg.cancelled = True
        return msg

    bus.on_request("summarize", fake_summarizer)
    bus.on("before_summarize", cancel)
    plugin.register(bus)

    ctx = BeforeModelCall(messages=[{"role": "user", "content": "hi " * 100}], tools=[])
    result = await plugin.apply(ctx)

    assert result is None
    assert called == []


async def test_summarize_done_emitted_with_result_on_success():
    plugin = TokenBudgetPlugin(budget_tokens=1)
    bus = MessageBus()

    summary_result = SummarizeResult(messages=[{"role": "system", "content": "summary"}])

    async def fake_summarizer(req: SummarizeRequest):
        return summary_result

    done = []

    async def on_done(msg: SummarizeDone) -> None:
        done.append(msg.result)

    bus.on_request("summarize", fake_summarizer)
    bus.on("summarize_done", on_done)
    plugin.register(bus)

    ctx = BeforeModelCall(messages=[{"role": "user", "content": "hi " * 100}], tools=[])
    await plugin.apply(ctx)

    assert done == [summary_result]


async def test_summarize_failed_emitted_and_reraised_on_error():
    plugin = TokenBudgetPlugin(budget_tokens=1)
    bus = MessageBus()

    boom = RuntimeError("summarizer backend down")

    async def failing_summarizer(req: SummarizeRequest):
        raise boom

    failed = []

    async def on_failed(msg: SummarizeFailed) -> None:
        failed.append(msg.exc)

    bus.on_request("summarize", failing_summarizer)
    bus.on("summarize_failed", on_failed)
    plugin.register(bus)

    ctx = BeforeModelCall(messages=[{"role": "user", "content": "hi " * 100}], tools=[])
    with pytest.raises(RuntimeError):
        await plugin.apply(ctx)

    assert failed == [boom]
```

Also append to `tests/plugins/context/test_summarizer.py`:

```python
async def test_summarize_uses_custom_instructions_when_provided():
    plugin = SummarizerPlugin(keep_recent=1)
    bus = MessageBus()

    captured_prompts = []

    async def fake_model_request(msg: ModelRequest):
        captured_prompts.append(msg.messages)
        return ModelResponse(text="summary", tool_calls=[], raw_message={})

    bus.on_request("model_request", fake_model_request)
    plugin.register(bus)

    messages = [
        {"role": "user", "content": "old"},
        {"role": "user", "content": "recent"},
    ]
    await plugin.summarize(
        SummarizeRequest(messages=messages, budget_tokens=1, instructions="focus on decisions only")
    )

    assert captured_prompts[0][0] == {"role": "system", "content": "focus on decisions only"}
```

`tests/plugins/context/test_summarizer.py` already imports `ModelRequest`, `ModelResponse`, and `SummarizeRequest` at the top — no import changes needed for this test.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/plugins/context/test_token_budget.py tests/plugins/context/test_summarizer.py -v`
Expected: FAIL — `ImportError: cannot import name 'BeforeSummarize'`.

- [ ] **Step 3: Add the topic constants**

In `src/conic/plugins/meta.py`, change:
```python
# ── Plugin: SummarizerPlugin / TokenBudgetPlugin ──────────────────────────
# dispatched when context exceeds token budget; returned as summary result
SummarizeEvent = "summarize"
```
to:
```python
# ── Plugin: SummarizerPlugin / TokenBudgetPlugin ──────────────────────────
# emitted before SummarizeEvent; hooks may customize instructions or cancel
BeforeSummarizeEvent = "before_summarize"
# dispatched when context exceeds token budget; returned as summary result
SummarizeEvent = "summarize"
# emitted after a successful summarize, carrying the SummarizeResult
SummarizeDoneEvent = "summarize_done"
# emitted when summarize raises, before the exception propagates
SummarizeFailedEvent = "summarize_failed"
```

- [ ] **Step 4: Add the dataclasses and the new field**

In `src/conic/core/messages.py`, change:
```python
@dataclass
class SummarizeRequest:
    messages: list[dict]
    budget_tokens: int
```
to:
```python
@dataclass
class SummarizeRequest:
    messages: list[dict]
    budget_tokens: int
    instructions: str | None = None
```

Add right after `SummarizeResult`:
```python
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
```

- [ ] **Step 5: Wire the hooks into TokenBudgetPlugin**

Replace the full contents of `src/conic/plugins/context/token_budget.py`:

```python
from conic.core.messages import BeforeModelCall, BeforeSummarize, SummarizeDone, SummarizeFailed, SummarizeRequest
from conic.core.tokencount import estimate_tokens
from conic.plugins import meta


class TokenBudgetPlugin:
    def __init__(self, budget_tokens: int):
        self._budget_tokens = budget_tokens
        self._bus = None

    def register(self, bus) -> None:
        self._bus = bus
        bus.on(meta.BeforeModelCallEvent, self.apply)

    async def apply(self, ctx: BeforeModelCall) -> BeforeModelCall | None:
        if estimate_tokens(ctx.messages) <= self._budget_tokens:
            return None
        request = SummarizeRequest(messages=ctx.messages, budget_tokens=self._budget_tokens)
        before = await self._bus.emit(meta.BeforeSummarizeEvent, BeforeSummarize(request=request))
        if before.cancelled:
            return None
        try:
            result = await self._bus.request(meta.SummarizeEvent, before.request)
        except Exception as exc:
            await self._bus.emit(meta.SummarizeFailedEvent, SummarizeFailed(exc=exc))
            raise
        await self._bus.emit(meta.SummarizeDoneEvent, SummarizeDone(result=result))
        return BeforeModelCall(messages=result.messages, tools=ctx.tools)
```

- [ ] **Step 6: Honor `instructions` in SummarizerPlugin**

In `src/conic/plugins/context/summarizer.py`, change:
```python
        transcript = "\n".join(f"{m.get('role')}: {m.get('content')}" for m in to_summarize)
        prompt = [
            {
                "role": "system",
                "content": "Summarize the following conversation history concisely, preserving key facts and decisions.",
            },
            {"role": "user", "content": transcript},
        ]
```
to:
```python
        transcript = "\n".join(f"{m.get('role')}: {m.get('content')}" for m in to_summarize)
        instructions = req.instructions or (
            "Summarize the following conversation history concisely, preserving key facts and decisions."
        )
        prompt = [
            {"role": "system", "content": instructions},
            {"role": "user", "content": transcript},
        ]
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/plugins/context/test_token_budget.py tests/plugins/context/test_summarizer.py -v`
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add src/conic/plugins/meta.py src/conic/core/messages.py src/conic/plugins/context/token_budget.py src/conic/plugins/context/summarizer.py tests/plugins/context/test_token_budget.py tests/plugins/context/test_summarizer.py
git commit -m "feat: add before_summarize/summarize_done/summarize_failed hooks"
```

---

### Task 4: Input interception — `input` event (continue / transform / handled)

**Files:**
- Modify: `src/conic/plugins/meta.py`
- Modify: `src/conic/core/messages.py`
- Modify: `src/conic/discord/gateway.py`
- Test: `tests/discord/test_gateway.py`

**Interfaces:**
- Produces: `meta.InputEvent = "input"`; `messages.Input(text: str, handled: bool = False)`.
- Consumes: `messages.UserInput` (existing) — `handle_message` now constructs it from the (possibly transformed) `Input.text` instead of the raw argument.

- [ ] **Step 1: Write the failing tests**

Add to `tests/discord/test_gateway.py` (after the Task 1 tests, or anywhere convenient):

```python
async def test_handle_message_input_hook_can_transform_text_before_user_input():
    from conic.core.messages import Input, UserInput

    manager = FakePluginManagerRecorder()
    gateway = DiscordGateway(bot_token="t", plugin_manager=manager, storage=None)
    scope = manager.start_session("discord", "222", lambda: object())
    gateway._sessions[222] = scope

    async def upcase(msg: Input) -> Input:
        return Input(text=msg.text.upper())

    received = []

    async def on_user_input(msg: UserInput) -> None:
        received.append(msg.text)

    scope.bus.on(meta.InputEvent, upcase)
    scope.bus.on(meta.UserInputEvent, on_user_input)

    await gateway.handle_message(thread_id=222, text="hello")

    assert received == ["HELLO"]


async def test_handle_message_input_hook_can_mark_handled_and_short_circuit():
    from conic.core.messages import Input, UserInput

    manager = FakePluginManagerRecorder()
    gateway = DiscordGateway(bot_token="t", plugin_manager=manager, storage=None)
    scope = manager.start_session("discord", "222", lambda: object())
    gateway._sessions[222] = scope

    async def handle_command(msg: Input) -> Input:
        if msg.text == "!status":
            return Input(text=msg.text, handled=True)
        return msg

    received = []

    async def on_user_input(msg: UserInput) -> None:
        received.append(msg.text)

    scope.bus.on(meta.InputEvent, handle_command)
    scope.bus.on(meta.UserInputEvent, on_user_input)

    await gateway.handle_message(thread_id=222, text="!status")

    assert received == []
```

(`meta` is already imported at the top of this file from Task 1.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/discord/test_gateway.py -k input_hook -v`
Expected: FAIL — `AttributeError: module 'conic.plugins.meta' has no attribute 'InputEvent'`.

- [ ] **Step 3: Add the topic constant**

In `src/conic/plugins/meta.py`, change:
```python
# emitted when a non-bot message arrives in a monitored Discord thread
UserInputEvent = "user_input"
```
to:
```python
# emitted when a non-bot message arrives in a monitored Discord thread
UserInputEvent = "user_input"
# emitted right before UserInputEvent, so hooks can transform the text or
# mark it handled to keep it from reaching the session's loop entirely
InputEvent = "input"
```

(If Task 1 already ran, this constant is inserted right before the `SessionStartEvent`/`SessionEndEvent` lines Task 1 added — order among the three doesn't matter.)

- [ ] **Step 4: Add the dataclass**

In `src/conic/core/messages.py`, add right after `UserInput` (before `SessionStart`, if Task 1 already ran):
```python
@dataclass
class Input:
    text: str
    handled: bool = False
```

- [ ] **Step 5: Wire it into DiscordGateway.handle_message**

In `src/conic/discord/gateway.py`, update the import line to include `Input`:
```python
from conic.core.messages import Input, SessionEnd, SessionStart, TurnEnd, UserInput
```

Change:
```python
    async def handle_message(self, thread_id: int, text: str) -> None:
        scope = self._sessions.get(thread_id)
        if scope is None:
            return
        async with scope.lock:
            await scope.bus.emit(meta.UserInputEvent, UserInput(text=text))
```
to:
```python
    async def handle_message(self, thread_id: int, text: str) -> None:
        scope = self._sessions.get(thread_id)
        if scope is None:
            return
        async with scope.lock:
            ctx = await scope.bus.emit(meta.InputEvent, Input(text=text))
            if ctx.handled:
                return
            await scope.bus.emit(meta.UserInputEvent, UserInput(text=ctx.text))
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/discord/test_gateway.py -v`
Expected: all PASS.

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest -q`
Expected: all PASS, no regressions across the whole project.

- [ ] **Step 8: Commit**

```bash
git add src/conic/plugins/meta.py src/conic/core/messages.py src/conic/discord/gateway.py tests/discord/test_gateway.py
git commit -m "feat: add input interception event (continue/transform/handled) to DiscordGateway"
```

---

## Post-plan documentation update (not a code task)

After all four tasks are committed, update `docs/Improvement.md` section 2 to mark items 1, 2, 4, 5 as done (or remove them / move to a "已完成" note), and update `docs/superpowers/specs/2026-09-13-conic-agentic-engine-design.md` section 8 (LoopPlugin, DiscordGateway, SummarizerPlugin subsections) and the event-topic list to mention the 8 new topics — this keeps the "已知缺口" docs from going stale now that they're implemented. Do this as its own small commit, not folded into Task 4.
