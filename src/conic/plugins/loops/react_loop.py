from conic.core.errors import AbortTurn
from conic.core.messages import (
    AssistantMessage, BeforeModelCall, Error, ModelRequest, ModelResponse,
    StepStart, ToolCall, ToolCallResult, TurnEnd, TurnStart, UserInput,
)


class ReactLoopPlugin:
    def __init__(self, storage_handle, tool_schemas: list[dict], tool_payload_map: dict[str, type]):
        self._storage = storage_handle
        self._tool_schemas = tool_schemas
        self._tool_payload_map = tool_payload_map
        self._bus = None

    def register(self, bus) -> None:
        self._bus = bus
        bus.on("user_input", self.handle_user_input)

    async def handle_user_input(self, msg: UserInput) -> None:
        bus = self._bus
        self._storage.append_message({"role": "user", "content": msg.text})
        await bus.emit("turn_start", TurnStart())

        step_index = 0
        try:
            while True:
                await bus.emit("step_start", StepStart(step_index=step_index))
                step_index += 1

                history = self._storage.load_history()
                ctx = await bus.emit(
                    "before_model_call", BeforeModelCall(messages=history, tools=self._tool_schemas)
                )

                response: ModelResponse = await bus.request(
                    "model_request", ModelRequest(messages=ctx.messages, tools=ctx.tools)
                )
                response = await bus.emit("model_response", response)

                if not response.tool_calls:
                    out = await bus.emit("assistant_message", AssistantMessage(text=response.text or ""))
                    self._storage.append_message({"role": "assistant", "content": out.text})
                    break

                self._storage.append_message(response.raw_message)
                for call in response.tool_calls:
                    original_id = call.id
                    call_ctx = await bus.emit("before_tool_call", ToolCall(call=call))
                    payload_cls = self._tool_payload_map[call_ctx.call.name]
                    payload = payload_cls(**call_ctx.call.args)
                    result: ToolCallResult = await bus.request("tool_call", payload)
                    result = await bus.emit("tool_result", result)
                    content = result.output if result.error is None else f"Error: {result.error}"
                    self._storage.append_message(
                        {"role": "tool", "tool_call_id": original_id, "content": content}
                    )
        except AbortTurn as exc:
            await bus.emit("error", Error(exc=exc))
            return

        await bus.emit("turn_end", TurnEnd())
