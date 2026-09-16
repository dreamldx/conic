from conic.types.messages import BuildSystemPrompt
from conic.plugins import meta


class ToolingSectionPlugin:
    def __init__(self, tool_schemas: list[dict]):
        self._tool_schemas = tool_schemas

    def register(self, bus) -> None:
        bus.on(meta.BuildSystemPromptEvent, self.contribute)

    async def contribute(self, msg: BuildSystemPrompt) -> BuildSystemPrompt | None:
        if not self._tool_schemas:
            return msg
        lines = ["Available tools:"]
        for schema in self._tool_schemas:
            fn = schema["function"]
            params = fn["parameters"]["properties"]
            param_desc = ", ".join(
                f"{k}: {v.get('type', 'str')}" for k, v in params.items()
            )
            lines.append(f"  - {fn['name']}({param_desc}) — {fn['description']}")
        msg.sections["tooling"] = "\n".join(lines)
        return msg
