# Skill System (list_skills / load_skill) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a minimal skill system to conic: `SKILL.md` files discovered under a project-level and a session-level directory, surfaced to the model as a system prompt catalog plus two tools (`list_skills`, `load_skill`).

**Architecture:** A pure-function discovery/parsing module (`plugins/skills.py`, no bus dependency) shared by two new tool plugins (`plugins/tools/skills.py`, following the existing `read_file.py` pattern) and a new system prompt section plugin (`plugins/context/sections/skills.py`, following `tooling.py`). No changes to `react_loop.py`, `bus.py`, or `manager.py`'s `start_session`.

**Tech Stack:** Python 3.12+, `pyyaml` (new explicit dependency, for `SKILL.md` frontmatter), pytest (async tests using `tmp_path` for real directory fixtures — no mocking of the filesystem).

**Spec:** `docs/superpowers/specs/2026-09-19-conic-skill-system-design.md`

## Global Constraints

- Run `uv run pytest` after every change; do not commit if tests fail (AGENTS.md).
- Run `uv run ruff check` on changed files after each task; run `uv run ruff check src tests` before the final commit (AGENTS.md).
- No comments in source code (AGENTS.md / CONTRIB.md).
- Bus topic names must use constants from `src/conic/plugins/meta.py`, never hardcoded strings (AGENTS.md).
- Tools are instantiated by `manager.py` as `tool_cls(workspace_dir=...)` only — the two skill tools need `project_root` too, so it's bound via `Configured*` closure subclasses in `registry.py`, same as `ConfiguredBashToolPlugin`.
- All tool failures return `ToolCallResult(error=...)`, never raise (spec §7).
- `SKILL.md` frontmatter uses hyphenated keys (`disable-model-invocation`); `SkillEntry` uses the underscore field `disable_model_invocation` (spec §5).
- A malformed `SKILL.md` (missing frontmatter delimiters, missing `name`/`description`, invalid YAML) is skipped with a `logger.warning`, never raised out of `discover_skills` (spec §5, §10).
- Discovery roots: `<project_root>/.conic/skills/<name>/SKILL.md` (scope `"project"`) and `<workspace_dir>/skills/<name>/SKILL.md` (scope `"session"`); same `name` in both — session wins (spec §4).
- No new `Config` fields — discovery roots derive from the existing `config.project_root` and per-session `workspace_dir` (spec §8).
- The two skill tools are registered unconditionally (no API-key-style on/off switch) — an empty catalog is a normal, harmless state (spec §8).

---

## File Structure

- `pyproject.toml` — add `pyyaml` dependency
- `src/conic/plugins/skills.py` — new: `SkillEntry`, `SkillFormatError`, `parse_skill_file`, `discover_skills`, `visible_to_model`, `resolve_skill_reference`
- `src/conic/plugins/tools/skills.py` — new: `ListSkillsCall`, `ListSkillsToolPlugin`, `LoadSkillCall`, `LoadSkillToolPlugin`
- `src/conic/plugins/context/sections/skills.py` — new: `SkillsSectionPlugin`, `SKILLS_INTRO`, `SKILLS_OUTRO`
- `src/conic/plugins/context/system_prompt.py` — modify: `SECTION_ORDER` gains `"skills"` after `"tooling"`
- `src/conic/plugins/registry.py` — modify: wire the two tool classes + the section plugin
- `tests/plugins/test_skills.py`, `tests/plugins/tools/test_skills.py`, `tests/plugins/context/test_skills_section.py` — new
- `tests/plugins/context/test_system_prompt.py`, `tests/plugins/test_registry.py` — modify (append/adjust)

---

### Task 1: Skill discovery and parsing (`plugins/skills.py`)

**Files:**
- Create: `src/conic/plugins/skills.py`
- Modify: `pyproject.toml` (dependencies list)
- Test: `tests/plugins/test_skills.py`

