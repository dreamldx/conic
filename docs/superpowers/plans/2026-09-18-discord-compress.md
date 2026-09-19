# Context Compression: `/compress` Command + Turn-Count Auto Trigger

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unify context compression behind one persistent, user-visible mechanism: a `/compress` Discord slash command compresses on demand, and an automatic trigger compresses every N turns (N from settings). Both paths post a `SteeringCompressCommand` into a new `steering.compress` mailbox; `ReactLoopPlugin` executes compression before starting a turn (never mid-turn), persists the result, and every compression — manual or auto — posts a separator into the Discord thread. Subsequent model calls start from the compressed context only.

**Architecture:** A third mailbox `steering.compress` (typed `SteeringCompressCommand`) is created alongside `steering.high`/`steering.low` in `ReactLoopPlugin.register`. `run_loop` waits on all three and drains `steering.compress` first each wake: after the abort check, it runs `_compress()` — `load_history` → `SummarizeEvent` (existing `SummarizerPlugin`) → persist via a new append-only *compress marker* row (`load_history` only reads rows after the last marker) → emit new `CompressDoneEvent`. `DiscordThreadPlugin` hooks `CompressDoneEvent` and posts the separator. The manual path is the Discord gateway posting to the mailbox; the auto path is a new `TurnCompressPlugin` that counts turns since the last successful compression and posts to the same mailbox when the configured threshold is reached. The old token-budget path (`TokenBudgetPlugin`, transient per-model-call summarization) is removed along with its now-dead events and config.

**Tech Stack:** Python 3.12+, discord.py app commands, DuckDB, pydantic-settings, pytest.

## Key design decisions

1. **Dedicated `steering.compress` mailbox → mid-turn safety for free.** Compression rewrites persisted history; running it mid-turn could orphan a `tool_calls`/`tool_result` pairing (marker lands between the assistant tool_calls row and its tool result → next `load_history` returns a dangling `tool_result` → API error). Because `run_loop` only drains `steering.compress` at the top of its idle loop, a command posted mid-turn just sits in the mailbox (`wait_multiply_mailbox` sees the event immediately after the turn) and runs before the next turn. No checkpoint changes inside `_run_turn` at all.
2. **Persistence = append-only marker, not deletion.** `messages` is append-only with `PRIMARY KEY (session_key, seq)` and no delete query anywhere. Compression appends a `role = "compress_marker"` row followed by the summarized messages; `load_history_sql` filters to `seq >` the last marker's seq. Old rows stay in DuckDB for audit; "context starts from the compressed context" is a read-side property.
3. **Auto trigger = turns since last compression, not raw `turn_count`.** `TurnCompressPlugin` keeps an in-memory counter: +1 on `TurnEndEvent`, reset to 0 on `CompressDoneEvent`. This means a manual `/compress` also resets the auto clock, and a *failed* compression (no `CompressDoneEvent`) leaves the counter high so it retries after the next turn. Counter is not persisted across restarts — worst case a compression is delayed by up to N turns after a resume, which is acceptable.
4. **Both paths converge on `_compress()` in the loop.** `SteeringCompressCommand.reason` (`"manual"` | `"auto"`) is carried through to `CompressDone.reason` so the separator can say which trigger fired and logs stay diagnosable. Multiple queued commands in one drain (e.g. auto + manual racing) collapse into a single `_compress` call.
5. **Reuse `SummarizeEvent`/`SummarizerPlugin`, drop the veto hook.** `_compress` requests `SummarizeEvent` directly (wrapped in `asyncio.wait_for(..., self._model_timeout)`). `BeforeSummarizeEvent` existed to let plugins veto *automatic budget-triggered* summarization inside the model-call chain; with that path gone and compression now an explicit user/threshold action, the hook, `SummarizeDoneEvent`/`SummarizeFailedEvent`, and `SummarizeRequest.budget_tokens` are dead and removed. `CompressDoneEvent`/`ErrorEvent` are their replacements.
6. **`TokenBudgetPlugin` and `CONTEXT_TOKEN_BUDGET` are removed, replaced by `COMPRESS_TURN_THRESHOLD`.** Default 10; `0` disables auto compression (manual `/compress` still works). `core/tokencount.py` (`estimate_tokens`) becomes unused by `src/` and is removed with its test — git history keeps it if token-based heuristics ever return. `TruncatorPlugin` (per-call tail truncation) is orthogonal and stays.
7. **Failure behavior.** On any exception `_compress` emits `ErrorEvent` (thread shows `⚠️ ...`), persists nothing, and the loop keeps running.
8. **Batch-abort semantics preserved.** A `SteeringStopCommand` in the drained `steering.high` batch finalizes the session and discards any pending compress commands, consistent with `test_run_loop_drops_high_batch_that_contains_both_message_and_stop`.
9. **Edge case: short history.** If there is nothing to summarize (`len(rest) <= keep_recent`), `SummarizerPlugin` returns the messages unchanged; compress still writes the marker + re-appends them (semantically a no-op) and the separator is still posted — the user always gets feedback.

## Global Constraints

