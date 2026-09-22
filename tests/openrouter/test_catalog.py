import asyncio

import pytest

from conic.openrouter.catalog import (
    OPENROUTER_MODELS_URL,
    fetch_openrouter_models,
    run_periodic_sync,
)


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def raise_for_status(self):
        pass

    async def json(self):
        return self._payload


class FakeSession:
    def __init__(self, payload, capture=None):
        self._payload = payload
        self._capture = capture

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def get(self, url, headers):
        if self._capture is not None:
            self._capture.append({"url": url, "headers": headers})
        return FakeResponse(self._payload)


def session_factory(payload, capture=None):
    def factory(timeout):
        return FakeSession(payload, capture=capture)
    return factory


async def test_maps_id_context_length_and_tool_support():
    payload = {
        "data": [
            {"id": "deepseek/deepseek-v4-flash-0731", "context_length": 128000, "supported_parameters": ["tools"]},
            {"id": "openai/gpt-audio", "context_length": 32000, "supported_parameters": ["temperature"]},
        ]
    }
    result = await fetch_openrouter_models("test-key", session_factory=session_factory(payload))
    assert result[0]["id"] == "deepseek/deepseek-v4-flash-0731"
    assert result[0]["context_length"] == 128000
    assert result[0]["supports_tools"] is True
    assert result[1]["supports_tools"] is False


async def test_maps_name_description_pricing_and_modalities():
    payload = {
        "data": [
            {
                "id": "deepseek/deepseek-v4-flash-0731",
                "name": "DeepSeek: DeepSeek V4 Flash 0731",
                "description": "A sparse mixture-of-experts model.",
                "pricing": {"prompt": "0.00000004", "completion": "0.00000064"},
                "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
                "supported_parameters": ["tools", "reasoning"],
            }
        ]
    }
    result = await fetch_openrouter_models("test-key", session_factory=session_factory(payload))
    assert result == [
        {
            "id": "deepseek/deepseek-v4-flash-0731",
            "name": "DeepSeek: DeepSeek V4 Flash 0731",
            "description": "A sparse mixture-of-experts model.",
            "context_length": 0,
            "supports_tools": True,
            "pricing_prompt": 0.00000004,
            "pricing_completion": 0.00000064,
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "supported_parameters": ["tools", "reasoning"],
        }
    ]


async def test_missing_fields_default_safely():
    payload = {"data": [{"id": "some/model"}]}
    result = await fetch_openrouter_models("test-key", session_factory=session_factory(payload))
    assert result == [
        {
            "id": "some/model",
            "name": "",
            "description": "",
            "context_length": 0,
            "supports_tools": False,
            "pricing_prompt": 0.0,
            "pricing_completion": 0.0,
            "input_modalities": [],
            "output_modalities": [],
            "supported_parameters": [],
        }
    ]


async def test_empty_data_is_empty_list():
    result = await fetch_openrouter_models("test-key", session_factory=session_factory({"data": []}))
    assert result == []


async def test_sends_bearer_auth_header_and_correct_url():
    capture = []
    payload = {"data": []}
    await fetch_openrouter_models("secret-key", session_factory=session_factory(payload, capture=capture))
    assert capture == [{"url": OPENROUTER_MODELS_URL, "headers": {"Authorization": "Bearer secret-key"}}]


async def test_no_api_key_sends_no_auth_header():
    capture = []
    payload = {"data": []}
    await fetch_openrouter_models("", session_factory=session_factory(payload, capture=capture))
    assert capture == [{"url": OPENROUTER_MODELS_URL, "headers": {}}]


class FakeStorage:
    def __init__(self):
        self.saved: list[list] = []

    def save_model_catalog(self, entries):
        self.saved.append(entries)


def sleep_n_times_then_cancel(n):
    calls = []

    async def fake_sleep(seconds):
        calls.append(seconds)
        if len(calls) >= n:
            raise asyncio.CancelledError

    return fake_sleep, calls


async def test_run_periodic_sync_syncs_immediately_before_the_first_sleep():
    storage = FakeStorage()
    payload = {"data": [{"id": "a/b", "context_length": 1000, "supported_parameters": ["tools"]}]}
    fake_sleep, calls = sleep_n_times_then_cancel(1)

    with pytest.raises(asyncio.CancelledError):
        await run_periodic_sync(storage, "key", session_factory=session_factory(payload), sleep=fake_sleep)

    assert len(storage.saved) == 1
    assert storage.saved[0][0].id == "a/b"
    assert storage.saved[0][0].context_length == 1000
    assert calls == [3600]


async def test_run_periodic_sync_resyncs_on_every_interval():
    storage = FakeStorage()
    payload = {"data": [{"id": "a/b"}]}
    fake_sleep, calls = sleep_n_times_then_cancel(3)

    with pytest.raises(asyncio.CancelledError):
        await run_periodic_sync(
            storage, "key", interval_seconds=1, session_factory=session_factory(payload), sleep=fake_sleep
        )

    assert len(storage.saved) == 3
    assert calls == [1, 1, 1]


async def test_run_periodic_sync_keeps_looping_after_a_failed_sync():
    storage = FakeStorage()

    def failing_session_factory(timeout):
        raise RuntimeError("boom")

    fake_sleep, calls = sleep_n_times_then_cancel(2)

    with pytest.raises(asyncio.CancelledError):
        await run_periodic_sync(storage, "key", session_factory=failing_session_factory, sleep=fake_sleep)

    assert storage.saved == []
    assert len(calls) == 2