**Interfaces:**
- Consumes: `resolve_within_workspace`, `WorkspaceEscapeError` from `conic.plugins.tools.base` (already exist).
- Produces:
  - `SkillEntry(name: str, description: str, disable_model_invocation: bool, scope: str, dir: Path)` — dataclass
  - `SkillFormatError(Exception)`
  - `parse_skill_file(path: Path) -> tuple[dict, str]`
  - `discover_skills(workspace_dir: str, project_root: str) -> list[SkillEntry]`
  - `visible_to_model(entries: list[SkillEntry]) -> list[SkillEntry]`
  - `resolve_skill_reference(entry: SkillEntry, file_path: str) -> Path`

- [ ] **Step 1: Write the failing tests** — create `tests/plugins/test_skills.py`:

```python
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
    write_skill(project_root / ".conic" / "skills", "deploy", "Deploy the app")
    write_skill(workspace_dir / "skills", "code-review", "Review a PR")

    entries = discover_skills(str(workspace_dir), str(project_root))

    assert [e.name for e in entries] == ["code-review", "deploy"]
    by_name = {e.name: e for e in entries}
    assert by_name["deploy"].scope == "project"
    assert by_name["code-review"].scope == "session"


def test_session_skill_overrides_project_skill_with_same_name(tmp_path):
    project_root = tmp_path / "proj"
    workspace_dir = tmp_path / "ws"
    write_skill(project_root / ".conic" / "skills", "deploy", "Project version")
    write_skill(workspace_dir / "skills", "deploy", "Session version")

    entries = discover_skills(str(workspace_dir), str(project_root))

    assert len(entries) == 1
    assert entries[0].scope == "session"
    assert entries[0].description == "Session version"


def test_missing_directories_return_empty_list(tmp_path):
    entries = discover_skills(str(tmp_path / "ws"), str(tmp_path / "proj"))
    assert entries == []


def test_skips_skill_missing_frontmatter_delimiters(tmp_path):
    project_root = tmp_path / "proj"
    skill_dir = project_root / ".conic" / "skills" / "broken"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("no frontmatter here", encoding="utf-8")

    entries = discover_skills(str(tmp_path / "ws"), str(project_root))
    assert entries == []


def test_skips_skill_missing_required_fields(tmp_path):
    project_root = tmp_path / "proj"
    skill_dir = project_root / ".conic" / "skills" / "broken"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: broken\n---\nbody\n", encoding="utf-8")

    entries = discover_skills(str(tmp_path / "ws"), str(project_root))
    assert entries == []


def test_skips_skill_with_invalid_yaml(tmp_path):
    project_root = tmp_path / "proj"
    skill_dir = project_root / ".conic" / "skills" / "broken"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: [unclosed\n---\nbody\n", encoding="utf-8")

    entries = discover_skills(str(tmp_path / "ws"), str(project_root))
    assert entries == []


def test_disable_model_invocation_parses_yaml_bool(tmp_path):
    project_root = tmp_path / "proj"
    write_skill(
        project_root / ".conic" / "skills", "hidden", "Hidden from the model",
        extra="disable-model-invocation: true\n",
    )
    entries = discover_skills(str(tmp_path / "ws"), str(project_root))
    assert entries[0].disable_model_invocation is True


def test_disable_model_invocation_defaults_to_false(tmp_path):
    project_root = tmp_path / "proj"
    write_skill(project_root / ".conic" / "skills", "visible", "Visible to the model")
    entries = discover_skills(str(tmp_path / "ws"), str(project_root))
    assert entries[0].disable_model_invocation is False


def test_visible_to_model_filters_disabled_skills(tmp_path):
    project_root = tmp_path / "proj"
    write_skill(project_root / ".conic" / "skills", "a", "A skill")
    write_skill(project_root / ".conic" / "skills", "b", "B skill", extra="disable-model-invocation: true\n")

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
    skill_dir = write_skill(project_root / ".conic" / "skills", "deploy", "Deploy")
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "rollback.md").write_text("rollback steps", encoding="utf-8")
    entries = discover_skills(str(tmp_path / "ws"), str(project_root))

    resolved = resolve_skill_reference(entries[0], "references/rollback.md")

    assert resolved.read_text(encoding="utf-8") == "rollback steps"


def test_resolve_skill_reference_rejects_escape(tmp_path):
    project_root = tmp_path / "proj"
    write_skill(project_root / ".conic" / "skills", "deploy", "Deploy")
    entries = discover_skills(str(tmp_path / "ws"), str(project_root))

    with pytest.raises(WorkspaceEscapeError):
        resolve_skill_reference(entries[0], "../other-skill/SKILL.md")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/plugins/test_skills.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'conic.plugins.skills'`

