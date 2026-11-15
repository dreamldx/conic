import asyncio
from types import SimpleNamespace

from conic.core.bus import MessageBus
from conic.plugins import meta
from conic.plugins.models import openrouter
from conic.plugins.models.nano_gpt import (
    NanoGptModelPlugin,
)
from conic.services.models import ModelCatalogEntry
from conic.services.storage import DEFAULT_MODEL_CONTEXT_LENGTH
from conic.types.messages import ModelRequest


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
    plugin = NanoGptModelPlugin(api_key="k", model="m", client=client, **kwargs)
    plugin.register(MessageBus())
    return plugin


def keys(completions):
    return [c["extra_headers"]["Idempotency-Key"] for c in completions.calls]


def test_register_does_not_handle_switch_model_or_catalog_section():
    registered = []

    class SpyBus:
        def on_request(self, event, handler):
            registered.append(event)

        def on_chain(self, event, handler):
            registered.append(event)

    NanoGptModelPlugin(api_key="k", model="m", client=object()).register(SpyBus())

    assert meta.ModelRequestEvent in registered
    assert meta.SwitchModelRequestEvent not in registered
    assert meta.BuildSystemPromptEvent not in registered


async def test_blocking_request_sends_idempotency_key_header():
    completions = RecordingCompletions()
    plugin = make_plugin(completions)

    await plugin.complete(ModelRequest(messages=[], tools=[]))
    await plugin.complete(ModelRequest(messages=[], tools=[]))

    first, second = keys(completions)
    assert first and second and first != second


async def test_streaming_retry_reuses_the_same_idempotency_key():
    completions = RecordingCompletions(hangs=1)
    plugin = make_plugin(completions, stream_timeouts=(0.01, 0.5))

    result = await plugin.complete(ModelRequest(messages=[], tools=[], stream_updates=True))

    assert result.text == "ok"
    attempt_keys = keys(completions)
    assert len(attempt_keys) == 2
    assert attempt_keys[0] == attempt_keys[1]


def test_default_timeouts_match_openrouter():
    plugin = NanoGptModelPlugin(api_key="k", model="m", client=object())

    assert plugin._stream_timeouts == openrouter.STREAM_ATTEMPT_TIMEOUTS
    assert plugin._blocking_timeout == openrouter.BLOCKING_TIMEOUT


def test_default_client_targets_nano_gpt_base_url():
    plugin = NanoGptModelPlugin(api_key="k", model="m")

    assert str(plugin._client.base_url).rstrip("/") == "https://api.nano-gpt.com/api/v1"


def test_context_length_comes_from_catalog_lookup_and_falls_back_to_default():
    entry = ModelCatalogEntry(slug="m", context_length=1000000)
    found = {}
    NanoGptModelPlugin(
        api_key="k", model="m", client=object(),
        catalog_lookup=lambda query: [entry], global_variables=found,
    )
    missing = {}
    NanoGptModelPlugin(
        api_key="k", model="m", client=object(),
        catalog_lookup=lambda query: [], global_variables=missing,
    )

    assert found["model_context_length"] == 1000000
    assert missing["model_context_length"] == DEFAULT_MODEL_CONTEXT_LENGTH
