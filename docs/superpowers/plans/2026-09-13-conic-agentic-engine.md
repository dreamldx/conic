# Conic Agentic Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Conic, a plugin-based agentic engine — OpenRouter LLM backend, Discord bot channel, 4 mini-Kode v1 tools (bash/read_file/write_file/edit_file), DuckDB persistence — where every component (loop, backend, tools, channel) is a plugin connected through a message bus.

**Architecture:** A per-session `MessageBus` (chain-style `on`/`emit` for 0+ observers, request-style `on_request`/`request` for exactly one responder) is assembled fresh for each conversation by `PluginManager`. `Core Service`s (`StorageService`, `DiscordGateway`) exist before any session and are used via direct calls; `Plugin`s (Loop, Backend, Tool, Channel, ContextBuilder, Policy) exist only for a session's lifetime and only talk over its bus.

**Tech Stack:** Python 3.12+, `openai` SDK (OpenRouter's OpenAI-compatible endpoint), `discord.py`, `duckdb`, `pytest` + `pytest-asyncio`, `uv` for dependency management.

**Spec:** `docs/superpowers/specs/2026-09-13-conic-agentic-engine-design.md` — this plan implements it section by section; read both together.

## Global Constraints

- Python >= 3.12 (per `pyproject.toml`, already set).
- No dynamic plugin discovery — plugins are statically registered in `src/conic/plugins/registry.py` (spec §13).
- Every tool path operation must be rejected if it resolves outside the session's `workspace_dir` (spec §7.3) — never raise from a tool; return `ToolCallResult(error=...)`.
- `MessageBus` message types are the pair (string topic, payload's Python class) — never a bare string (spec §4).
- No `session_id` field on any message dataclass — session identity is structural (one `MessageBus` instance per session) (spec §5).
- `PluginManager` must never import anything Discord-specific — channel details only enter via the `channel_plugin_factory` closure (spec §5, §7.4/7.5).
- Config defaults exactly as spec §9: `OPENROUTER_MODEL=anthropic/claude-sonnet-4.5`, `WORKSPACE_ROOT=./workspace`, `DUCKDB_PATH=./data/conic.duckdb`, `MAX_STEPS_PER_TURN=25`, `CONTEXT_TOKEN_BUDGET=50000`, `TRUNCATE_KEEP_LAST_N=40`.

---

## File Structure

```
src/conic/
  core/
    errors.py       # AbortTurn, NoResponderError, DuplicateResponderError
    messages.py      # all message dataclasses
    tokencount.py     # estimate_tokens()
    bus.py            # MessageBus, infer_payload_type()
    session.py         # SessionRow, SessionScope
    gateway.py          # Gateway Protocol
    manager.py           # PluginManager, PluginSet
  services/
    storage.py            # StorageService, SessionHandle (DuckDB)
  plugins/
    registry.py             # build_plugin_set()
    loops/react_loop.py       # ReactLoopPlugin
    backends/openrouter.py     # OpenRouterBackendPlugin
    tools/
      base.py                    # resolve_within_workspace, WorkspaceEscapeError
      bash.py                     # BashCall, BashToolPlugin
      read_file.py                 # ReadFileCall, ReadFileToolPlugin
      write_file.py                 # WriteFileCall, WriteFileToolPlugin
      edit_file.py                   # EditFileCall, EditFileToolPlugin
    channels/discord/
      adapter.py                       # DiscordThreadPlugin
      gateway.py                        # DiscordGateway
    context/
      system_prompt.py                   # SystemPromptPlugin
      truncator.py                        # TruncatorPlugin
      token_budget.py                      # TokenBudgetPlugin
      summarizer.py                         # SummarizerPlugin
    policy/
      permission.py                          # PermissionPolicyPlugin
      step_limit.py                            # StepLimitPlugin
  config.py                                     # load_config(), Config, ConfigError
main.py                                          # build_app(), main()
tests/
  (mirrors src/conic/ structure, one test file per module above)
```

---

### Task 1: Project setup — dependencies and test harness

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/__init__.py` (empty)

**Interfaces:**
- Produces: a working `pytest` invocation and the three runtime dependencies (`openai`, `discord.py`, `duckdb`) available to every later task.

- [ ] **Step 1: Add runtime dependencies**

Run: `uv add openai "discord.py>=2.4" duckdb`

- [ ] **Step 2: Add dev dependencies**

Run: `uv add --dev pytest pytest-asyncio`

- [ ] **Step 3: Configure pytest for async tests**

Add to `pyproject.toml`:
```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 4: Create empty tests package**

Create `tests/__init__.py` with no content.

- [ ] **Step 5: Verify pytest runs (with zero tests)**

Run: `uv run pytest`
Expected: `no tests ran` (exit code 0 or 5), no import errors.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock tests/__init__.py
git commit -m "chore: add runtime/test dependencies and pytest config"
```

---

### Task 2: Core data model — errors and messages

**Files:**
- Create: `src/conic/core/__init__.py` (empty)
- Create: `src/conic/core/errors.py`
- Create: `src/conic/core/messages.py`
- Test: `tests/core/test_messages.py`

**Interfaces:**
- Produces: `AbortTurn`, `NoResponderError`, `DuplicateResponderError` (all `Exception` subclasses); dataclasses `UserInput(text: str)`, `TurnStart()`, `TurnEnd()`, `StepStart(step_index: int)`, `BeforeModelCall(messages: list[dict], tools: list[dict])`, `ModelRequest(messages: list[dict], tools: list[dict])`, `ToolCallSpec(id: str, name: str, args: dict)`, `ModelResponse(text: str | None, tool_calls: list[ToolCallSpec], raw_message: dict)`, `ToolCall(call: ToolCallSpec)`, `ToolCallResult(output: str | None = None, error: str | None = None)`, `AssistantMessage(text: str)`, `Error(exc: Exception)`, `SummarizeRequest(messages: list[dict], budget_tokens: int)`, `SummarizeResult(messages: list[dict])`.

- [ ] **Step 1: Write the failing test**

Create `tests/core/__init__.py` (empty), then `tests/core/test_messages.py`:
```python
from conic.core.errors import AbortTurn, NoResponderError, DuplicateResponderError
from conic.core.messages import (
    UserInput, TurnStart, TurnEnd, StepStart, BeforeModelCall, ModelRequest,
    ToolCallSpec, ModelResponse, ToolCall, ToolCallResult, AssistantMessage,
    Error, SummarizeRequest, SummarizeResult,
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


def test_error_types_are_exceptions():
    assert issubclass(AbortTurn, Exception)
    assert issubclass(NoResponderError, Exception)
    assert issubclass(DuplicateResponderError, Exception)
    try:
        raise AbortTurn("too many steps")
    except AbortTurn as exc:
        assert "too many steps" in str(exc)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/core/test_messages.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'conic.core'`

- [ ] **Step 3: Write minimal implementation**

Create `src/conic/core/__init__.py` (empty).

Create `src/conic/core/errors.py`:
```python
class AbortTurn(Exception):
    """Raised by any hook to abort the current turn cleanly."""


class NoResponderError(Exception):
    """Raised by MessageBus.request when no handler matches the payload type."""


class DuplicateResponderError(Exception):
    """Raised by MessageBus.on_request when a second responder is registered
    for the same (topic, payload type) pair."""
```

Create `src/conic/core/messages.py`:
```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/core/test_messages.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/conic/core/__init__.py src/conic/core/errors.py src/conic/core/messages.py tests/core/__init__.py tests/core/test_messages.py
git commit -m "feat: add core error types and message dataclasses"
```

---

### Task 3: MessageBus

**Files:**
- Create: `src/conic/core/bus.py`
- Test: `tests/core/test_bus.py`

**Interfaces:**
- Consumes: `AbortTurn`, `NoResponderError`, `DuplicateResponderError` from `conic.core.errors` (Task 2)
- Produces: `infer_payload_type(handler) -> type`; `class MessageBus` with `on(type_name: str, handler) -> None`, `async emit(type_name: str, payload: Any) -> Any`, `on_request(type_name: str, handler) -> None`, `async request(type_name: str, payload: Any) -> Any`

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_bus.py`:
```python
from dataclasses import dataclass

import pytest

from conic.core.bus import MessageBus, infer_payload_type
from conic.core.errors import DuplicateResponderError, NoResponderError


@dataclass
class Ping:
    n: int


@dataclass
class Pong:
    n: int


def test_infer_payload_type_from_handler_annotation():
    async def handler(msg: Ping) -> None:
        return None

    assert infer_payload_type(handler) is Ping


def test_infer_payload_type_ignores_self():
    class Handler:
        async def handle(self, msg: Ping) -> None:
            return None

    assert infer_payload_type(Handler.handle) is Ping


async def test_emit_calls_handlers_in_registration_order():
    bus = MessageBus()
    calls = []

    async def first(msg: Ping):
        calls.append("first")
        return None

    async def second(msg: Ping):
        calls.append("second")
        return None

    bus.on("ping", first)
    bus.on("ping", second)
    await bus.emit("ping", Ping(n=1))
    assert calls == ["first", "second"]


async def test_emit_chains_mutated_payload_to_next_handler():
    bus = MessageBus()

    async def increment(msg: Ping) -> Ping:
        return Ping(n=msg.n + 1)

    async def double(msg: Ping) -> Ping:
        return Ping(n=msg.n * 2)

    bus.on("ping", increment)
    bus.on("ping", double)
    result = await bus.emit("ping", Ping(n=1))
    assert result.n == 4  # (1 + 1) * 2


async def test_emit_keeps_current_payload_when_handler_returns_none():
    bus = MessageBus()

    async def observer(msg: Ping) -> None:
        return None

    bus.on("ping", observer)
    result = await bus.emit("ping", Ping(n=5))
    assert result.n == 5


async def test_emit_only_calls_handlers_matching_payload_type():
    bus = MessageBus()
    seen = []

    async def on_ping(msg: Ping):
        seen.append(("ping", msg.n))

    async def on_pong(msg: Pong):
        seen.append(("pong", msg.n))

    bus.on("event", on_ping)
    bus.on("event", on_pong)
    await bus.emit("event", Ping(n=1))
    assert seen == [("ping", 1)]


async def test_request_returns_single_responder_result():
    bus = MessageBus()

    async def responder(msg: Ping) -> Pong:
        return Pong(n=msg.n + 100)

    bus.on_request("ask", responder)
    result = await bus.request("ask", Ping(n=1))
    assert result == Pong(n=101)


def test_on_request_rejects_duplicate_responder_for_same_topic_and_type():
    bus = MessageBus()

    async def responder_a(msg: Ping) -> Pong:
        return Pong(n=1)

    async def responder_b(msg: Ping) -> Pong:
        return Pong(n=2)

    bus.on_request("ask", responder_a)
    with pytest.raises(DuplicateResponderError):
        bus.on_request("ask", responder_b)


def test_on_request_allows_same_topic_different_payload_type():
    bus = MessageBus()

    async def responder_a(msg: Ping) -> Pong:
        return Pong(n=1)

    async def responder_b(msg: Pong) -> Ping:
        return Ping(n=2)

    bus.on_request("ask", responder_a)
    bus.on_request("ask", responder_b)  # must not raise


async def test_request_raises_when_no_responder_matches():
    bus = MessageBus()
    with pytest.raises(NoResponderError):
        await bus.request("ask", Ping(n=1))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_bus.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'conic.core.bus'`

- [ ] **Step 3: Write minimal implementation**

Create `src/conic/core/bus.py`:
```python
import inspect
from typing import Any, Callable, get_type_hints

from conic.core.errors import DuplicateResponderError, NoResponderError


def infer_payload_type(handler: Callable) -> type:
    hints = get_type_hints(handler)
    params = [p for p in inspect.signature(handler).parameters if p != "self"]
    return hints[params[0]]


class MessageBus:
    def __init__(self) -> None:
        self._chain: dict[str, list[tuple[type, Callable]]] = {}
        self._request: dict[str, list[tuple[type, Callable]]] = {}

    def on(self, type_name: str, handler: Callable) -> None:
        payload_cls = infer_payload_type(handler)
        self._chain.setdefault(type_name, []).append((payload_cls, handler))

    async def emit(self, type_name: str, payload: Any) -> Any:
        for payload_cls, handler in self._chain.get(type_name, []):
            if isinstance(payload, payload_cls):
                result = await handler(payload)
                if result is not None:
                    payload = result
        return payload

    def on_request(self, type_name: str, handler: Callable) -> None:
        payload_cls = infer_payload_type(handler)
        bucket = self._request.setdefault(type_name, [])
        if any(existing_cls is payload_cls for existing_cls, _ in bucket):
            raise DuplicateResponderError(
                f"{type_name!r} already has a responder for payload type {payload_cls!r}"
            )
        bucket.append((payload_cls, handler))

    async def request(self, type_name: str, payload: Any) -> Any:
        for payload_cls, handler in self._request.get(type_name, []):
            if isinstance(payload, payload_cls):
                return await handler(payload)
        raise NoResponderError(
            f"no responder for topic {type_name!r} with payload type {type(payload)!r}"
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_bus.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add src/conic/core/bus.py tests/core/test_bus.py
git commit -m "feat: add MessageBus with chain (emit) and single-responder (request) dispatch"
```

---

### Task 4: Tool workspace sandbox helper

**Files:**
- Create: `src/conic/plugins/__init__.py`, `src/conic/plugins/tools/__init__.py` (empty)
- Create: `src/conic/plugins/tools/base.py`
- Test: `tests/plugins/tools/test_base.py`

**Interfaces:**
- Produces: `class WorkspaceEscapeError(Exception)`; `resolve_within_workspace(workspace_dir: str, path: str) -> Path` (raises `WorkspaceEscapeError` if the resolved path is outside `workspace_dir`)

- [ ] **Step 1: Write the failing tests**

Create `tests/plugins/__init__.py`, `tests/plugins/tools/__init__.py` (empty), then `tests/plugins/tools/test_base.py`:
```python
import pytest

from conic.plugins.tools.base import WorkspaceEscapeError, resolve_within_workspace


def test_resolves_path_inside_workspace(tmp_path):
    (tmp_path / "sub").mkdir()
    resolved = resolve_within_workspace(str(tmp_path), "sub/file.txt")
    assert resolved == (tmp_path / "sub" / "file.txt").resolve()


def test_workspace_root_itself_is_allowed(tmp_path):
    resolved = resolve_within_workspace(str(tmp_path), ".")
    assert resolved == tmp_path.resolve()


def test_rejects_relative_escape(tmp_path):
    with pytest.raises(WorkspaceEscapeError):
        resolve_within_workspace(str(tmp_path), "../outside.txt")


def test_rejects_absolute_path_outside_workspace(tmp_path):
    with pytest.raises(WorkspaceEscapeError):
        resolve_within_workspace(str(tmp_path), str(tmp_path.parent / "outside.txt"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/plugins/tools/test_base.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'conic.plugins'`

- [ ] **Step 3: Write minimal implementation**

Create `src/conic/plugins/__init__.py` and `src/conic/plugins/tools/__init__.py` (both empty).

Create `src/conic/plugins/tools/base.py`:
```python
from pathlib import Path


class WorkspaceEscapeError(Exception):
    pass


def resolve_within_workspace(workspace_dir: str, path: str) -> Path:
    base = Path(workspace_dir).resolve()
    candidate = (base / path).resolve()
    if candidate != base and base not in candidate.parents:
        raise WorkspaceEscapeError(f"path escapes workspace: {path!r}")
    return candidate
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/plugins/tools/test_base.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/conic/plugins/__init__.py src/conic/plugins/tools/__init__.py src/conic/plugins/tools/base.py tests/plugins/__init__.py tests/plugins/tools/__init__.py tests/plugins/tools/test_base.py
git commit -m "feat: add workspace path sandboxing helper for tools"
```

---

### Task 5: BashToolPlugin

**Files:**
- Create: `src/conic/plugins/tools/bash.py`
- Test: `tests/plugins/tools/test_bash.py`

**Interfaces:**
- Consumes: `ToolCallResult` from `conic.core.messages` (Task 2)
- Produces: `@dataclass BashCall(command: str)`; `class BashToolPlugin` with `llm_name = "bash"`, `schema: dict` (OpenAI function-calling schema), `__init__(self, workspace_dir: str, timeout: float = 60.0, max_output_bytes: int = 20_000)`, `register(self, bus) -> None`, `async execute(self, call: BashCall) -> ToolCallResult`

- [ ] **Step 1: Write the failing tests**

Create `tests/plugins/tools/test_bash.py`:
```python
import sys

from conic.plugins.tools.bash import BashCall, BashToolPlugin


async def test_execute_runs_command_and_returns_stdout(tmp_path):
    tool = BashToolPlugin(workspace_dir=str(tmp_path))
    result = await tool.execute(BashCall(command="echo hello"))
    assert result.error is None
    assert "hello" in result.output


async def test_execute_runs_in_workspace_directory(tmp_path):
    (tmp_path / "marker.txt").write_text("x")
    tool = BashToolPlugin(workspace_dir=str(tmp_path))
    list_cmd = "dir /b" if sys.platform == "win32" else "ls"
    result = await tool.execute(BashCall(command=list_cmd))
    assert "marker.txt" in result.output


async def test_execute_reports_nonzero_exit_code_as_error(tmp_path):
    tool = BashToolPlugin(workspace_dir=str(tmp_path))
    fail_cmd = f'{sys.executable} -c "import sys; sys.exit(3)"'
    result = await tool.execute(BashCall(command=fail_cmd))
    assert result.error is not None
    assert "3" in result.error


async def test_execute_times_out_long_running_command(tmp_path):
    tool = BashToolPlugin(workspace_dir=str(tmp_path), timeout=0.2)
    sleep_cmd = f'{sys.executable} -c "import time; time.sleep(5)"'
    result = await tool.execute(BashCall(command=sleep_cmd))
    assert result.error is not None
    assert "timed out" in result.error


async def test_execute_truncates_large_output(tmp_path):
    tool = BashToolPlugin(workspace_dir=str(tmp_path), max_output_bytes=100)
    big_cmd = f'{sys.executable} -c "print(\'x\' * 5000)"'
    result = await tool.execute(BashCall(command=big_cmd))
    assert result.error is None
    assert len(result.output.encode()) < 5000
    assert "truncated" in result.output


def test_register_wires_tool_call_request():
    from conic.core.bus import MessageBus

    tool = BashToolPlugin(workspace_dir=".")
    bus = MessageBus()
    tool.register(bus)
    assert bus._request["tool_call"][0][0] is BashCall
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/plugins/tools/test_bash.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'conic.plugins.tools.bash'`

- [ ] **Step 3: Write minimal implementation**

Create `src/conic/plugins/tools/bash.py`:
```python
import asyncio
from dataclasses import dataclass

from conic.core.messages import ToolCallResult


@dataclass
class BashCall:
    command: str


class BashToolPlugin:
    llm_name = "bash"
    schema = {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute a shell command in the session workspace directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run."},
                },
                "required": ["command"],
            },
        },
    }

    def __init__(self, workspace_dir: str, timeout: float = 60.0, max_output_bytes: int = 20_000):
        self._workspace_dir = workspace_dir
        self._timeout = timeout
        self._max_output_bytes = max_output_bytes

    def register(self, bus) -> None:
        bus.on_request("tool_call", self.execute)

    async def execute(self, call: BashCall) -> ToolCallResult:
        try:
            proc = await asyncio.create_subprocess_shell(
                call.command,
                cwd=self._workspace_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return ToolCallResult(error=str(exc))

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return ToolCallResult(error=f"command timed out after {self._timeout}s")

        output = stdout.decode(errors="replace")
        err = stderr.decode(errors="replace")
        combined = output + (("\n" + err) if err else "")
        combined = self._truncate(combined)
        if proc.returncode != 0:
            return ToolCallResult(error=f"exit code {proc.returncode}: {combined}")
        return ToolCallResult(output=combined)

    def _truncate(self, text: str) -> str:
        data = text.encode()
        if len(data) <= self._max_output_bytes:
            return text
        return data[: self._max_output_bytes].decode(errors="replace") + "\n...[truncated]"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/plugins/tools/test_bash.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/conic/plugins/tools/bash.py tests/plugins/tools/test_bash.py
git commit -m "feat: add BashToolPlugin"
```

---

### Task 6: ReadFileToolPlugin

**Files:**
- Create: `src/conic/plugins/tools/read_file.py`
- Test: `tests/plugins/tools/test_read_file.py`

**Interfaces:**
- Consumes: `resolve_within_workspace`, `WorkspaceEscapeError` from `conic.plugins.tools.base` (Task 4); `ToolCallResult` from `conic.core.messages` (Task 2)
- Produces: `@dataclass ReadFileCall(path: str, offset: int = 0, limit: int = 2000)`; `class ReadFileToolPlugin` with `llm_name = "read_file"`, `schema`, `__init__(self, workspace_dir: str)`, `register(self, bus) -> None`, `async execute(self, call: ReadFileCall) -> ToolCallResult`

- [ ] **Step 1: Write the failing tests**

Create `tests/plugins/tools/test_read_file.py`:
```python
from conic.plugins.tools.read_file import ReadFileCall, ReadFileToolPlugin


async def test_reads_full_small_file(tmp_path):
    (tmp_path / "a.txt").write_text("line1\nline2\nline3")
    tool = ReadFileToolPlugin(workspace_dir=str(tmp_path))
    result = await tool.execute(ReadFileCall(path="a.txt"))
    assert result.error is None
    assert result.output == "line1\nline2\nline3"


async def test_reads_with_offset_and_limit(tmp_path):
    (tmp_path / "a.txt").write_text("\n".join(f"line{i}" for i in range(10)))
    tool = ReadFileToolPlugin(workspace_dir=str(tmp_path))
    result = await tool.execute(ReadFileCall(path="a.txt", offset=2, limit=3))
    assert result.output.splitlines()[:3] == ["line2", "line3", "line4"]
    assert "truncated" in result.output


async def test_errors_on_missing_file(tmp_path):
    tool = ReadFileToolPlugin(workspace_dir=str(tmp_path))
    result = await tool.execute(ReadFileCall(path="missing.txt"))
    assert result.error is not None


async def test_rejects_path_outside_workspace(tmp_path):
    tool = ReadFileToolPlugin(workspace_dir=str(tmp_path))
    result = await tool.execute(ReadFileCall(path="../outside.txt"))
    assert result.error is not None
    assert result.output is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/plugins/tools/test_read_file.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

Create `src/conic/plugins/tools/read_file.py`:
```python
from dataclasses import dataclass

from conic.core.messages import ToolCallResult
from conic.plugins.tools.base import WorkspaceEscapeError, resolve_within_workspace


@dataclass
class ReadFileCall:
    path: str
    offset: int = 0
    limit: int = 2000


class ReadFileToolPlugin:
    llm_name = "read_file"
    schema = {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file within the session workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "description": "0-based line to start from"},
                    "limit": {"type": "integer", "description": "max number of lines to return"},
                },
                "required": ["path"],
            },
        },
    }

    def __init__(self, workspace_dir: str):
        self._workspace_dir = workspace_dir

    def register(self, bus) -> None:
        bus.on_request("tool_call", self.execute)

    async def execute(self, call: ReadFileCall) -> ToolCallResult:
        try:
            resolved = resolve_within_workspace(self._workspace_dir, call.path)
        except WorkspaceEscapeError as exc:
            return ToolCallResult(error=str(exc))
        if not resolved.is_file():
            return ToolCallResult(error=f"not a file: {call.path}")
        lines = resolved.read_text(errors="replace").splitlines()
        selected = lines[call.offset : call.offset + call.limit]
        text = "\n".join(selected)
        if call.offset + call.limit < len(lines):
            text += f"\n...[truncated, {len(lines)} lines total]"
        return ToolCallResult(output=text)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/plugins/tools/test_read_file.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/conic/plugins/tools/read_file.py tests/plugins/tools/test_read_file.py
