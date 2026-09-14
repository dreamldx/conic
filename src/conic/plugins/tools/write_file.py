from dataclasses import dataclass

from conic.core.messages import ToolCallResult
from conic.plugins.tools.base import WorkspaceEscapeError, resolve_within_workspace
from conic.plugins import meta


@dataclass
class WriteFileCall:
    path: str
    content: str


class WriteFileToolPlugin:
    llm_name = "write_file"
    schema = {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a text file within the session workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    }

    def __init__(self, workspace_dir: str):
        self._workspace_dir = workspace_dir

    def register(self, bus) -> None:
        bus.on_request(meta.ToolCallRequestEvent, self.execute)

    async def execute(self, call: WriteFileCall) -> ToolCallResult:
        try:
            resolved = resolve_within_workspace(self._workspace_dir, call.path)
        except WorkspaceEscapeError as exc:
            return ToolCallResult(error=str(exc))
        try:
            existed = resolved.is_file()
            old_line_count = (
                len(resolved.read_text(encoding="utf-8", errors="replace").splitlines()) if existed else 0
            )
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(call.content, encoding="utf-8")
        except OSError as exc:
            return ToolCallResult(error=str(exc))
        new_line_count = len(call.content.splitlines())
        status = "overwritten" if existed else "created"
        return ToolCallResult(
            output=f"wrote {new_line_count} lines to {call.path} ({status}, was {old_line_count} lines)"
        )
