from jinja2 import Template

from conic.plugins import meta
from conic.types.messages import BeforeModelCall, BuildSystemPrompt

SECTION_ORDER = ["identity", "tooling", "workspace", "runtime", "execution"]


class SystemPromptPlugin:
    def __init__(self, section_plugins: list):
        self._section_plugins = section_plugins
        self._bus = None
        self._cached_content: str | None = None

    def register(self, bus) -> None:
        self._bus = bus
        for plugin in self._section_plugins:
            plugin.register(bus)
        bus.on_chain(meta.BeforeModelCallEvent, self.apply)

    async def apply(self, ctx: BeforeModelCall) -> BeforeModelCall | None:
        if ctx.messages and ctx.messages[0].get("role") == "system":
            return None

        if self._cached_content is None:
            sections_msg = await self._bus.chain(
                meta.BuildSystemPromptEvent, BuildSystemPrompt(sections={})
            )
            system_text = self._assemble(sections_msg.sections)
            self._cached_content = Template(system_text).render(**ctx.variables)

        return BeforeModelCall(
            messages=[{"role": "system", "content": self._cached_content}, *ctx.messages],
            tools=ctx.tools,
            variables=ctx.variables,
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
