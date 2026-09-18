import asyncio
from dataclasses import dataclass
from typing import Callable

from loguru import logger

from conic.core.bus import MessageBus, infer_payload_type
from conic.core.session_gateway import SessionGatewayPlugin
from conic.plugins import meta
from conic.types.messages import SessionEnd, SessionStart
from conic.types.session import SessionScope


@dataclass
class PluginSet:
    tool_classes: tuple[type, ...]
    backend: Callable[[str], object]
    context_plugins: tuple[Callable[[str, list[dict]], object], ...]
    policy_plugins: tuple[Callable[[], object], ...]
    summarizer: Callable[[], object]
    loop_factory: Callable[[object, list[dict], dict[str, type], str, dict], object]


class PluginManager:
    def __init__(self, storage, plugin_set: PluginSet):
        self._storage = storage
        self._plugin_set = plugin_set

    async def start_session(
        self,
        channel: str,
        native_id: str,
        channel_plugin_factory: Callable[[], object],
        reason: str = "new",
    ) -> SessionScope:
        logger.debug("starting session channel={} native_id={}", channel, native_id)
        row = self._storage.get_or_create(channel=channel, native_id=native_id)
        bus = MessageBus()
        scope = SessionScope(bus=bus, row=row)

        tool_schemas = [cls.schema for cls in self._plugin_set.tool_classes]
        tool_payload_map = {
            cls.llm_name: infer_payload_type(cls.execute) for cls in self._plugin_set.tool_classes
        }

        for tool_cls in self._plugin_set.tool_classes:
            tool_cls(workspace_dir=row.workspace_dir).register(bus)

        self._plugin_set.backend(row.session_key).register(bus)
        for ctx_plugin_factory in self._plugin_set.context_plugins:
            ctx_plugin_factory(row.workspace_dir, tool_schemas).register(bus)
        for policy_factory in self._plugin_set.policy_plugins:
            policy_factory().register(bus)
        self._plugin_set.summarizer().register(bus)

        handle = self._storage.handle_for(row)
        loop_plugin = self._plugin_set.loop_factory(
            handle, tool_schemas, tool_payload_map, row.workspace_dir, row.variables
        )
        loop_plugin.register(bus)

        channel_plugin_factory().register(bus)

        session_gateway = SessionGatewayPlugin(scope)
        session_gateway.register(bus)

        await bus.chain(meta.SessionStartEvent, SessionStart(reason=reason))

        session_end_emitted = False

        async def _mark_session_end_emitted(msg: SessionEnd) -> SessionEnd:
            nonlocal session_end_emitted
            session_end_emitted = True
            return msg

        # Registered synchronously, before any task below gets a chance to
        # run, so it can never miss a SessionEndEvent raced against join().
        bus.on_chain(meta.SessionEndEvent, _mark_session_end_emitted)

        scope.tasks = {
            "loop": asyncio.create_task(loop_plugin.run_loop()),
            "gateway": asyncio.create_task(session_gateway.run()),
        }
        asyncio.create_task(self._join_and_cleanup(scope, lambda: session_end_emitted))

        return scope

    async def _join_and_cleanup(self, scope: SessionScope, session_end_emitted: Callable[[], bool]) -> None:
        await asyncio.gather(*scope.tasks.values())
        if not session_end_emitted():
            await scope.bus.chain(meta.SessionEndEvent, SessionEnd(reason="unexpected_exit"))
        self._storage.handle_for(scope.row).set_status("ended")
        await scope.bus.close()
