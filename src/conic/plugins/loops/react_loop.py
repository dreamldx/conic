from loguru import logger

from conic.types.errors import AbortTurn
from conic.types.messages import (
    AssistantMessage, BeforeModelCall, Error, MessageUpdate, ModelRequest, ModelResponse,
    StepEnd, StepStart, ToolCall, ToolCallResult, ToolCallSpec, ToolExecutionEnd,
    ToolExecutionStart, TurnEnd, TurnStart, UserInput,
)
from conic.plugins import meta


class ReactLoopPlugin:
    def __init__(
        self,
        storage_handle,
        tool_schemas: list[dict],
        tool_payload_map: dict[str, type],
        workspace_dir: str = "",
        global_variables: dict | None = None,
        persisted_session_variables: dict | None = None,
    ):
        self._storage = storage_handle
        self._tool_schemas = tool_schemas
        self._tool_payload_map = tool_payload_map
        self._global_variables = global_variables if global_variables is not None else {}
        self._session_variables = {
            "tokens_used": 0,
            "turn_count": 0,
            **(persisted_session_variables or {}),
            "workspace_dir": workspace_dir,
        }
        self._bus = None

    def register(self, bus) -> None:
        self._bus = bus
        bus.on_chain(meta.UserInputEvent, self.handle_user_input)

    def _format_tool_status(self, call: ToolCallSpec) -> str:
        args = ", ".join(f"{k}={v!r}" for k, v in call.args.items())
        return f"🔧 {call.name}({args})"

    async def handle_user_input(self, msg: UserInput) -> None:
        bus = self._bus
        self._session_variables["turn_count"] += 1
        variables: dict = {
            "global": self._global_variables,
            "session": self._session_variables,
            "turn": {},
        }
        self._storage.append_message({"role": "user", "content": msg.text})
        await bus.chain(meta.TurnStartEvent, TurnStart(variables=variables))
        step_index = 0
        try:
            while True:
                this_step = step_index
                variables["turn"]["step_count"] = this_step
                await bus.chain(meta.StepStartEvent, StepStart(step_index=this_step, variables=variables))
                step_index += 1
                history = self._storage.load_history()
                ctx = await bus.chain(
                    meta.BeforeModelCallEvent,
                    BeforeModelCall(messages=history, tools=self._tool_schemas, variables=variables),
                )
                response: ModelResponse = await bus.request(
                    meta.ModelRequestEvent,
                    ModelRequest(
                        messages=ctx.messages, tools=ctx.tools, stream_updates=True, variables=variables
                    ),
                )
                response = await bus.chain(meta.ModelResponseEvent, response)
                if not response.tool_calls:
                    out = await bus.chain(
                        meta.AssistantMessageEvent,
                        AssistantMessage(text=response.text or "", variables=variables),
                    )
                    self._storage.append_message({"role": "assistant", "content": out.text})
                    await bus.chain(meta.StepEndEvent, StepEnd(step_index=this_step, variables=variables))
                    break
                self._storage.append_message(response.raw_message)
                for call in response.tool_calls:
                    original_id = call.id
                    try:
                        call_ctx = await bus.chain(
                            meta.ToolCallEvent, ToolCall(call=call, variables=variables)
                        )
                        payload_cls = self._tool_payload_map[call_ctx.call.name]
                        payload = payload_cls(**call_ctx.call.args)
                        await bus.chain(
                            meta.ToolExecutionStartEvent,
                            ToolExecutionStart(call=call_ctx.call, variables=variables),
                        )
                        await bus.chain(
                            meta.MessageUpdateEvent, MessageUpdate(text=self._format_tool_status(call_ctx.call))
                        )
                        result: ToolCallResult = await bus.request(meta.ToolCallRequestEvent, payload)
                        await bus.chain(
                            meta.ToolExecutionEndEvent,
                            ToolExecutionEnd(call=call_ctx.call, result=result, variables=variables),
                        )
                        result = await bus.chain(meta.ToolCallResultEvent, result)
                        content = result.output if result.error is None else f"Error: {result.error}"
                    except AbortTurn:
                        raise
                    except Exception as exc:
                        logger.warning("tool call failed name={} error={}", call.name, exc)
                        content = f"Error: {exc}"
                    self._storage.append_message(
                        {"role": "tool", "tool_call_id": original_id, "content": content}
                    )
                await bus.chain(meta.StepEndEvent, StepEnd(step_index=this_step, variables=variables))
        except AbortTurn as exc:
            logger.info("turn aborted: {}", exc)
            await bus.chain(meta.ErrorEvent, Error(exc=exc, variables=variables))
            return
        except Exception as exc:
            logger.exception("turn failed with unexpected error")
            await bus.chain(meta.ErrorEvent, Error(exc=exc, variables=variables))
            return
        finally:
            self._storage.save_variables(self._session_variables)
        await bus.chain(meta.TurnEndEvent, TurnEnd(variables=variables))