- Run `uv run pytest` after every change; do not commit if tests fail (AGENTS.md).
- No comments in new source code (AGENTS.md). Exception: `src/conic/plugins/meta.py` is a documented topic registry whose established format is one comment block per topic — follow the file's own convention there.
- Bus topic names in `src/` must use constants from `src/conic/plugins/meta.py` (AGENTS.md). Tests use literal strings, matching existing test style.
- SQL belongs in `src/conic/services/queries.py`, not inline (AGENTS.md).
- Discord user-facing strings (separator, command feedback) are Chinese, matching `THINKING_TEXTS` style in `plugins/channels/discord.py`.

## File Structure

- `src/conic/types/steering.py` — `SteeringCompressCommand(reason: str = "manual")`
- `src/conic/types/messages.py` — `CompressDone`; remove `BeforeSummarize`/`SummarizeDone`/`SummarizeFailed`; `SummarizeRequest` loses `budget_tokens`
- `src/conic/plugins/meta.py` — `CompressDoneEvent = "compress_done"`; remove `BeforeSummarizeEvent`/`SummarizeDoneEvent`/`SummarizeFailedEvent`
- `src/conic/services/queries.py` — `COMPRESS_MARKER_ROLE`, marker-aware `load_history_sql`
- `src/conic/services/storage.py` — `SessionHandle.compress_history`
- `src/conic/plugins/loops/react_loop.py` — `steering.compress` mailbox, compress drain in `run_loop`, `_compress`
- `src/conic/plugins/context/turn_compress.py` — new `TurnCompressPlugin` (auto trigger)
- `src/conic/config.py` — `compress_turn_threshold` replaces `context_token_budget`
- `src/conic/plugins/registry.py` — wire `TurnCompressPlugin`, unwire `TokenBudgetPlugin`
- `src/conic/discord/gateway.py` — `/compress` slash command + `handle_compress_command`
- `src/conic/plugins/channels/discord.py` — `COMPRESS_SEPARATOR`, `on_compress_done`
- Deleted: `src/conic/plugins/context/token_budget.py`, `src/conic/core/tokencount.py`, `tests/plugins/context/test_token_budget.py`, `tests/core/test_tokencount.py`

---

### Task 1: `SteeringCompressCommand` steering type

**Files:**
- Modify: `src/conic/types/steering.py`
- Test: `tests/types/test_steering.py`

**Interfaces:**
- Produces: `SteeringCompressCommand(reason: str = "manual")` — source `"system"`, `to_history_entries() == []`, `is_turn_abort() is False`.

- [ ] **Step 1: Write the failing tests** — append to `tests/types/test_steering.py` (add `SteeringCompressCommand` to the import):

```python
def test_steering_compress_command_has_system_source():
    assert SteeringCompressCommand().source == "system"


def test_steering_compress_command_defaults_to_manual_reason():
    assert SteeringCompressCommand().reason == "manual"


def test_steering_compress_command_accepts_auto_reason():
    assert SteeringCompressCommand(reason="auto").reason == "auto"


def test_steering_compress_command_is_not_turn_abort():
    assert SteeringCompressCommand().is_turn_abort() is False


def test_steering_compress_command_to_history_entries_is_empty():
    assert SteeringCompressCommand().to_history_entries() == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/types/test_steering.py -v -k compress`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement** — at the end of `src/conic/types/steering.py`:

```python
@dataclass
class SteeringCompressCommand(SteeringItem):
    reason: str = "manual"

    source: ClassVar[str] = "system"

    def to_history_entries(self) -> list[dict]:
        return []
```

- [ ] **Step 4: Run tests to verify they pass** — `uv run pytest tests/types/ -v`

- [ ] **Step 5: Commit**

```bash
git add src/conic/types/steering.py tests/types/test_steering.py
git commit -m "feat: SteeringCompressCommand steering item for context compression"
```

---

### Task 2: compress-marker persistence in storage

**Files:**
- Modify: `src/conic/services/queries.py`
- Modify: `src/conic/services/storage.py`
- Test: `tests/services/test_storage.py`

**Interfaces:**
- Produces: `queries.COMPRESS_MARKER_ROLE = "compress_marker"`; `load_history_sql` returns only rows with `seq` greater than the last marker's; `SessionHandle.compress_history(messages: list[dict]) -> None` appends a marker row then each message.

- [ ] **Step 1: Write the failing tests** — append to `tests/services/test_storage.py`:

