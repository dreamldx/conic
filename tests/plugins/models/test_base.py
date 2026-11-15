import asyncio
from types import SimpleNamespace

from conic.core.bus import MessageBus
from conic.plugins.models.base import ChatCompletionModelPlugin
from conic.types.messages import ModelRequest


class KeyedPlugin(ChatCompletionModelPlugin):
    Name = "fake_chat"

    def _extra_headers(self, idempotency_key: str) -> dict:
        return {"Idempotency-Key": idempotency_key}


class RecordingCompletions:
    def __init__(self, hangs=0):
        self.hangs = hangs
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            if len(self.calls) <= self.hangs:
                await asyncio.sleep(10)
            chunk = SimpleNamespace(
                usage=None,
                choices=[SimpleNamespace(delta=SimpleNamespace(content="ok", tool_calls=None))],
            )

            async def _iter():
                yield chunk

            return _iter()
        message = SimpleNamespace(
            content="hi", tool_calls=None,
            model_dump=lambda: {"role": "assistant", "content": "hi", "tool_calls": None},
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


def make_plugin(completions, **kwargs):
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    plugin = KeyedPlugin(model="m", client=client, **kwargs)
    plugin.register(MessageBus())
    return plugin


def keys(completions):
    return [c["extra_headers"]["Idempotency-Key"] for c in completions.calls]


async def test_complete_calls_hooks_with_one_key_per_call():
    completions = RecordingCompletions()
    plugin = make_plugin(completions)

    await plugin.complete(ModelRequest(messages=[], tools=[]))
    await plugin.complete(ModelRequest(messages=[], tools=[]))

    first, second = keys(completions)
    assert first and second and first != second


async def test_streaming_retries_reuse_the_same_key():
    completions = RecordingCompletions(hangs=1)
    plugin = make_plugin(completions, stream_timeouts=(0.01, 0.5))

    result = await plugin.complete(ModelRequest(messages=[], tools=[], stream_updates=True))

    assert result.text == "ok"
    attempt_keys = keys(completions)
    assert len(attempt_keys) == 2
    assert attempt_keys[0] == attempt_keys[1]
