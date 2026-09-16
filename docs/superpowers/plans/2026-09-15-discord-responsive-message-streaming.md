# Discord Responsive Message + Token Streaming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the Discord experience from "typing indicator + one final message" into a single message that live-updates for the whole Turn: a placeholder appears immediately, cycles through "thinking" / tool-status text, streams the model's own token output live, and is edited into the final answer (or error) at the end.

**Architecture:** Two new plain chain-emit bus events carry the live-update signal — `MessageUpdateEvent` (`MessageUpdate(text)`, wholesale replace) and `MessageDeltaUpdateEvent` (`MessageDeltaUpdate(text_delta)`, append). `OpenRouterBackendPlugin` emits deltas while streaming a model call (only when the caller opts in via a new `ModelRequest.stream_updates` field); `ReactLoopPlugin` emits full-replace updates at step/tool-status transitions; `DiscordThreadPlugin` is the only subscriber to both — it owns the placeholder message, an in-memory text buffer, and a throttle so live token updates don't flood the Discord edit API. `react_loop.py`'s `bus.request(ModelRequestEvent, ...)` contract is unchanged — it still gets back one complete `ModelResponse` per call, streaming or not.

**Tech Stack:** Python 3.12, pytest + pytest-asyncio, `openai` AsyncOpenAI SDK's `stream=True` chat completions.

**Spec:** `docs/superpowers/specs/2026-09-13-conic-agentic-engine-design.md` (event table in section 6; sections 8.1 ReactLoopPlugin, 8.2 OpenRouterBackendPlugin, 8.5 DiscordThreadPlugin)

## Global Constraints

- Bus topic names must use constants from `src/conic/plugins/meta.py`, never hardcoded strings (AGENTS.md).
- Do not add comments to source code, EXCEPT `meta.py`'s existing one-line-per-topic convention, which new topics follow.
- Update or add unit tests for every code change (AGENTS.md) — every task below is test-first.
- Run `uv run pytest` after every task; do not move to the next task if it fails.
- `SummarizerPlugin`'s own model call must NOT stream and must NOT drive any Discord update — it never sets `stream_updates`, so it always gets the existing non-streaming path unchanged. Do not touch `src/conic/plugins/context/summarizer.py` or `src/conic/plugins/context/token_budget.py` in this plan.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/conic/types/messages.py` | Modify — add `MessageUpdate`, `MessageDeltaUpdate` dataclasses; add `ModelRequest.stream_updates` field |
| `src/conic/plugins/meta.py` | Modify — add `MessageUpdateEvent`, `MessageDeltaUpdateEvent` topic constants |
| `src/conic/plugins/backends/openrouter.py` | Modify — split `complete()` into blocking (unchanged, default) and streaming (new) paths |
| `src/conic/plugins/loops/react_loop.py` | Modify — emit `MessageUpdateEvent` at step/tool-status transitions; request with `stream_updates=True` |
| `src/conic/plugins/channels/discord.py` | Modify — send a placeholder at Turn start, live-render `MessageUpdateEvent`/`MessageDeltaUpdateEvent`, finalize by editing (not sending) |
| `tests/types/test_messages.py` | Modify — construct the two new dataclasses, confirm `ModelRequest.stream_updates` defaults to `False` |
| `tests/plugins/backends/test_openrouter.py` | Modify — add streaming-path tests; 3 existing tests untouched |
| `tests/plugins/loops/test_react_loop.py` | Modify — add status-emission and `stream_updates` tests |
| `tests/plugins/channels/test_discord.py` | Modify — new `FakeMessage`/clock-injection support; add responsive-message tests |

No new files.

---

### Task 1: Types, topics, and `OpenRouterBackendPlugin` streaming path

**Files:**
- Modify: `src/conic/types/messages.py`
- Modify: `src/conic/plugins/meta.py`
- Modify: `src/conic/plugins/backends/openrouter.py`
- Test: `tests/types/test_messages.py`
- Test: `tests/plugins/backends/test_openrouter.py`

**Interfaces:**
- Produces: `meta.MessageUpdateEvent = "message_update"`, `meta.MessageDeltaUpdateEvent = "message_delta_update"`; `messages.MessageUpdate(text: str)`, `messages.MessageDeltaUpdate(text_delta: str)`; `messages.ModelRequest` gains `stream_updates: bool = False`.
- Consumes: nothing new.

- [ ] **Step 1: Write the failing tests**