git commit -m "feat: add ReadFileToolPlugin"
```

---

### Task 7: WriteFileToolPlugin

**Files:**
- Create: `src/conic/plugins/tools/write_file.py`
- Test: `tests/plugins/tools/test_write_file.py`

**Interfaces:**
- Consumes: `resolve_within_workspace`, `WorkspaceEscapeError` (Task 4); `ToolCallResult` (Task 2)
- Produces: `@dataclass WriteFileCall(path: str, content: str)`; `class WriteFileToolPlugin` with `llm_name = "write_file"`, `schema`, `__init__(self, workspace_dir: str)`, `register(self, bus) -> None`, `async execute(self, call: WriteFileCall) -> ToolCallResult`

- [ ] **Step 1: Write the failing tests**

Create `tests/plugins/tools/test_write_file.py`:
```python
from conic.plugins.tools.write_file import WriteFileCall, WriteFileToolPlugin


async def test_creates_new_file(tmp_path):
    tool = WriteFileToolPlugin(workspace_dir=str(tmp_path))
    result = await tool.execute(WriteFileCall(path="a.txt", content="hello\nworld"))
    assert result.error is None
    assert (tmp_path / "a.txt").read_text() == "hello\nworld"
    assert "created" in result.output


async def test_overwrites_existing_file(tmp_path):
    (tmp_path / "a.txt").write_text("old")
    tool = WriteFileToolPlugin(workspace_dir=str(tmp_path))
    result = await tool.execute(WriteFileCall(path="a.txt", content="new"))
    assert (tmp_path / "a.txt").read_text() == "new"
    assert "overwritten" in result.output


