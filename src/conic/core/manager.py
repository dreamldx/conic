from dataclasses import dataclass
from typing import Callable

from conic.core.bus import MessageBus, infer_payload_type
from conic.core.session import SessionScope


@dataclass
class PluginSet:
    tool_classes: tuple[type, ...]
    backend: object
    context_plugins: tuple[object, ...]
    policy_plugins: tuple[object, ...]
    summarizer: object
    loop_factory: Callable[[object, list[dict], dict[str, type]], object]


class PluginManager:
    def __init__(self, storage, plugin_set: PluginSet):
        self._storage = storage
        self._plugin_set = plugin_set

    def start_session(
        self, channel: str, native_id: str, channel_plugin_factory: Callable[[], object]
    ) -> SessionScope:
        row = self._storage.get_or_create(channel=channel, native_id=native_id)
        bus = MessageBus()

        for tool_cls in self._plugin_set.tool_classes:
            tool_cls(workspace_dir=row.workspace_dir).register(bus)

        self._plugin_set.backend.register(bus)
        for ctx_plugin in self._plugin_set.context_plugins:
            ctx_plugin.register(bus)
        for policy in self._plugin_set.policy_plugins:
            policy.register(bus)
        self._plugin_set.summarizer.register(bus)

        tool_schemas = [cls.schema for cls in self._plugin_set.tool_classes]
        tool_payload_map = {
            cls.llm_name: infer_payload_type(cls.execute) for cls in self._plugin_set.tool_classes
        }
        handle = self._storage.handle_for(row)
        self._plugin_set.loop_factory(handle, tool_schemas, tool_payload_map).register(bus)

        channel_plugin_factory().register(bus)

        return SessionScope(bus=bus, row=row)

    def stop_session(self, scope: SessionScope) -> None:
        self._storage.handle_for(scope.row).set_status("ended")