```python
def test_compress_history_hides_messages_before_the_marker(tmp_path):
    storage = make_storage(tmp_path)
    row = storage.get_or_create(channel="discord", native_id="123")
    handle = storage.handle_for(row)
    handle.append_message({"role": "user", "content": "old question"})
    handle.append_message({"role": "assistant", "content": "old answer"})

    handle.compress_history([
        {"role": "system", "content": "[Earlier conversation summary]\nstuff happened"},
        {"role": "user", "content": "recent"},
    ])

    assert handle.load_history() == [
        {"role": "system", "content": "[Earlier conversation summary]\nstuff happened"},
        {"role": "user", "content": "recent"},
    ]
    storage.shutdown()


def test_messages_appended_after_compress_are_included(tmp_path):
    storage = make_storage(tmp_path)
    row = storage.get_or_create(channel="discord", native_id="123")
    handle = storage.handle_for(row)
    handle.append_message({"role": "user", "content": "old"})
    handle.compress_history([{"role": "system", "content": "summary"}])
    handle.append_message({"role": "user", "content": "new question"})

    assert handle.load_history() == [
        {"role": "system", "content": "summary"},
        {"role": "user", "content": "new question"},
    ]
    storage.shutdown()


def test_second_compress_supersedes_the_first(tmp_path):
    storage = make_storage(tmp_path)
    row = storage.get_or_create(channel="discord", native_id="123")
    handle = storage.handle_for(row)
    handle.append_message({"role": "user", "content": "old"})
    handle.compress_history([{"role": "system", "content": "first summary"}])
    handle.append_message({"role": "user", "content": "middle"})
    handle.compress_history([{"role": "system", "content": "second summary"}])

    assert handle.load_history() == [{"role": "system", "content": "second summary"}]
    storage.shutdown()


def test_compressed_history_persists_across_reconnect(tmp_path):
    storage = make_storage(tmp_path)
    row = storage.get_or_create(channel="discord", native_id="123")
    handle = storage.handle_for(row)
    handle.append_message({"role": "user", "content": "old"})
    handle.compress_history([{"role": "system", "content": "summary"}])
    storage.shutdown()

    reopened = StorageService(
        db_path=str(tmp_path / "conic.duckdb"),
        workspace_root=str(tmp_path / "workspace"),
        default_model="test-model",
    )
    reopened.startup()
    reloaded_row = reopened.get_or_create(channel="discord", native_id="123")
    assert reopened.handle_for(reloaded_row).load_history() == [{"role": "system", "content": "summary"}]
    reopened.shutdown()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/services/test_storage.py -v -k compress`
Expected: FAIL with `AttributeError: 'SessionHandle' object has no attribute 'compress_history'`

- [ ] **Step 3: Implement** — in `src/conic/services/queries.py`, add near the top:

```python
COMPRESS_MARKER_ROLE = "compress_marker"
```

and replace `load_history_sql`:

```python
def load_history_sql(session_key: str) -> tuple[str, list]:
    return (
        "SELECT content FROM messages WHERE session_key = ? AND seq > COALESCE("
        "(SELECT MAX(seq) FROM messages WHERE session_key = ? AND role = ?), -1) ORDER BY seq",
        [session_key, session_key, COMPRESS_MARKER_ROLE],
    )
```

In `src/conic/services/storage.py`, add to `SessionHandle`:

```python
    def compress_history(self, messages: list[dict]) -> None:
        self.append_message({"role": queries.COMPRESS_MARKER_ROLE})
        for message in messages:
            self.append_message(message)
```

- [ ] **Step 4: Run tests to verify they pass** — `uv run pytest tests/services/ -v` (the pre-existing round-trip tests must still pass: no marker → full history).

- [ ] **Step 5: Commit**

```bash
git add src/conic/services/queries.py src/conic/services/storage.py tests/services/test_storage.py
git commit -m "feat: append-only compress marker so load_history starts at the last compression"
```

---

### Task 3: `steering.compress` mailbox and compression execution in `ReactLoopPlugin`

**Files:**
- Modify: `src/conic/types/messages.py` (add `CompressDone`)
- Modify: `src/conic/plugins/meta.py` (add `CompressDoneEvent`)
- Modify: `src/conic/plugins/loops/react_loop.py`
- Test: `tests/plugins/loops/test_react_loop.py`

**Interfaces:**
- Produces: `CompressDone(messages_before: int, messages_after: int, reason: str)`; `meta.CompressDoneEvent = "compress_done"`; `ReactLoopPlugin.register` creates the `steering.compress` mailbox (payload type `SteeringCompressCommand`); `run_loop` waits on all three mailboxes and drains `steering.compress` each wake — after the abort check, before any turn — collapsing the batch into one `_compress(reason)` call; `_compress` = `load_history` → `SummarizeEvent` (with `model_timeout`) → `compress_history` → `CompressDoneEvent`, or `ErrorEvent` on failure.

- [ ] **Step 1: Write the failing tests** — in `tests/plugins/loops/test_react_loop.py`:

Extend the imports:

```python
from conic.types.messages import (
    AssistantMessage, BeforeModelCall, CompressDone, Error, MessageUpdate, ModelRequest,
    ModelResponse, SessionEnd, StepEnd, StepStart, SummarizeRequest, SummarizeResult, ToolCall,
    ToolCallResult, ToolCallSpec, ToolExecutionEnd, ToolExecutionStart, TurnEnd, TurnStart,
)
from conic.types.steering import (
    SteeringBackgroundResult, SteeringCompressCommand, SteeringStopCommand, SteeringUserMessage,
)
```

Make `FakeStorageHandle` marker-aware (mirrors the real `SessionHandle` read semantics; existing tests never compress, so `idx = -1` keeps them unchanged):

```python
class FakeStorageHandle:
    def __init__(self):
        self.messages: list[dict] = []
        self.saved_variables: list[dict] = []

    def append_message(self, message: dict) -> None:
        self.messages.append(message)

    def compress_history(self, messages: list[dict]) -> None:
        self.messages.append({"role": "compress_marker"})
        self.messages.extend(messages)

    def load_history(self) -> list[dict]:
        idx = -1
        for i, m in enumerate(self.messages):
            if m.get("role") == "compress_marker":
                idx = i
        return list(self.messages[idx + 1:])

    def set_status(self, status: str) -> None:
        pass

    def save_variables(self, variables: dict) -> None:
        self.saved_variables.append(dict(variables))
```