async def test_creates_parent_directories(tmp_path):
    tool = WriteFileToolPlugin(workspace_dir=str(tmp_path))
    await tool.execute(WriteFileCall(path="sub/dir/a.txt", content="hi"))
    assert (tmp_path / "sub" / "dir" / "a.txt").read_text() == "hi"


async def test_rejects_path_outside_workspace(tmp_path):
    tool = WriteFileToolPlugin(workspace_dir=str(tmp_path))
    result = await tool.execute(WriteFileCall(path="../outside.txt", content="x"))
    assert result.error is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/plugins/tools/test_write_file.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

Create `src/conic/plugins/tools/write_file.py`:
```python
from dataclasses import dataclass

from conic.core.messages import ToolCallResult
from conic.plugins.tools.base import WorkspaceEscapeError, resolve_within_workspace


@dataclass
class WriteFileCall:
    path: str
    content: str


class WriteFileToolPlugin:
    llm_name = "write_file"
    schema = {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a text file within the session workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    }

    def __init__(self, workspace_dir: str):
        self._workspace_dir = workspace_dir

    def register(self, bus) -> None:
        bus.on_request("tool_call", self.execute)

    async def execute(self, call: WriteFileCall) -> ToolCallResult:
        try:
            resolved = resolve_within_workspace(self._workspace_dir, call.path)
        except WorkspaceEscapeError as exc:
            return ToolCallResult(error=str(exc))
        existed = resolved.is_file()
        old_line_count = len(resolved.read_text(errors="replace").splitlines()) if existed else 0
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(call.content)
        new_line_count = len(call.content.splitlines())
        status = "overwritten" if existed else "created"
        return ToolCallResult(
            output=f"wrote {new_line_count} lines to {call.path} ({status}, was {old_line_count} lines)"
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/plugins/tools/test_write_file.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/conic/plugins/tools/write_file.py tests/plugins/tools/test_write_file.py
git commit -m "feat: add WriteFileToolPlugin"
```

---

### Task 8: EditFileToolPlugin

**Files:**
- Create: `src/conic/plugins/tools/edit_file.py`
- Test: `tests/plugins/tools/test_edit_file.py`

**Interfaces:**
- Consumes: `resolve_within_workspace`, `WorkspaceEscapeError` (Task 4); `ToolCallResult` (Task 2)
- Produces: `@dataclass EditFileCall(path: str, old_text: str, new_text: str)`; `class EditFileToolPlugin` with `llm_name = "edit_file"`, `schema`, `__init__(self, workspace_dir: str)`, `register(self, bus) -> None`, `async execute(self, call: EditFileCall) -> ToolCallResult`

- [ ] **Step 1: Write the failing tests**

Create `tests/plugins/tools/test_edit_file.py`:
```python
from conic.plugins.tools.edit_file import EditFileCall, EditFileToolPlugin


async def test_replaces_unique_occurrence(tmp_path):
    (tmp_path / "a.txt").write_text("hello world")
    tool = EditFileToolPlugin(workspace_dir=str(tmp_path))
    result = await tool.execute(EditFileCall(path="a.txt", old_text="world", new_text="there"))
    assert result.error is None
    assert (tmp_path / "a.txt").read_text() == "hello there"


async def test_errors_when_old_text_not_found(tmp_path):
    (tmp_path / "a.txt").write_text("hello world")
    tool = EditFileToolPlugin(workspace_dir=str(tmp_path))
    result = await tool.execute(EditFileCall(path="a.txt", old_text="missing", new_text="x"))
    assert result.error is not None
    assert (tmp_path / "a.txt").read_text() == "hello world"


async def test_errors_when_old_text_is_not_unique(tmp_path):
    (tmp_path / "a.txt").write_text("foo foo")
    tool = EditFileToolPlugin(workspace_dir=str(tmp_path))
    result = await tool.execute(EditFileCall(path="a.txt", old_text="foo", new_text="bar"))
    assert result.error is not None
    assert "not unique" in result.error
    assert (tmp_path / "a.txt").read_text() == "foo foo"


async def test_errors_on_missing_file(tmp_path):
    tool = EditFileToolPlugin(workspace_dir=str(tmp_path))
    result = await tool.execute(EditFileCall(path="missing.txt", old_text="a", new_text="b"))
    assert result.error is not None


async def test_rejects_path_outside_workspace(tmp_path):
    tool = EditFileToolPlugin(workspace_dir=str(tmp_path))
    result = await tool.execute(EditFileCall(path="../outside.txt", old_text="a", new_text="b"))
    assert result.error is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/plugins/tools/test_edit_file.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

Create `src/conic/plugins/tools/edit_file.py`:
```python
from dataclasses import dataclass

from conic.core.messages import ToolCallResult
from conic.plugins.tools.base import WorkspaceEscapeError, resolve_within_workspace


@dataclass
class EditFileCall:
    path: str
    old_text: str
    new_text: str


