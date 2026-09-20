from loguru import logger

from conic.plugins import meta
from conic.plugins.skills import discover_skills, visible_to_model
from conic.types.messages import BuildSystemPrompt

SKILLS_INTRO = (
    "Skills are reusable, task-specific instructions stored on disk. This list is "
    "a snapshot from session start; call list_skills for the current state."
)

SKILLS_OUTRO = (
    "These are summaries only, not instructions -- call load_skill with the exact "
    "name before acting on one, even if you think you already know how to do the "
    "task; it may encode this project's specific conventions."
)


class SkillsSectionPlugin:
    def __init__(self, workspace_dir: str, project_root: str):
        entries = visible_to_model(discover_skills(workspace_dir, project_root))
        logger.debug("system prompt: loaded {} skill(s): {}", len(entries), [e.name for e in entries])
        self._catalog_text = self._render(entries) if entries else None

    def register(self, bus) -> None:
        bus.on_chain(meta.BuildSystemPromptEvent, self.contribute)

    async def contribute(self, msg: BuildSystemPrompt) -> BuildSystemPrompt:
        if self._catalog_text is not None:
            msg.sections["skills"] = self._catalog_text
        return msg

    @staticmethod
    def _render(entries) -> str:
        lines = [f"- {e.name} ({e.scope}): {e.description}" for e in entries]
        catalog = "<available_skills>\n" + "\n".join(lines) + "\n</available_skills>"
        return f"{SKILLS_INTRO}\n\n{catalog}\n\n{SKILLS_OUTRO}"
