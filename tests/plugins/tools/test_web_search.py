import aiohttp

from conic.core.bus import MessageBus
from conic.plugins import meta
from conic.plugins.tools.base import UNTRUSTED_NOTICE
from conic.plugins.tools.web_search import (
    CITE_SUFFIX,
    TOPIC_OPTIONS,
    WEB_SEARCH_SECTION,
    WebSearchCall,
    WebSearchToolPlugin,
)
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


async def test_sends_default_params_with_advanced_depth():
    captured = []
    tool = make_tool(tavily_session_factory(RESULTS, capture=captured))
    await tool.execute(WebSearchCall(query="q"))
    body = captured[0]["json"]
    assert body == {"query": "q", "max_results": 5, "search_depth": "advanced", "topic": "general"}
    assert "time_range" not in body


async def test_sends_clamped_params_and_all_optional_fields():
    captured = []
    tool = make_tool(tavily_session_factory(RESULTS, capture=captured))
    await tool.execute(WebSearchCall(
        query="q", max_results=99, time_range="week",
        topic="general", country="china", exact_match=True,
        include_domains=["docs.example.com"], include_answer="advanced",
    ))
    body = captured[0]["json"]
    assert body["search_depth"] == "advanced"
    assert body["topic"] == "general"
    assert body["country"] == "china"
    assert body["exact_match"] is True
    assert body["include_domains"] == ["docs.example.com"]
    assert body["include_answer"] == "advanced"


async def test_country_only_sent_with_general_topic():
    captured = []
    tool = make_tool(tavily_session_factory(RESULTS, capture=captured))
    await tool.execute(WebSearchCall(query="q", topic="news", country="china"))
    body = captured[0]["json"]
    assert "country" not in body


async def test_invalid_topic_falls_back_to_general():
    captured = []
    tool = make_tool(tavily_session_factory(RESULTS, capture=captured))
    await tool.execute(WebSearchCall(query="q", topic="invalid"))
    body = captured[0]["json"]
    assert body["topic"] == "general"


async def test_invalid_include_answer_falls_back_to_basic():
    captured = []
    tool = make_tool(tavily_session_factory(RESULTS, capture=captured))
    await tool.execute(WebSearchCall(query="q", include_answer="fast"))
    body = captured[0]["json"]
    assert body["include_answer"] == "basic"


async def test_empty_include_domains_and_answer_omitted():
    captured = []
    tool = make_tool(tavily_session_factory(RESULTS, capture=captured))
    await tool.execute(WebSearchCall(query="q"))
    body = captured[0]["json"]
    assert "include_domains" not in body
    assert "include_answer" not in body


async def test_formats_answer_before_results_when_present():
    body = {
        "results": [{"title": "X", "url": "https://x.com", "content": "data"}],
        "answer": "This is the synthesized answer.",
    }
    tool = make_tool(tavily_session_factory(body))
    result = await tool.execute(WebSearchCall(query="q"))
    assert "Answer: This is the synthesized answer." in result.output
    answer_pos = result.output.index("Answer:")
    results_pos = result.output.index("Results for")
    assert answer_pos < results_pos


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
    tool = make_tool(tavily_session_factory(error=TimeoutError("timed out")))
    result = await tool.execute(WebSearchCall(query="q"))
    assert result.error is not None
    assert "timed out" in result.error


async def test_non_json_success_response_is_error_not_empty_results():
    class HtmlResponse(FakeResponse):
        async def text(self):
            return "<html>blocked</html>"

        async def json(self):
            raise aiohttp.ContentTypeError(None, ())

    class HtmlSession(FakeSession):
        def post(self, url, json, headers):
            return HtmlResponse()

    def factory(timeout):
        return HtmlSession()
    tool = make_tool(factory)
    result = await tool.execute(WebSearchCall(query="q"))
    assert result.error is not None
    assert "non-JSON" in result.error
    assert result.output is None


async def test_contributes_web_search_section_with_turn_now_and_untrusted_rule():
    tool = make_tool(tavily_session_factory(RESULTS))
    bus = MessageBus()
    tool.register(bus)
    result = await bus.chain(meta.BuildSystemPromptEvent, BuildSystemPrompt(sections={}))
    assert result.sections["web_search"] == WEB_SEARCH_SECTION
    assert "{{ turn.now }}" in WEB_SEARCH_SECTION
    assert "if web_fetch is available" in WEB_SEARCH_SECTION
    assert "EXTERNAL_UNTRUSTED_CONTENT" in WEB_SEARCH_SECTION
    assert "`news`" in WEB_SEARCH_SECTION
    assert "`finance`" in WEB_SEARCH_SECTION
    assert "include_domains" in WEB_SEARCH_SECTION
    assert "include_answer" in WEB_SEARCH_SECTION


async def test_topic_options_are_general_news_finance():
    assert TOPIC_OPTIONS == ["general", "news", "finance"]


async def test_schema_exposes_all_new_parameters():
    schema = WebSearchToolPlugin.schema
    props = schema["function"]["parameters"]["properties"]
    assert "query" in props
    assert "max_results" in props
    assert "time_range" in props
    assert "topic" in props
    assert props["topic"]["enum"] == TOPIC_OPTIONS
    assert "country" in props
    assert "exact_match" in props
    assert "include_domains" in props
    assert props["include_domains"]["items"]["type"] == "string"
    assert "include_answer" in props
    assert props["include_answer"]["enum"] == ["basic", "advanced"]