- [ ] **Step 3: Implement** — add to `pyproject.toml` dependencies:

```toml
    "pyyaml>=6.0",
```

Then run `uv sync`. Create `src/conic/plugins/skills.py`:

```python
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
    project_entries = _scan_root(Path(project_root) / ".conic" / "skills", "project")
    session_entries = _scan_root(Path(workspace_dir) / "skills", "session")
    merged = {**project_entries, **session_entries}
    return sorted(merged.values(), key=lambda e: e.name)


def visible_to_model(entries: list[SkillEntry]) -> list[SkillEntry]:
    return [e for e in entries if not e.disable_model_invocation]


def resolve_skill_reference(entry: SkillEntry, file_path: str) -> Path:
    return resolve_within_workspace(str(entry.dir), file_path)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/plugins/test_skills.py -v`
Expected: all PASS

- [ ] **Step 5: Run lint**

Run: `uv run ruff check src/conic/plugins/skills.py tests/plugins/test_skills.py`
Expected: clean

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/conic/plugins/skills.py tests/plugins/test_skills.py
git commit -m "feat: add skill discovery and frontmatter parsing"
```

---

### Task 2: `list_skills` and `load_skill` tool plugins

**Files:**
- Create: `src/conic/plugins/tools/skills.py`
- Test: `tests/plugins/tools/test_skills.py`

**Interfaces:**
- Consumes: `SkillEntry`, `discover_skills`, `visible_to_model`, `resolve_skill_reference`, `parse_skill_file` from Task 1 (`conic.plugins.skills`); `WorkspaceEscapeError` from `conic.plugins.tools.base`; `ToolCallResult` from `conic.types.messages`.
- Produces:
  - `ListSkillsCall()` — empty dataclass
  - `ListSkillsToolPlugin(workspace_dir: str, project_root: str)` — `llm_name = "list_skills"`, class attr `schema`, `register(bus)`, `async execute(call: ListSkillsCall) -> ToolCallResult`
  - `LoadSkillCall(name: str, file_path: str = "")`
  - `LoadSkillToolPlugin(workspace_dir: str, project_root: str)` — `llm_name = "load_skill"`, class attr `schema`, `register(bus)`, `async execute(call: LoadSkillCall) -> ToolCallResult`

- [ ] **Step 1: Write the failing tests** — create `tests/plugins/tools/test_skills.py`:

```python
from conic.plugins.tools.skills import ListSkillsCall, ListSkillsToolPlugin, LoadSkillCall, LoadSkillToolPlugin


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
    write_skill(tmp_path / "proj" / ".conic" / "skills", "deploy", "Deploy the app")
    result = await make_list_tool(tmp_path).execute(ListSkillsCall())
    assert result.output == "1 skill(s) available:\n- deploy (project): Deploy the app"


async def test_list_skills_hides_disabled_skills(tmp_path):
    write_skill(
        tmp_path / "proj" / ".conic" / "skills", "hidden", "Hidden",
        extra="disable-model-invocation: true\n",
    )
    result = await make_list_tool(tmp_path).execute(ListSkillsCall())
    assert result.output == "No skills available."


async def test_list_skills_rescans_disk_on_every_call(tmp_path):
    tool = make_list_tool(tmp_path)
    first = await tool.execute(ListSkillsCall())
    assert first.output == "No skills available."

    write_skill(tmp_path / "proj" / ".conic" / "skills", "deploy", "Deploy the app")
    second = await tool.execute(ListSkillsCall())
    assert "deploy" in second.output