In `tests/types/test_messages.py`, change the import line:
```python
from conic.types.messages import (
    UserInput, TurnStart, TurnEnd, StepStart, BeforeModelCall, ModelRequest,
    ToolCallSpec, ModelResponse, ToolCall, ToolCallResult, AssistantMessage,
    Error, SummarizeRequest, SummarizeResult, MessageUpdate, MessageDeltaUpdate,
)
```
and add these two lines inside `test_message_dataclasses_construct` (anywhere, e.g. right after the `TurnEnd()` line):
```python
    assert MessageUpdate(text="hi").text == "hi"
    assert MessageDeltaUpdate(text_delta="he").text_delta == "he"
    assert ModelRequest(messages=[], tools=[]).stream_updates is False
```

In `tests/plugins/backends/test_openrouter.py`, replace the whole file with:
```python
import json
from dataclasses import dataclass, field
from types import SimpleNamespace

from conic.core.bus import MessageBus
from conic.types.messages import MessageDeltaUpdate, ModelRequest
from conic.plugins import meta
from conic.plugins.backends.openrouter import OpenRouterBackendPlugin


class FakeCompletions:
    def __init__(self, response):
        self._response = response
        self.last_kwargs = None

    async def create(self, **kwargs):
        self.last_kwargs = kwargs
        return self._response


class FakeChat:
    def __init__(self, completions):
        self.completions = completions


class FakeClient:
    def __init__(self, completions):
        self.chat = FakeChat(completions)


def make_message(content=None, tool_calls=None):
    return SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
        model_dump=lambda: {"role": "assistant", "content": content, "tool_calls": tool_calls},
    )


def make_response(message):
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


async def test_complete_returns_text_response_with_no_tool_calls():
    message = make_message(content="hello there")
    client = FakeClient(FakeCompletions(make_response(message)))
    backend = OpenRouterBackendPlugin(api_key="k", model="test-model", client=client)

    result = await backend.complete(ModelRequest(messages=[{"role": "user", "content": "hi"}], tools=[]))

    assert result.text == "hello there"
    assert result.tool_calls == []
    assert client.chat.completions.last_kwargs["model"] == "test-model"


async def test_complete_parses_tool_calls():
    fn = SimpleNamespace(name="bash", arguments=json.dumps({"command": "ls"}))
    tool_call = SimpleNamespace(id="call_1", function=fn)
    message = make_message(content=None, tool_calls=[tool_call])
    client = FakeClient(FakeCompletions(make_response(message)))
    backend = OpenRouterBackendPlugin(api_key="k", model="test-model", client=client)

    result = await backend.complete(ModelRequest(messages=[], tools=[]))

    assert result.tool_calls[0].id == "call_1"
    assert result.tool_calls[0].name == "bash"
    assert result.tool_calls[0].args == {"command": "ls"}


def test_register_wires_model_request():
    backend = OpenRouterBackendPlugin(
        api_key="k", model="m", client=FakeClient(FakeCompletions(make_response(make_message())))
    )
    bus = MessageBus()
    backend.register(bus)
    assert bus._request["model_request"][0][0] is ModelRequest


class FakeStreamingCompletions:
    def __init__(self, chunks):
        self._chunks = chunks
        self.last_kwargs = None

    async def create(self, **kwargs):
        self.last_kwargs = kwargs

        async def _iter():
            for chunk in self._chunks:
                yield chunk

        return _iter()


def make_delta(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def make_chunk(delta):
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def make_tool_call_delta(index, id=None, name=None, arguments=None):
    return SimpleNamespace(index=index, id=id, function=SimpleNamespace(name=name, arguments=arguments))


async def test_streaming_complete_assembles_text_and_emits_deltas():
    chunks = [
        make_chunk(make_delta(content="Hel")),
        make_chunk(make_delta(content="lo")),
        make_chunk(make_delta(content=None)),
    ]
    client = FakeClient(FakeStreamingCompletions(chunks))
    backend = OpenRouterBackendPlugin(api_key="k", model="m", client=client)
    bus = MessageBus()
    backend.register(bus)

    deltas = []

    async def on_delta(msg: MessageDeltaUpdate) -> None:
        deltas.append(msg.text_delta)

    bus.on(meta.MessageDeltaUpdateEvent, on_delta)

    result = await backend.complete(ModelRequest(messages=[], tools=[], stream_updates=True))

    assert result.text == "Hello"
    assert result.tool_calls == []
    assert result.raw_message == {"role": "assistant", "content": "Hello", "tool_calls": None}
    assert deltas == ["Hel", "lo"]
    assert client.chat.completions.last_kwargs["stream"] is True


async def test_streaming_complete_assembles_tool_call_from_fragments():
    chunks = [
        make_chunk(make_delta(tool_calls=[make_tool_call_delta(0, id="call_1", name="bash", arguments="")])),
        make_chunk(make_delta(tool_calls=[make_tool_call_delta(0, arguments='{"command"')])),
        make_chunk(make_delta(tool_calls=[make_tool_call_delta(0, arguments=': "ls"}')])),
    ]
    client = FakeClient(FakeStreamingCompletions(chunks))
    backend = OpenRouterBackendPlugin(api_key="k", model="m", client=client)
    bus = MessageBus()
    backend.register(bus)

    deltas = []
    bus.on(meta.MessageDeltaUpdateEvent, lambda msg: deltas.append(msg.text_delta))

    result = await backend.complete(ModelRequest(messages=[], tools=[], stream_updates=True))

    assert result.text is None
    assert result.tool_calls[0].id == "call_1"
    assert result.tool_calls[0].name == "bash"
    assert result.tool_calls[0].args == {"command": "ls"}
    assert result.raw_message["tool_calls"] == [
        {"id": "call_1", "type": "function", "function": {"name": "bash", "arguments": '{"command": "ls"}'}}
    ]
    assert deltas == []


async def test_streaming_complete_orders_parallel_tool_calls_by_index():
    chunks = [
        make_chunk(make_delta(tool_calls=[
            make_tool_call_delta(1, id="call_b", name="read_file", arguments="{}"),
            make_tool_call_delta(0, id="call_a", name="bash", arguments="{}"),
        ])),
    ]
    client = FakeClient(FakeStreamingCompletions(chunks))
    backend = OpenRouterBackendPlugin(api_key="k", model="m", client=client)
    bus = MessageBus()
    backend.register(bus)

    result = await backend.complete(ModelRequest(messages=[], tools=[], stream_updates=True))

    assert [tc.id for tc in result.tool_calls] == ["call_a", "call_b"]


async def test_streaming_complete_defaults_empty_arguments_to_empty_dict():
    chunks = [
        make_chunk(make_delta(tool_calls=[make_tool_call_delta(0, id="call_1", name="ls", arguments=None)])),
    ]
    client = FakeClient(FakeStreamingCompletions(chunks))
    backend = OpenRouterBackendPlugin(api_key="k", model="m", client=client)
    bus = MessageBus()
    backend.register(bus)

    result = await backend.complete(ModelRequest(messages=[], tools=[], stream_updates=True))

    assert result.tool_calls[0].args == {}
```

