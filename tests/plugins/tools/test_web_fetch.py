import aiohttp

from conic.core.bus import MessageBus
from conic.plugins import meta
from conic.plugins.tools.web_fetch import (
    FETCH_CITE_SUFFIX,
    WEB_FETCH_SECTION,
    WebFetchCall,
    WebFetchToolPlugin,
    build_web_fetch_schema,
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


def firecrawl_session_factory(markdown="hello page", status=200, success=True, capture=None, error=None):
    body = {"success": success, "data": {"markdown": markdown}}
    if not success:
        body["error"] = "scrape failed upstream"
    def factory(timeout):
        return FakeSession(payload=body, status=status, capture=capture, error=error)
    return factory


def make_tool(tmp_path, session_factory, max_chars=15000, **kwargs):
    return WebFetchToolPlugin(
        workspace_dir=str(tmp_path), api_key="fc-key", max_chars=max_chars,
        session_factory=session_factory, **kwargs
    )


async def test_short_content_returned_whole_inside_boundary(tmp_path):
    tool = make_tool(tmp_path, firecrawl_session_factory("short body"))
    result = await tool.execute(WebFetchCall(url="https://example.com/a"))
    assert result.error is None
    header, rest = result.output.split("<<<EXTERNAL_UNTRUSTED_CONTENT", 1)
    assert "Fetched https://example.com/a" in header
    assert "short body" in rest
    assert not (tmp_path / "web").exists()
    assert result.output.rstrip().endswith(FETCH_CITE_SUFFIX)
    assert result.output.rindex(FETCH_CITE_SUFFIX) > result.output.rindex("END_EXTERNAL_UNTRUSTED_CONTENT")


async def test_sends_fixed_firecrawl_params(tmp_path):
    captured = []
    tool = make_tool(tmp_path, firecrawl_session_factory(capture=captured))
    await tool.execute(WebFetchCall(url="https://example.com/a"))
    request = captured[0]
    assert request["headers"]["Authorization"] == "Bearer fc-key"
    body = request["json"]
    assert body == {
        "url": "https://example.com/a", "formats": ["markdown"],
        "onlyMainContent": True, "onlyCleanContent": True,
        "skipTlsVerification": True,
        "proxy": "auto", "maxAge": 172800000, "timeout": 30000,
    }


async def test_long_content_truncated_head_tail_and_spilled(tmp_path):
    long_md = "".join(f"line{i:06d}\n" for i in range(20000))
    tool = make_tool(tmp_path, firecrawl_session_factory(long_md), max_chars=15000)
    result = await tool.execute(WebFetchCall(url="https://example.com/long"))
    header = result.output.split("<<<EXTERNAL_UNTRUSTED_CONTENT", 1)[0]
    assert "kept first 11250 and last 3750" in header
    assert "use read_file with offset" in header
    spilled = list((tmp_path / "web").glob("*.md"))
    assert len(spilled) == 1
    assert spilled[0].read_text(encoding="utf-8") == long_md
    assert f"web/{spilled[0].name}" in header
    assert "...[omitted" in result.output
    assert result.output.count("line") < long_md.count("line")


async def test_spilled_file_has_special_tokens_stripped(tmp_path):
    long_md = "<|im_start|>" + ("x" * 20000)
    tool = make_tool(tmp_path, firecrawl_session_factory(long_md), max_chars=15000)
    await tool.execute(WebFetchCall(url="https://example.com/p"))
    spilled = next(iter((tmp_path / "web").glob("*.md")))
    assert "<|im_start|>" not in spilled.read_text(encoding="utf-8")


async def test_rejects_bad_urls_without_calling_provider(tmp_path):
    captured = []
    tool = make_tool(tmp_path, firecrawl_session_factory(capture=captured))
    for url in ("ftp://x.com", "http://169.254.169.254/", "https://u:p@x.com/"):
        result = await tool.execute(WebFetchCall(url=url))
        assert result.error is not None
    assert captured == []


async def test_provider_failure_becomes_result_error(tmp_path):
    tool = make_tool(tmp_path, firecrawl_session_factory(success=False))
    result = await tool.execute(WebFetchCall(url="https://example.com/x"))
    assert result.error is not None
    assert "scrape failed upstream" in result.error


async def test_http_error_and_timeout_become_result_errors(tmp_path):
    tool = make_tool(tmp_path, firecrawl_session_factory(status=500))
    result = await tool.execute(WebFetchCall(url="https://example.com/x"))
    assert result.error is not None and "500" in result.error

    tool = make_tool(tmp_path, firecrawl_session_factory(error=TimeoutError("timed out")))
    result = await tool.execute(WebFetchCall(url="https://example.com/x"))
    assert result.error is not None and "timed out" in result.error


async def test_non_json_success_response_becomes_result_error(tmp_path):
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
    tool = make_tool(tmp_path, factory)
    result = await tool.execute(WebFetchCall(url="https://example.com/x"))
    assert result.error is not None
    assert "non-JSON" in result.error


async def test_empty_markdown_is_an_error(tmp_path):
    tool = make_tool(tmp_path, firecrawl_session_factory(markdown=""))
    result = await tool.execute(WebFetchCall(url="https://example.com/x"))
    assert result.error is not None


async def test_raw_schema_has_no_prompt_param(tmp_path):
    schema = build_web_fetch_schema(include_prompt=False)
    assert "prompt" not in schema["function"]["parameters"]["properties"]
    assert WebFetchToolPlugin.schema == schema


async def test_contributes_web_fetch_section_with_conditional_wording(tmp_path):
    tool = make_tool(tmp_path, firecrawl_session_factory())
    bus = MessageBus()
    tool.register(bus)
    result = await bus.chain(meta.BuildSystemPromptEvent, BuildSystemPrompt(sections={}))
    assert result.sections["web_fetch"] == WEB_FETCH_SECTION
    assert "when it is\navailable" in WEB_FETCH_SECTION or "when it is available" in WEB_FETCH_SECTION
    assert "EXTERNAL_UNTRUSTED_CONTENT" in WEB_FETCH_SECTION


from types import SimpleNamespace


class FakeCompletions:
    def __init__(self, answer, error=None):
        self.answer = answer
        self.error = error
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        message = SimpleNamespace(content=self.answer)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def fake_summary_client(answer, error=None):
    completions = FakeCompletions(answer, error)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


async def test_summary_mode_returns_wrapped_answer_and_spills_full_text(tmp_path):
    client, completions = fake_summary_client("The price is $5.")
    tool = make_tool(
        tmp_path, firecrawl_session_factory("page about prices"),
        summary_model="fast-model", summary_client=client,
    )
    result = await tool.execute(WebFetchCall(url="https://example.com/p", prompt="what is the price?"))
    assert result.error is None
    assert "Answer from https://example.com/p" in result.output
    assert "The price is $5." in result.output
    assert "<<<EXTERNAL_UNTRUSTED_CONTENT" in result.output
    assert completions.calls[0]["model"] == "fast-model"
    assert "what is the price?" in completions.calls[0]["messages"][0]["content"]
    assert "EXTERNAL_UNTRUSTED_CONTENT" in completions.calls[0]["messages"][0]["content"]
    spilled = list((tmp_path / "web").glob("*.md"))
    assert len(spilled) == 1


async def test_summary_mode_falls_back_to_raw_on_model_failure(tmp_path):
    client, _ = fake_summary_client("", error=RuntimeError("model down"))
    tool = make_tool(
        tmp_path, firecrawl_session_factory("page body"),
        summary_model="fast-model", summary_client=client,
    )
    result = await tool.execute(WebFetchCall(url="https://example.com/p", prompt="q"))
    assert result.error is None
    assert "page body" in result.output


async def test_prompt_ignored_when_summary_model_not_configured(tmp_path):
    tool = make_tool(tmp_path, firecrawl_session_factory("raw body"))
    result = await tool.execute(WebFetchCall(url="https://example.com/p", prompt="q"))
    assert "raw body" in result.output


async def test_summary_schema_includes_optional_prompt():
    schema = build_web_fetch_schema(include_prompt=True)
    properties = schema["function"]["parameters"]["properties"]
    assert "prompt" in properties
    assert schema["function"]["parameters"]["required"] == ["url"]