Add a fake summarizer helper next to `make_loop`:

```python
def add_fake_summarizer(bus, summary_text="summary"):
    calls = []

    async def fake_summarize(req: SummarizeRequest) -> SummarizeResult:
        calls.append(req)
        return SummarizeResult(messages=[{"role": "system", "content": summary_text}])

    bus.on_request("summarize", fake_summarize)
    return calls
```

New tests (append under a `# --- compression via steering.compress ---` divider):

```python
async def test_run_loop_compress_persists_summary_and_emits_compress_done():
    handle = FakeStorageHandle()
    handle.messages = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
    ]
    bus, loop = make_loop(handle, [])
    calls = add_fake_summarizer(bus)

    done = []

    async def on_done(msg: CompressDone) -> None:
        done.append(msg)
        await bus.post("steering.high", SteeringStopCommand())

    bus.on_chain("compress_done", on_done)

    await bus.post("steering.compress", SteeringCompressCommand())
    await asyncio.wait_for(loop.run_loop(), timeout=1.0)

    assert len(calls) == 1
    assert calls[0].messages == [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
    ]
    assert done[0].messages_before == 2
    assert done[0].messages_after == 1
    assert done[0].reason == "manual"
    assert handle.load_history() == [{"role": "system", "content": "summary"}]


async def test_run_loop_compress_alone_does_not_start_a_turn():
    handle = FakeStorageHandle()
    bus, loop = make_loop(handle, [])
    add_fake_summarizer(bus)

    turn_starts = []

    async def on_turn_start(msg: TurnStart) -> None:
        turn_starts.append(msg)

    async def stop_after_done(msg: CompressDone) -> None:
        await bus.post("steering.high", SteeringStopCommand())

    bus.on_chain("turn_start", on_turn_start)
    bus.on_chain("compress_done", stop_after_done)

    await bus.post("steering.compress", SteeringCompressCommand())
    await asyncio.wait_for(loop.run_loop(), timeout=1.0)

    assert turn_starts == []


async def test_run_loop_compress_runs_before_a_pending_turn():
    handle = FakeStorageHandle()
    handle.messages = [{"role": "user", "content": "old"}]
    responses = [ModelResponse(text="hi", tool_calls=[], raw_message={})]
    bus, loop = make_loop(handle, responses)
    add_fake_summarizer(bus)

    seen = []

    async def observe(msg: BeforeModelCall) -> None:
        seen.append(list(msg.messages))

    async def stop_after_turn(msg: TurnEnd) -> TurnEnd:
        await bus.post("steering.high", SteeringStopCommand())
        return msg

    bus.on_chain("before_model_call", observe)
    bus.on_chain("turn_end", stop_after_turn)

    await bus.post("steering.compress", SteeringCompressCommand())
    await bus.post("steering.high", SteeringUserMessage("hello"))
    await asyncio.wait_for(loop.run_loop(), timeout=1.0)

    assert seen[0] == [
        {"role": "system", "content": "summary"},
        {"role": "user", "content": "hello"},
    ]


async def test_compress_posted_mid_turn_waits_until_the_turn_ends():
    handle = FakeStorageHandle()
    responses = [ModelResponse(text="final answer", tool_calls=[], raw_message={})]
    bus, loop = make_loop(handle, responses)
    calls = add_fake_summarizer(bus)

    async def inject_compress(msg: ModelResponse) -> ModelResponse:
        await bus.post("steering.compress", SteeringCompressCommand())
        return msg

    async def stop_after_done(msg: CompressDone) -> None:
        await bus.post("steering.high", SteeringStopCommand())

    bus.on_chain("model_response", inject_compress)
    bus.on_chain("compress_done", stop_after_done)

    await bus.post("steering.high", SteeringUserMessage("hello"))
    await asyncio.wait_for(loop.run_loop(), timeout=1.0)

    assert handle.messages == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "final answer"},
        {"role": "compress_marker"},
        {"role": "system", "content": "summary"},
    ]
    assert calls[0].messages == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "final answer"},
    ]


async def test_run_loop_collapses_a_compress_batch_into_one_compression():
    handle = FakeStorageHandle()
    bus, loop = make_loop(handle, [])
    calls = add_fake_summarizer(bus)

    async def stop_after_done(msg: CompressDone) -> None:
        await bus.post("steering.high", SteeringStopCommand())

    bus.on_chain("compress_done", stop_after_done)

    await bus.post("steering.compress", SteeringCompressCommand(reason="auto"))
    await bus.post("steering.compress", SteeringCompressCommand(reason="manual"))
    await asyncio.wait_for(loop.run_loop(), timeout=1.0)

    assert len(calls) == 1


async def test_run_loop_stop_in_the_same_wake_discards_pending_compress():
    handle = FakeStorageHandle()
    bus, loop = make_loop(handle, [])
    calls = add_fake_summarizer(bus)

    await bus.post("steering.compress", SteeringCompressCommand())
    await bus.post("steering.high", SteeringStopCommand())
    await asyncio.wait_for(loop.run_loop(), timeout=1.0)

    assert calls == []
    assert handle.messages == []


async def test_run_loop_compress_failure_emits_error_and_keeps_history():
    handle = FakeStorageHandle()
    handle.messages = [{"role": "user", "content": "old"}]
    bus, loop = make_loop(handle, [])

    async def failing_summarize(req: SummarizeRequest) -> SummarizeResult:
        raise RuntimeError("summarizer blew up")

    bus.on_request("summarize", failing_summarize)

    errors = []
    done = []

    async def on_error(msg: Error) -> None:
        errors.append(msg.exc)
        await bus.post("steering.high", SteeringStopCommand())

    async def on_done(msg: CompressDone) -> None:
        done.append(msg)

    bus.on_chain("error", on_error)
    bus.on_chain("compress_done", on_done)

    await bus.post("steering.compress", SteeringCompressCommand())
    await asyncio.wait_for(loop.run_loop(), timeout=1.0)

    assert len(errors) == 1
    assert done == []
    assert handle.messages == [{"role": "user", "content": "old"}]
```