class EditFileToolPlugin:
    llm_name = "edit_file"
    schema = {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace an exact, unique text snippet in a file within the session workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    }

    def __init__(self, workspace_dir: str):
        self._workspace_dir = workspace_dir

    def register(self, bus) -> None:
        bus.on_request("tool_call", self.execute)

    async def execute(self, call: EditFileCall) -> ToolCallResult:
        try:
            resolved = resolve_within_workspace(self._workspace_dir, call.path)
        except WorkspaceEscapeError as exc:
            return ToolCallResult(error=str(exc))
        if not resolved.is_file():
            return ToolCallResult(error=f"not a file: {call.path}")
        text = resolved.read_text(errors="replace")
        count = text.count(call.old_text)
        if count == 0:
            return ToolCallResult(error="old_text not found in file")
        if count > 1:
            return ToolCallResult(error=f"old_text is not unique ({count} occurrences)")
        resolved.write_text(text.replace(call.old_text, call.new_text, 1))
        return ToolCallResult(output=f"replaced 1 occurrence in {call.path}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/plugins/tools/test_edit_file.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/conic/plugins/tools/edit_file.py tests/plugins/tools/test_edit_file.py
git commit -m "feat: add EditFileToolPlugin"
```

---

### Task 9: StorageService

**Files:**
- Create: `src/conic/services/__init__.py` (empty)
- Create: `src/conic/services/storage.py`
- Test: `tests/services/test_storage.py`

**Interfaces:**
- Produces: `@dataclass SessionRow(session_key: str, channel: str, native_id: str, workspace_dir: str, model: str, status: str, created_at: datetime)`; `class SessionHandle` with `append_message(self, message: dict) -> None`, `load_history(self) -> list[dict]`, `set_status(self, status: str) -> None`; `class StorageService` with `__init__(self, db_path: str, workspace_root: str, default_model: str)`, `startup(self) -> None`, `shutdown(self) -> None`, `get_or_create(self, channel: str, native_id: str) -> SessionRow`, `handle_for(self, row: SessionRow) -> SessionHandle`, `active_sessions(self, channel: str) -> list[SessionRow]`

Note: `SessionRow` lives in `conic.services.storage` in this task; Task 10 (`core/session.py`) imports it from here rather than redefining it.

- [ ] **Step 1: Write the failing tests**

Create `tests/services/__init__.py` (empty), then `tests/services/test_storage.py`:
```python
from conic.services.storage import StorageService


def make_storage(tmp_path):
    storage = StorageService(
        db_path=str(tmp_path / "conic.duckdb"),
        workspace_root=str(tmp_path / "workspace"),
        default_model="test-model",
    )
    storage.startup()
    return storage


def test_get_or_create_creates_new_session_with_workspace_dir(tmp_path):
    storage = make_storage(tmp_path)
    row = storage.get_or_create(channel="discord", native_id="123")
    assert row.session_key == "discord:123"
    assert row.channel == "discord"
    assert row.native_id == "123"
    assert row.model == "test-model"
    assert row.status == "active"
    assert (tmp_path / "workspace" / "discord" / "123").is_dir()
    storage.shutdown()


def test_get_or_create_is_idempotent(tmp_path):
    storage = make_storage(tmp_path)
    first = storage.get_or_create(channel="discord", native_id="123")
    second = storage.get_or_create(channel="discord", native_id="123")
    assert first == second
    storage.shutdown()


def test_append_and_load_history_round_trip(tmp_path):
    storage = make_storage(tmp_path)
    row = storage.get_or_create(channel="discord", native_id="123")
    handle = storage.handle_for(row)
    handle.append_message({"role": "user", "content": "hi"})
    handle.append_message({"role": "assistant", "content": "hello"})
    history = handle.load_history()
    assert history == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    storage.shutdown()


def test_set_status_updates_row(tmp_path):
    storage = make_storage(tmp_path)
    row = storage.get_or_create(channel="discord", native_id="123")
    storage.handle_for(row).set_status("ended")
    reloaded = storage.get_or_create(channel="discord", native_id="123")
    assert reloaded.status == "ended"
    storage.shutdown()


def test_active_sessions_filters_by_channel_and_status(tmp_path):
    storage = make_storage(tmp_path)
    active = storage.get_or_create(channel="discord", native_id="1")
    ended = storage.get_or_create(channel="discord", native_id="2")
    storage.get_or_create(channel="slack", native_id="3")
    storage.handle_for(ended).set_status("ended")

    rows = storage.active_sessions(channel="discord")
    assert [r.native_id for r in rows] == ["1"]
    assert rows[0].session_key == active.session_key
    storage.shutdown()


def test_persists_across_reconnect(tmp_path):
    storage = make_storage(tmp_path)
    row = storage.get_or_create(channel="discord", native_id="123")
    storage.handle_for(row).append_message({"role": "user", "content": "hi"})
    storage.shutdown()

    reopened = StorageService(
        db_path=str(tmp_path / "conic.duckdb"),
        workspace_root=str(tmp_path / "workspace"),
        default_model="test-model",
    )
    reopened.startup()
    reloaded_row = reopened.get_or_create(channel="discord", native_id="123")
    history = reopened.handle_for(reloaded_row).load_history()
    assert history == [{"role": "user", "content": "hi"}]
    reopened.shutdown()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/services/test_storage.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'conic.services'`

- [ ] **Step 3: Write minimal implementation**

Create `src/conic/services/__init__.py` (empty).

Create `src/conic/services/storage.py`:
```python
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import duckdb


@dataclass
class SessionRow:
    session_key: str
    channel: str
    native_id: str
    workspace_dir: str
    model: str
    status: str
    created_at: datetime


class SessionHandle:
    def __init__(self, conn: duckdb.DuckDBPyConnection, session_key: str):
        self._conn = conn
        self._session_key = session_key

    def append_message(self, message: dict) -> None:
        seq = self._next_seq()
        self._conn.execute(
            "INSERT INTO messages (session_key, seq, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            [self._session_key, seq, message.get("role", ""), json.dumps(message), datetime.now(timezone.utc)],
        )

    def load_history(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT content FROM messages WHERE session_key = ? ORDER BY seq", [self._session_key]
        ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def set_status(self, status: str) -> None:
        self._conn.execute(
            "UPDATE sessions SET status = ? WHERE session_key = ?", [status, self._session_key]
        )

    def _next_seq(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 FROM messages WHERE session_key = ?", [self._session_key]
        ).fetchone()
        return row[0]


class StorageService:
    def __init__(self, db_path: str, workspace_root: str, default_model: str):
        self._db_path = db_path
        self._workspace_root = Path(workspace_root)
        self._default_model = default_model
        self._conn: duckdb.DuckDBPyConnection | None = None

    def startup(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = duckdb.connect(self._db_path)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS sessions (
                session_key VARCHAR PRIMARY KEY,
                channel VARCHAR,
                native_id VARCHAR,
                workspace_dir VARCHAR,
                model VARCHAR,
                status VARCHAR,
                created_at TIMESTAMP
            )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                session_key VARCHAR,
                seq INTEGER,
                role VARCHAR,
                content VARCHAR,
                created_at TIMESTAMP,
                PRIMARY KEY (session_key, seq)
            )"""
        )

    def shutdown(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def get_or_create(self, channel: str, native_id: str) -> SessionRow:
        session_key = f"{channel}:{native_id}"
        row = self._conn.execute(
            "SELECT session_key, channel, native_id, workspace_dir, model, status, created_at "
            "FROM sessions WHERE session_key = ?",
            [session_key],
        ).fetchone()
        if row is not None:
            return self._row_from_tuple(row)

        workspace_dir = str(self._workspace_root / channel / native_id)
        Path(workspace_dir).mkdir(parents=True, exist_ok=True)
        created_at = datetime.now(timezone.utc)
        self._conn.execute(
            "INSERT INTO sessions (session_key, channel, native_id, workspace_dir, model, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [session_key, channel, native_id, workspace_dir, self._default_model, "active", created_at],
        )
        return SessionRow(
            session_key=session_key,
            channel=channel,
            native_id=native_id,
            workspace_dir=workspace_dir,
            model=self._default_model,
            status="active",
            created_at=created_at,
        )

    def handle_for(self, row: SessionRow) -> SessionHandle:
        return SessionHandle(self._conn, row.session_key)

    def active_sessions(self, channel: str) -> list[SessionRow]:
        rows = self._conn.execute(
            "SELECT session_key, channel, native_id, workspace_dir, model, status, created_at "
            "FROM sessions WHERE channel = ? AND status = 'active'",
            [channel],
        ).fetchall()
        return [self._row_from_tuple(r) for r in rows]

    def _row_from_tuple(self, r) -> SessionRow:
        return SessionRow(
            session_key=r[0], channel=r[1], native_id=r[2], workspace_dir=r[3],
            model=r[4], status=r[5], created_at=r[6],
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/services/test_storage.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/conic/services/__init__.py src/conic/services/storage.py tests/services/__init__.py tests/services/test_storage.py
git commit -m "feat: add StorageService with DuckDB-backed session/message persistence"
```

---

### Task 10: Session scope and Gateway protocol

**Files:**
- Create: `src/conic/core/session.py`
- Create: `src/conic/core/gateway.py`
- Test: `tests/core/test_session.py`

**Interfaces:**
- Consumes: `MessageBus` (Task 3); `SessionRow` from `conic.services.storage` (Task 9)
- Produces: `@dataclass SessionScope(bus: MessageBus, row: SessionRow)`; `class Gateway(Protocol)` with `name: str`, `async start(self) -> None`, `async stop(self) -> None`

- [ ] **Step 1: Write the failing test**

Create `tests/core/test_session.py`:
```python
from datetime import datetime, timezone

from conic.core.bus import MessageBus
from conic.core.gateway import Gateway
from conic.core.session import SessionScope
from conic.services.storage import SessionRow


def test_session_scope_holds_bus_and_row():
    row = SessionRow(
        session_key="discord:1", channel="discord", native_id="1",
        workspace_dir="/tmp/ws", model="m", status="active",
        created_at=datetime.now(timezone.utc),
    )
    bus = MessageBus()
    scope = SessionScope(bus=bus, row=row)
    assert scope.bus is bus
    assert scope.row is row


def test_gateway_protocol_is_satisfied_by_a_minimal_implementation():
    class FakeGateway:
        name = "fake"

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    gw: Gateway = FakeGateway()
    assert gw.name == "fake"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/core/test_session.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'conic.core.session'`

- [ ] **Step 3: Write minimal implementation**

Create `src/conic/core/session.py`:
```python
from dataclasses import dataclass

from conic.core.bus import MessageBus
from conic.services.storage import SessionRow


@dataclass
class SessionScope:
    bus: MessageBus
    row: SessionRow
```

Create `src/conic/core/gateway.py`:
```python
from typing import Protocol


class Gateway(Protocol):
    name: str

    async def start(self) -> None: ...

    async def stop(self) -> None: ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/core/test_session.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/conic/core/session.py src/conic/core/gateway.py tests/core/test_session.py
git commit -m "feat: add SessionScope and channel-agnostic Gateway protocol"
```

---

### Task 11: OpenRouterBackendPlugin

**Files:**
- Create: `src/conic/plugins/backends/__init__.py` (empty)
- Create: `src/conic/plugins/backends/openrouter.py`
- Test: `tests/plugins/backends/test_openrouter.py`

**Interfaces:**
- Consumes: `ModelRequest`, `ModelResponse`, `ToolCallSpec` from `conic.core.messages` (Task 2)
- Produces: `class OpenRouterBackendPlugin` with `__init__(self, api_key: str, model: str, client=None)`, `register(self, bus) -> None`, `async complete(self, msg: ModelRequest) -> ModelResponse`

- [ ] **Step 1: Write the failing tests**

Create `tests/plugins/backends/__init__.py` (empty), then `tests/plugins/backends/test_openrouter.py`:
```python
import json
from dataclasses import dataclass, field
from types import SimpleNamespace

from conic.core.messages import ModelRequest
from conic.plugins.backends.openrouter import OpenRouterBackendPlugin


class FakeCompletions:
    def __init__(self, response):
        self._response = response
        self.last_kwargs = None

    async def create(self, **kwargs):
        self.last_kwargs = kwargs
        return self._response


class FakeChat:
    def __init__(self, response):
        self.completions = FakeCompletions(response)


class FakeClient:
    def __init__(self, response):
        self.chat = FakeChat(response)


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
    client = FakeClient(make_response(message))
    backend = OpenRouterBackendPlugin(api_key="k", model="test-model", client=client)

    result = await backend.complete(ModelRequest(messages=[{"role": "user", "content": "hi"}], tools=[]))

    assert result.text == "hello there"
    assert result.tool_calls == []
    assert client.chat.completions.last_kwargs["model"] == "test-model"


async def test_complete_parses_tool_calls():
    fn = SimpleNamespace(name="bash", arguments=json.dumps({"command": "ls"}))
    tool_call = SimpleNamespace(id="call_1", function=fn)
    message = make_message(content=None, tool_calls=[tool_call])
    client = FakeClient(make_response(message))
    backend = OpenRouterBackendPlugin(api_key="k", model="test-model", client=client)

    result = await backend.complete(ModelRequest(messages=[], tools=[]))

    assert result.tool_calls[0].id == "call_1"
    assert result.tool_calls[0].name == "bash"
    assert result.tool_calls[0].args == {"command": "ls"}


def test_register_wires_model_request():
    from conic.core.bus import MessageBus

    backend = OpenRouterBackendPlugin(api_key="k", model="m", client=FakeClient(make_response(make_message())))
    bus = MessageBus()
    backend.register(bus)
    assert bus._request["model_request"][0][0] is ModelRequest
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/plugins/backends/test_openrouter.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

Create `src/conic/plugins/backends/__init__.py` (empty).

Create `src/conic/plugins/backends/openrouter.py`:
```python
import json

from openai import AsyncOpenAI

from conic.core.messages import ModelRequest, ModelResponse, ToolCallSpec


class OpenRouterBackendPlugin:
    def __init__(self, api_key: str, model: str, client: AsyncOpenAI | None = None):
        self.model = model
        self._client = client or AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=api_key)

    def register(self, bus) -> None:
        bus.on_request("model_request", self.complete)

    async def complete(self, msg: ModelRequest) -> ModelResponse:
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
        return ModelResponse(text=message.content, tool_calls=tool_calls, raw_message=raw_message)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/plugins/backends/test_openrouter.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/conic/plugins/backends/__init__.py src/conic/plugins/backends/openrouter.py tests/plugins/backends/__init__.py tests/plugins/backends/test_openrouter.py
git commit -m "feat: add OpenRouterBackendPlugin"
```

---

### Task 12: SystemPromptPlugin and TruncatorPlugin

**Files:**
- Create: `src/conic/plugins/context/__init__.py` (empty)
- Create: `src/conic/plugins/context/system_prompt.py`
- Create: `src/conic/plugins/context/truncator.py`
- Test: `tests/plugins/context/test_system_prompt.py`
- Test: `tests/plugins/context/test_truncator.py`

**Interfaces:**
- Consumes: `BeforeModelCall` from `conic.core.messages` (Task 2)
- Produces: `class SystemPromptPlugin` with `__init__(self, prompt: str)`, `register(self, bus) -> None`, `async apply(self, ctx: BeforeModelCall) -> BeforeModelCall | None`; `class TruncatorPlugin` with `__init__(self, keep_last_n: int)`, `register(self, bus) -> None`, `async apply(self, ctx: BeforeModelCall) -> BeforeModelCall | None`

- [ ] **Step 1: Write the failing tests**

Create `tests/plugins/context/__init__.py` (empty), then `tests/plugins/context/test_system_prompt.py`:
```python
from conic.core.messages import BeforeModelCall
from conic.plugins.context.system_prompt import SystemPromptPlugin


async def test_injects_system_prompt_when_missing():
    plugin = SystemPromptPlugin(prompt="You are Conic.")
    ctx = BeforeModelCall(messages=[{"role": "user", "content": "hi"}], tools=[])
    result = await plugin.apply(ctx)
    assert result.messages[0] == {"role": "system", "content": "You are Conic."}
    assert result.messages[1] == {"role": "user", "content": "hi"}


async def test_does_not_duplicate_existing_system_prompt():
    plugin = SystemPromptPlugin(prompt="You are Conic.")
    ctx = BeforeModelCall(
        messages=[{"role": "system", "content": "You are Conic."}, {"role": "user", "content": "hi"}],
        tools=[],
    )
    result = await plugin.apply(ctx)
    assert result is None
```

Create `tests/plugins/context/test_truncator.py`:
```python
from conic.core.messages import BeforeModelCall
from conic.plugins.context.truncator import TruncatorPlugin


async def test_passes_through_when_under_limit():
    plugin = TruncatorPlugin(keep_last_n=5)
    messages = [{"role": "user", "content": str(i)} for i in range(3)]
    result = await plugin.apply(BeforeModelCall(messages=messages, tools=[]))
    assert result is None


async def test_keeps_only_last_n_non_system_messages():
    plugin = TruncatorPlugin(keep_last_n=2)
    messages = [{"role": "user", "content": str(i)} for i in range(5)]
    result = await plugin.apply(BeforeModelCall(messages=messages, tools=[]))
    assert result.messages == [{"role": "user", "content": "3"}, {"role": "user", "content": "4"}]


async def test_always_keeps_system_messages():
    plugin = TruncatorPlugin(keep_last_n=1)
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "0"},
        {"role": "user", "content": "1"},
        {"role": "user", "content": "2"},
    ]
    result = await plugin.apply(BeforeModelCall(messages=messages, tools=[]))
    assert result.messages == [{"role": "system", "content": "sys"}, {"role": "user", "content": "2"}]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/plugins/context/ -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

Create `src/conic/plugins/context/__init__.py` (empty).

Create `src/conic/plugins/context/system_prompt.py`:
```python
from conic.core.messages import BeforeModelCall


class SystemPromptPlugin:
    def __init__(self, prompt: str):
        self._prompt = prompt

    def register(self, bus) -> None:
        bus.on("before_model_call", self.apply)

    async def apply(self, ctx: BeforeModelCall) -> BeforeModelCall | None:
        if ctx.messages and ctx.messages[0].get("role") == "system":
            return None
        return BeforeModelCall(
            messages=[{"role": "system", "content": self._prompt}, *ctx.messages],
            tools=ctx.tools,
        )
```

Create `src/conic/plugins/context/truncator.py`:
```python
from conic.core.messages import BeforeModelCall


class TruncatorPlugin:
    def __init__(self, keep_last_n: int):
        self._keep_last_n = keep_last_n

    def register(self, bus) -> None:
        bus.on("before_model_call", self.apply)

    async def apply(self, ctx: BeforeModelCall) -> BeforeModelCall | None:
        non_system = [m for m in ctx.messages if m.get("role") != "system"]
        if len(non_system) <= self._keep_last_n:
            return None
        system = [m for m in ctx.messages if m.get("role") == "system"]
        kept = non_system[-self._keep_last_n:]
        return BeforeModelCall(messages=[*system, *kept], tools=ctx.tools)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/plugins/context/ -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/conic/plugins/context/__init__.py src/conic/plugins/context/system_prompt.py src/conic/plugins/context/truncator.py tests/plugins/context/__init__.py tests/plugins/context/test_system_prompt.py tests/plugins/context/test_truncator.py
git commit -m "feat: add SystemPromptPlugin and TruncatorPlugin"
```

---

### Task 13: Token estimation, TokenBudgetPlugin, SummarizerPlugin

**Files:**
- Create: `src/conic/core/tokencount.py`
- Create: `src/conic/plugins/context/token_budget.py`
- Create: `src/conic/plugins/context/summarizer.py`
- Test: `tests/core/test_tokencount.py`
- Test: `tests/plugins/context/test_token_budget.py`
- Test: `tests/plugins/context/test_summarizer.py`

**Interfaces:**
- Consumes: `BeforeModelCall`, `SummarizeRequest`, `SummarizeResult`, `ModelRequest`, `ModelResponse` from `conic.core.messages` (Task 2); `MessageBus` (Task 3)
- Produces: `estimate_tokens(messages: list[dict]) -> int`; `class TokenBudgetPlugin` with `__init__(self, budget_tokens: int)`, `register(self, bus) -> None`, `async apply(self, ctx: BeforeModelCall) -> BeforeModelCall | None`; `class SummarizerPlugin` with `__init__(self, keep_recent: int = 5)`, `register(self, bus) -> None`, `async summarize(self, req: SummarizeRequest) -> SummarizeResult`

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_tokencount.py`:
```python
from conic.core.tokencount import estimate_tokens


def test_empty_messages_has_zero_tokens():
    assert estimate_tokens([]) == 0


def test_longer_content_yields_more_tokens():
    short = [{"role": "user", "content": "hi"}]
    long = [{"role": "user", "content": "hi " * 1000}]
    assert estimate_tokens(long) > estimate_tokens(short)
```

Create `tests/plugins/context/test_token_budget.py`:
```python
from conic.core.bus import MessageBus
from conic.core.messages import BeforeModelCall, SummarizeResult
from conic.plugins.context.token_budget import TokenBudgetPlugin


async def test_passes_through_when_under_budget():
    plugin = TokenBudgetPlugin(budget_tokens=10_000)
    bus = MessageBus()
    plugin.register(bus)
    ctx = BeforeModelCall(messages=[{"role": "user", "content": "hi"}], tools=[])
    result = await plugin.apply(ctx)
    assert result is None


async def test_requests_summary_when_over_budget():
    plugin = TokenBudgetPlugin(budget_tokens=1)
    bus = MessageBus()

    async def fake_summarizer(req):
        return SummarizeResult(messages=[{"role": "system", "content": "summary"}])

    bus.on_request("summarize", fake_summarizer)
    plugin.register(bus)

    ctx = BeforeModelCall(messages=[{"role": "user", "content": "hi " * 100}], tools=["schema"])
    result = await plugin.apply(ctx)
    assert result.messages == [{"role": "system", "content": "summary"}]
    assert result.tools == ["schema"]
```

Create `tests/plugins/context/test_summarizer.py`:
```python
from conic.core.bus import MessageBus
from conic.core.messages import ModelResponse, SummarizeRequest
from conic.plugins.context.summarizer import SummarizerPlugin


async def test_summarize_keeps_recent_messages_and_replaces_older_ones_with_summary():
    plugin = SummarizerPlugin(keep_recent=1)
    bus = MessageBus()

    async def fake_model_request(msg):
        return ModelResponse(text="summary of earlier turns", tool_calls=[], raw_message={})

    bus.on_request("model_request", fake_model_request)
    plugin.register(bus)

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old-1"},
        {"role": "assistant", "content": "old-2"},
        {"role": "user", "content": "recent"},
    ]
    result = await plugin.summarize(SummarizeRequest(messages=messages, budget_tokens=1))

    assert result.messages[0] == {"role": "system", "content": "sys"}
    assert "summary of earlier turns" in result.messages[1]["content"]
    assert result.messages[2] == {"role": "user", "content": "recent"}


async def test_summarize_returns_input_unchanged_when_nothing_to_summarize():
    plugin = SummarizerPlugin(keep_recent=5)
    bus = MessageBus()
    plugin.register(bus)
    messages = [{"role": "user", "content": "only one"}]
    result = await plugin.summarize(SummarizeRequest(messages=messages, budget_tokens=1))
    assert result.messages == messages
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_tokencount.py tests/plugins/context/test_token_budget.py tests/plugins/context/test_summarizer.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

Create `src/conic/core/tokencount.py`:
```python
def estimate_tokens(messages: list[dict]) -> int:
    total_chars = sum(len(str(m.get("content", ""))) for m in messages)
    return total_chars // 4
```

Create `src/conic/plugins/context/token_budget.py`:
```python
from conic.core.messages import BeforeModelCall, SummarizeRequest
from conic.core.tokencount import estimate_tokens


class TokenBudgetPlugin:
    def __init__(self, budget_tokens: int):
        self._budget_tokens = budget_tokens
        self._bus = None

    def register(self, bus) -> None:
        self._bus = bus
        bus.on("before_model_call", self.apply)

    async def apply(self, ctx: BeforeModelCall) -> BeforeModelCall | None:
        if estimate_tokens(ctx.messages) <= self._budget_tokens:
            return None
        result = await self._bus.request(
            "summarize", SummarizeRequest(messages=ctx.messages, budget_tokens=self._budget_tokens)
        )
        return BeforeModelCall(messages=result.messages, tools=ctx.tools)
```

Create `src/conic/plugins/context/summarizer.py`:
```python
from conic.core.messages import ModelRequest, SummarizeRequest, SummarizeResult


class SummarizerPlugin:
    def __init__(self, keep_recent: int = 5):
        self._keep_recent = keep_recent
        self._bus = None

    def register(self, bus) -> None:
        self._bus = bus
        bus.on_request("summarize", self.summarize)

    async def summarize(self, req: SummarizeRequest) -> SummarizeResult:
        system = [m for m in req.messages if m.get("role") == "system"]
        rest = [m for m in req.messages if m.get("role") != "system"]
        to_summarize = rest[: -self._keep_recent] if self._keep_recent else rest
        recent = rest[-self._keep_recent:] if self._keep_recent else []

        if not to_summarize:
            return SummarizeResult(messages=req.messages)

        transcript = "\n".join(f"{m.get('role')}: {m.get('content')}" for m in to_summarize)
        prompt = [
            {
                "role": "system",
                "content": "Summarize the following conversation history concisely, preserving key facts and decisions.",
            },
            {"role": "user", "content": transcript},
        ]
        response = await self._bus.request("model_request", ModelRequest(messages=prompt, tools=[]))
        summary_message = {"role": "system", "content": f"[Earlier conversation summary]\n{response.text}"}
        return SummarizeResult(messages=[*system, summary_message, *recent])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_tokencount.py tests/plugins/context/test_token_budget.py tests/plugins/context/test_summarizer.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/conic/core/tokencount.py src/conic/plugins/context/token_budget.py src/conic/plugins/context/summarizer.py tests/core/test_tokencount.py tests/plugins/context/test_token_budget.py tests/plugins/context/test_summarizer.py
git commit -m "feat: add token budget check and on-demand history summarization"
```

---

### Task 14: PermissionPolicyPlugin and StepLimitPlugin

**Files:**
- Create: `src/conic/plugins/policy/__init__.py` (empty)
- Create: `src/conic/plugins/policy/permission.py`
- Create: `src/conic/plugins/policy/step_limit.py`
- Test: `tests/plugins/policy/test_permission.py`
- Test: `tests/plugins/policy/test_step_limit.py`

**Interfaces:**
- Consumes: `ToolCall`, `ToolCallSpec`, `StepStart` from `conic.core.messages` (Task 2); `AbortTurn` from `conic.core.errors` (Task 2)
- Produces: `class PermissionPolicyPlugin` with `register(self, bus) -> None`, `async check(self, ctx: ToolCall) -> None`; `class StepLimitPlugin` with `__init__(self, max_steps: int)`, `register(self, bus) -> None`, `async check(self, msg: StepStart) -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/plugins/policy/__init__.py` (empty), then `tests/plugins/policy/test_permission.py`:
```python
from conic.core.messages import ToolCall, ToolCallSpec
from conic.plugins.policy.permission import PermissionPolicyPlugin


async def test_v1_allows_every_tool_call():
    plugin = PermissionPolicyPlugin()
    ctx = ToolCall(call=ToolCallSpec(id="1", name="bash", args={"command": "ls"}))
    result = await plugin.check(ctx)
    assert result is None
```

Create `tests/plugins/policy/test_step_limit.py`:
```python
import pytest

from conic.core.errors import AbortTurn
from conic.core.messages import StepStart
from conic.plugins.policy.step_limit import StepLimitPlugin


async def test_allows_steps_under_the_limit():
    plugin = StepLimitPlugin(max_steps=3)
    await plugin.check(StepStart(step_index=0))
    await plugin.check(StepStart(step_index=2))


async def test_aborts_when_step_index_reaches_limit():
    plugin = StepLimitPlugin(max_steps=3)
    with pytest.raises(AbortTurn):
        await plugin.check(StepStart(step_index=3))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/plugins/policy/ -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

Create `src/conic/plugins/policy/__init__.py` (empty).

Create `src/conic/plugins/policy/permission.py`:
```python
from conic.core.messages import ToolCall


class PermissionPolicyPlugin:
    def register(self, bus) -> None:
        bus.on("before_tool_call", self.check)

    async def check(self, ctx: ToolCall) -> None:
        return None
```

Create `src/conic/plugins/policy/step_limit.py`:
```python
from conic.core.errors import AbortTurn
from conic.core.messages import StepStart


class StepLimitPlugin:
    def __init__(self, max_steps: int):
        self._max_steps = max_steps

    def register(self, bus) -> None:
        bus.on("step_start", self.check)

    async def check(self, msg: StepStart) -> None:
        if msg.step_index >= self._max_steps:
            raise AbortTurn(f"exceeded max steps ({self._max_steps})")
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/plugins/policy/ -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/conic/plugins/policy/__init__.py src/conic/plugins/policy/permission.py src/conic/plugins/policy/step_limit.py tests/plugins/policy/__init__.py tests/plugins/policy/test_permission.py tests/plugins/policy/test_step_limit.py
git commit -m "feat: add PermissionPolicyPlugin (v1 allow-all) and StepLimitPlugin"
```

---

### Task 15: ReactLoopPlugin

**Files:**
- Create: `src/conic/plugins/loops/__init__.py` (empty)
- Create: `src/conic/plugins/loops/react_loop.py`
- Test: `tests/plugins/loops/test_react_loop.py`

**Interfaces:**
- Consumes: everything in `conic.core.messages` (Task 2); `MessageBus` (Task 3); `SessionHandle` shape from `conic.services.storage` (Task 9, used structurally/duck-typed here — tests use an in-memory fake)
- Produces: `class ReactLoopPlugin` with `__init__(self, storage_handle, tool_schemas: list[dict], tool_payload_map: dict[str, type])`, `register(self, bus) -> None`, `async handle_user_input(self, msg: UserInput) -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/plugins/loops/__init__.py` (empty), then `tests/plugins/loops/test_react_loop.py`:
```python
from dataclasses import dataclass

import pytest

from conic.core.bus import MessageBus
from conic.core.errors import AbortTurn
from conic.core.messages import (
    AssistantMessage, ModelRequest, ModelResponse, ToolCallResult, ToolCallSpec, UserInput,
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
    bus.on("assistant_message", lambda msg: received.append(msg.text) or None)

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
    bus.on("step_start", lambda msg: steps.append(msg.step_index) or None)

    await bus.emit("user_input", UserInput(text="run ls"))

    assert steps == [0, 1]
    tool_messages = [m for m in handle.messages if m.get("role") == "tool"]
    assert tool_messages == [{"role": "tool", "tool_call_id": "call_1", "content": "ran ls"}]


async def test_abort_turn_from_a_hook_emits_error_and_stops_the_loop():
    handle = FakeStorageHandle()
    responses = [ModelResponse(text="unreachable", tool_calls=[], raw_message={})]
    bus, loop = make_loop(handle, responses)

    async def always_abort(msg):
        raise AbortTurn("blocked by policy")

    bus.on("step_start", always_abort)

    errors = []
    bus.on("error", lambda msg: errors.append(str(msg.exc)) or None)

    await bus.emit("user_input", UserInput(text="hello"))

    assert errors == ["blocked by policy"]
    assert not any(m.get("role") == "assistant" for m in handle.messages)


async def test_turn_end_emitted_after_final_assistant_message():
    handle = FakeStorageHandle()
    responses = [ModelResponse(text="hi", tool_calls=[], raw_message={})]
    bus, loop = make_loop(handle, responses)

    order = []
    bus.on("assistant_message", lambda msg: order.append("assistant_message") or None)
    bus.on("turn_end", lambda msg: order.append("turn_end") or None)

    await bus.emit("user_input", UserInput(text="hello"))

    assert order == ["assistant_message", "turn_end"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/plugins/loops/test_react_loop.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

Create `src/conic/plugins/loops/__init__.py` (empty).

Create `src/conic/plugins/loops/react_loop.py`:
```python
from conic.core.errors import AbortTurn
from conic.core.messages import (
    AssistantMessage, BeforeModelCall, Error, ModelRequest, ModelResponse,
    StepStart, ToolCall, ToolCallResult, TurnEnd, TurnStart, UserInput,
)


class ReactLoopPlugin:
    def __init__(self, storage_handle, tool_schemas: list[dict], tool_payload_map: dict[str, type]):
        self._storage = storage_handle
        self._tool_schemas = tool_schemas
        self._tool_payload_map = tool_payload_map
        self._bus = None

    def register(self, bus) -> None:
        self._bus = bus
        bus.on("user_input", self.handle_user_input)

    async def handle_user_input(self, msg: UserInput) -> None:
        bus = self._bus
        self._storage.append_message({"role": "user", "content": msg.text})
        await bus.emit("turn_start", TurnStart())

        step_index = 0
        try:
            while True:
                await bus.emit("step_start", StepStart(step_index=step_index))
                step_index += 1

                history = self._storage.load_history()
                ctx = await bus.emit(
                    "before_model_call", BeforeModelCall(messages=history, tools=self._tool_schemas)
                )

                response: ModelResponse = await bus.request(
                    "model_request", ModelRequest(messages=ctx.messages, tools=ctx.tools)
                )
                response = await bus.emit("model_response", response)

                if not response.tool_calls:
                    out = await bus.emit("assistant_message", AssistantMessage(text=response.text or ""))
                    self._storage.append_message({"role": "assistant", "content": out.text})
                    break

                self._storage.append_message(response.raw_message)
                for call in response.tool_calls:
                    call_ctx = await bus.emit("before_tool_call", ToolCall(call=call))
                    payload_cls = self._tool_payload_map[call_ctx.call.name]
                    payload = payload_cls(**call_ctx.call.args)
                    result: ToolCallResult = await bus.request("tool_call", payload)
                    result = await bus.emit("tool_result", result)
                    content = result.output if result.error is None else f"Error: {result.error}"
                    self._storage.append_message(
                        {"role": "tool", "tool_call_id": call_ctx.call.id, "content": content}
                    )
        except AbortTurn as exc:
            await bus.emit("error", Error(exc=exc))
            return

        await bus.emit("turn_end", TurnEnd())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/plugins/loops/test_react_loop.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/conic/plugins/loops/__init__.py src/conic/plugins/loops/react_loop.py tests/plugins/loops/__init__.py tests/plugins/loops/test_react_loop.py
git commit -m "feat: add ReactLoopPlugin driving the turn/step agent loop"
```

---

### Task 16: PluginManager

**Files:**
- Create: `src/conic/core/manager.py`
- Test: `tests/core/test_manager.py`

**Interfaces:**
- Consumes: `MessageBus` (Task 3); `SessionScope` (Task 10); `StorageService` shape (Task 9, duck-typed via fake in tests); `infer_payload_type` (Task 3)
- Produces: `@dataclass PluginSet(tool_classes: tuple[type, ...], backend: object, context_plugins: tuple[object, ...], policy_plugins: tuple[object, ...], summarizer: object, loop_factory: Callable[[object, list[dict], dict[str, type]], object])`; `class PluginManager` with `__init__(self, storage, plugin_set: PluginSet)`, `start_session(self, channel: str, native_id: str, channel_plugin_factory: Callable[[], object]) -> SessionScope`, `stop_session(self, scope: SessionScope) -> None`

- [ ] **Step 1: Write the failing test**

Create `tests/core/test_manager.py`:
```python
from dataclasses import dataclass
from pathlib import Path

from conic.core.manager import PluginManager, PluginSet
from conic.services.storage import StorageService


@dataclass
class FakeCall:
    command: str


class FakeToolPlugin:
    llm_name = "bash"
    schema = {"type": "function", "function": {"name": "bash"}}

    def __init__(self, workspace_dir: str):
        self.workspace_dir = workspace_dir

    def register(self, bus):
        bus.on_request("tool_call", self.execute)

    async def execute(self, call: FakeCall):
        from conic.core.messages import ToolCallResult
        return ToolCallResult(output=f"ran {call.command} in {self.workspace_dir}")


class FakeBackend:
    def register(self, bus):
        bus.on_request("model_request", self.complete)

    async def complete(self, msg):
        from conic.core.messages import ModelResponse
        return ModelResponse(text="ack", tool_calls=[], raw_message={"role": "assistant"})


class FakeContextPlugin:
    def register(self, bus):
        bus.on("before_model_call", self.apply)

    async def apply(self, ctx):
        return None


class FakePolicyPlugin:
    def register(self, bus):
        bus.on("step_start", self.check)

    async def check(self, msg):
        return None


class FakeSummarizer:
    def register(self, bus):
        bus.on_request("summarize", self.summarize)

    async def summarize(self, req):
        from conic.core.messages import SummarizeResult
        return SummarizeResult(messages=req.messages)


class FakeChannelPlugin:
    def __init__(self):
        self.received = []

    def register(self, bus):
        bus.on("assistant_message", self.on_assistant_message)

    async def on_assistant_message(self, msg):
        self.received.append(msg.text)


def make_manager(tmp_path):
    from conic.plugins.loops.react_loop import ReactLoopPlugin

    storage = StorageService(
        db_path=str(tmp_path / "conic.duckdb"),
        workspace_root=str(tmp_path / "workspace"),
        default_model="test-model",
    )
    storage.startup()
    plugin_set = PluginSet(
        tool_classes=(FakeToolPlugin,),
        backend=FakeBackend(),
        context_plugins=(FakeContextPlugin(),),
        policy_plugins=(FakePolicyPlugin(),),
        summarizer=FakeSummarizer(),
        loop_factory=lambda handle, schemas, payload_map: ReactLoopPlugin(handle, schemas, payload_map),
    )
    return storage, PluginManager(storage, plugin_set)


async def test_start_session_assembles_a_working_bus(tmp_path):
    storage, manager = make_manager(tmp_path)
    channel_plugin = FakeChannelPlugin()

    scope = manager.start_session(
        channel="discord", native_id="1", channel_plugin_factory=lambda: channel_plugin
    )

    from conic.core.messages import UserInput
    await scope.bus.emit("user_input", UserInput(text="hi"))

    assert channel_plugin.received == ["ack"]
    assert Path(scope.row.workspace_dir) == tmp_path / "workspace" / "discord" / "1"
    storage.shutdown()


def test_stop_session_marks_row_as_ended(tmp_path):
    storage, manager = make_manager(tmp_path)
    scope = manager.start_session(
        channel="discord", native_id="2", channel_plugin_factory=lambda: FakeChannelPlugin()
    )

    manager.stop_session(scope)

    reloaded = storage.get_or_create(channel="discord", native_id="2")
    assert reloaded.status == "ended"
    storage.shutdown()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/core/test_manager.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'conic.core.manager'`

- [ ] **Step 3: Write minimal implementation**

Create `src/conic/core/manager.py`:
```python
from dataclasses import dataclass
from typing import Callable

from conic.core.bus import MessageBus, infer_payload_type
from conic.core.session import SessionScope


@dataclass
class PluginSet:
    tool_classes: tuple[type, ...]
    backend: object
    context_plugins: tuple[object, ...]
    policy_plugins: tuple[object, ...]
    summarizer: object
    loop_factory: Callable[[object, list[dict], dict[str, type]], object]


class PluginManager:
    def __init__(self, storage, plugin_set: PluginSet):
        self._storage = storage
        self._plugin_set = plugin_set

    def start_session(
        self, channel: str, native_id: str, channel_plugin_factory: Callable[[], object]
    ) -> SessionScope:
        row = self._storage.get_or_create(channel=channel, native_id=native_id)
        bus = MessageBus()

        for tool_cls in self._plugin_set.tool_classes:
            tool_cls(workspace_dir=row.workspace_dir).register(bus)

        self._plugin_set.backend.register(bus)
        for ctx_plugin in self._plugin_set.context_plugins:
            ctx_plugin.register(bus)
        for policy in self._plugin_set.policy_plugins:
            policy.register(bus)
        self._plugin_set.summarizer.register(bus)

        tool_schemas = [cls.schema for cls in self._plugin_set.tool_classes]
        tool_payload_map = {
            cls.llm_name: infer_payload_type(cls.execute) for cls in self._plugin_set.tool_classes
        }
        handle = self._storage.handle_for(row)
        self._plugin_set.loop_factory(handle, tool_schemas, tool_payload_map).register(bus)

        channel_plugin_factory().register(bus)

        return SessionScope(bus=bus, row=row)

    def stop_session(self, scope: SessionScope) -> None:
        self._storage.handle_for(scope.row).set_status("ended")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/core/test_manager.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/conic/core/manager.py tests/core/test_manager.py
git commit -m "feat: add PluginManager assembling a per-session MessageBus from a channel-agnostic PluginSet"
```

---

### Task 17: config.py

**Files:**
- Create: `src/conic/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `class ConfigError(Exception)`; `@dataclass Config(discord_bot_token: str, openrouter_api_key: str, openrouter_model: str, workspace_root: str, duckdb_path: str, max_steps_per_turn: int, context_token_budget: int, truncate_keep_last_n: int)`; `load_config(env: dict[str, str] | None = None) -> Config`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_config.py`:
```python
import pytest

from conic.config import Config, ConfigError, load_config


def test_load_config_applies_defaults():
    env = {"DISCORD_BOT_TOKEN": "d-token", "OPENROUTER_API_KEY": "or-key"}
    config = load_config(env)
    assert config == Config(
        discord_bot_token="d-token",
        openrouter_api_key="or-key",
        openrouter_model="anthropic/claude-sonnet-4.5",
        workspace_root="./workspace",
        duckdb_path="./data/conic.duckdb",
        max_steps_per_turn=25,
        context_token_budget=50000,
        truncate_keep_last_n=40,
    )


def test_load_config_reads_overrides():
    env = {
        "DISCORD_BOT_TOKEN": "d-token",
        "OPENROUTER_API_KEY": "or-key",
        "OPENROUTER_MODEL": "openai/gpt-4o",
        "MAX_STEPS_PER_TURN": "10",
    }
    config = load_config(env)
    assert config.openrouter_model == "openai/gpt-4o"
    assert config.max_steps_per_turn == 10


def test_load_config_raises_when_discord_token_missing():
    with pytest.raises(ConfigError, match="DISCORD_BOT_TOKEN"):
        load_config({"OPENROUTER_API_KEY": "or-key"})


def test_load_config_raises_when_openrouter_key_missing():
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        load_config({"DISCORD_BOT_TOKEN": "d-token"})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'conic.config'`

- [ ] **Step 3: Write minimal implementation**

Create `src/conic/config.py`:
```python
import os
from dataclasses import dataclass


class ConfigError(Exception):
    pass


@dataclass
class Config:
    discord_bot_token: str
    openrouter_api_key: str
    openrouter_model: str
    workspace_root: str
    duckdb_path: str
    max_steps_per_turn: int
    context_token_budget: int
    truncate_keep_last_n: int


def load_config(env: dict[str, str] | None = None) -> Config:
    env = env if env is not None else os.environ

    def required(key: str) -> str:
        value = env.get(key)
        if not value:
            raise ConfigError(f"missing required environment variable: {key}")
        return value

    return Config(
        discord_bot_token=required("DISCORD_BOT_TOKEN"),
        openrouter_api_key=required("OPENROUTER_API_KEY"),
        openrouter_model=env.get("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.5"),
        workspace_root=env.get("WORKSPACE_ROOT", "./workspace"),
        duckdb_path=env.get("DUCKDB_PATH", "./data/conic.duckdb"),
        max_steps_per_turn=int(env.get("MAX_STEPS_PER_TURN", "25")),
        context_token_budget=int(env.get("CONTEXT_TOKEN_BUDGET", "50000")),
        truncate_keep_last_n=int(env.get("TRUNCATE_KEEP_LAST_N", "40")),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/conic/config.py tests/test_config.py
git commit -m "feat: add environment-variable configuration loader"
```

---

### Task 18: plugins/registry.py

**Files:**
- Create: `src/conic/plugins/registry.py`
- Test: `tests/plugins/test_registry.py`

**Interfaces:**
- Consumes: `Config` (Task 17); all tool/backend/context/policy/loop plugin classes (Tasks 5-8, 11-15); `PluginSet` (Task 16)
- Produces: `build_plugin_set(config: Config) -> PluginSet`

- [ ] **Step 1: Write the failing test**

Create `tests/plugins/test_registry.py`:
```python
from conic.config import Config
from conic.plugins.backends.openrouter import OpenRouterBackendPlugin
from conic.plugins.context.summarizer import SummarizerPlugin
from conic.plugins.context.system_prompt import SystemPromptPlugin
from conic.plugins.context.token_budget import TokenBudgetPlugin
from conic.plugins.context.truncator import TruncatorPlugin
from conic.plugins.policy.permission import PermissionPolicyPlugin
from conic.plugins.policy.step_limit import StepLimitPlugin
from conic.plugins.registry import build_plugin_set
from conic.plugins.tools.bash import BashToolPlugin
from conic.plugins.tools.edit_file import EditFileToolPlugin
from conic.plugins.tools.read_file import ReadFileToolPlugin
from conic.plugins.tools.write_file import WriteFileToolPlugin


def make_config():
    return Config(
        discord_bot_token="d", openrouter_api_key="k", openrouter_model="test-model",
        workspace_root="./workspace", duckdb_path="./data/conic.duckdb",
        max_steps_per_turn=7, context_token_budget=123, truncate_keep_last_n=9,
    )


def test_build_plugin_set_wires_the_four_v1_tools():
    plugin_set = build_plugin_set(make_config())
    assert plugin_set.tool_classes == (
        BashToolPlugin, ReadFileToolPlugin, WriteFileToolPlugin, EditFileToolPlugin,
    )


def test_build_plugin_set_wires_backend_with_configured_model():
    plugin_set = build_plugin_set(make_config())
    assert isinstance(plugin_set.backend, OpenRouterBackendPlugin)
    assert plugin_set.backend.model == "test-model"


def test_build_plugin_set_wires_context_chain_with_configured_values():
    plugin_set = build_plugin_set(make_config())
    kinds = [type(p) for p in plugin_set.context_plugins]
    assert kinds == [SystemPromptPlugin, TruncatorPlugin, TokenBudgetPlugin]
    truncator = plugin_set.context_plugins[1]
    assert truncator._keep_last_n == 9
    token_budget = plugin_set.context_plugins[2]
    assert token_budget._budget_tokens == 123


def test_build_plugin_set_wires_policy_plugins_with_configured_max_steps():
    plugin_set = build_plugin_set(make_config())
    kinds = [type(p) for p in plugin_set.policy_plugins]
    assert kinds == [PermissionPolicyPlugin, StepLimitPlugin]
    step_limit = plugin_set.policy_plugins[1]
    assert step_limit._max_steps == 7


def test_build_plugin_set_wires_summarizer():
    plugin_set = build_plugin_set(make_config())
    assert isinstance(plugin_set.summarizer, SummarizerPlugin)


def test_loop_factory_produces_a_react_loop_plugin():
    from conic.plugins.loops.react_loop import ReactLoopPlugin

    plugin_set = build_plugin_set(make_config())
    loop = plugin_set.loop_factory(object(), [], {})
    assert isinstance(loop, ReactLoopPlugin)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/plugins/test_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'conic.plugins.registry'`

- [ ] **Step 3: Write minimal implementation**

Create `src/conic/plugins/registry.py`:
```python
from conic.config import Config
from conic.core.manager import PluginSet
from conic.plugins.backends.openrouter import OpenRouterBackendPlugin
from conic.plugins.context.summarizer import SummarizerPlugin
from conic.plugins.context.system_prompt import SystemPromptPlugin
from conic.plugins.context.token_budget import TokenBudgetPlugin
from conic.plugins.context.truncator import TruncatorPlugin
from conic.plugins.loops.react_loop import ReactLoopPlugin
from conic.plugins.policy.permission import PermissionPolicyPlugin
from conic.plugins.policy.step_limit import StepLimitPlugin
from conic.plugins.tools.bash import BashToolPlugin
from conic.plugins.tools.edit_file import EditFileToolPlugin
from conic.plugins.tools.read_file import ReadFileToolPlugin
from conic.plugins.tools.write_file import WriteFileToolPlugin

DEFAULT_SYSTEM_PROMPT = (
    "You are Conic, a helpful coding agent with access to bash, read_file, "
    "write_file, and edit_file tools scoped to this session's workspace directory."
)


def build_plugin_set(config: Config) -> PluginSet:
    return PluginSet(
        tool_classes=(BashToolPlugin, ReadFileToolPlugin, WriteFileToolPlugin, EditFileToolPlugin),
        backend=OpenRouterBackendPlugin(api_key=config.openrouter_api_key, model=config.openrouter_model),
        context_plugins=(
            SystemPromptPlugin(DEFAULT_SYSTEM_PROMPT),
            TruncatorPlugin(keep_last_n=config.truncate_keep_last_n),
            TokenBudgetPlugin(budget_tokens=config.context_token_budget),
        ),
        policy_plugins=(
            PermissionPolicyPlugin(),
            StepLimitPlugin(max_steps=config.max_steps_per_turn),
        ),
        summarizer=SummarizerPlugin(),
        loop_factory=lambda handle, schemas, payload_map: ReactLoopPlugin(handle, schemas, payload_map),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/plugins/test_registry.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/conic/plugins/registry.py tests/plugins/test_registry.py
git commit -m "feat: add static plugin registry wiring the v1 plugin set from config"
```

---

### Task 19: DiscordThreadPlugin

**Files:**
- Create: `src/conic/plugins/channels/__init__.py`, `src/conic/plugins/channels/discord/__init__.py` (empty)
- Create: `src/conic/plugins/channels/discord/adapter.py`
- Test: `tests/plugins/channels/discord/test_adapter.py`

**Interfaces:**
- Consumes: `AssistantMessage`, `Error` from `conic.core.messages` (Task 2)
- Produces: `class DiscordThreadPlugin` with `__init__(self, thread)`, `register(self, bus) -> None`, `async on_assistant_message(self, msg: AssistantMessage) -> None`, `async on_error(self, msg: Error) -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/plugins/channels/__init__.py`, `tests/plugins/channels/discord/__init__.py` (empty), then `tests/plugins/channels/discord/test_adapter.py`:
```python
from conic.core.bus import MessageBus
from conic.core.messages import AssistantMessage, Error
from conic.plugins.channels.discord.adapter import DiscordThreadPlugin


class FakeThread:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


async def test_forwards_assistant_message_to_thread():
    thread = FakeThread()
    plugin = DiscordThreadPlugin(thread)
    bus = MessageBus()
    plugin.register(bus)

    await bus.emit("assistant_message", AssistantMessage(text="hello"))

    assert thread.sent == ["hello"]


async def test_forwards_error_to_thread_with_marker():
    thread = FakeThread()
    plugin = DiscordThreadPlugin(thread)
    bus = MessageBus()
    plugin.register(bus)

    await bus.emit("error", Error(exc=ValueError("boom")))

    assert len(thread.sent) == 1
    assert "boom" in thread.sent[0]


async def test_chunks_messages_longer_than_discord_limit():
    thread = FakeThread()
    plugin = DiscordThreadPlugin(thread)
    bus = MessageBus()
    plugin.register(bus)

    long_text = "x" * 4500
    await bus.emit("assistant_message", AssistantMessage(text=long_text))

    assert len(thread.sent) == 3
    assert all(len(chunk) <= 2000 for chunk in thread.sent)
    assert "".join(thread.sent) == long_text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/plugins/channels/discord/test_adapter.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

Create `src/conic/plugins/channels/__init__.py` and `src/conic/plugins/channels/discord/__init__.py` (both empty).

Create `src/conic/plugins/channels/discord/adapter.py`:
```python
from conic.core.messages import AssistantMessage, Error

DISCORD_MESSAGE_LIMIT = 2000


class DiscordThreadPlugin:
    def __init__(self, thread):
        self._thread = thread

    def register(self, bus) -> None:
        bus.on("assistant_message", self.on_assistant_message)
        bus.on("error", self.on_error)

    async def on_assistant_message(self, msg: AssistantMessage) -> None:
        await self._send(msg.text)

    async def on_error(self, msg: Error) -> None:
        await self._send(f"⚠️ {msg.exc}")

    async def _send(self, text: str) -> None:
        for i in range(0, len(text), DISCORD_MESSAGE_LIMIT):
            await self._thread.send(text[i: i + DISCORD_MESSAGE_LIMIT])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/plugins/channels/discord/test_adapter.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/conic/plugins/channels/__init__.py src/conic/plugins/channels/discord/__init__.py src/conic/plugins/channels/discord/adapter.py tests/plugins/channels/__init__.py tests/plugins/channels/discord/__init__.py tests/plugins/channels/discord/test_adapter.py
git commit -m "feat: add DiscordThreadPlugin forwarding assistant replies/errors to a thread"
```

---

### Task 20: DiscordGateway

**Files:**
- Create: `src/conic/plugins/channels/discord/gateway.py`
- Test: `tests/plugins/channels/discord/test_gateway.py`

**Interfaces:**
- Consumes: `PluginManager` (Task 16); `StorageService` (Task 9); `DiscordThreadPlugin` (Task 19); `UserInput` (Task 2); `Gateway` protocol shape (Task 10)
- Produces: `class DiscordGateway` with `name = "discord"`, `__init__(self, bot_token: str, plugin_manager, storage)`, `async start(self) -> None`, `async stop(self) -> None`, plus independently-testable methods `async resume_active_sessions(self) -> None`, `async handle_message(self, thread_id: int, text: str) -> None`, `async handle_start_command(self, guild_channel, respond) -> None`, `async handle_stop_command(self, thread_id: int, archive) -> None`

To keep this unit-testable without a live Discord connection, the plan factors the routing/session-lifecycle logic (which this task tests) out of the raw `discord.py` event wiring (constructed in `__init__`/`start`, not separately tested — verified manually per Task 21).

- [ ] **Step 1: Write the failing tests**

Create `tests/plugins/channels/discord/test_gateway.py`:
```python
from conic.plugins.channels.discord.gateway import DiscordGateway
from conic.services.storage import StorageService


class FakePluginManagerRecorder:
    def __init__(self):
        self.started: list[tuple[str, str]] = []
        self.stopped: list[object] = []

    def start_session(self, channel, native_id, channel_plugin_factory):
        from conic.core.bus import MessageBus
        from conic.core.session import SessionScope
        from conic.services.storage import SessionRow
        from datetime import datetime, timezone

        self.started.append((channel, native_id))
        channel_plugin_factory()  # exercise the closure like the real PluginManager does
        row = SessionRow(
            session_key=f"{channel}:{native_id}", channel=channel, native_id=native_id,
            workspace_dir="/tmp", model="m", status="active", created_at=datetime.now(timezone.utc),
        )
        return SessionScope(bus=MessageBus(), row=row)

    def stop_session(self, scope):
        self.stopped.append(scope)


def make_storage(tmp_path):
    storage = StorageService(
        db_path=str(tmp_path / "conic.duckdb"),
        workspace_root=str(tmp_path / "workspace"),
        default_model="test-model",
    )
    storage.startup()
    return storage


async def test_resume_active_sessions_rebuilds_scope_for_each_active_row(tmp_path):
    storage = make_storage(tmp_path)
    storage.get_or_create(channel="discord", native_id="111")
    manager = FakePluginManagerRecorder()
    gateway = DiscordGateway(bot_token="t", plugin_manager=manager, storage=storage)

    async def fake_fetch_thread(native_id: str):
        return object()

    await gateway.resume_active_sessions(fetch_thread=fake_fetch_thread)

    assert manager.started == [("discord", "111")]
    assert 111 in gateway._sessions
    storage.shutdown()


async def test_handle_message_routes_to_known_session():
    manager = FakePluginManagerRecorder()
    gateway = DiscordGateway(bot_token="t", plugin_manager=manager, storage=None)
    scope = manager.start_session("discord", "222", lambda: object())
    gateway._sessions[222] = scope

    received = []
    from conic.core.messages import UserInput
    scope.bus.on("user_input", lambda msg: received.append(msg.text) or None)

    await gateway.handle_message(thread_id=222, text="hello")

    assert received == ["hello"]


async def test_handle_message_ignores_unknown_thread():
    manager = FakePluginManagerRecorder()
    gateway = DiscordGateway(bot_token="t", plugin_manager=manager, storage=None)

    await gateway.handle_message(thread_id=999, text="hello")  # must not raise


async def test_handle_start_command_registers_new_session():
    manager = FakePluginManagerRecorder()
    gateway = DiscordGateway(bot_token="t", plugin_manager=manager, storage=None)

    responses = []

    async def fake_create_thread():
        class FakeThread:
            id = 333
        return FakeThread()

    async def fake_respond(text: str):
        responses.append(text)

    await gateway.handle_start_command(create_thread=fake_create_thread, respond=fake_respond)

    assert manager.started == [("discord", "333")]
    assert 333 in gateway._sessions
    assert responses  # a confirmation was sent


async def test_handle_stop_command_removes_session_and_calls_stop_session():
    manager = FakePluginManagerRecorder()
    gateway = DiscordGateway(bot_token="t", plugin_manager=manager, storage=None)
    scope = manager.start_session("discord", "444", lambda: object())
    gateway._sessions[444] = scope

    archived = []

    async def fake_archive():
        archived.append(True)

    await gateway.handle_stop_command(thread_id=444, archive=fake_archive)

    assert 444 not in gateway._sessions
    assert manager.stopped == [scope]
    assert archived == [True]


async def test_handle_stop_command_on_unknown_thread_is_a_noop():
    manager = FakePluginManagerRecorder()
    gateway = DiscordGateway(bot_token="t", plugin_manager=manager, storage=None)

    async def fake_archive():
        raise AssertionError("should not be called")

    await gateway.handle_stop_command(thread_id=555, archive=fake_archive)  # must not raise
    assert manager.stopped == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/plugins/channels/discord/test_gateway.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'conic.plugins.channels.discord.gateway'`

- [ ] **Step 3: Write minimal implementation**

Create `src/conic/plugins/channels/discord/gateway.py`:
```python
from typing import Awaitable, Callable

import discord
from discord import app_commands

from conic.core.messages import UserInput
from conic.plugins.channels.discord.adapter import DiscordThreadPlugin


class DiscordGateway:
    name = "discord"

    def __init__(self, bot_token: str, plugin_manager, storage):
        self._token = bot_token
        self._plugin_manager = plugin_manager
        self._storage = storage
        self._sessions: dict[int, object] = {}

        intents = discord.Intents.default()
        intents.message_content = True
        self._client = discord.Client(intents=intents)
        self._tree = app_commands.CommandTree(self._client)
        self._register_discord_wiring()

    def _register_discord_wiring(self) -> None:
        @self._tree.command(name="agent_start", description="Start a new agent session in a thread")
        async def agent_start(interaction: discord.Interaction) -> None:
            async def create_thread():
                return await interaction.channel.create_thread(
                    name="agent-session", type=discord.ChannelType.public_thread
                )

            async def respond(text: str) -> None:
                await interaction.response.send_message(text, ephemeral=True)

            await self.handle_start_command(create_thread=create_thread, respond=respond)

        @self._tree.command(name="agent_stop", description="Stop the agent session in this thread")
        async def agent_stop(interaction: discord.Interaction) -> None:
            async def archive() -> None:
                await interaction.channel.edit(archived=True, locked=True)

            await self.handle_stop_command(thread_id=interaction.channel.id, archive=archive)
            await interaction.response.send_message("Session stopped.", ephemeral=True)

        @self._client.event
        async def on_ready() -> None:
            await self._tree.sync()

            async def fetch_thread(native_id: str):
                return await self._client.fetch_channel(int(native_id))

            await self.resume_active_sessions(fetch_thread=fetch_thread)

        @self._client.event
        async def on_message(message: discord.Message) -> None:
            if message.author.bot:
                return
            await self.handle_message(thread_id=message.channel.id, text=message.content)

    async def start(self) -> None:
        await self._client.start(self._token)

    async def stop(self) -> None:
        await self._client.close()

    async def resume_active_sessions(self, fetch_thread: Callable[[str], Awaitable[object]]) -> None:
        for row in self._storage.active_sessions(channel="discord"):
            thread = await fetch_thread(row.native_id)
            scope = self._plugin_manager.start_session(
                channel="discord",
                native_id=row.native_id,
                channel_plugin_factory=lambda t=thread: DiscordThreadPlugin(t),
            )
            self._sessions[int(row.native_id)] = scope

    async def handle_message(self, thread_id: int, text: str) -> None:
        scope = self._sessions.get(thread_id)
        if scope is None:
            return
        await scope.bus.emit("user_input", UserInput(text=text))

    async def handle_start_command(
        self, create_thread: Callable[[], Awaitable[object]], respond: Callable[[str], Awaitable[None]]
    ) -> None:
        thread = await create_thread()
        scope = self._plugin_manager.start_session(
            channel="discord",
            native_id=str(thread.id),
            channel_plugin_factory=lambda: DiscordThreadPlugin(thread),
        )
        self._sessions[thread.id] = scope
        await respond(f"Started session in thread {thread.id}")

    async def handle_stop_command(self, thread_id: int, archive: Callable[[], Awaitable[None]]) -> None:
        scope = self._sessions.pop(thread_id, None)
        if scope is None:
            return
        self._plugin_manager.stop_session(scope)
        await archive()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/plugins/channels/discord/test_gateway.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/conic/plugins/channels/discord/gateway.py tests/plugins/channels/discord/test_gateway.py
git commit -m "feat: add DiscordGateway (connection, slash commands, session resumption, message routing)"
```

---

### Task 21: main.py wiring

**Files:**
- Create: `main.py` (replace existing placeholder content)
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `load_config` (Task 17), `StorageService` (Task 9), `PluginManager` (Task 16), `build_plugin_set` (Task 18), `DiscordGateway` (Task 20)
- Produces: `build_app(env: dict[str, str] | None = None) -> tuple[StorageService, DiscordGateway]`, `async main() -> None`

- [ ] **Step 1: Write the failing test**

Create `tests/test_main.py`:
```python
from main import build_app


def test_build_app_wires_storage_and_gateway_without_connecting(tmp_path):
    env = {
        "DISCORD_BOT_TOKEN": "d-token",
        "OPENROUTER_API_KEY": "or-key",
        "DUCKDB_PATH": str(tmp_path / "conic.duckdb"),
        "WORKSPACE_ROOT": str(tmp_path / "workspace"),
    }
    storage, gateway = build_app(env)
    try:
        assert gateway.name == "discord"
        row = storage.get_or_create(channel="discord", native_id="smoke-test")
        assert row.status == "active"
    finally:
        storage.shutdown()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_main.py -v`
Expected: FAIL (either `ModuleNotFoundError` or `AttributeError: module 'main' has no attribute 'build_app'`, since `main.py` is still the PyCharm placeholder)

- [ ] **Step 3: Write minimal implementation**

Replace `main.py`:
```python
import asyncio

from conic.config import load_config
from conic.core.manager import PluginManager
from conic.plugins.channels.discord.gateway import DiscordGateway
from conic.plugins.registry import build_plugin_set
from conic.services.storage import StorageService


def build_app(env: dict[str, str] | None = None) -> tuple[StorageService, DiscordGateway]:
    config = load_config(env)
    storage = StorageService(config.duckdb_path, config.workspace_root, config.openrouter_model)
    storage.startup()
    plugin_manager = PluginManager(storage, build_plugin_set(config))
    gateway = DiscordGateway(config.discord_bot_token, plugin_manager, storage)
    return storage, gateway


async def main() -> None:
    storage, gateway = build_app()
    try:
        await gateway.start()
    finally:
        storage.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_main.py -v`
Expected: PASS (1 test)

- [ ] **Step 5: Run the full test suite**

Run: `uv run pytest -v`
Expected: PASS (all tests across every task)

- [ ] **Step 6: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "feat: wire storage, plugin manager, and Discord gateway together in main.py"
```

---

### Task 22: Manual end-to-end verification

This task has no automated steps — `discord.py` and OpenRouter cannot be driven in an offline test suite (per spec §12). Perform these checks against a real Discord test server and a real OpenRouter API key before considering v1 done.

- [ ] **Step 1: Set required environment variables**

Set `DISCORD_BOT_TOKEN` and `OPENROUTER_API_KEY` (e.g. in a local `.env` loaded by your shell, or exported directly). Confirm `.env`-style secrets files are covered by `.gitignore`.

- [ ] **Step 2: Start the bot**

Run: `uv run python main.py`
Expected: process starts, logs in, no exceptions; slash commands sync.

- [ ] **Step 3: Start a session**

In the Discord test server, run `/agent_start` in a channel. Expected: a new thread is created and the bot confirms with an ephemeral message.

- [ ] **Step 4: Exercise the tool loop**

In the new thread, ask the bot to run a shell command and read/write a file (e.g. "list the files in your workspace, then create a file named notes.txt with the text 'hello'"). Expected: the bot's reply reflects a multi-step tool loop (bash + write_file), and `notes.txt` exists under `<WORKSPACE_ROOT>/discord/<thread_id>/`.

- [ ] **Step 5: Verify workspace sandboxing**

Ask the bot to read a file outside its workspace (e.g. "read the file at ../../../etc/passwd"). Expected: the bot reports a tool error, not a crash, and no data from outside the workspace is returned.

- [ ] **Step 6: Verify restart resumption**

Stop the process (Ctrl+C) without running `/agent_stop`, restart it (`uv run python main.py`), then send another message in the same thread. Expected: the bot responds with awareness of the earlier conversation (loaded from DuckDB), without needing another `/agent_start`.

- [ ] **Step 7: Stop the session**

Run `/agent_stop` in the thread. Expected: confirmation message, thread gets archived/locked, and a restart afterward does **not** resume that thread.

- [ ] **Step 8: Record results**

Note any deviations from the expected behavior above as follow-up bugs; do not mark v1 complete until all seven checks pass.
