from dataclasses import dataclass
from typing import ClassVar

from conic.plugins import meta
from conic.plugins.tools.base import WorkspaceEscapeError, resolve_within_workspace
from conic.types.messages import BuildSystemPrompt, ToolCallResult


@dataclass
class EditFileCall:
    path: str
    old_text: str
    new_text: str


class EditFileToolPlugin:
    llm_name = "edit_file"
    schema: ClassVar[dict] = {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace an exact, unique text snippet in a file within the session workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    }

    def __init__(self, workspace_dir: str):
        self._workspace_dir = workspace_dir

    def register(self, bus) -> None:
        bus.on_request(meta.ToolCallRequestEvent, self.execute)
        bus.on_chain(meta.BuildSystemPromptEvent, self.contribute_workspace_restriction)

    async def contribute_workspace_restriction(self, msg: BuildSystemPrompt) -> BuildSystemPrompt:
        msg.sections["edit_file"] = (
            f"edit_file can only modify files inside this session's workspace directory "
            f"({self._workspace_dir}). Paths that resolve outside it are rejected."
        )
        return msg

    async def execute(self, call: EditFileCall) -> ToolCallResult:
        try:
            resolved = resolve_within_workspace(self._workspace_dir, call.path)
        except WorkspaceEscapeError as exc:
            return ToolCallResult(error=str(exc))
        if not resolved.is_file():
            return ToolCallResult(error=f"not a file: {call.path}")
        try:
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ToolCallResult(error=str(exc))
        count = text.count(call.old_text)
        if count == 0:
            return ToolCallResult(error="old_text not found in file")
        if count > 1:
            return ToolCallResult(error=f"old_text is not unique ({count} occurrences)")
        try:
            resolved.write_text(text.replace(call.old_text, call.new_text, 1), encoding="utf-8")
        except OSError as exc:
            return ToolCallResult(error=str(exc))
        return ToolCallResult(output=f"replaced 1 occurrence in {call.path}")
