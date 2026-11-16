import pytest

from conic.experiential.catalog import fetch_experiential_models


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


def model_row(slug, status="active", **overrides):
    model = {
        "slug": slug,
        "display_name": slug.upper(),
        "description": "d",
        "context_window": 1000,
        "maker": "Acme",
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "supported_params": {"tools": True},
        "status": status,
    }
    model.update(overrides)
    return {"model": model, "providers": []}


async def test_fetch_maps_a_model_to_a_catalog_entry():
    page = {
        "models": [
            {
                "model": {
                    "slug": "claude-opus-5.5",
                    "display_name": "Claude Opus 5.5",
                    "description": "d",
                    "context_window": 1000000,
                    "maker": "Anthropic",
                    "input_modalities": ["text", "image"],
                    "output_modalities": ["text"],
                    "supported_params": {"tools": True, "reasoning": True, "stop": False},
                    "status": "active",
                },
                "providers": [
                    {"input_nano_usd_per_million": 4000000000, "output_nano_usd_per_million": 20000000000}
                ],
            }
        ],
        "total": 1,
        "limit": 500,
        "offset": 0,
    }

    result = await fetch_experiential_models(session_factory=session_factory([page]))

    assert len(result) == 1
    entry = result[0]
    assert entry.provider == "experiential"
    assert entry.slug == entry.real_model == "claude-opus-5.5"
    assert entry.vendor == "anthropic"
    assert entry.name == "Claude Opus 5.5"
    assert entry.description == "d"
    assert entry.context_length == 1000000
    assert entry.supports_tools is True
    assert entry.supported_parameters == ["tools", "reasoning"]
    assert entry.input_modalities == ["text", "image"]
    assert entry.output_modalities == ["text"]
    assert entry.pricing_prompt == pytest.approx(4e-6)
    assert entry.pricing_completion == pytest.approx(2e-5)


async def test_fetch_follows_pagination_until_total_is_reached():
    capture = []
    pages = [
        {"models": [model_row("a"), model_row("b")], "total": 3},
        {"models": [model_row("c")], "total": 3},
    ]

    result = await fetch_experiential_models(session_factory=session_factory(pages, capture))

    assert [e.slug for e in result] == ["a", "b", "c"]
    assert [c["params"]["offset"] for c in capture] == [0, 2]
    assert capture[0]["params"]["limit"] == 500


async def test_fetch_stops_on_empty_page():
    capture = []
    pages = [{"models": [model_row("a")], "total": 10}, {"models": [], "total": 10}]

    result = await fetch_experiential_models(session_factory=session_factory(pages, capture))

    assert [e.slug for e in result] == ["a"]
    assert len(capture) == 2


async def test_fetch_tolerates_missing_context_window_pricing_and_providers():
    page = {"models": [model_row("a", context_window=None)], "total": 1}

    result = await fetch_experiential_models(session_factory=session_factory([page]))

    assert result[0].context_length == 0
    assert result[0].pricing_prompt == 0.0
    assert result[0].pricing_completion == 0.0


async def test_fetch_uses_first_provider_that_has_a_price():
    row = model_row("a")
    row["providers"] = [
        {},
        {"input_nano_usd_per_million": 1000000000, "output_nano_usd_per_million": 2000000000},
    ]

    result = await fetch_experiential_models(session_factory=session_factory([{"models": [row], "total": 1}]))

    assert result[0].pricing_prompt == pytest.approx(1e-6)
    assert result[0].pricing_completion == pytest.approx(2e-6)


async def test_fetch_skips_models_that_are_not_active():
    page = {"models": [model_row("a"), model_row("b", status="deprecated")], "total": 2}

    result = await fetch_experiential_models(session_factory=session_factory([page]))

    assert [e.slug for e in result] == ["a"]


async def test_fetch_raises_on_http_error():
    with pytest.raises(RuntimeError, match="boom"):
        await fetch_experiential_models(session_factory=session_factory([{}], error=RuntimeError("boom")))


async def test_fetch_keeps_paging_until_an_empty_page_when_total_is_missing():
    capture = []
    pages = [
        {"models": [model_row("a"), model_row("b")]},
        {"models": [model_row("c")]},
        {"models": []},
    ]

    result = await fetch_experiential_models(session_factory=session_factory(pages, capture))

    assert [e.slug for e in result] == ["a", "b", "c"]
    assert [c["params"]["offset"] for c in capture] == [0, 2, 3]


async def test_fetch_defaults_non_numeric_price_to_zero():
    row = model_row("a")
    row["providers"] = [{"input_nano_usd_per_million": "N/A", "output_nano_usd_per_million": 2000000000}]

    result = await fetch_experiential_models(session_factory=session_factory([{"models": [row], "total": 1}]))

    assert [e.slug for e in result] == ["a"]
    assert result[0].pricing_prompt == 0.0
    assert result[0].pricing_completion == pytest.approx(2e-6)