async def test_load_skill_returns_body_without_frontmatter(tmp_path):
    write_skill(tmp_path / "proj" / ".conic" / "skills", "deploy", "Deploy the app", body="1. test\n2. ship")
    result = await make_load_tool(tmp_path).execute(LoadSkillCall(name="deploy"))
    assert result.error is None
    assert result.output == '<skill name="deploy">\n1. test\n2. ship\n</skill>'


async def test_load_skill_lists_reference_files(tmp_path):
    skill_dir = write_skill(tmp_path / "proj" / ".conic" / "skills", "deploy", "Deploy the app")
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "rollback.md").write_text("steps", encoding="utf-8")
    result = await make_load_tool(tmp_path).execute(LoadSkillCall(name="deploy"))
    assert "references/rollback.md" in result.output
    assert 'load_skill(name="deploy", file_path=...)' in result.output


async def test_load_skill_reads_reference_file(tmp_path):
    skill_dir = write_skill(tmp_path / "proj" / ".conic" / "skills", "deploy", "Deploy the app")
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
    write_skill(tmp_path / "proj" / ".conic" / "skills", "deploy", "Deploy the app")
    result = await make_load_tool(tmp_path).execute(
        LoadSkillCall(name="deploy", file_path="../other/SKILL.md")
    )
    assert result.error is not None


async def test_load_skill_missing_reference_file(tmp_path):
    write_skill(tmp_path / "proj" / ".conic" / "skills", "deploy", "Deploy the app")
    result = await make_load_tool(tmp_path).execute(
        LoadSkillCall(name="deploy", file_path="references/missing.md")
    )
    assert result.error is not None


async def test_load_skill_not_found(tmp_path):
    result = await make_load_tool(tmp_path).execute(LoadSkillCall(name="nope"))
    assert result.error == "skill 'nope' not found"


async def test_load_skill_rejects_disabled_skill(tmp_path):
    write_skill(
        tmp_path / "proj" / ".conic" / "skills", "hidden", "Hidden",
        extra="disable-model-invocation: true\n",
    )
    result = await make_load_tool(tmp_path).execute(LoadSkillCall(name="hidden"))
    assert result.error == "skill 'hidden' is not available for model invocation"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/plugins/tools/test_skills.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'conic.plugins.tools.skills'`

- [ ] **Step 3: Implement** — create `src/conic/plugins/tools/skills.py`:

```python
from dataclasses import dataclass

