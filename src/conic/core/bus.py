import asyncio
import inspect
from typing import Any, Callable, get_type_hints

from loguru import logger

from conic.types.errors import (
    DuplicateMailboxError,
    DuplicateResponderError,
    NoResponderError,
    UnknownMailboxError,
)


def infer_payload_type(handler: Callable) -> type:
    hints = get_type_hints(handler)
    params = [p for p in inspect.signature(handler).parameters if p != "self"]
    return hints[params[0]]


class MessageBus:
    def __init__(self) -> None:
        self._chain: dict[str, list[tuple[type, Callable]]] = {}
        self._request: dict[str, list[tuple[type, Callable]]] = {}
        self._mailbox_types: dict[str, type] = {}
        self._mailbox_items: dict[str, list[Any]] = {}
        self._mailbox_events: dict[str, asyncio.Event] = {}
        self._closed = False
        self._closed_event = asyncio.Event()

    def on(self, type_name: str, handler: Callable) -> None:
        payload_cls = infer_payload_type(handler)
        self._chain.setdefault(type_name, []).append((payload_cls, handler))

    async def emit(self, type_name: str, payload: Any) -> Any:
        for payload_cls, handler in self._chain.get(type_name, []):
            if isinstance(payload, payload_cls):
                result = await handler(payload)
                if result is not None:
                    payload = result
            else:
                logger.info(
                    "chain interrupted: topic={} handler={} expected={} got={}",
                    type_name, getattr(handler, "__qualname__", repr(handler)),
                    payload_cls.__name__, type(payload).__name__,
                )
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

    def _mailbox_type(self, name: str) -> type:
        try:
            return self._mailbox_types[name]
        except KeyError:
            raise UnknownMailboxError(f"no mailbox registered for {name!r}") from None

    async def post(self, name: str, payload: Any) -> None:
        if self._closed:
            logger.warning(
                "post after close: mailbox={} payload={}", name, type(payload).__name__
            )
            return
        payload_cls = self._mailbox_type(name)
        if not isinstance(payload, payload_cls):
            raise TypeError(
                f"mailbox {name!r} expects {payload_cls!r}, got {type(payload)!r}"
            )
        self._mailbox_items[name].append(payload)
        self._mailbox_events[name].set()

    async def drain(self, name: str) -> list[Any]:
        self._mailbox_type(name)
        items = self._mailbox_items[name]
        self._mailbox_items[name] = []
        self._mailbox_events[name].clear()
        return items

    def create_mailbox(self, name: str, payload_type: type) -> None:
        if name in self._mailbox_types:
            raise DuplicateMailboxError(
                f"mailbox {name!r} already has a consumer bound"
            )
        self._mailbox_types[name] = payload_type
        self._mailbox_items[name] = []
        self._mailbox_events[name] = asyncio.Event()

    async def wait_multiply_mailbox(self, *names: str) -> None:
        for name in names:
            self._mailbox_type(name)  # validates registration, raises if unknown
        events = [self._mailbox_events[name] for name in names]
        if self._closed or any(event.is_set() for event in events):
            return

        waiters = [asyncio.ensure_future(event.wait()) for event in events]
        closed_waiter = asyncio.ensure_future(self._closed_event.wait())
        pending = [*waiters, closed_waiter]
        try:
            await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def close(self) -> None:
        self._closed = True
        self._closed_event.set()
