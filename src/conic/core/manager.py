import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from loguru import logger

from conic.core.bus import MessageBus
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
    instantiate_session: Callable[[MessageBus, str, str, object, dict], object]


class PluginManager:
    def __init__(self, storage, plugin_sets: dict[str, PluginSet]):
        self._storage = storage
        self._plugin_sets = plugin_sets

    async def start_session(
        self,
        channel: str,
        native_id: str,
        channel_plugin_factory: Callable[[], object],
        reason: str = "new",
        plugin_set_name: str = "main",
    ) -> SessionScope:
        logger.debug(
            "starting session channel={} native_id={} plugin_set={}", channel, native_id, plugin_set_name
        )
        row = self._storage.get_or_create(channel=channel, native_id=native_id)
        bus = MessageBus()
        scope = SessionScope(bus=bus, row=row)
        handle = self._storage.handle_for(row)

        plugin_set = self._plugin_sets[plugin_set_name]
        loop_plugin = plugin_set.instantiate_session(
            bus, row.workspace_dir, row.session_key, handle, row.variables
        )

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
