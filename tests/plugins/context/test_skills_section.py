from loguru import logger

from conic.core.bus import MessageBus
from conic.plugins import meta
from conic.plugins.context.sections.skills import SkillsSectionPlugin
from conic.types.messages import BuildSystemPrompt


def write_skill(root, name, description, extra=""):
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n{extra}---\nbody\n", encoding="utf-8"
    )
    return skill_dir


async def test_no_section_when_no_skills(tmp_path):
    plugin = SkillsSectionPlugin(workspace_dir=str(tmp_path / "ws"), project_root=str(tmp_path / "proj"))
    bus = MessageBus()
    plugin.register(bus)
    result = await bus.chain(meta.BuildSystemPromptEvent, BuildSystemPrompt(sections={}))
    assert "skills" not in result.sections


async def test_section_lists_scoped_catalog(tmp_path):
    write_skill(tmp_path / "proj" / "skills", "deploy", "Deploy the app")
    write_skill(tmp_path / "ws" / "skills", "code-review", "Review a PR")
    plugin = SkillsSectionPlugin(workspace_dir=str(tmp_path / "ws"), project_root=str(tmp_path / "proj"))
    bus = MessageBus()
    plugin.register(bus)
    result = await bus.chain(meta.BuildSystemPromptEvent, BuildSystemPrompt(sections={}))
    section = result.sections["skills"]
    assert "<available_skills>" in section
    assert "- deploy (project): Deploy the app" in section
    assert "- code-review (session): Review a PR" in section
    assert "call list_skills for the current state" in section
    assert "call load_skill with the exact" in section


async def test_section_hides_disabled_skills(tmp_path):
    write_skill(
        tmp_path / "proj" / "skills", "hidden", "Hidden",
        extra="disable-model-invocation: true\n",
    )
    plugin = SkillsSectionPlugin(workspace_dir=str(tmp_path / "ws"), project_root=str(tmp_path / "proj"))
    bus = MessageBus()
    plugin.register(bus)
    result = await bus.chain(meta.BuildSystemPromptEvent, BuildSystemPrompt(sections={}))
    assert "skills" not in result.sections


def test_construction_logs_loaded_skill_count_and_names_at_debug(tmp_path):
    write_skill(tmp_path / "proj" / "skills", "deploy", "Deploy the app")

    logged = []
    sink_id = logger.add(lambda msg: logged.append(msg.record["message"]), level="DEBUG")
    try:
        SkillsSectionPlugin(workspace_dir=str(tmp_path / "ws"), project_root=str(tmp_path / "proj"))
    finally:
        logger.remove(sink_id)

    assert any("1" in m and "deploy" in m for m in logged)
