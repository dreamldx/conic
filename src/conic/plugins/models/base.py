import asyncio
import json
import uuid
from collections.abc import Callable

from loguru import logger
from openai import AsyncOpenAI

from conic.plugins import Plugin, meta
from conic.services.models import ModelCatalogEntry
from conic.services.storage import DEFAULT_MODEL_CONTEXT_LENGTH
from conic.types.messages import (
    MessageDeltaUpdate,
    ModelRequest,
    ModelResponse,
    ToolCallSpec,
)

STREAM_ATTEMPT_TIMEOUTS = (10.0, 15.0, 30.0)
BLOCKING_TIMEOUT = 30.0


class ChatCompletionModelPlugin(Plugin):
    Name = "chat_completion_base"

    def __init__(
        self,
        model: str,
        client: AsyncOpenAI,
        session_id: str | None = None,
        catalog_lookup: Callable[[str], list[ModelCatalogEntry]] | None = None,
        global_variables: dict | None = None,
        stream_timeouts: tuple[float, ...] = STREAM_ATTEMPT_TIMEOUTS,
        blocking_timeout: float = BLOCKING_TIMEOUT,
    ):
        self.model = model
        self._stream_timeouts = stream_timeouts
        self._blocking_timeout = blocking_timeout
        self._client = client
        self._bus = None
        self._session_id = session_id
        self._catalog_lookup = catalog_lookup
        self._global_variables = global_variables
        self._reload_model_context_length()

    def register(self, bus) -> None:
        self._bus = bus
        bus.on_request(meta.ModelRequestEvent, self.complete)

    def _resolve_catalog_entry(self, model_id: str) -> tuple[ModelCatalogEntry | None, str | None]:
        if self._catalog_lookup is None:
            return None, "model catalog is not available"
        matches = self._catalog_lookup(model_id)
        if not matches:
            return None, f"model not found: {model_id}"
        exact = [m for m in matches if m.slug.lower() == model_id.strip().lower()]
        candidates = exact or matches
        if len(candidates) > 1:
            names = ", ".join(m.slug for m in candidates)
            return None, f"ambiguous model: {model_id} matches {names}"
        return candidates[0], None

    def _reload_model_context_length(self, entry: ModelCatalogEntry | None = None) -> None:
        if self._global_variables is None:
            return
        if entry is None:
            entry, _ = self._resolve_catalog_entry(self.model)
        length = entry.context_length if entry and entry.context_length else DEFAULT_MODEL_CONTEXT_LENGTH
        self._global_variables["model_context_length"] = length

    async def complete(self, msg: ModelRequest) -> ModelResponse:
        idempotency_key = uuid.uuid4().hex
        try:
            if msg.stream_updates:
                return await self._complete_streaming(msg, idempotency_key)
            return await self._complete_blocking(msg, idempotency_key)
        except Exception as exc:
            logger.debug("{} request failed model={} error={}", self.Name, self.model, exc)
            raise

    def _extra_body(self) -> dict | None:
        return None

    def _extra_headers(self, idempotency_key: str) -> dict:
        return {}

    def _record_usage(self, msg: ModelRequest, usage) -> None:
        if usage is None:
            return
        session = msg.variables.get("session")
        if session is None:
            return
        session["tokens_used"] = session.get("tokens_used", 0) + usage.total_tokens
        # TODO: this tracks the session's all-time peak prompt size and never
        # comes back down. Once TokenBudgetPlugin's summarization actually
        # shrinks the history, decide whether context_usage should keep
        # meaning "historical peak" (current behavior) or get an explicit
        # reset/adjustment hook so it also reflects post-compression reality.
        session["context_usage"] = max(session.get("context_usage", 0), usage.prompt_tokens)

    async def _complete_blocking(self, msg: ModelRequest, idempotency_key: str) -> ModelResponse:
        logger.debug(
            "calling {} model={} messages={} tools={}",
            self.Name, self.model, len(msg.messages), len(msg.tools or []),
        )
        response = await asyncio.wait_for(
            self._client.chat.completions.create(
                model=self.model,
                messages=msg.messages,
                tools=msg.tools or None,
                extra_body=self._extra_body(),
                extra_headers=self._extra_headers(idempotency_key),
            ),
            self._blocking_timeout,
        )
        self._record_usage(msg, getattr(response, "usage", None))
        message = response.choices[0].message
        raw_message = message.model_dump()
        tool_calls = [
            ToolCallSpec(id=tc.id, name=tc.function.name, args=json.loads(tc.function.arguments))
            for tc in (message.tool_calls or [])
        ]
        logger.debug(
            "{} response text_len={} tool_calls={}",
            self.Name, len(message.content or ""), len(tool_calls),
        )
        return ModelResponse(text=message.content, tool_calls=tool_calls, raw_message=raw_message)

    async def _complete_streaming(self, msg: ModelRequest, idempotency_key: str) -> ModelResponse:
        last_exc: TimeoutError | None = None
        for attempt, timeout in enumerate(self._stream_timeouts, start=1):
            content_parts: list[str] = []
            try:
                return await self._stream_once(msg, timeout, content_parts, idempotency_key)
            except TimeoutError as exc:
                if content_parts:
                    raise
                logger.warning(
                    "{} stream timed out model={} attempt={}/{} timeout={}s",
                    self.Name, self.model, attempt, len(self._stream_timeouts), timeout,
                )
                last_exc = exc
        raise TimeoutError(
            f"{self.Name} stream timed out after {len(self._stream_timeouts)} attempts"
        ) from last_exc

    async def _stream_once(
        self, msg: ModelRequest, timeout: float, content_parts: list[str], idempotency_key: str
    ) -> ModelResponse:
        logger.debug(
            "calling {} (streaming) model={} messages={} tools={} timeout={}s",
            self.Name, self.model, len(msg.messages), len(msg.tools or []), timeout,
        )
        stream = await asyncio.wait_for(
            self._client.chat.completions.create(
                model=self.model,
                messages=msg.messages,
                tools=msg.tools or None,
                stream=True,
                stream_options={"include_usage": True},
                extra_body=self._extra_body(),
                extra_headers=self._extra_headers(idempotency_key),
            ),
            timeout,
        )
        tool_call_acc: dict[int, dict] = {}
        usage = None
        chunks = stream.__aiter__()
        while True:
            try:
                chunk = await asyncio.wait_for(chunks.__anext__(), timeout)
            except StopAsyncIteration:
                break
            if getattr(chunk, "usage", None):
                usage = chunk.usage
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                content_parts.append(delta.content)
                await self._bus.chain(meta.MessageDeltaUpdateEvent, MessageDeltaUpdate(text_delta=delta.content))
            for tc in (delta.tool_calls or []):
                acc = tool_call_acc.setdefault(tc.index, {"id": None, "name": None, "arguments": ""})
                if tc.id:
                    acc["id"] = tc.id
                if tc.function and tc.function.name:
                    acc["name"] = tc.function.name
                if tc.function and tc.function.arguments:
                    acc["arguments"] += tc.function.arguments

        self._record_usage(msg, usage)
        text = "".join(content_parts) or None
        tool_calls = [
            ToolCallSpec(
                id=acc["id"], name=acc["name"],
                args=json.loads(acc["arguments"]) if acc["arguments"] else {},
            )
            for _, acc in sorted(tool_call_acc.items())
        ]
        raw_message = {
            "role": "assistant",
            "content": text,
            "tool_calls": [
                {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": json.dumps(tc.args)}}
                for tc in tool_calls
            ] or None,
        }
        logger.debug(
            "{} streaming response text_len={} tool_calls={}",
            self.Name, len(text or ""), len(tool_calls),
        )
        return ModelResponse(text=text, tool_calls=tool_calls, raw_message=raw_message)
