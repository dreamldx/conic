from jinja2 import Template

from conic.plugins import meta
from conic.types.messages import BeforeModelCall, BuildDynamicPrompt


class ExtraPromptPlugin:
    def __init__(self, section_plugins: list):
        self._section_plugins = section_plugins
        self._bus = None

    def register(self, bus) -> None:
        self._bus = bus
        for plugin in self._section_plugins:
            plugin.register(bus)
        bus.on_chain(meta.BeforeModelCallEvent, self.apply)

    async def apply(self, ctx: BeforeModelCall) -> BeforeModelCall | None:
        if not ctx.messages:
            return None
        if ctx.variables.get("turn", {}).get("step_count") != 0:
            return None

        sections_msg = await self._bus.chain(
            meta.BuildDynamicPromptEvent, BuildDynamicPrompt(sections={})
        )
        text = self._assemble(sections_msg.sections)
        text = Template(text).render(**ctx.variables)
        if not text:
            return None

        # Insert before this turn's own steering-injected messages (rather
        # than after them) so the state snapshot reads as context established
        # ahead of the turn's actual input, not a trailing note appended to
        # it. steering_count is however many of the most recent messages in
        # ctx.messages belong to this turn's steering injection.
        steering_count = ctx.variables.get("turn", {}).get("steering_count", 0)
        insert_at = max(0, len(ctx.messages) - steering_count)
        messages = [*ctx.messages[:insert_at], {"role": "user", "content": text}, *ctx.messages[insert_at:]]
        return BeforeModelCall(
            messages=messages,
            tools=ctx.tools,
            variables=ctx.variables,
        )

    @staticmethod
    def _assemble(sections: dict[str, str]) -> str:
        if not sections:
            return ""
        body = "\n\n".join(f"<{key}>\n{content}\n</{key}>" for key, content in sections.items())
        return f"<context_state>\n{body}\n</context_state>"