Note on the mid-turn test: the single-response list doubles as a "no extra model call" assertion, and the deferral works because `bus.post` sets the mailbox event, so the post-turn `wait_multiply_mailbox` returns immediately. Note on the stop-discard test: the abort check runs before the compress batch is executed, so stop wins.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/plugins/loops/test_react_loop.py -v -k compress`
Expected: FAIL with `ImportError: cannot import name 'CompressDone'`

- [ ] **Step 3: Implement**

In `src/conic/types/messages.py`, after `SummarizeFailed`:

```python
@dataclass
class CompressDone:
    messages_before: int
    messages_after: int
    reason: str
```

In `src/conic/plugins/meta.py`, after the summarize topic block:

```python
# ── Plugin: ReactLoopPlugin (context compression) ──────────────────────────
# emitted after a compress command (manual /compress or TurnCompressPlugin's
# auto trigger, both posted to the steering.compress mailbox) has summarized
# the history via SummarizeEvent and persisted it behind a storage compress
# marker — channel plugins hook this to post a visible separator
CompressDoneEvent = "compress_done"
```

In `src/conic/plugins/loops/react_loop.py`:

1. Extend the `conic.types.messages` import with `CompressDone, SummarizeRequest`; extend the `conic.types.steering` import with `SteeringCompressCommand`.
2. In `register`:

```python
        bus.create_mailbox("steering.high", SteeringItem)
        bus.create_mailbox("steering.low", SteeringItem)
        bus.create_mailbox("steering.compress", SteeringCompressCommand)
```

3. Rework the top of `run_loop`:

```python
    async def run_loop(self) -> None:
        bus = self._bus
        while True:
            await bus.wait_multiply_mailbox("steering.high", "steering.low", "steering.compress")
            compress = await bus.drain("steering.compress")
            high = await bus.drain("steering.high")
            low = await bus.drain("steering.low")
            if not compress and not high and not low:
                continue
            if any(item.is_turn_abort() for item in high):
                await self._finalize_session()
                return
            if compress:
                await self._compress(compress[0].reason)
            if not high and not low:
                continue
            try:
                await self._run_turn(high, low)
            except AbortTurn as exc:
                if exc.reason.ends_session:
                    await self._finalize_session()
                    return
```

4. New method:

```python
    async def _compress(self, reason: str) -> None:
        bus = self._bus
        history = self._storage.load_history()
        logger.info("compressing context reason={} ({} messages)", reason, len(history))
        try:
            result = await asyncio.wait_for(
                bus.request(meta.SummarizeEvent, SummarizeRequest(messages=history)),
                timeout=self._model_timeout,
            )
            self._storage.compress_history(result.messages)
        except Exception as exc:
            logger.exception("context compression failed")
            await bus.chain(meta.ErrorEvent, Error(exc=exc))
            return
        await bus.chain(
            meta.CompressDoneEvent,
            CompressDone(
                messages_before=len(history), messages_after=len(result.messages), reason=reason
            ),
        )
