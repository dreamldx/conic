from dataclasses import dataclass

from conic.core.messages import ToolCallResult
from conic.plugins.tools.base import WorkspaceEscapeError, resolve_within_workspace


@dataclass
class ReadFileCall:
    path: str
    offset: int = 0
    limit: int = 2000


class ReadFileToolPlugin:
    llm_name = "read_file"
    schema = {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file within the session workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "description": "0-based line to start from"},
                    "limit": {"type": "integer", "description": "max number of lines to return"},
                },
                "required": ["path"],
            },
        },
    }

    def __init__(self, workspace_dir: str):
        self._workspace_dir = workspace_dir

    def register(self, bus) -> None:
        bus.on_request("tool_call", self.execute)

    async def execute(self, call: ReadFileCall) -> ToolCallResult:
        try:
            resolved = resolve_within_workspace(self._workspace_dir, call.path)
        except WorkspaceEscapeError as exc:
            return ToolCallResult(error=str(exc))
        if not resolved.is_file():
            return ToolCallResult(error=f"not a file: {call.path}")
        lines = resolved.read_text(errors="replace").splitlines()
        selected = lines[call.offset : call.offset + call.limit]
        text = "\n".join(selected)
        if call.offset + call.limit < len(lines):
            text += f"\n...[truncated, {len(lines)} lines total]"
        return ToolCallResult(output=text)
