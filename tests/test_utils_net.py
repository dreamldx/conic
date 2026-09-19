import json

import aiohttp
import pytest

from conic.utils.net import ProviderResponseError, http_post_json, validate_web_url


class FakeResponse:
    def __init__(self, payload=None, text="created", json_error=None, status=201):
        self._payload = payload if payload is not None else {"ok": True}
        self._text = text
        self._json_error = json_error
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def text(self):
        return self._text

    async def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class FakeSession:
    def __init__(self, capture=None, response=None):
        self._capture = capture
        self._response = response or FakeResponse()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def post(self, url, json, headers):
        if self._capture is not None:
            self._capture.append({"url": url, "json": json, "headers": headers})
        return self._response


async def test_http_post_json_uses_aiohttp_session_factory():
    captured = []

    def factory(timeout):
        assert isinstance(timeout, aiohttp.ClientTimeout)
        return FakeSession(capture=captured)

    status, body, text = await http_post_json(factory, "https://api.example.test", {"x": 1}, {"A": "b"}, 12)
    assert status == 201
    assert body == {"ok": True}
    assert text == "created"
    assert captured == [{"url": "https://api.example.test", "json": {"x": 1}, "headers": {"A": "b"}}]


async def test_http_post_json_rejects_non_json_response():
    def factory(timeout):
        response = FakeResponse(text="<html>blocked</html>", json_error=aiohttp.ContentTypeError(None, ()))
        return FakeSession(response=response)

    with pytest.raises(ProviderResponseError, match="non-JSON"):
        await http_post_json(factory, "https://api.example.test", {}, {}, 12)


async def test_http_post_json_rejects_invalid_json_response():
    def factory(timeout):
        response = FakeResponse(text="{nope", json_error=json.JSONDecodeError("bad", "{nope", 0))
        return FakeSession(response=response)

    with pytest.raises(ProviderResponseError, match="invalid JSON"):
        await http_post_json(factory, "https://api.example.test", {}, {}, 12)


async def test_http_post_json_rejects_non_object_json_response():
    def factory(timeout):
        return FakeSession(response=FakeResponse(payload=[]))

    with pytest.raises(ProviderResponseError, match="unexpected JSON type"):
        await http_post_json(factory, "https://api.example.test", {}, {}, 12)


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