Note: `test_register_wires_model_request` had to change slightly (build the client differently) because `FakeCompletions`/`FakeClient` no longer bundle a fixed response the way the old `FakeChat` did — this is a mechanical fixture reshape, not a behavior change; `bus._request["model_request"][0][0] is ModelRequest` still asserts the same thing.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/types/test_messages.py tests/plugins/backends/test_openrouter.py -v`
Expected: FAIL — `ImportError: cannot import name 'MessageUpdate'` (and similar).

- [ ] **Step 3: Add the topic constants**

In `src/conic/plugins/meta.py`, add a new section right after the `SystemPromptPlugin` section (before the final `SessionStopEvent` line, order doesn't matter — pick anywhere sensible):
```python
# ── Plugin: ReactLoopPlugin / OpenRouterBackendPlugin ─────────────────────
# emitted by ReactLoopPlugin to wholesale-replace the current live status
# text (e.g. "thinking", a tool-status line)
MessageUpdateEvent = "message_update"
# emitted by OpenRouterBackendPlugin per streaming chunk when
# ModelRequest.stream_updates is True; carries only visible text, never
# tool-call argument fragments
MessageDeltaUpdateEvent = "message_delta_update"
```

- [ ] **Step 4: Add the dataclasses and the new field**

In `src/conic/types/messages.py`, add after `ModelRequest`:
```python
@dataclass
class MessageUpdate:
    text: str


@dataclass
class MessageDeltaUpdate:
    text_delta: str
```

Change `ModelRequest` to:
```python
@dataclass
class ModelRequest:
    messages: list[dict]
    tools: list[dict]
    stream_updates: bool = False
```

- [ ] **Step 5: Split `OpenRouterBackendPlugin.complete()` into blocking/streaming**

Replace the full contents of `src/conic/plugins/backends/openrouter.py`:
```python
import json

from loguru import logger
from openai import AsyncOpenAI

