import asyncio

from loguru import logger

from conic.plugins import meta
from conic.types.errors import AbortReason, AbortTurn
from conic.types.messages import (
    AssistantMessage,
    BeforeModelCall,
    Error,
    MessageUpdate,
    ModelRequest,
    ModelResponse,
    SessionEnd,
    StepEnd,
    StepStart,
    ToolCall,
    ToolCallResult,
    ToolCallSpec,
    ToolExecutionEnd,
    ToolExecutionStart,
    TurnEnd,
    TurnStart,
)
from conic.types.steering import SteeringItem


class ReactLoopPlugin:
    def __init__(
        self,
        storage_handle,
        tool_schemas: list[dict],
        tool_payload_map: dict[str, type],
        workspace_dir: str = "",
        global_variables: dict | None = None,
        persisted_session_variables: dict | None = None,
        model_timeout: float = 120,
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
        self._model_timeout = model_timeout
        self._bus = None

    def register(self, bus) -> None:
        self._bus = bus
        bus.create_mailbox("steering.high", SteeringItem)
        bus.create_mailbox("steering.low", SteeringItem)

    def _format_tool_status(self, call: ToolCallSpec) -> str:
        args = ", ".join(f"{k}={v!r}" for k, v in call.args.items() if v != "" and v != [])
        return f"🔧 {call.name}({args})"

    def _inject(self, items: list[SteeringItem]) -> None:
        for item in items:
            for entry in item.to_history_entries():
                self._storage.append_message(entry)

    async def run_loop(self) -> None:
        bus = self._bus
        while True:
            await bus.wait_multiply_mailbox("steering.high", "steering.low")
            high = await bus.drain("steering.high")
            low = await bus.drain("steering.low")
            if not high and not low:
                continue
            if any(item.is_turn_abort() for item in high):
                await self._finalize_session()
                return
            try:
                await self._run_turn(high, low)
            except AbortTurn as exc:
                if exc.reason.ends_session:
                    await self._finalize_session()
                    return
                # else (e.g. ModelTimeout): _run_turn already emitted
                # ErrorEvent/TurnEndEvent — go back to idle and keep looping.

    async def _finalize_session(self) -> None:
        await self._bus.chain(meta.SessionEndEvent, SessionEnd(reason="agent_stop"))

    async def _run_turn(self, high: list[SteeringItem], low: list[SteeringItem]) -> None:
        bus = self._bus
        self._session_variables["turn_count"] += 1
        variables: dict = {
            "global": self._global_variables,
            "session": self._session_variables,
            "turn": {},
        }
        self._inject([*high, *low])

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
                try:
                    response: ModelResponse = await asyncio.wait_for(
                        bus.request(
                            meta.ModelRequestEvent,
                            ModelRequest(
                                messages=ctx.messages, tools=ctx.tools, stream_updates=True,
                                variables=variables,
                            ),
                        ),
                        timeout=self._model_timeout,
                    )
                except TimeoutError:
                    raise AbortTurn("model call timed out", reason=AbortReason.MODEL_TIMEOUT)

                response = await bus.chain(meta.ModelResponseEvent, response)

                if not response.tool_calls:
                    out = await bus.chain(
                        meta.AssistantMessageEvent,
                        AssistantMessage(text=response.text or "", variables=variables),
                    )
                    self._storage.append_message({"role": "assistant", "content": out.text})

                    # checkpoint: drain high once the answer is already in
                    # history, so a stop that lands right as the model
                    # finishes doesn't throw the completed answer away.
                    drained_high = await bus.drain("steering.high")
                    if any(item.is_turn_abort() for item in drained_high):
                        raise AbortTurn("user requested stop", reason=AbortReason.USER_ABORT)

                    if drained_high:
                        self._inject(drained_high)
                        await bus.chain(meta.StepEndEvent, StepEnd(step_index=this_step, variables=variables))
                        continue
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
                            meta.MessageUpdateEvent,
                            MessageUpdate(text=self._format_tool_status(call_ctx.call)),
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

                # checkpoint: drain high once every tool_call in this step has
                # finished and has a matching tool_result in history (never
                # mid-loop, so an abort here never leaves a dangling
                # tool_calls/tool_result pairing).
                tool_high = await bus.drain("steering.high")
                if any(item.is_turn_abort() for item in tool_high):
                    raise AbortTurn("user requested stop", reason=AbortReason.USER_ABORT)
                self._inject(tool_high)

                await bus.chain(meta.StepEndEvent, StepEnd(step_index=this_step, variables=variables))
        except AbortTurn as exc:
            if exc.reason is AbortReason.USER_ABORT:
                await bus.chain(meta.TurnEndEvent, TurnEnd(variables=variables))
                raise
            elif exc.reason is AbortReason.MODEL_TIMEOUT:
                await bus.chain(meta.ErrorEvent, Error(exc=exc, variables=variables))
                await bus.chain(meta.TurnEndEvent, TurnEnd(variables=variables))
                raise
            else:
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
        self._inject(await bus.drain("steering.low"))
