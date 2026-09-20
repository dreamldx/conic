from conic.plugins.tools.skills import (
    ListSkillsCall,
    ListSkillsToolPlugin,
    LoadSkillCall,
    LoadSkillToolPlugin,
)


def write_skill(root, name, description, body="do the thing", extra=""):
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n{extra}---\n{body}\n", encoding="utf-8"
    )
    return skill_dir


def make_list_tool(tmp_path):
    return ListSkillsToolPlugin(workspace_dir=str(tmp_path / "ws"), project_root=str(tmp_path / "proj"))


def make_load_tool(tmp_path):
    return LoadSkillToolPlugin(workspace_dir=str(tmp_path / "ws"), project_root=str(tmp_path / "proj"))


async def test_list_skills_reports_no_skills(tmp_path):
    result = await make_list_tool(tmp_path).execute(ListSkillsCall())
    assert result.output == "No skills available."


async def test_list_skills_formats_entries_with_scope(tmp_path):
    write_skill(tmp_path / "proj" / "skills", "deploy", "Deploy the app")
    result = await make_list_tool(tmp_path).execute(ListSkillsCall())
    assert result.output == "1 skill(s) available:\n- deploy (project): Deploy the app"


async def test_list_skills_hides_disabled_skills(tmp_path):
    write_skill(
        tmp_path / "proj" / "skills", "hidden", "Hidden",
        extra="disable-model-invocation: true\n",
    )
    result = await make_list_tool(tmp_path).execute(ListSkillsCall())
    assert result.output == "No skills available."


async def test_list_skills_rescans_disk_on_every_call(tmp_path):
    tool = make_list_tool(tmp_path)
    first = await tool.execute(ListSkillsCall())
    assert first.output == "No skills available."

    write_skill(tmp_path / "proj" / "skills", "deploy", "Deploy the app")
    second = await tool.execute(ListSkillsCall())
    assert "deploy" in second.output


async def test_load_skill_returns_body_without_frontmatter(tmp_path):
    write_skill(tmp_path / "proj" / "skills", "deploy", "Deploy the app", body="1. test\n2. ship")
    result = await make_load_tool(tmp_path).execute(LoadSkillCall(name="deploy"))
    assert result.error is None
    assert result.output == '<skill name="deploy">\n1. test\n2. ship\n</skill>'


async def test_load_skill_lists_reference_files(tmp_path):
    skill_dir = write_skill(tmp_path / "proj" / "skills", "deploy", "Deploy the app")
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "rollback.md").write_text("steps", encoding="utf-8")
    result = await make_load_tool(tmp_path).execute(LoadSkillCall(name="deploy"))
    assert "references/rollback.md" in result.output
    assert 'load_skill(name="deploy", file_path=...)' in result.output


async def test_load_skill_reads_reference_file(tmp_path):
    skill_dir = write_skill(tmp_path / "proj" / "skills", "deploy", "Deploy the app")
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "rollback.md").write_text("rollback steps", encoding="utf-8")
    result = await make_load_tool(tmp_path).execute(
        LoadSkillCall(name="deploy", file_path="references/rollback.md")
    )
    assert result.error is None
    assert result.output == (
        '<skill_reference name="deploy" file="references/rollback.md">\nrollback steps\n</skill_reference>'
    )


async def test_load_skill_rejects_escaping_reference_path(tmp_path):
    write_skill(tmp_path / "proj" / "skills", "deploy", "Deploy the app")
    result = await make_load_tool(tmp_path).execute(
        LoadSkillCall(name="deploy", file_path="../other/SKILL.md")
    )
    assert result.error is not None


async def test_load_skill_missing_reference_file(tmp_path):
    write_skill(tmp_path / "proj" / "skills", "deploy", "Deploy the app")
    result = await make_load_tool(tmp_path).execute(
        LoadSkillCall(name="deploy", file_path="references/missing.md")
    )
    assert result.error is not None


async def test_load_skill_not_found(tmp_path):
    result = await make_load_tool(tmp_path).execute(LoadSkillCall(name="nope"))
    assert result.error == "skill 'nope' not found"


async def test_load_skill_rejects_disabled_skill(tmp_path):
    write_skill(
        tmp_path / "proj" / "skills", "hidden", "Hidden",
        extra="disable-model-invocation: true\n",
    )
    result = await make_load_tool(tmp_path).execute(LoadSkillCall(name="hidden"))
    assert result.error == "skill 'hidden' is not available for model invocation"