```

`SummarizeRequest(messages=history)` relies on Task 4 dropping `budget_tokens`; until Task 4 lands, pass `budget_tokens=0` and remove it in Task 4. (If executing tasks strictly in order, use `budget_tokens=0` here and let Task 4's sweep delete it.)

- [ ] **Step 4: Run tests to verify they pass** — `uv run pytest tests/plugins/loops/ -v` (all pre-existing checkpoint/steering tests must still pass; `_run_turn` is untouched).

- [ ] **Step 5: Commit**

```bash
git add src/conic/types/messages.py src/conic/plugins/meta.py src/conic/plugins/loops/react_loop.py tests/plugins/loops/test_react_loop.py
git commit -m "feat: steering.compress mailbox executes persistent context compression before turns"
```

---

### Task 4: turn-count auto trigger, remove the token-budget path

**Files:**
- Create: `src/conic/plugins/context/turn_compress.py`
- Modify: `src/conic/config.py`, `src/conic/plugins/registry.py`, `src/conic/types/messages.py`, `src/conic/plugins/meta.py`, `src/conic/plugins/loops/react_loop.py` (drop `budget_tokens=0` if used)
- Delete: `src/conic/plugins/context/token_budget.py`, `src/conic/core/tokencount.py`
- Test: create `tests/plugins/context/test_turn_compress.py`; update `tests/test_config.py`, `tests/plugins/test_registry.py`, `tests/types/test_messages.py`, `tests/plugins/context/test_summarizer.py`, `tests/plugins/context/test_extra_prompt.py`; delete `tests/plugins/context/test_token_budget.py`, `tests/core/test_tokencount.py`

**Interfaces:**
- Produces: `Config.compress_turn_threshold: int` (default 10, ge=0, alias `COMPRESS_TURN_THRESHOLD`, `0` = auto compression disabled); `TurnCompressPlugin(turn_threshold: int)` — on `TurnEndEvent` increments an internal counter and posts `SteeringCompressCommand(reason="auto")` to `steering.compress` when `counter >= threshold`; on `CompressDoneEvent` resets the counter.
- Removes: `TokenBudgetPlugin`, `Config.context_token_budget`, `estimate_tokens`, `BeforeSummarize`/`SummarizeDone`/`SummarizeFailed` dataclasses, `BeforeSummarizeEvent`/`SummarizeDoneEvent`/`SummarizeFailedEvent` topics, `SummarizeRequest.budget_tokens`.

- [ ] **Step 1: Write the failing tests** — create `tests/plugins/context/test_turn_compress.py`:

```python
from conic.core.bus import MessageBus
from conic.types.messages import CompressDone, TurnEnd
from conic.types.steering import SteeringCompressCommand
from conic.plugins.context.turn_compress import TurnCompressPlugin


def make_bus():
    bus = MessageBus()
    bus.create_mailbox("steering.compress", SteeringCompressCommand)
    return bus


async def test_posts_auto_compress_when_turn_threshold_reached():
    bus = make_bus()
    TurnCompressPlugin(turn_threshold=2).register(bus)

    await bus.chain("turn_end", TurnEnd())
    assert await bus.drain("steering.compress") == []

    await bus.chain("turn_end", TurnEnd())
    posted = await bus.drain("steering.compress")
    assert len(posted) == 1
    assert posted[0].reason == "auto"


async def test_compress_done_resets_the_counter():
    bus = make_bus()
    TurnCompressPlugin(turn_threshold=2).register(bus)

    await bus.chain("turn_end", TurnEnd())
    await bus.chain("compress_done", CompressDone(messages_before=5, messages_after=1, reason="manual"))
    await bus.chain("turn_end", TurnEnd())

    assert await bus.drain("steering.compress") == []


async def test_keeps_posting_until_a_compression_succeeds():
    bus = make_bus()
    TurnCompressPlugin(turn_threshold=1).register(bus)

    await bus.chain("turn_end", TurnEnd())
    await bus.chain("turn_end", TurnEnd())

    posted = await bus.drain("steering.compress")
    assert len(posted) == 2


async def test_zero_threshold_disables_auto_compression():
    bus = make_bus()
    TurnCompressPlugin(turn_threshold=0).register(bus)

    for _ in range(5):
        await bus.chain("turn_end", TurnEnd())

    assert await bus.drain("steering.compress") == []
```

Update `tests/test_config.py`: replace the `context_token_budget == 50000` assertion with `compress_turn_threshold == 10`, and add an env-override case (`COMPRESS_TURN_THRESHOLD="3"` → 3) following the file's existing style.

Update `tests/plugins/test_registry.py`: replace `CONTEXT_TOKEN_BUDGET=123` with `COMPRESS_TURN_THRESHOLD=123` in the config kwargs, swap `TokenBudgetPlugin` for `TurnCompressPlugin` in the import and in the expected context-plugin class list, and assert `instances[3]._turn_threshold == 123` (adjust index to the actual wiring position).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/plugins/context/test_turn_compress.py tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'conic.plugins.context.turn_compress'`

- [ ] **Step 3: Implement**

Create `src/conic/plugins/context/turn_compress.py`:

```python
from conic.types.messages import CompressDone, TurnEnd
from conic.types.steering import SteeringCompressCommand
from conic.plugins import meta


class TurnCompressPlugin:
    def __init__(self, turn_threshold: int):
        self._turn_threshold = turn_threshold
        self._turns_since_compress = 0
        self._bus = None

    def register(self, bus) -> None:
        self._bus = bus
        bus.on_chain(meta.TurnEndEvent, self.on_turn_end)
        bus.on_chain(meta.CompressDoneEvent, self.on_compress_done)

    async def on_turn_end(self, msg: TurnEnd) -> None:
        if not self._turn_threshold:
            return
        self._turns_since_compress += 1
        if self._turns_since_compress >= self._turn_threshold:
            await self._bus.post("steering.compress", SteeringCompressCommand(reason="auto"))

    async def on_compress_done(self, msg: CompressDone) -> None:
        self._turns_since_compress = 0
```

Wait — `bus.post` topic strings: use the literal `"steering.compress"` consistently with `react_loop.py`'s existing literal mailbox names (`"steering.high"`/`"steering.low"` are literals today; mailbox names are not `meta.py` topics).

In `src/conic/config.py`, replace:

