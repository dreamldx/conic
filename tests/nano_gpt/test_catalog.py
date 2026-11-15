import pytest

from conic.nano_gpt.catalog import NANO_GPT_CATALOG_URL, fetch_nano_gpt_models


class FakeResponse:
    def __init__(self, payload, error=None):
        self._payload = payload
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def raise_for_status(self):
        if self._error is not None:
            raise self._error

    async def json(self):
        return self._payload


class FakeSession:
    def __init__(self, pages, capture, error):
        self._pages = pages
        self._capture = capture
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def get(self, url, params=None):
        self._capture.append({"url": url, "params": dict(params or {})})
        index = len(self._capture) - 1
        return FakeResponse(self._pages[min(index, len(self._pages) - 1)], self._error)


def session_factory(pages, capture=None, error=None):
    capture = capture if capture is not None else []

    def factory(timeout):
        return FakeSession(pages, capture, error)

    return factory



def model_row(model_id, **overrides):
    row = {"id": model_id, "name": model_id.upper(), "description": "d", "context_length": 1000}
    row.update(overrides)
    return row


async def test_fetch_maps_detailed_model():
    page = {
        "data": [
            {
                "id": "openai/gpt-x",
                "name": "GPT X",
                "description": "d",
                "context_length": 128000,
                "capabilities": {"vision": True, "tool_calling": True, "audio_input": False},
                "pricing": {"prompt": 2, "completion": 10},
            }
        ]
    }

    result = await fetch_nano_gpt_models(session_factory=session_factory([page]))

    assert len(result) == 1
    entry = result[0]
    assert entry.provider == "nano_gpt"
    assert entry.slug == entry.real_model == "openai/gpt-x"
    assert entry.vendor == "openai"
    assert entry.name == "GPT X"
    assert entry.description == "d"
    assert entry.context_length == 128000
    assert entry.supports_tools is True
    assert "tools" in entry.supported_parameters
    assert entry.input_modalities == ["text", "image"]
    assert entry.output_modalities == ["text"]
    assert entry.pricing_prompt == pytest.approx(2e-6)
    assert entry.pricing_completion == pytest.approx(1e-5)


async def test_fetch_requests_detailed_listing():
    capture = []

    await fetch_nano_gpt_models(session_factory=session_factory([{"data": [model_row("a/b")]}], capture))

    assert capture == [{"url": NANO_GPT_CATALOG_URL, "params": {"detailed": "true"}}]


async def test_fetch_maps_video_audio_and_reasoning_capabilities():
    row = model_row("a/b", capabilities={"video_input": True, "audio_input": True, "reasoning": True})

    result = await fetch_nano_gpt_models(session_factory=session_factory([{"data": [row]}]))

    assert result[0].input_modalities == ["text", "video", "audio"]
    assert result[0].supports_tools is False
    assert result[0].supported_parameters == ["reasoning"]


async def test_fetch_tolerates_null_context_and_missing_pricing():
    page = {"data": [model_row("plain", context_length=None)]}

    result = await fetch_nano_gpt_models(session_factory=session_factory([page]))

    assert result[0].vendor == ""
    assert result[0].context_length == 0
    assert result[0].pricing_prompt == 0.0
    assert result[0].pricing_completion == 0.0


async def test_fetch_skips_rows_without_id():
    page = {"data": [model_row("a/b"), {"name": "no id"}]}

    result = await fetch_nano_gpt_models(session_factory=session_factory([page]))

    assert [e.slug for e in result] == ["a/b"]


async def test_fetch_raises_on_http_error():
    with pytest.raises(RuntimeError, match="boom"):
        await fetch_nano_gpt_models(session_factory=session_factory([{}], error=RuntimeError("boom")))
