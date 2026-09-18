import json

from loguru import logger
from openai import AsyncOpenAI

from conic.types.messages import MessageDeltaUpdate, ModelRequest, ModelResponse, ToolCallSpec
from conic.plugins import meta

APP_HTTP_REFERER = "https://github.com/dreamldx/conic"


class OpenRouterModelPlugin:
    def __init__(
        self,
        api_key: str,
        model: str,
        client: AsyncOpenAI | None = None,
        provider_blacklist: list[str] | None = None,
        session_id: str | None = None,
        app_name: str | None = None,
    ):
        self.model = model
        self._client = client or AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=api_key)
        self._bus = None
        self._provider_blacklist = provider_blacklist or []
        self._session_id = session_id
        self._app_name = app_name

    def register(self, bus) -> None:
        self._bus = bus
        bus.on_request(meta.ModelRequestEvent, self.complete)

    async def complete(self, msg: ModelRequest) -> ModelResponse:
        try:
            if msg.stream_updates:
                return await self._complete_streaming(msg)
            return await self._complete_blocking(msg)
        except Exception as exc:
            logger.debug("openrouter request failed model={} error={}", self.model, exc)
            raise

    def _extra_body(self) -> dict | None:
        if not self._provider_blacklist:
            return None
        return {"provider": {"ignore": self._provider_blacklist}}

    def _extra_headers(self) -> dict:
        headers: dict = {"HTTP-Referer": APP_HTTP_REFERER}
        if self._session_id:
            headers["x-session-id"] = self._session_id
        if self._app_name:
            headers["X-OpenRouter-Title"] = self._app_name
        return headers

    def _record_usage(self, msg: ModelRequest, usage) -> None:
        if usage is None:
            return
        session = msg.variables.get("session")
        if session is None:
            return
        session["tokens_used"] = session.get("tokens_used", 0) + usage.total_tokens

    async def _complete_blocking(self, msg: ModelRequest) -> ModelResponse:
        logger.debug(
            "calling openrouter model={} messages={} tools={}",
            self.model, len(msg.messages), len(msg.tools or []),
        )
        response = await self._client.chat.completions.create(
            model=self.model,
            messages=msg.messages,
            tools=msg.tools or None,
            extra_body=self._extra_body(),
            extra_headers=self._extra_headers(),
        )
        self._record_usage(msg, getattr(response, "usage", None))
        message = response.choices[0].message
        raw_message = message.model_dump()
        tool_calls = [
            ToolCallSpec(id=tc.id, name=tc.function.name, args=json.loads(tc.function.arguments))
            for tc in (message.tool_calls or [])
        ]
        logger.debug(
            "openrouter response text_len={} tool_calls={}",
            len(message.content or ""), len(tool_calls),
        )
        return ModelResponse(text=message.content, tool_calls=tool_calls, raw_message=raw_message)

    async def _complete_streaming(self, msg: ModelRequest) -> ModelResponse:
        logger.debug(
            "calling openrouter (streaming) model={} messages={} tools={}",
            self.model, len(msg.messages), len(msg.tools or []),
        )
        stream = await self._client.chat.completions.create(
            model=self.model,
            messages=msg.messages,
            tools=msg.tools or None,
            stream=True,
            stream_options={"include_usage": True},
            extra_body=self._extra_body(),
            extra_headers=self._extra_headers(),
        )
        content_parts: list[str] = []
        tool_call_acc: dict[int, dict] = {}
        usage = None
        async for chunk in stream:
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
            "openrouter streaming response text_len={} tool_calls={}",
            len(text or ""), len(tool_calls),
        )
        return ModelResponse(text=text, tool_calls=tool_calls, raw_message=raw_message)
