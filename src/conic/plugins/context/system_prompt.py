from conic.core.messages import BeforeModelCall, BuildSystemPrompt
from conic.plugins import meta

SECTION_ORDER = ["identity", "tooling", "workspace", "runtime", "execution"]


class SystemPromptPlugin:
    def __init__(self, section_plugins: list):
        self._section_plugins = section_plugins
        self._bus = None

    def register(self, bus) -> None:
        self._bus = bus
        for plugin in self._section_plugins:
            plugin.register(bus)
        bus.on(meta.BeforeModelCallEvent, self.apply)

    async def apply(self, ctx: BeforeModelCall) -> BeforeModelCall | None:
        if ctx.messages and ctx.messages[0].get("role") == "system":
            return None

        sections_msg = await self._bus.emit(
            meta.BuildSystemPromptEvent, BuildSystemPrompt(sections={})
        )
        system_text = self._assemble(sections_msg.sections)
        return BeforeModelCall(
            messages=[{"role": "system", "content": system_text}, *ctx.messages],
            tools=ctx.tools,
        )

    @staticmethod
    def _assemble(sections: dict[str, str]) -> str:
        parts = []
        for key in SECTION_ORDER:
            if key in sections:
                parts.append(f"## {key}\n{sections[key]}")
        for key, content in sections.items():
            if key not in SECTION_ORDER:
                parts.append(f"## {key}\n{content}")
        return "\n\n".join(parts)
