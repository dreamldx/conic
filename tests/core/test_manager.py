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
