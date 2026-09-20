from dataclasses import dataclass
from typing import ClassVar

from conic.plugins import meta
from conic.plugins.skills import (
    discover_skills,
    parse_skill_file,
    resolve_skill_reference,
    visible_to_model,
)
from conic.plugins.tools.base import WorkspaceEscapeError
from conic.types.messages import ToolCallResult


@dataclass
class ListSkillsCall:
    pass


@dataclass
class LoadSkillCall:
    name: str
    file_path: str = ""


class ListSkillsToolPlugin:
    llm_name = "list_skills"
    schema: ClassVar[dict] = {
        "type": "function",
        "function": {
            "name": "list_skills",
            "description": (
                "List available skills as name + description pairs. Rescans disk on "
                "every call, so it reflects skills added after this session started. "
                "Call load_skill with a name to get its full instructions."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }

    def __init__(self, workspace_dir: str, project_root: str):
        self._workspace_dir = workspace_dir
        self._project_root = project_root

    def register(self, bus) -> None:
        bus.on_request(meta.ToolCallRequestEvent, self.execute)

    async def execute(self, call: ListSkillsCall) -> ToolCallResult:
        entries = visible_to_model(discover_skills(self._workspace_dir, self._project_root))
        if not entries:
            return ToolCallResult(output="No skills available.")
        lines = [f"{len(entries)} skill(s) available:"]
        for entry in entries:
            lines.append(f"- {entry.name} ({entry.scope}): {entry.description}")
        return ToolCallResult(output="\n".join(lines))


class LoadSkillToolPlugin:
    llm_name = "load_skill"
    schema: ClassVar[dict] = {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": (
                "Load the full instructions for a skill by name. Returns the SKILL.md "
                "body (not just the summary from list_skills or the system prompt) -- "
                "call this before acting on a skill, even if you think you already know "
                "how to do the task. If the skill has reference files, the result lists "
                "their names; pass one as file_path to read it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Exact skill name, as shown by list_skills or in the system prompt catalog.",
                    },
                    "file_path": {
                        "type": "string",
                        "description": (
                            "Optional. A reference file listed in a previous load_skill "
                            "result for this skill, e.g. 'references/rollback.md'."
                        ),
                    },
                },
                "required": ["name"],
            },
        },
    }

    def __init__(self, workspace_dir: str, project_root: str):
        self._workspace_dir = workspace_dir
        self._project_root = project_root

    def register(self, bus) -> None:
        bus.on_request(meta.ToolCallRequestEvent, self.execute)

    async def execute(self, call: LoadSkillCall) -> ToolCallResult:
        entries = discover_skills(self._workspace_dir, self._project_root)
        entry = next((e for e in entries if e.name == call.name), None)
        if entry is None:
            return ToolCallResult(error=f"skill '{call.name}' not found")
        if entry.disable_model_invocation:
            return ToolCallResult(error=f"skill '{call.name}' is not available for model invocation")
        if call.file_path:
            return self._load_reference(entry, call.file_path)
        return self._load_body(entry)

    def _load_reference(self, entry, file_path: str) -> ToolCallResult:
        try:
            resolved = resolve_skill_reference(entry, file_path)
        except WorkspaceEscapeError as exc:
            return ToolCallResult(error=str(exc))
        if not resolved.is_file():
            return ToolCallResult(error=f"reference file not found: {file_path}")
        content = resolved.read_text(encoding="utf-8", errors="replace")
        return ToolCallResult(
            output=f'<skill_reference name="{entry.name}" file="{file_path}">\n{content}\n</skill_reference>'
        )

    def _load_body(self, entry) -> ToolCallResult:
        _, body = parse_skill_file(entry.dir / "SKILL.md")
        references = sorted(
            str(p.relative_to(entry.dir)).replace("\\", "/")
            for p in entry.dir.rglob("*")
            if p.is_file() and p.name != "SKILL.md"
        )
        text = f'<skill name="{entry.name}">\n{body}\n</skill>'
        if references:
            text += (
                f'\n\nReference files (call load_skill(name="{entry.name}", '
                f"file_path=...) to read): {', '.join(references)}"
            )
        return ToolCallResult(output=text)
