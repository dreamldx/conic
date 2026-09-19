import ipaddress
import json
from urllib.parse import urlsplit

import aiohttp


class ProviderResponseError(Exception):
    pass


def validate_web_url(url: str) -> str | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return f"invalid URL: {url!r}"
    if parts.scheme not in ("http", "https"):
        return f"unsupported URL scheme (only http/https allowed): {url!r}"
    if not parts.hostname:
        return f"invalid URL: {url!r}"
    if parts.username or parts.password:
        return f"credentialed URLs are not allowed: {url!r}"
    try:
        ipaddress.ip_address(parts.hostname)
    except ValueError:
        return None
    return f"IP-literal URLs are not allowed: {url!r}"


async def http_post_json(
    session_factory,
    url: str,
    payload: dict,
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, dict, str]:
    timeout_config = aiohttp.ClientTimeout(total=timeout)
    async with session_factory(timeout=timeout_config) as session, session.post(
        url, json=payload, headers=headers
    ) as response:
        text = await response.text()
        try:
            body = await response.json()
        except aiohttp.ContentTypeError as exc:
            raise ProviderResponseError(f"provider returned non-JSON response: {text[:200]}") from exc
        except json.JSONDecodeError as exc:
            raise ProviderResponseError(f"provider returned invalid JSON: {text[:200]}") from exc
        if not isinstance(body, dict):
            raise ProviderResponseError(f"provider returned unexpected JSON type: {type(body).__name__}")
        return response.status, body, text
