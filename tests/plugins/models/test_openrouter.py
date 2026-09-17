import json
from dataclasses import dataclass, field
from types import SimpleNamespace

from conic.core.bus import MessageBus
from conic.types.messages import MessageDeltaUpdate, ModelRequest
from conic.plugins import meta
from conic.plugins.models.openrouter import OpenRouterModelPlugin


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


def make_response(message, usage=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


async def test_complete_returns_text_response_with_no_tool_calls():
    message = make_message(content="hello there")
    client = FakeClient(FakeCompletions(make_response(message)))
    backend = OpenRouterModelPlugin(api_key="k", model="test-model", client=client)

    result = await backend.complete(ModelRequest(messages=[{"role": "user", "content": "hi"}], tools=[]))

    assert result.text == "hello there"
    assert result.tool_calls == []
    assert client.chat.completions.last_kwargs["model"] == "test-model"


async def test_complete_parses_tool_calls():
    fn = SimpleNamespace(name="bash", arguments=json.dumps({"command": "ls"}))
    tool_call = SimpleNamespace(id="call_1", function=fn)
    message = make_message(content=None, tool_calls=[tool_call])
    client = FakeClient(FakeCompletions(make_response(message)))
    backend = OpenRouterModelPlugin(api_key="k", model="test-model", client=client)

    result = await backend.complete(ModelRequest(messages=[], tools=[]))

    assert result.tool_calls[0].id == "call_1"
    assert result.tool_calls[0].name == "bash"
    assert result.tool_calls[0].args == {"command": "ls"}


async def test_complete_records_token_usage_into_session_variables():
    message = make_message(content="hello there")
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    client = FakeClient(FakeCompletions(make_response(message, usage=usage)))
    backend = OpenRouterModelPlugin(api_key="k", model="test-model", client=client)

    variables = {"session": {"tokens_used": 0}}
    await backend.complete(
        ModelRequest(messages=[{"role": "user", "content": "hi"}], tools=[], variables=variables)
    )

    assert variables["session"]["tokens_used"] == 15


async def test_complete_accumulates_token_usage_across_calls():
    message = make_message(content="hello there")
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    client = FakeClient(FakeCompletions(make_response(message, usage=usage)))
    backend = OpenRouterModelPlugin(api_key="k", model="test-model", client=client)

    variables = {"session": {"tokens_used": 100}}
    await backend.complete(
        ModelRequest(messages=[{"role": "user", "content": "hi"}], tools=[], variables=variables)
    )

    assert variables["session"]["tokens_used"] == 115


async def test_complete_without_usage_or_session_does_not_raise():
    message = make_message(content="hello there")
    client = FakeClient(FakeCompletions(make_response(message)))
    backend = OpenRouterModelPlugin(api_key="k", model="test-model", client=client)

    result = await backend.complete(ModelRequest(messages=[], tools=[]))

    assert result.text == "hello there"


def test_register_wires_model_request():
    backend = OpenRouterModelPlugin(
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
    backend = OpenRouterModelPlugin(api_key="k", model="m", client=client)
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
    backend = OpenRouterModelPlugin(api_key="k", model="m", client=client)
    bus = MessageBus()
    backend.register(bus)

    deltas = []

    async def on_delta(msg: MessageDeltaUpdate) -> None:
        deltas.append(msg.text_delta)

    bus.on(meta.MessageDeltaUpdateEvent, on_delta)

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
    backend = OpenRouterModelPlugin(api_key="k", model="m", client=client)
    bus = MessageBus()
    backend.register(bus)

    result = await backend.complete(ModelRequest(messages=[], tools=[], stream_updates=True))

    assert [tc.id for tc in result.tool_calls] == ["call_a", "call_b"]


async def test_streaming_complete_defaults_empty_arguments_to_empty_dict():
    chunks = [
        make_chunk(make_delta(tool_calls=[make_tool_call_delta(0, id="call_1", name="ls", arguments=None)])),
    ]
    client = FakeClient(FakeStreamingCompletions(chunks))
    backend = OpenRouterModelPlugin(api_key="k", model="m", client=client)
    bus = MessageBus()
    backend.register(bus)

    result = await backend.complete(ModelRequest(messages=[], tools=[], stream_updates=True))

    assert result.tool_calls[0].args == {}


async def test_streaming_complete_skips_chunks_with_no_choices():
    chunks = [
        SimpleNamespace(choices=[]),
        make_chunk(make_delta(content="ok")),
    ]
    client = FakeClient(FakeStreamingCompletions(chunks))
    backend = OpenRouterModelPlugin(api_key="k", model="m", client=client)
    bus = MessageBus()
    backend.register(bus)

    result = await backend.complete(ModelRequest(messages=[], tools=[], stream_updates=True))

    assert result.text == "ok"


async def test_streaming_complete_records_usage_from_final_chunk():
    usage = SimpleNamespace(prompt_tokens=20, completion_tokens=8, total_tokens=28)
    chunks = [
        make_chunk(make_delta(content="Hi")),
        SimpleNamespace(choices=[], usage=usage),
    ]
    client = FakeClient(FakeStreamingCompletions(chunks))
    backend = OpenRouterModelPlugin(api_key="k", model="m", client=client)
    bus = MessageBus()
    backend.register(bus)

    variables = {"session": {"tokens_used": 0}}
    await backend.complete(
        ModelRequest(messages=[], tools=[], stream_updates=True, variables=variables)
    )

    assert variables["session"]["tokens_used"] == 28
    assert client.chat.completions.last_kwargs["stream_options"] == {"include_usage": True}


async def test_streaming_deltas_only_reach_the_registering_bus_not_other_sessions():
    chunks = [make_chunk(make_delta(content="secret"))]
    client = FakeClient(FakeStreamingCompletions(chunks))

    backend_a = OpenRouterModelPlugin(api_key="k", model="m", client=client)
    bus_a = MessageBus()
    backend_a.register(bus_a)

    backend_b = OpenRouterModelPlugin(api_key="k", model="m", client=client)
    bus_b = MessageBus()
    backend_b.register(bus_b)

    received_on_b = []

    async def on_delta(msg: MessageDeltaUpdate) -> None:
        received_on_b.append(msg.text_delta)

    bus_b.on(meta.MessageDeltaUpdateEvent, on_delta)

    await backend_a.complete(ModelRequest(messages=[], tools=[], stream_updates=True))

    assert received_on_b == []
