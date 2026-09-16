from loguru import logger

from conic.types.errors import AbortTurn
from conic.types.messages import (
    AssistantMessage, BeforeModelCall, Error, MessageUpdate, ModelRequest, ModelResponse,
    StepEnd, StepStart, ToolCall, ToolCallResult, ToolCallSpec, ToolExecutionEnd,
    ToolExecutionStart, TurnEnd, TurnStart, UserInput,
)
from conic.plugins import meta


class ReactLoopPlugin:
    def __init__(self, storage_handle, tool_schemas: list[dict], tool_payload_map: dict[str, type]):
        self._storage = storage_handle
        self._tool_schemas = tool_schemas
        self._tool_payload_map = tool_payload_map
        self._bus = None

    def register(self, bus) -> None:
        self._bus = bus
        bus.on(meta.UserInputEvent, self.handle_user_input)

    def _format_tool_status(self, call: ToolCallSpec) -> str:
        args = ", ".join(f"{k}={v!r}" for k, v in call.args.items())
        return f"🔧 {call.name}({args})"

    async def handle_user_input(self, msg: UserInput) -> None:
        bus = self._bus
        self._storage.append_message({"role": "user", "content": msg.text})
        await bus.emit(meta.TurnStartEvent, TurnStart())
        step_index = 0
        try:
            while True:
                this_step = step_index
                await bus.emit(meta.StepStartEvent, StepStart(step_index=this_step))
                step_index += 1
                history = self._storage.load_history()
                ctx = await bus.emit(
                    meta.BeforeModelCallEvent, BeforeModelCall(messages=history, tools=self._tool_schemas)
                )
                response: ModelResponse = await bus.request(
                    meta.ModelRequestEvent,
                    ModelRequest(messages=ctx.messages, tools=ctx.tools, stream_updates=True),
                )
                response = await bus.emit(meta.ModelResponseEvent, response)
                if not response.tool_calls:
                    out = await bus.emit(meta.AssistantMessageEvent, AssistantMessage(text=response.text or ""))
                    self._storage.append_message({"role": "assistant", "content": out.text})
                    await bus.emit(meta.StepEndEvent, StepEnd(step_index=this_step))
                    break
                self._storage.append_message(response.raw_message)
                for call in response.tool_calls:
                    original_id = call.id
                    try:
                        call_ctx = await bus.emit(meta.ToolCallEvent, ToolCall(call=call))
                        payload_cls = self._tool_payload_map[call_ctx.call.name]
                        payload = payload_cls(**call_ctx.call.args)
                        await bus.emit(meta.ToolExecutionStartEvent, ToolExecutionStart(call=call_ctx.call))
                        await bus.emit(
                            meta.MessageUpdateEvent, MessageUpdate(text=self._format_tool_status(call_ctx.call))
                        )
                        result: ToolCallResult = await bus.request(meta.ToolCallRequestEvent, payload)
                        await bus.emit(
                            meta.ToolExecutionEndEvent, ToolExecutionEnd(call=call_ctx.call, result=result)
                        )
                        result = await bus.emit(meta.ToolCallResultEvent, result)
                        content = result.output if result.error is None else f"Error: {result.error}"
                    except AbortTurn:
                        raise
                    except Exception as exc:
                        logger.warning("tool call failed name={} error={}", call.name, exc)
                        content = f"Error: {exc}"
                    self._storage.append_message(
                        {"role": "tool", "tool_call_id": original_id, "content": content}
                    )
                await bus.emit(meta.StepEndEvent, StepEnd(step_index=this_step))
        except AbortTurn as exc:
            logger.info("turn aborted: {}", exc)
            await bus.emit(meta.ErrorEvent, Error(exc=exc))
            return
        except Exception as exc:
            logger.exception("turn failed with unexpected error")
            await bus.emit(meta.ErrorEvent, Error(exc=exc))
            return
        await bus.emit(meta.TurnEndEvent, TurnEnd())
