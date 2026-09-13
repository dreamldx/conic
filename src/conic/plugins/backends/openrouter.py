import json

from openai import AsyncOpenAI

from conic.core.messages import ModelRequest, ModelResponse, ToolCallSpec


class OpenRouterBackendPlugin:
    def __init__(self, api_key: str, model: str, client: AsyncOpenAI | None = None):
        self.model = model
        self._client = client or AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=api_key)

    def register(self, bus) -> None:
        bus.on_request("model_request", self.complete)

    async def complete(self, msg: ModelRequest) -> ModelResponse:
        response = await self._client.chat.completions.create(
            model=self.model,
            messages=msg.messages,
            tools=msg.tools or None,
        )
        message = response.choices[0].message
        raw_message = message.model_dump()
        tool_calls = [
            ToolCallSpec(id=tc.id, name=tc.function.name, args=json.loads(tc.function.arguments))
            for tc in (message.tool_calls or [])
        ]
        return ModelResponse(text=message.content, tool_calls=tool_calls, raw_message=raw_message)
