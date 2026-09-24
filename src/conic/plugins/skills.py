from dataclasses import dataclass
from pathlib import Path

import yaml
from loguru import logger

from conic.plugins.tools.base import resolve_within_workspace


@dataclass
class SkillEntry:
    name: str
    description: str
    disable_model_invocation: bool
    scope: str
    dir: Path


class SkillFormatError(Exception):
    pass


def parse_skill_file(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    if len(parts) < 3 or parts[0].strip() != "":
        raise SkillFormatError(f"{path}: missing frontmatter delimiters")
    frontmatter = yaml.safe_load(parts[1])
    if frontmatter is None:
        frontmatter = {}
    if not isinstance(frontmatter, dict):
        raise SkillFormatError(f"{path}: frontmatter is not a mapping")
    return frontmatter, parts[2].strip()


def _load_entry(skill_dir: Path, scope: str) -> SkillEntry | None:
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return None
    try:
        frontmatter, _ = parse_skill_file(skill_md)
    except (SkillFormatError, yaml.YAMLError) as exc:
        logger.warning("skipping malformed skill at {}: {}", skill_md, exc)
        return None
    name = frontmatter.get("name")
    description = frontmatter.get("description")
    if not name or not description:
        logger.warning("skipping skill at {}: missing name or description", skill_md)
        return None
    return SkillEntry(
        name=str(name),
        description=str(description),
        disable_model_invocation=bool(frontmatter.get("disable-model-invocation", False)),
        scope=scope,
        dir=skill_dir,
    )


def _scan_root(root: Path, scope: str) -> dict[str, SkillEntry]:
    entries: dict[str, SkillEntry] = {}
    if not root.is_dir():
        return entries
    for skill_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        entry = _load_entry(skill_dir, scope)
        if entry is not None:
            entries[entry.name] = entry
    return entries


def discover_skills(workspace_dir: str, project_root: str) -> list[SkillEntry]:
    project_base = Path(project_root)
    session_base = Path(workspace_dir)
    merged = {
        **_scan_root(Path.home() / ".agents" / "skills", "global"),
        **_scan_root(project_base / ".agents" / "skills", "project"),
        **_scan_root(project_base / "skills", "project"),
        **_scan_root(session_base / ".agents" / "skills", "session"),
        **_scan_root(session_base / "skills", "session"),
    }
    return sorted(merged.values(), key=lambda e: e.name)


def visible_to_model(entries: list[SkillEntry]) -> list[SkillEntry]:
    return [e for e in entries if not e.disable_model_invocation]


def resolve_skill_reference(entry: SkillEntry, file_path: str) -> Path:
    return resolve_within_workspace(str(entry.dir), file_path)
