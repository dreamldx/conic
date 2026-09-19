import aiohttp

from conic.core.bus import MessageBus
from conic.plugins import meta
from conic.plugins.tools.web_search import CITE_SUFFIX, WEB_SEARCH_SECTION, WebSearchCall, WebSearchToolPlugin
from conic.plugins.tools.base import UNTRUSTED_NOTICE
from conic.types.messages import BuildSystemPrompt


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self._payload = payload or {}
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def json(self):
        return self._payload

    async def text(self):
        return str(self._payload)


class FakeSession:
    def __init__(self, payload=None, status=200, capture=None, error=None):
        self._payload = payload or {}
        self._status = status
        self._capture = capture
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def post(self, url, json, headers):
        if self._error is not None:
            raise self._error
        if self._capture is not None:
            self._capture.append({"url": url, "json": json, "headers": headers})
        return FakeResponse(self._payload, self._status)


def tavily_session_factory(payload=None, status=200, capture=None, error=None):
    def factory(timeout):
        return FakeSession(payload=payload, status=status, capture=capture, error=error)
    return factory


def make_tool(session_factory):
    return WebSearchToolPlugin(workspace_dir="/tmp/ws", api_key="tv-key", session_factory=session_factory)


RESULTS = {
    "results": [
        {"title": "Conic sections", "url": "https://example.com/a", "content": "Apollonius studied them.", "published_date": "2026-08-14"},
        {"title": "Ellipse", "url": "https://example.com/b", "content": "A closed curve."},
    ]
}


async def test_formats_results_with_header_outside_and_list_inside_boundary():
    tool = make_tool(tavily_session_factory(RESULTS))
    result = await tool.execute(WebSearchCall(query="conic history"))
    assert result.error is None
    header, rest = result.output.split("<<<EXTERNAL_UNTRUSTED_CONTENT", 1)
    assert 'Results for "conic history" (2 shown):' in header
    assert "[Conic sections](https://example.com/a)" in rest
    assert "2026-08-14" in rest
    assert "Apollonius studied them." in rest
    assert result.output.rstrip().endswith(CITE_SUFFIX)
    assert result.output.rindex(CITE_SUFFIX) > result.output.rindex("END_EXTERNAL_UNTRUSTED_CONTENT")


async def test_strips_special_tokens_from_results():
    poisoned = {"results": [{"title": "x <|im_start|>", "url": "https://e.com", "content": "y <|endoftext|>"}]}
    tool = make_tool(tavily_session_factory(poisoned))
    result = await tool.execute(WebSearchCall(query="q"))
    assert "<|im_start|>" not in result.output
    assert "<|endoftext|>" not in result.output


async def test_sends_clamped_params_and_bearer_auth():
    captured = []
    tool = make_tool(tavily_session_factory(RESULTS, capture=captured))
    await tool.execute(WebSearchCall(query="q", max_results=99, time_range="week"))
    request = captured[0]
    assert request["headers"]["Authorization"] == "Bearer tv-key"
    body = request["json"]
    assert body == {"query": "q", "max_results": 10, "search_depth": "basic", "time_range": "week"}


async def test_omits_time_range_when_empty():
    captured = []
    tool = make_tool(tavily_session_factory(RESULTS, capture=captured))
    await tool.execute(WebSearchCall(query="q"))
    body = captured[0]["json"]
    assert "time_range" not in body
    assert body["max_results"] == 5


async def test_empty_results_return_actionable_message():
    tool = make_tool(tavily_session_factory({"results": []}))
    result = await tool.execute(WebSearchCall(query="zxq"))
    assert result.error is None
    assert 'No results found for "zxq"' in result.output
    assert UNTRUSTED_NOTICE not in result.output


async def test_http_error_becomes_result_error():
    tool = make_tool(tavily_session_factory({"error": "bad key"}, status=401))
    result = await tool.execute(WebSearchCall(query="q"))
    assert result.error is not None
    assert "401" in result.error


async def test_client_failure_becomes_result_error():
    tool = make_tool(tavily_session_factory(error=aiohttp.ClientError("timed out")))
    result = await tool.execute(WebSearchCall(query="q"))
    assert result.error is not None
    assert "timed out" in result.error


async def test_contributes_web_search_section_with_turn_now_and_untrusted_rule():
    tool = make_tool(tavily_session_factory(RESULTS))
    bus = MessageBus()
    tool.register(bus)
    result = await bus.chain(meta.BuildSystemPromptEvent, BuildSystemPrompt(sections={}))
    assert result.sections["web_search"] == WEB_SEARCH_SECTION
    assert "{{ turn.now }}" in WEB_SEARCH_SECTION
    assert "if web_fetch is available" in WEB_SEARCH_SECTION
    assert "EXTERNAL_UNTRUSTED_CONTENT" in WEB_SEARCH_SECTION
