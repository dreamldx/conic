from dataclasses import dataclass
from typing import Callable

from loguru import logger

from conic.core.bus import MessageBus, infer_payload_type
from conic.types.session import SessionScope


@dataclass
class PluginSet:
    tool_classes: tuple[type, ...]
    backend: Callable[[], object]
    context_plugins: tuple[Callable[[str, list[dict]], object], ...]
    policy_plugins: tuple[Callable[[], object], ...]
    summarizer: Callable[[], object]
    loop_factory: Callable[[object, list[dict], dict[str, type], str, dict], object]


class PluginManager:
    def __init__(self, storage, plugin_set: PluginSet):
        self._storage = storage
        self._plugin_set = plugin_set

    def start_session(
        self, channel: str, native_id: str, channel_plugin_factory: Callable[[], object]
    ) -> SessionScope:
        logger.debug("starting session channel={} native_id={}", channel, native_id)
        row = self._storage.get_or_create(channel=channel, native_id=native_id)
        bus = MessageBus()

        tool_schemas = [cls.schema for cls in self._plugin_set.tool_classes]
        tool_payload_map = {
            cls.llm_name: infer_payload_type(cls.execute) for cls in self._plugin_set.tool_classes
        }

        for tool_cls in self._plugin_set.tool_classes:
            tool_cls(workspace_dir=row.workspace_dir).register(bus)

        self._plugin_set.backend().register(bus)
        for ctx_plugin_factory in self._plugin_set.context_plugins:
            ctx_plugin_factory(row.workspace_dir, tool_schemas).register(bus)
        for policy_factory in self._plugin_set.policy_plugins:
            policy_factory().register(bus)
        self._plugin_set.summarizer().register(bus)

        handle = self._storage.handle_for(row)
        self._plugin_set.loop_factory(
            handle, tool_schemas, tool_payload_map, row.workspace_dir, row.variables
        ).register(bus)

        channel_plugin_factory().register(bus)

        return SessionScope(bus=bus, row=row)

    def stop_session(self, scope: SessionScope) -> None:
        logger.info("ending session {}", scope.row.session_key)
        self._storage.handle_for(scope.row).set_status("ended")
