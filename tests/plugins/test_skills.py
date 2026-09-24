import pytest

from conic.plugins.skills import (
    discover_skills,
    parse_skill_file,
    resolve_skill_reference,
    visible_to_model,
)
from conic.plugins.tools.base import WorkspaceEscapeError


def write_skill(root, name, description, body="do the thing", extra=""):
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n{extra}---\n{body}\n", encoding="utf-8"
    )
    return skill_dir


def test_discovers_project_and_session_skills(tmp_path):
    project_root = tmp_path / "proj"
    workspace_dir = tmp_path / "ws"
    write_skill(project_root / "skills", "deploy", "Deploy the app")
    write_skill(workspace_dir / "skills", "code-review", "Review a PR")

    entries = discover_skills(str(workspace_dir), str(project_root))

    assert [e.name for e in entries] == ["code-review", "deploy"]
    by_name = {e.name: e for e in entries}
    assert by_name["deploy"].scope == "project"
    assert by_name["code-review"].scope == "session"


def test_session_skill_overrides_project_skill_with_same_name(tmp_path):
    project_root = tmp_path / "proj"
    workspace_dir = tmp_path / "ws"
    write_skill(project_root / "skills", "deploy", "Project version")
    write_skill(workspace_dir / "skills", "deploy", "Session version")

    entries = discover_skills(str(workspace_dir), str(project_root))

    assert len(entries) == 1
    assert entries[0].scope == "session"
    assert entries[0].description == "Session version"


def test_discovers_npx_skills_add_directories(tmp_path):
    project_root = tmp_path / "proj"
    workspace_dir = tmp_path / "ws"
    write_skill(project_root / ".agents" / "skills", "pdf", "Handle PDFs")
    write_skill(workspace_dir / ".agents" / "skills", "xlsx", "Handle sheets")
    write_skill(project_root / ".agents" / "skills", "deploy", "Agents version")
    write_skill(project_root / "skills", "deploy", "Skills version")

    by_name = {e.name: e for e in discover_skills(str(workspace_dir), str(project_root))}

    assert by_name["pdf"].scope == "project"
    assert by_name["xlsx"].scope == "session"
    assert by_name["deploy"].description == "Skills version"


def test_discovers_global_skills_with_lowest_priority(tmp_path, fake_home):
    project_root = tmp_path / "proj"
    write_skill(fake_home / ".agents" / "skills", "notion-cli", "Global notion")
    write_skill(fake_home / ".agents" / "skills", "deploy", "Global deploy")
    write_skill(project_root / "skills", "deploy", "Project deploy")

    by_name = {e.name: e for e in discover_skills(str(tmp_path / "ws"), str(project_root))}

    assert by_name["notion-cli"].scope == "global"
    assert by_name["deploy"].scope == "project"


def test_missing_directories_return_empty_list(tmp_path):
    entries = discover_skills(str(tmp_path / "ws"), str(tmp_path / "proj"))
    assert entries == []


def test_skips_skill_missing_frontmatter_delimiters(tmp_path):
    project_root = tmp_path / "proj"
    skill_dir = project_root / "skills" / "broken"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("no frontmatter here", encoding="utf-8")

    entries = discover_skills(str(tmp_path / "ws"), str(project_root))
    assert entries == []


def test_skips_skill_missing_required_fields(tmp_path):
    project_root = tmp_path / "proj"
    skill_dir = project_root / "skills" / "broken"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: broken\n---\nbody\n", encoding="utf-8")

    entries = discover_skills(str(tmp_path / "ws"), str(project_root))
    assert entries == []


def test_skips_skill_with_invalid_yaml(tmp_path):
    project_root = tmp_path / "proj"
    skill_dir = project_root / "skills" / "broken"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: [unclosed\n---\nbody\n", encoding="utf-8")

    entries = discover_skills(str(tmp_path / "ws"), str(project_root))
    assert entries == []


def test_disable_model_invocation_parses_yaml_bool(tmp_path):
    project_root = tmp_path / "proj"
    write_skill(
        project_root / "skills", "hidden", "Hidden from the model",
        extra="disable-model-invocation: true\n",
    )
    entries = discover_skills(str(tmp_path / "ws"), str(project_root))
    assert entries[0].disable_model_invocation is True


def test_disable_model_invocation_defaults_to_false(tmp_path):
    project_root = tmp_path / "proj"
    write_skill(project_root / "skills", "visible", "Visible to the model")
    entries = discover_skills(str(tmp_path / "ws"), str(project_root))
    assert entries[0].disable_model_invocation is False


def test_visible_to_model_filters_disabled_skills(tmp_path):
    project_root = tmp_path / "proj"
    write_skill(project_root / "skills", "a", "A skill")
    write_skill(project_root / "skills", "b", "B skill", extra="disable-model-invocation: true\n")

    entries = discover_skills(str(tmp_path / "ws"), str(project_root))
    visible = visible_to_model(entries)

    assert [e.name for e in visible] == ["a"]


def test_parse_skill_file_returns_frontmatter_and_body(tmp_path):
    path = tmp_path / "SKILL.md"
    path.write_text("---\nname: x\ndescription: y\n---\nbody text\n", encoding="utf-8")
    frontmatter, body = parse_skill_file(path)
    assert frontmatter == {"name": "x", "description": "y"}
    assert body == "body text"


def test_resolve_skill_reference_reads_within_skill_dir(tmp_path):
    project_root = tmp_path / "proj"
    skill_dir = write_skill(project_root / "skills", "deploy", "Deploy")
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "rollback.md").write_text("rollback steps", encoding="utf-8")
    entries = discover_skills(str(tmp_path / "ws"), str(project_root))

    resolved = resolve_skill_reference(entries[0], "references/rollback.md")

    assert resolved.read_text(encoding="utf-8") == "rollback steps"


def test_resolve_skill_reference_rejects_escape(tmp_path):
    project_root = tmp_path / "proj"
    write_skill(project_root / "skills", "deploy", "Deploy")
    entries = discover_skills(str(tmp_path / "ws"), str(project_root))

    with pytest.raises(WorkspaceEscapeError):
        resolve_skill_reference(entries[0], "../other-skill/SKILL.md")