from conic.types.messages import MessageDeltaUpdate, ModelRequest, ModelResponse, ToolCallSpec
from conic.plugins import meta


class OpenRouterBackendPlugin:
    def __init__(self, api_key: str, model: str, client: AsyncOpenAI | None = None):
        self.model = model
        self._client = client or AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=api_key)
        self._bus = None

    def register(self, bus) -> None:
        self._bus = bus
        bus.on_request(meta.ModelRequestEvent, self.complete)

    async def complete(self, msg: ModelRequest) -> ModelResponse:
        if msg.stream_updates:
            return await self._complete_streaming(msg)
        return await self._complete_blocking(msg)

    async def _complete_blocking(self, msg: ModelRequest) -> ModelResponse:
        logger.debug(
            "calling openrouter model={} messages={} tools={}",
            self.model, len(msg.messages), len(msg.tools or []),
        )
        response = await self._client.chat.completions.create(
            model=self.model,
            messages=msg.messages,
            tools=msg.tools or None,
        )
        message = response.choices[0].message
        raw_message = message.model_dump()
        tool_calls = [
            ToolCallSpec(id=tc.id, name=tc.function.name, args=json.loads(tc.function.arguments))
            for tc in (message.tool_calls or [])
        ]
        logger.debug(
            "openrouter response text_len={} tool_calls={}", len(message.content or ""), len(tool_calls)
        )
        return ModelResponse(text=message.content, tool_calls=tool_calls, raw_message=raw_message)

    async def _complete_streaming(self, msg: ModelRequest) -> ModelResponse:
        logger.debug(
            "calling openrouter (streaming) model={} messages={} tools={}",
            self.model, len(msg.messages), len(msg.tools or []),
        )
        stream = await self._client.chat.completions.create(
            model=self.model,
            messages=msg.messages,
            tools=msg.tools or None,
            stream=True,
        )
        content_parts: list[str] = []
        tool_call_acc: dict[int, dict] = {}
        async for chunk in stream:
            delta = chunk.choices[0].delta
            if delta.content:
                content_parts.append(delta.content)
                await self._bus.emit(meta.MessageDeltaUpdateEvent, MessageDeltaUpdate(text_delta=delta.content))
            for tc in (delta.tool_calls or []):
                acc = tool_call_acc.setdefault(tc.index, {"id": None, "name": None, "arguments": ""})
                if tc.id:
                    acc["id"] = tc.id
                if tc.function and tc.function.name:
                    acc["name"] = tc.function.name
                if tc.function and tc.function.arguments:
                    acc["arguments"] += tc.function.arguments

        text = "".join(content_parts) or None
        tool_calls = [
            ToolCallSpec(
                id=acc["id"], name=acc["name"],
                args=json.loads(acc["arguments"]) if acc["arguments"] else {},
            )
            for _, acc in sorted(tool_call_acc.items())
        ]
        raw_message = {
            "role": "assistant",
            "content": text,
            "tool_calls": [
                {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": json.dumps(tc.args)}}
                for tc in tool_calls
            ] or None,
        }
        logger.debug(
            "openrouter streaming response text_len={} tool_calls={}", len(text or ""), len(tool_calls)
        )
        return ModelResponse(text=text, tool_calls=tool_calls, raw_message=raw_message)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/types/test_messages.py tests/plugins/backends/test_openrouter.py -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add src/conic/types/messages.py src/conic/plugins/meta.py src/conic/plugins/backends/openrouter.py tests/types/test_messages.py tests/plugins/backends/test_openrouter.py
git commit -m "feat: add MessageUpdate/MessageDeltaUpdate events and streaming completions"
```

---

### Task 2: `ReactLoopPlugin` — status transitions + opt in to streaming

**Files:**
- Modify: `src/conic/plugins/loops/react_loop.py`
- Test: `tests/plugins/loops/test_react_loop.py`

**Interfaces:**
- Consumes: `meta.MessageUpdateEvent`, `messages.MessageUpdate` (from Task 1); `messages.ModelRequest.stream_updates` (from Task 1).
- Produces: `ReactLoopPlugin._format_tool_status(call: ToolCallSpec) -> str` (private, but referenced by its test via the emitted `MessageUpdate.text`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/plugins/loops/test_react_loop.py`. Extend the existing `conic.types.messages` import to include `MessageUpdate`:
```python
from conic.types.messages import (
    AssistantMessage, Error, MessageUpdate, ModelRequest, ModelResponse, StepEnd, StepStart,
    ToolCall, ToolCallResult, ToolCallSpec, ToolExecutionEnd, ToolExecutionStart,
    TurnEnd, UserInput,
)
```

Add these tests (e.g. after `test_tool_execution_end_carries_raw_result_before_tool_result_mutation`):

```python
async def test_message_update_cycles_through_thinking_and_tool_status():
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

    bus.on("message_update", on_update)

    await bus.emit("user_input", UserInput(text="run ls"))

    assert updates[0] == "🤔 思考中…"
    assert updates[1] == "🔧 bash(command='ls')"
    assert updates[2] == "🤔 思考中…"


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

    await bus.emit("user_input", UserInput(text="hello"))

    assert captured == [True]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/plugins/loops/test_react_loop.py -v`
Expected: FAIL — `ImportError: cannot import name 'MessageUpdate'` (until Task 1 is merged; if Task 1 already landed, expect `AssertionError` since the emits don't exist yet).

- [ ] **Step 3: Wire the emits into `ReactLoopPlugin`**

In `src/conic/plugins/loops/react_loop.py`, change the import block:
```python
from conic.types.messages import (
    AssistantMessage, BeforeModelCall, Error, MessageUpdate, ModelRequest, ModelResponse,
    StepEnd, StepStart, ToolCall, ToolCallResult, ToolCallSpec, ToolExecutionEnd,
    ToolExecutionStart, TurnEnd, TurnStart, UserInput,
)
```

Change:
```python
                this_step = step_index
                await bus.emit(meta.StepStartEvent, StepStart(step_index=this_step))
                step_index += 1
```
to:
```python
                this_step = step_index
                await bus.emit(meta.StepStartEvent, StepStart(step_index=this_step))
                await bus.emit(meta.MessageUpdateEvent, MessageUpdate(text="🤔 思考中…"))
                step_index += 1
```

Change:
```python
                response: ModelResponse = await bus.request(
                    meta.ModelRequestEvent, ModelRequest(messages=ctx.messages, tools=ctx.tools)
                )
```
to:
```python
                response: ModelResponse = await bus.request(
                    meta.ModelRequestEvent,
                    ModelRequest(messages=ctx.messages, tools=ctx.tools, stream_updates=True),
                )
```

Change:
```python
                        await bus.emit(meta.ToolExecutionStartEvent, ToolExecutionStart(call=call_ctx.call))
                        result: ToolCallResult = await bus.request(meta.ToolCallRequestEvent, payload)
```
to:
```python
                        await bus.emit(meta.ToolExecutionStartEvent, ToolExecutionStart(call=call_ctx.call))
                        await bus.emit(
                            meta.MessageUpdateEvent, MessageUpdate(text=self._format_tool_status(call_ctx.call))
                        )
                        result: ToolCallResult = await bus.request(meta.ToolCallRequestEvent, payload)
```

Add this method to the `ReactLoopPlugin` class (e.g. right after `handle_user_input`, at the same indent level as `register`):
```python
    def _format_tool_status(self, call: ToolCallSpec) -> str:
        args = ", ".join(f"{k}={v!r}" for k, v in call.args.items())
        return f"🔧 {call.name}({args})"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/plugins/loops/test_react_loop.py -v`
Expected: all PASS (all pre-existing tests in this file too).

- [ ] **Step 5: Commit**

```bash
git add src/conic/plugins/loops/react_loop.py tests/plugins/loops/test_react_loop.py
git commit -m "feat: emit MessageUpdate status transitions from ReactLoopPlugin, stream model calls"
```

---

### Task 3: `DiscordThreadPlugin` — responsive placeholder rendering

**Files:**
- Modify: `src/conic/plugins/channels/discord.py`
- Test: `tests/plugins/channels/test_discord.py`

**Interfaces:**
- Consumes: `meta.MessageUpdateEvent`/`meta.MessageDeltaUpdateEvent`, `messages.MessageUpdate`/`messages.MessageDeltaUpdate` (from Task 1).
- Produces: nothing new consumed by later tasks (this is the last task).

- [ ] **Step 1: Write the failing tests**

Replace the top of `tests/plugins/channels/test_discord.py` (imports and `FakeThread`) with:
```python
import asyncio

import pytest

from conic.core.bus import MessageBus
from conic.types.messages import (
    AssistantMessage, Error, MessageDeltaUpdate, MessageUpdate, StepStart, TurnStart,
)
from conic.plugins import meta
from conic.plugins.channels.discord import DiscordThreadPlugin


class FakeMessage:
    def __init__(self, content: str):
        self.content = content
        self.edits: list[str] = []

    async def edit(self, content: str) -> None:
        self.content = content
        self.edits.append(content)


class FakeThread:
    def __init__(self):
        self.sent: list[str] = []
        self.messages: list[FakeMessage] = []

    async def send(self, text: str) -> FakeMessage:
        self.sent.append(text)
        message = FakeMessage(text)
        self.messages.append(message)
        return message

    def typing(self):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _ctx():
            yield

        return _ctx()


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds
```

(Keep every existing test below this point — `test_forwards_assistant_message_to_thread`, `test_forwards_error_to_thread_with_marker`, `test_step_start_restarts_typing_after_it_has_timed_out`, `test_chunks_messages_longer_than_discord_limit` — unchanged. None of them ever emit `TurnStartEvent` before checking `thread.sent` for the finalize-path assertions [`test_step_start_restarts_typing_after_it_has_timed_out` does emit `TurnStartEvent` but only asserts on `plugin._typing_task`], so they keep passing against the new "no placeholder yet" fallback path unmodified.)

Add these new tests (anywhere after the fakes, e.g. before `test_chunks_messages_longer_than_discord_limit`):

```python
async def test_turn_start_sends_a_placeholder_message():
    thread = FakeThread()
    plugin = DiscordThreadPlugin(thread)
    bus = MessageBus()
    plugin.register(bus)

    await bus.emit(meta.TurnStartEvent, TurnStart())

    assert thread.sent == ["🤔 思考中…"]


async def test_message_delta_update_appends_to_the_placeholder_and_edits_immediately():
    thread = FakeThread()
    clock = FakeClock()
    plugin = DiscordThreadPlugin(thread, clock=clock)
    bus = MessageBus()
    plugin.register(bus)

    await bus.emit(meta.TurnStartEvent, TurnStart())
    placeholder = thread.messages[0]

    await bus.emit(meta.MessageDeltaUpdateEvent, MessageDeltaUpdate(text_delta="Hel"))
    clock.advance(2.0)
    await bus.emit(meta.MessageDeltaUpdateEvent, MessageDeltaUpdate(text_delta="lo"))

    assert placeholder.edits == ["Hel", "Hello"]


async def test_message_delta_updates_within_the_throttle_window_are_coalesced():
    thread = FakeThread()
    clock = FakeClock()
    plugin = DiscordThreadPlugin(thread, clock=clock)
    bus = MessageBus()
    plugin.register(bus)

    await bus.emit(meta.TurnStartEvent, TurnStart())
    placeholder = thread.messages[0]

    await bus.emit(meta.MessageDeltaUpdateEvent, MessageDeltaUpdate(text_delta="Hel"))
    clock.advance(0.1)
    await bus.emit(meta.MessageDeltaUpdateEvent, MessageDeltaUpdate(text_delta="lo"))

    assert placeholder.edits == ["Hel"]

    clock.advance(1.0)
    await bus.emit(meta.MessageDeltaUpdateEvent, MessageDeltaUpdate(text_delta="!"))

    assert placeholder.edits == ["Hel", "Hello!"]


async def test_message_update_edits_immediately_ignoring_the_throttle():
    thread = FakeThread()
    clock = FakeClock()
    plugin = DiscordThreadPlugin(thread, clock=clock)
    bus = MessageBus()
    plugin.register(bus)

    await bus.emit(meta.TurnStartEvent, TurnStart())
    placeholder = thread.messages[0]

    await bus.emit(meta.MessageDeltaUpdateEvent, MessageDeltaUpdate(text_delta="Hel"))
    await bus.emit(meta.MessageUpdateEvent, MessageUpdate(text="🔧 bash(command='ls')"))

    assert placeholder.edits == ["Hel", "🔧 bash(command='ls')"]


async def test_message_update_then_delta_clears_prior_text_instead_of_appending():
    thread = FakeThread()
    clock = FakeClock()
    plugin = DiscordThreadPlugin(thread, clock=clock)
    bus = MessageBus()
    plugin.register(bus)

    await bus.emit(meta.TurnStartEvent, TurnStart())
    placeholder = thread.messages[0]

    await bus.emit(meta.MessageUpdateEvent, MessageUpdate(text="🔧 bash(command='ls')"))
    await bus.emit(meta.MessageDeltaUpdateEvent, MessageDeltaUpdate(text_delta="Final answer"))

    assert placeholder.edits[-1] == "Final answer"


async def test_assistant_message_finalizes_by_editing_the_placeholder():
    thread = FakeThread()
    plugin = DiscordThreadPlugin(thread)
    bus = MessageBus()
    plugin.register(bus)

    await bus.emit(meta.TurnStartEvent, TurnStart())
    placeholder = thread.messages[0]

    await bus.emit(meta.AssistantMessageEvent, AssistantMessage(text="the final answer"))

    assert placeholder.edits[-1] == "the final answer"
    assert len(thread.sent) == 1


async def test_error_finalizes_by_editing_the_placeholder():
    thread = FakeThread()
    plugin = DiscordThreadPlugin(thread)
    bus = MessageBus()
    plugin.register(bus)

    await bus.emit(meta.TurnStartEvent, TurnStart())
    placeholder = thread.messages[0]

    await bus.emit(meta.ErrorEvent, Error(exc=ValueError("boom")))

    assert "boom" in placeholder.edits[-1]
    assert len(thread.sent) == 1


async def test_finalize_edits_first_chunk_and_sends_the_overflow():
    thread = FakeThread()
    plugin = DiscordThreadPlugin(thread)
    bus = MessageBus()
    plugin.register(bus)

    await bus.emit(meta.TurnStartEvent, TurnStart())
    long_text = "x" * 2500

    await bus.emit(meta.AssistantMessageEvent, AssistantMessage(text=long_text))

    assert thread.sent[0] == "🤔 思考中…"
    assert thread.messages[0].edits[-1] == "x" * 2000
    assert thread.sent[1] == "x" * 500


async def test_streaming_preview_truncates_to_the_last_2000_chars():
    thread = FakeThread()
    clock = FakeClock()
    plugin = DiscordThreadPlugin(thread, clock=clock)
    bus = MessageBus()
    plugin.register(bus)

    await bus.emit(meta.TurnStartEvent, TurnStart())
    placeholder = thread.messages[0]

    for i in range(3):
        clock.advance(2.0)
        await bus.emit(meta.MessageDeltaUpdateEvent, MessageDeltaUpdate(text_delta="a" * 1000))

    last_edit = placeholder.edits[-1]
    assert len(last_edit) == 2000
    assert last_edit.startswith("…")
    assert last_edit.endswith("a" * 1999)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/plugins/channels/test_discord.py -v`
Expected: FAIL — `TypeError: DiscordThreadPlugin.__init__() got an unexpected keyword argument 'clock'` (and `AttributeError`/`AssertionError` for the new behaviors).

- [ ] **Step 3: Rewrite `DiscordThreadPlugin`**

Replace the full contents of `src/conic/plugins/channels/discord.py`:
```python
import asyncio
import time
from typing import Callable

from conic.types.messages import (
    AssistantMessage, Error, MessageDeltaUpdate, MessageUpdate, StepStart, TurnStart, TurnEnd,
)
from conic.plugins import meta

DISCORD_MESSAGE_LIMIT = 2000
TYPING_INTERVAL = 8
TYPING_TIMEOUT = 20
STREAM_EDIT_INTERVAL = 1.0
THINKING_TEXT = "🤔 思考中…"


class DiscordThreadPlugin:
    def __init__(self, thread, clock: Callable[[], float] = time.monotonic):
        self._thread = thread
        self._clock = clock
        self._typing_task: asyncio.Task | None = None
        self._stopped = False
        self._status_message = None
        self._buffer = ""
        self._awaiting_first_delta = False
        self._last_edit_time = float("-inf")

    def register(self, bus) -> None:
        bus.on(meta.SessionStopEvent, self.on_session_stop)
        bus.on(meta.TurnStartEvent, self.on_turn_start)
        bus.on(meta.StepStartEvent, self.on_step_start)
        bus.on(meta.MessageUpdateEvent, self.on_message_update)
        bus.on(meta.MessageDeltaUpdateEvent, self.on_message_delta_update)
        bus.on(meta.TurnEndEvent, self.on_turn_end)
        bus.on(meta.ErrorEvent, self.on_error)
        bus.on(meta.AssistantMessageEvent, self.on_assistant_message)

    async def on_session_stop(self, _msg: TurnEnd) -> None:
        self._stopped = True
        self._stop_typing()

    async def on_turn_start(self, _msg: TurnStart) -> None:
        self._ensure_typing()
        self._buffer = THINKING_TEXT
        self._awaiting_first_delta = True
        self._last_edit_time = float("-inf")
        self._status_message = await self._thread.send(self._buffer)

    async def on_step_start(self, _msg: StepStart) -> None:
        self._ensure_typing()

    def _ensure_typing(self) -> None:
        if self._typing_task is not None and not self._typing_task.done():
            return
        self._typing_task = asyncio.create_task(self._keep_typing())

    async def on_message_update(self, msg: MessageUpdate) -> None:
        self._buffer = msg.text
        self._awaiting_first_delta = True
        await self._apply_edit(force=True)

    async def on_message_delta_update(self, msg: MessageDeltaUpdate) -> None:
        force = self._awaiting_first_delta
        if self._awaiting_first_delta:
            self._buffer = ""
            self._awaiting_first_delta = False
        self._buffer += msg.text_delta
        await self._apply_edit(force=force)

    async def _apply_edit(self, force: bool) -> None:
        if self._status_message is None or self._stopped:
            return
        now = self._clock()
        if not force and (now - self._last_edit_time) < STREAM_EDIT_INTERVAL:
            return
        self._last_edit_time = now
        content = self._buffer
        if len(content) > DISCORD_MESSAGE_LIMIT:
            content = "…" + content[-(DISCORD_MESSAGE_LIMIT - 1):]
        await self._status_message.edit(content=content)

    async def on_turn_end(self, _msg: TurnEnd) -> None:
        self._stop_typing()

    async def on_error(self, msg: Error) -> None:
        self._stop_typing()
        await self._finalize(f"⚠️ {msg.exc}")

    async def on_assistant_message(self, msg: AssistantMessage) -> None:
        await self._finalize(msg.text)

    async def _finalize(self, text: str) -> None:
        if self._stopped:
            return
        if self._status_message is not None:
            await self._status_message.edit(content=text[:DISCORD_MESSAGE_LIMIT])
            await self._send(text[DISCORD_MESSAGE_LIMIT:])
        else:
            await self._send(text)
        self._status_message = None

    def _stop_typing(self) -> None:
        if self._typing_task is not None and not self._typing_task.done():
            self._typing_task.cancel()
            self._typing_task = None

    async def _keep_typing(self) -> None:
        try:
            async with asyncio.timeout(TYPING_TIMEOUT):
                while True:
                    async with self._thread.typing():
                        await asyncio.sleep(TYPING_INTERVAL)
        except (asyncio.CancelledError, TimeoutError):
            pass

    async def _send(self, text: str) -> None:
        if self._stopped:
            return
        for i in range(0, len(text), DISCORD_MESSAGE_LIMIT):
            await self._thread.send(text[i: i + DISCORD_MESSAGE_LIMIT])
```

Two throttle details worth calling out explicitly, since they're easy to get
wrong and the tests in Step 1 depend on them:

1. `_last_edit_time` is initialized to `float("-inf")` (both in `__init__`
   and again in `on_turn_start`) rather than `0.0`. With a real
   `time.monotonic()` clock this makes little difference, but with the
   `FakeClock` the tests use (which starts at `0.0`), initializing to `0.0`
   would make the very first edit of a turn look like it "just happened at
   time zero" and get incorrectly throttled by a same-instant follow-up
   call. `-inf` guarantees the first edit after any reset is never
   throttled, regardless of what the clock reads.
2. `on_message_delta_update` passes `force=self._awaiting_first_delta`
   (captured *before* clearing the flag) to `_apply_edit`, not a hardcoded
   `force=False`. The first delta after any reset (`TurnStartEvent` or
   `MessageUpdateEvent`) always edits immediately — it's the visually
   important transition from a status line to real content — while
   subsequent deltas within that same streaming run are throttled normally.
   Without this, a `MessageUpdateEvent` immediately followed by a delta (as
   in the "final answer starts, clear the tool-status text" case) could
   have its delta silently swallowed by the throttle window if both happen
   to land in the same clock tick.

Note on `.edit(content=...)`'s signature in `FakeMessage` above: it's declared as `async def edit(self, content: str) -> None`, matching a keyword call `edit(content=...)` — discord.py's real `Message.edit` accepts `content` as a keyword (or positional) argument, so this fake is call-compatible.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/plugins/channels/test_discord.py -v`
Expected: all PASS.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: all PASS, no regressions across the whole project (133 tests before this plan, plus the new ones from all 3 tasks).

- [ ] **Step 6: Commit**

```bash
git add src/conic/plugins/channels/discord.py tests/plugins/channels/test_discord.py
git commit -m "feat: render MessageUpdate/MessageDeltaUpdate as a live-editing Discord placeholder"
```
