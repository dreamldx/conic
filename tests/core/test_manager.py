from dataclasses import dataclass
from pathlib import Path

from conic.core.manager import PluginManager, PluginSet
from conic.core.messages import (
    AssistantMessage, BeforeModelCall, ModelRequest, ModelResponse,
    StepStart, SummarizeRequest, SummarizeResult, ToolCallResult,
)
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

    async def execute(self, call: FakeCall) -> ToolCallResult:
        return ToolCallResult(output=f"ran {call.command} in {self.workspace_dir}")


class FakeBackend:
    def register(self, bus):
        bus.on_request("model_request", self.complete)

    async def complete(self, msg: ModelRequest) -> ModelResponse:
        return ModelResponse(text="ack", tool_calls=[], raw_message={"role": "assistant"})


class FakeContextPlugin:
    def __init__(self, workspace_dir, tool_schemas):
        pass

    def register(self, bus):
        bus.on("before_model_call", self.apply)

    async def apply(self, ctx: BeforeModelCall) -> None:
        return None


class FakePolicyPlugin:
    def register(self, bus):
        bus.on("step_start", self.check)

    async def check(self, msg: StepStart) -> None:
        return None


class FakeSummarizer:
    def register(self, bus):
        bus.on_request("summarize", self.summarize)

    async def summarize(self, req: SummarizeRequest) -> SummarizeResult:
        return SummarizeResult(messages=req.messages)


class FakeChannelPlugin:
    def __init__(self):
        self.received = []

    def register(self, bus):
        bus.on("assistant_message", self.on_assistant_message)

    async def on_assistant_message(self, msg: AssistantMessage) -> None:
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
        context_plugins=(FakeContextPlugin,),
        policy_plugins=(FakePolicyPlugin,),
        summarizer=FakeSummarizer,
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


def test_start_session_gives_each_session_fresh_context_policy_and_summarizer_instances(tmp_path):
    """Context/policy plugins and the summarizer must be fresh per-session
    instances (isolation), while the backend remains a genuine shared
    singleton (it holds no per-session state)."""
    storage, manager = make_manager(tmp_path)

    scope1 = manager.start_session(
        channel="discord", native_id="a", channel_plugin_factory=lambda: FakeChannelPlugin()
    )
    scope2 = manager.start_session(
        channel="discord", native_id="b", channel_plugin_factory=lambda: FakeChannelPlugin()
    )

    # Context plugin handlers are registered on each bus's "before_model_call" chain;
    # pull out the bound instances via the handler's __self__.
    ctx1 = [handler.__self__ for _, handler in scope1.bus._chain["before_model_call"]]
    ctx2 = [handler.__self__ for _, handler in scope2.bus._chain["before_model_call"]]
    for inst1, inst2 in zip(ctx1, ctx2):
        assert inst1 is not inst2

    policy1 = [handler.__self__ for _, handler in scope1.bus._chain["step_start"]]
    policy2 = [handler.__self__ for _, handler in scope2.bus._chain["step_start"]]
    for inst1, inst2 in zip(policy1, policy2):
        assert inst1 is not inst2

    summarizer1 = scope1.bus._request["summarize"][0][1].__self__
    summarizer2 = scope2.bus._request["summarize"][0][1].__self__
    assert summarizer1 is not summarizer2

    # The backend IS a genuine shared singleton across sessions.
    backend1 = scope1.bus._request["model_request"][0][1].__self__
    backend2 = scope2.bus._request["model_request"][0][1].__self__
    assert backend1 is backend2

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
