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