```python
    context_token_budget: int = Field(default=50000, ge=1, alias="CONTEXT_TOKEN_BUDGET")
```

with:

```python
    compress_turn_threshold: int = Field(default=10, ge=0, alias="COMPRESS_TURN_THRESHOLD")
```

In `src/conic/plugins/registry.py`: replace the `TokenBudgetPlugin` import with `TurnCompressPlugin` (from `conic.plugins.context.turn_compress`) and replace the wiring line with:

```python
            lambda ws, schemas: TurnCompressPlugin(turn_threshold=config.compress_turn_threshold),
```

Removal sweep:
- Delete `src/conic/plugins/context/token_budget.py`, `src/conic/core/tokencount.py`, `tests/plugins/context/test_token_budget.py`, `tests/core/test_tokencount.py`.
- `src/conic/types/messages.py`: delete `BeforeSummarize`, `SummarizeDone`, `SummarizeFailed`; drop `budget_tokens` from `SummarizeRequest`.
- `src/conic/plugins/meta.py`: delete `BeforeSummarizeEvent`, `SummarizeDoneEvent`, `SummarizeFailedEvent`; rewrite the section comment to describe `SummarizeEvent` as the summarization request used by compression.
- `src/conic/plugins/loops/react_loop.py`: if `_compress` passed `budget_tokens=0`, drop it.
- `tests/types/test_messages.py`: drop `budget_tokens=100` from the `SummarizeRequest` construction (and any assertions on removed dataclasses).
- `tests/plugins/context/test_summarizer.py`: drop `budget_tokens=1` from all `SummarizeRequest` constructions.
- `tests/plugins/context/test_extra_prompt.py`: the two tests exercising `TokenBudgetPlugin` interplay — delete the summarize-interplay test (its scenario no longer exists: compression is not in the `before_model_call` chain anymore) and keep/rework the Truncator one untouched.

- [ ] **Step 4: Run the full suite** — `uv run pytest` (this task touches many files; everything must be green).

- [ ] **Step 5: Commit**

```bash
git add -A src tests
git commit -m "feat: turn-count auto compression replaces the transient token-budget summarizer"
```

---

### Task 5: `/compress` slash command in the Discord gateway

**Files:**
- Modify: `src/conic/discord/gateway.py`
- Test: `tests/discord/test_gateway.py`

**Interfaces:**
- Produces: `/compress` app command; `DiscordGateway.handle_compress_command(thread_id: int, respond: Callable[[str], Awaitable[None]]) -> None` — posts `SteeringCompressCommand(reason="manual")` to the session's `steering.compress` mailbox and confirms ephemerally; unknown thread responds with an explanation and posts nothing.

- [ ] **Step 1: Write the failing tests** — in `tests/discord/test_gateway.py`:

First update the fakes so scopes carry the new mailbox, mirroring the real `ReactLoopPlugin.register` (in both `FakePluginManagerRecorder.start_session` and `make_fixed_scope`, next to the existing `create_mailbox` call; add `SteeringCompressCommand` to the module import):

```python
        bus.create_mailbox("steering.high", SteeringItem)
        bus.create_mailbox("steering.compress", SteeringCompressCommand)
```

Then append:

```python
async def test_handle_compress_command_posts_to_the_compress_mailbox():
    manager = FakePluginManagerRecorder()
    gateway = DiscordGateway(make_config(), plugin_manager=manager, storage=None)
    scope = await manager.start_session("discord", "444", lambda: object())
    gateway._sessions[444] = scope

    responses = []

    async def fake_respond(text: str):
        responses.append(text)

    await gateway.handle_compress_command(thread_id=444, respond=fake_respond)

    posted = await scope.bus.drain("steering.compress")
    assert len(posted) == 1
    assert posted[0].reason == "manual"
    assert await scope.bus.drain("steering.high") == []
    assert responses


async def test_handle_compress_command_on_unknown_thread_responds_without_posting():
    manager = FakePluginManagerRecorder()
    gateway = DiscordGateway(make_config(), plugin_manager=manager, storage=None)

    responses = []

    async def fake_respond(text: str):
        responses.append(text)

    await gateway.handle_compress_command(thread_id=999, respond=fake_respond)

    assert responses
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/discord/test_gateway.py -v -k compress`
Expected: FAIL with `AttributeError: 'DiscordGateway' object has no attribute 'handle_compress_command'`

- [ ] **Step 3: Implement** — in `src/conic/discord/gateway.py`:

Extend the steering import:

```python
from conic.types.steering import SteeringCompressCommand, SteeringStopCommand
```

In `_register_discord_wiring`, after the `agent_stop` command:

```python
        @self._tree.command(name="compress", description="立刻压缩上下文,之后的对话从压缩后的摘要继续")
        async def compress(interaction: discord.Interaction) -> None:
            async def respond(text: str) -> None:
                await interaction.response.send_message(text, ephemeral=True)

            await self.handle_compress_command(thread_id=interaction.channel.id, respond=respond)
```

New handler next to `handle_stop_command`:

