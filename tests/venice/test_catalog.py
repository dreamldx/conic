import pytest

from conic.venice.catalog import VENICE_CATALOG_URL, fetch_venice_models


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
    row = {"id": model_id, "type": "text", "owned_by": "venice.ai", "model_spec": {"name": model_id.upper()}}
    row.update(overrides)
    return row


async def test_fetch_maps_text_model():
    row = {
        "id": "llama-x",
        "type": "text",
        "owned_by": "venice.ai",
        "context_length": 100,
        "model_spec": {
            "name": "Llama X",
            "description": "d",
            "availableContextTokens": 131072,
            "offline": False,
            "pricing": {"input": {"usd": 0.5, "diem": 0.5}, "output": {"usd": 2.0, "diem": 2.0}},
            "capabilities": {
                "supportsFunctionCalling": True,
                "supportsVision": True,
                "supportsAudioInput": False,
                "supportsVideoInput": False,
                "supportsReasoning": True,
            },
        },
    }

    result = await fetch_venice_models(session_factory=session_factory([{"data": [row]}]))

    assert len(result) == 1
    entry = result[0]
    assert entry.provider == "venice"
    assert entry.slug == entry.real_model == "llama-x"
    assert entry.vendor == "venice.ai"
    assert entry.name == "Llama X"
    assert entry.description == "d"
    assert entry.context_length == 131072
    assert entry.supports_tools is True
    assert entry.pricing_prompt == pytest.approx(5e-7)
    assert entry.pricing_completion == pytest.approx(2e-6)
    assert entry.input_modalities == ["text", "image"]
    assert entry.output_modalities == ["text"]
    assert "tools" in entry.supported_parameters
    assert "reasoning" in entry.supported_parameters


async def test_fetch_falls_back_to_top_level_context_length():
    row = model_row("a", context_length=4096)

    result = await fetch_venice_models(session_factory=session_factory([{"data": [row]}]))

    assert result[0].context_length == 4096


async def test_fetch_maps_audio_and_video_input():
    row = model_row("a", model_spec={"capabilities": {"supportsAudioInput": True, "supportsVideoInput": True}})

    result = await fetch_venice_models(session_factory=session_factory([{"data": [row]}]))

    assert result[0].input_modalities == ["text", "audio", "video"]


async def test_fetch_skips_non_text_and_offline_models():
    page = {
        "data": [
            model_row("text-ok"),
            model_row("img", type="image"),
            model_row("off", model_spec={"offline": True}),
        ]
    }

    result = await fetch_venice_models(session_factory=session_factory([page]))

    assert [e.slug for e in result] == ["text-ok"]


async def test_fetch_tolerates_missing_pricing_and_capabilities():
    row = {"id": "bare", "type": "text"}

    result = await fetch_venice_models(session_factory=session_factory([{"data": [row]}]))

    entry = result[0]
    assert entry.vendor == ""
    assert entry.context_length == 0
    assert entry.pricing_prompt == 0.0
    assert entry.pricing_completion == 0.0
    assert entry.supports_tools is False
    assert entry.supported_parameters == []
    assert entry.input_modalities == ["text"]


async def test_fetch_requests_models_endpoint_without_params():
    capture = []

    await fetch_venice_models(session_factory=session_factory([{"data": [model_row("a")]}], capture))

    assert capture == [{"url": VENICE_CATALOG_URL, "params": {}}]


async def test_fetch_raises_on_http_error():
    with pytest.raises(RuntimeError, match="boom"):
        await fetch_venice_models(session_factory=session_factory([{}], error=RuntimeError("boom")))


async def test_fetch_defaults_non_numeric_price_to_zero():
    row = model_row("a", model_spec={"pricing": {"input": {"usd": "N/A"}, "output": {"usd": 2.0}}})

    result = await fetch_venice_models(session_factory=session_factory([{"data": [row]}]))

    assert [e.slug for e in result] == ["a"]
    assert result[0].pricing_prompt == 0.0
    assert result[0].pricing_completion == pytest.approx(2e-6)
