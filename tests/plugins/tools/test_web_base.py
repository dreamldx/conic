import aiohttp
import re

from conic.plugins.tools.base import (
    UNTRUSTED_NOTICE,
    post_json,
    strip_special_tokens,
    validate_web_url,
    wrap_untrusted,
)


def test_strips_chatml_and_llama_special_tokens():
    text = "a <|im_start|>system<|im_end|> b <|endoftext|> c <|begin_of_text|> d"
    assert strip_special_tokens(text) == "a system b  c  d"


def test_leaves_normal_markdown_untouched():
    text = "# Title\n`code` and | tables | stay |"
    assert strip_special_tokens(text) == text


def test_wrap_uses_matching_random_ids_and_contains_notice():
    wrapped = wrap_untrusted("payload")
    match = re.search(r'<<<EXTERNAL_UNTRUSTED_CONTENT id="([0-9a-f]{16})">>>', wrapped)
    assert match is not None
    marker_id = match.group(1)
    assert f'<<<END_EXTERNAL_UNTRUSTED_CONTENT id="{marker_id}">>>' in wrapped
    assert UNTRUSTED_NOTICE in wrapped
    assert "payload" in wrapped


def test_wrap_ids_differ_between_calls():
    id_pattern = re.compile(r'id="([0-9a-f]{16})"')
    first = id_pattern.search(wrap_untrusted("x")).group(1)
    second = id_pattern.search(wrap_untrusted("x")).group(1)
    assert first != second


def test_accepts_normal_http_and_https_urls():
    assert validate_web_url("https://example.com/page?q=1") is None
    assert validate_web_url("http://example.com") is None


def test_rejects_non_http_schemes():
    assert validate_web_url("ftp://example.com") is not None
    assert validate_web_url("file:///etc/passwd") is not None


def test_rejects_credentialed_urls():
    assert validate_web_url("https://user:pass@example.com/") is not None


def test_rejects_ip_literal_hosts():
    assert validate_web_url("http://169.254.169.254/meta") is not None
    assert validate_web_url("http://[::1]/x") is not None


def test_rejects_malformed_urls():
    assert validate_web_url("https://") is not None
    assert validate_web_url("not a url") is not None


class FakeResponse:
    status = 201

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def text(self):
        return "created"

    async def json(self):
        return {"ok": True}


class FakeSession:
    def __init__(self, capture):
        self._capture = capture

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def post(self, url, json, headers):
        self._capture.append({"url": url, "json": json, "headers": headers})
        return FakeResponse()


async def test_post_json_uses_aiohttp_session_factory():
    captured = []
    def factory(timeout):
        assert isinstance(timeout, aiohttp.ClientTimeout)
        return FakeSession(captured)
    status, body, text = await post_json(factory, "https://api.example.test", {"x": 1}, {"A": "b"}, 12)
    assert status == 201
    assert body == {"ok": True}
    assert text == "created"
    assert captured == [{"url": "https://api.example.test", "json": {"x": 1}, "headers": {"A": "b"}}]