from conic.plugins import meta
from conic.plugins.skills import discover_skills, parse_skill_file, resolve_skill_reference, visible_to_model
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
    schema = {
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
    schema = {
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/plugins/tools/test_skills.py -v`
Expected: all PASS

- [ ] **Step 5: Run lint**

Run: `uv run ruff check src/conic/plugins/tools/skills.py tests/plugins/tools/test_skills.py`
Expected: clean

- [ ] **Step 6: Commit**

```bash
git add src/conic/plugins/tools/skills.py tests/plugins/tools/test_skills.py
git commit -m "feat: add list_skills and load_skill tool plugins"
```

---

### Task 3: system prompt `skills` section

**Files:**
- Create: `src/conic/plugins/context/sections/skills.py`
- Modify: `src/conic/plugins/context/system_prompt.py` (`SECTION_ORDER`)
- Test: `tests/plugins/context/test_skills_section.py`
- Test: `tests/plugins/context/test_system_prompt.py` (append)

**Interfaces:**
- Consumes: `discover_skills`, `visible_to_model` from Task 1.
- Produces: `SkillsSectionPlugin(workspace_dir: str, project_root: str)` — `register(bus)`, `async contribute(msg: BuildSystemPrompt) -> BuildSystemPrompt`; module constants `SKILLS_INTRO`, `SKILLS_OUTRO`. `system_prompt.SECTION_ORDER` gains `"skills"` right after `"tooling"`.

- [ ] **Step 1: Write the failing tests** — create `tests/plugins/context/test_skills_section.py`:

```python
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
    write_skill(tmp_path / "proj" / ".conic" / "skills", "deploy", "Deploy the app")
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
        tmp_path / "proj" / ".conic" / "skills", "hidden", "Hidden",
        extra="disable-model-invocation: true\n",
    )
    plugin = SkillsSectionPlugin(workspace_dir=str(tmp_path / "ws"), project_root=str(tmp_path / "proj"))
    bus = MessageBus()
    plugin.register(bus)
    result = await bus.chain(meta.BuildSystemPromptEvent, BuildSystemPrompt(sections={}))
    assert "skills" not in result.sections
```

Append to `tests/plugins/context/test_system_prompt.py`:

```python
def test_section_order_places_skills_right_after_tooling():
    from conic.plugins.context.system_prompt import SECTION_ORDER
    assert SECTION_ORDER.index("skills") == SECTION_ORDER.index("tooling") + 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/plugins/context/test_skills_section.py tests/plugins/context/test_system_prompt.py -v`
Expected: FAIL — `ModuleNotFoundError` for the new section test file, `ValueError: 'skills' is not in list` for the appended test

- [ ] **Step 3: Implement** — create `src/conic/plugins/context/sections/skills.py`:

```python
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
```

In `src/conic/plugins/context/system_prompt.py`, change:

```python
SECTION_ORDER = ["identity", "tooling", "workspace", "runtime", "execution"]
```

to:

```python
SECTION_ORDER = ["identity", "tooling", "skills", "workspace", "runtime", "execution"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/plugins/context/test_skills_section.py tests/plugins/context/test_system_prompt.py -v`
Expected: all PASS

- [ ] **Step 5: Run lint**

Run: `uv run ruff check src/conic/plugins/context/sections/skills.py src/conic/plugins/context/system_prompt.py tests/plugins/context/test_skills_section.py tests/plugins/context/test_system_prompt.py`
Expected: clean

- [ ] **Step 6: Commit**

```bash
git add src/conic/plugins/context/sections/skills.py src/conic/plugins/context/system_prompt.py tests/plugins/context/test_skills_section.py tests/plugins/context/test_system_prompt.py
git commit -m "feat: add skills system prompt section"
```

---

### Task 4: registry wiring

**Files:**
- Modify: `src/conic/plugins/registry.py`
- Modify: `tests/plugins/test_registry.py`

**Interfaces:**
- Consumes: `ListSkillsToolPlugin`, `LoadSkillToolPlugin` from Task 2; `SkillsSectionPlugin` from Task 3.
- Produces: `build_plugin_set(config).tool_classes` includes `ConfiguredListSkillsToolPlugin` and `ConfiguredLoadSkillToolPlugin` (subclasses of the Task 2 classes, unconditionally); the `SystemPromptPlugin` built in `context_plugins` includes a `SkillsSectionPlugin(ws, config.project_root)` instance right after `ToolingSectionPlugin`.

- [ ] **Step 1: Write the failing tests** — in `tests/plugins/test_registry.py`, add the import:

```python
from conic.plugins.tools.skills import ListSkillsToolPlugin, LoadSkillToolPlugin
```

Replace the existing `test_build_plugin_set_wires_the_four_v1_tools` with:

```python
def test_build_plugin_set_wires_the_v1_tools():
    plugin_set = build_plugin_set(make_config())
    assert issubclass(plugin_set.tool_classes[0], BashToolPlugin)
    assert plugin_set.tool_classes[1:4] == (ReadFileToolPlugin, WriteFileToolPlugin, EditFileToolPlugin)
    assert issubclass(plugin_set.tool_classes[4], ListSkillsToolPlugin)
    assert issubclass(plugin_set.tool_classes[5], LoadSkillToolPlugin)
```

Append:

```python
def test_build_plugin_set_wires_skill_tools_with_project_root():
    plugin_set = build_plugin_set(make_config())
    list_cls = next(c for c in plugin_set.tool_classes if issubclass(c, ListSkillsToolPlugin))
    load_cls = next(c for c in plugin_set.tool_classes if issubclass(c, LoadSkillToolPlugin))

    list_tool = list_cls(workspace_dir="/tmp/ws")
    load_tool = load_cls(workspace_dir="/tmp/ws")

    assert list_tool._workspace_dir == "/tmp/ws"
    assert list_tool._project_root == "/tmp"
    assert load_tool._workspace_dir == "/tmp/ws"
    assert load_tool._project_root == "/tmp"


def test_build_plugin_set_wires_skills_section_plugin():
    from conic.plugins.context.sections.skills import SkillsSectionPlugin

    plugin_set = build_plugin_set(make_config())
    system_prompt_plugin = plugin_set.context_plugins[1]("/tmp/ws", [])
    kinds = [type(p) for p in system_prompt_plugin._section_plugins]
    assert SkillsSectionPlugin in kinds
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/plugins/test_registry.py -v -k skill`
Expected: FAIL — `list_skills`/`load_skill` classes are not in `tool_classes` yet, `SkillsSectionPlugin` is not in the section list yet

- [ ] **Step 3: Implement** — in `src/conic/plugins/registry.py`, add imports:

```python
from conic.plugins.context.sections.skills import SkillsSectionPlugin
from conic.plugins.tools.skills import ListSkillsToolPlugin, LoadSkillToolPlugin
```

Change the `tool_classes` initial list to include the two skill tools right after the base four:

```python
    class ConfiguredListSkillsToolPlugin(ListSkillsToolPlugin):
        def __init__(self, workspace_dir: str):
            super().__init__(workspace_dir=workspace_dir, project_root=config.project_root)

    class ConfiguredLoadSkillToolPlugin(LoadSkillToolPlugin):
        def __init__(self, workspace_dir: str):
            super().__init__(workspace_dir=workspace_dir, project_root=config.project_root)

    tool_classes: list[type] = [
        ConfiguredBashToolPlugin, ReadFileToolPlugin, WriteFileToolPlugin, EditFileToolPlugin,
        ConfiguredListSkillsToolPlugin, ConfiguredLoadSkillToolPlugin,
    ]
```

(this replaces the existing `tool_classes: list[type] = [ConfiguredBashToolPlugin, ReadFileToolPlugin, WriteFileToolPlugin, EditFileToolPlugin]` line — keep everything below it, including the `tavily_api_key`/`firecrawl_api_key` conditional appends, unchanged).

In the `context_plugins` tuple, change:

```python
            lambda ws, schemas: SystemPromptPlugin([
                IdentitySectionPlugin(prompts.get("identity", "")),
                ToolingSectionPlugin(schemas),
                WorkspaceSectionPlugin(ws),
                RuntimeSectionPlugin(),
                ExecutionBiasSectionPlugin(prompts.get("execution", "")),
            ]),
```

to:

```python
            lambda ws, schemas: SystemPromptPlugin([
                IdentitySectionPlugin(prompts.get("identity", "")),
                ToolingSectionPlugin(schemas),
                SkillsSectionPlugin(ws, config.project_root),
                WorkspaceSectionPlugin(ws),
                RuntimeSectionPlugin(),
                ExecutionBiasSectionPlugin(prompts.get("execution", "")),
            ]),
```

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -q`
Expected: all PASS

- [ ] **Step 5: Run lint**

Run: `uv run ruff check src tests`
Expected: clean; fix anything reported

- [ ] **Step 6: Commit**

```bash
git add src/conic/plugins/registry.py tests/plugins/test_registry.py
git commit -m "feat: wire skill tools and skills section into the plugin set"
```

---

## Final Verification

- [ ] `uv run pytest -q` — full suite green
- [ ] `uv run ruff check src tests` — clean
- [ ] Spec sweep against `docs/superpowers/specs/2026-09-19-conic-skill-system-design.md`: §3 frontmatter fields (Task 1), §4 discovery roots and precedence (Task 1), §5 module layout and boundary-check reuse (Task 1), §6 system prompt section text and `SECTION_ORDER` placement (Task 3), §7 both tool schemas and error paths (Task 2), §8 registry wiring with no new Config fields (Task 4), §10 test list all covered
- [ ] Manual smoke (optional): create `.conic/skills/hello/SKILL.md` with `name: hello` / `description: Say hello`, run conic, ask the bot what skills are available and to use `hello`
