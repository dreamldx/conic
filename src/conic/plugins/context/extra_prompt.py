from jinja2 import Template

from conic.types.messages import BeforeModelCall, BuildDynamicPrompt
from conic.plugins import meta


class ExtraPromptPlugin:
    def __init__(self, section_plugins: list):
        self._section_plugins = section_plugins
        self._bus = None

    def register(self, bus) -> None:
        self._bus = bus
        for plugin in self._section_plugins:
            plugin.register(bus)
        bus.on(meta.BeforeModelCallEvent, self.apply)

    async def apply(self, ctx: BeforeModelCall) -> BeforeModelCall | None:
        if not ctx.messages:
            return None

        sections_msg = await self._bus.emit(
            meta.BuildDynamicPromptEvent, BuildDynamicPrompt(sections={})
        )
        text = self._assemble(sections_msg.sections)
        text = Template(text).render(**ctx.variables)
        if not text:
            return None

        *rest, last = ctx.messages
        content = last.get("content") or ""
        appended = {**last, "content": f"{content}\n\n{text}"}
        return BeforeModelCall(
            messages=[*rest, appended],
            tools=ctx.tools,
            variables=ctx.variables,
        )

    @staticmethod
    def _assemble(sections: dict[str, str]) -> str:
        if not sections:
            return ""
        body = "\n\n".join(f"<{key}>\n{content}\n</{key}>" for key, content in sections.items())
        return f"<context_state>\n{body}\n</context_state>"
