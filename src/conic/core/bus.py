import inspect
from typing import Any, Callable, get_type_hints

from conic.core.errors import DuplicateResponderError, NoResponderError


def infer_payload_type(handler: Callable) -> type:
    hints = get_type_hints(handler)
    params = [p for p in inspect.signature(handler).parameters if p != "self"]
    return hints[params[0]]


class MessageBus:
    def __init__(self) -> None:
        self._chain: dict[str, list[tuple[type, Callable]]] = {}
        self._request: dict[str, list[tuple[type, Callable]]] = {}

    def on(self, type_name: str, handler: Callable) -> None:
        payload_cls = infer_payload_type(handler)
        self._chain.setdefault(type_name, []).append((payload_cls, handler))

    async def emit(self, type_name: str, payload: Any) -> Any:
        for payload_cls, handler in self._chain.get(type_name, []):
            if isinstance(payload, payload_cls):
                result = await handler(payload)
                if result is not None:
                    payload = result
        return payload

    def on_request(self, type_name: str, handler: Callable) -> None:
        payload_cls = infer_payload_type(handler)
        bucket = self._request.setdefault(type_name, [])
        if any(existing_cls is payload_cls for existing_cls, _ in bucket):
            raise DuplicateResponderError(
                f"{type_name!r} already has a responder for payload type {payload_cls!r}"
            )
        bucket.append((payload_cls, handler))

    async def request(self, type_name: str, payload: Any) -> Any:
        for payload_cls, handler in self._request.get(type_name, []):
            if isinstance(payload, payload_cls):
                return await handler(payload)
        raise NoResponderError(
            f"no responder for topic {type_name!r} with payload type {type(payload)!r}"
        )