```python
    async def handle_compress_command(
        self, thread_id: int, respond: Callable[[str], Awaitable[None]]
    ) -> None:
        scope = self._sessions.get(thread_id)
        if scope is None:
            logger.warning("compress command for unknown thread {}", thread_id)
            await respond("这个 thread 里没有活跃的 session。")
            return
        logger.info("compress requested for thread {}", thread_id)
        await scope.bus.post("steering.compress", SteeringCompressCommand(reason="manual"))
        await respond("🗜️ 正在压缩上下文…")
```

- [ ] **Step 4: Run tests to verify they pass** — `uv run pytest tests/discord/ -v`

- [ ] **Step 5: Commit**

```bash
git add src/conic/discord/gateway.py tests/discord/test_gateway.py
git commit -m "feat: /compress slash command posts to the steering.compress mailbox"
```

---

### Task 6: separator message in `DiscordThreadPlugin`

**Files:**
- Modify: `src/conic/plugins/channels/discord.py`
- Test: `tests/plugins/channels/test_discord.py`

**Interfaces:**
- Produces: `COMPRESS_SEPARATOR` template; `on_compress_done(msg: CompressDone)` registered on `meta.CompressDoneEvent`, sends the separator (trigger kind + before → after message counts) into the thread. Fires for both manual and auto compressions since both emit `CompressDoneEvent`.

- [ ] **Step 1: Write the failing tests** — append to `tests/plugins/channels/test_discord.py` (add `CompressDone` to the `conic.types.messages` import):

```python
async def test_compress_done_posts_a_separator_to_the_thread():
    thread = FakeThread()
    plugin = DiscordThreadPlugin(thread)
    bus = MessageBus()
    plugin.register(bus)

    await bus.chain(meta.CompressDoneEvent, CompressDone(messages_before=12, messages_after=3, reason="manual"))

    assert len(thread.sent) == 1
    assert "━" in thread.sent[0]
    assert "12" in thread.sent[0]
    assert "3" in thread.sent[0]


async def test_compress_done_separator_mentions_the_auto_trigger():
    thread = FakeThread()
    plugin = DiscordThreadPlugin(thread)
    bus = MessageBus()
    plugin.register(bus)

    await bus.chain(meta.CompressDoneEvent, CompressDone(messages_before=12, messages_after=3, reason="auto"))

    assert "自动" in thread.sent[0]


async def test_compress_done_after_session_end_is_muted():
    thread = FakeThread()
    plugin = DiscordThreadPlugin(thread)
    bus = MessageBus()
    plugin.register(bus)

    await bus.chain(meta.SessionEndEvent, SessionEnd(reason="user_stop"))
    await bus.chain(meta.CompressDoneEvent, CompressDone(messages_before=12, messages_after=3, reason="manual"))

    assert thread.sent == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/plugins/channels/test_discord.py -v -k compress`
Expected: FAIL (no `on_compress_done` handler registered, nothing sent)

- [ ] **Step 3: Implement** — in `src/conic/plugins/channels/discord.py`:

Add `CompressDone` to the `conic.types.messages` import. Add near the other constants:

```python
COMPRESS_SEPARATOR = (
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "📦 **上下文已压缩**({trigger},{before} → {after} 条消息),之后的对话只基于压缩后的摘要\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
)
```

In `register`:

```python
        bus.on_chain(meta.CompressDoneEvent, self.on_compress_done)
```

New handler:

```python
    async def on_compress_done(self, msg: CompressDone) -> None:
        trigger = "自动触发" if msg.reason == "auto" else "手动触发"
        await self._send(
            COMPRESS_SEPARATOR.format(
                trigger=trigger, before=msg.messages_before, after=msg.messages_after
            )
        )
```

(`_send` already respects `self._stopped`, giving the mute-after-session-end behavior for free.)

- [ ] **Step 4: Run the full suite + lint**

Run: `uv run pytest && uv run ruff check`
Expected: all PASS, no lint errors.

- [ ] **Step 5: Commit**

```bash
git add src/conic/plugins/channels/discord.py tests/plugins/channels/test_discord.py
git commit -m "feat: post a separator to the thread on every context compression"
```

---

## Out of scope

- Token-based compression triggering (removed entirely; `COMPRESS_TURN_THRESHOLD` is the only auto trigger). If a session blows past the model's context window inside fewer than N turns, the model call fails visibly (`ErrorEvent`) and the user can `/compress` manually — a token-based fallback can be reintroduced later as another poster to `steering.compress`.
- No `/compress` support for non-Discord channels (none exist yet); the mailbox and loop logic are channel-agnostic, so a future channel only needs its own command wiring + separator hook.
- No custom summarization instructions parameter on the command (SummarizerPlugin's default instructions are used).
- Auto-compress counter is not persisted across process restarts (resets to 0 on resume).

## Known behaviors to be aware of

- `/compress` issued while a turn is running takes effect right after that turn ends (the compress mailbox is only drained between turns), not mid-turn.
- Auto compression fires between turns: `TurnCompressPlugin` posts during the `TurnEndEvent` chain, and `run_loop`'s next wake drains and compresses immediately — so in practice the separator appears right after the Nth turn's answer.
- `/compress` and `/agent_stop` landing in the same wake: the stop wins and the compress is discarded (existing batch-abort semantics).
- On compression failure the thread shows `⚠️ <exception>` and history is unchanged; the auto-trigger counter is not reset, so it retries after the next turn.
