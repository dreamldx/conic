# Skill 系统设计:load_skill 与 list_skills

日期:2026-09-19
状态:已评审,待实现

## 1. 背景与目标

为 conic 加入一套最小可用的 Skill 系统:把可复用的、任务特定的指令(部署步骤、代码评审清单、项目专属工作流)存成磁盘上的 `SKILL.md` 文件,模型按需加载,而不是把这些内容硬编码进 `prompts/execution.md` 或每次对话重复输入。

设计基于两份既有调研文档的结论:

- `docs/skill-system-survey.md`——OpenClaw、Hermes Agent、DeepSeek Harness、Claude Code、Codex CLI 五家的 skill/prompt 注入机制调研,结论推荐"渐进披露 + 单一加载工具 + system-reminder 式注入"
- `docs/improvement-with-hermes-openclaw-deepseek.md`——三家 loop 实现的深挖,与本设计不直接相关但确认了 conic 的插件化架构方向是对的

范围明确限定为 **v1 最小可用**:只做"发现 + 展示 + 加载"三件事。agent 自主创建 skill 的引导性 prompt、安全扫描、条件 gating(os/bin/env)、bundled 可执行脚本、`allowed-tools`/fork 隔离——全部不在这次设计与实现范围内,列在 §9。

## 2. 现状与约束

在 `src/conic/plugins/registry.py`(`build_plugin_set`)、`plugins/context/system_prompt.py`、`plugins/loops/react_loop.py`、`plugins/tools/read_file.py` 中确认的关键架构事实,决定了本设计的形状:

1. **system prompt 只在会话第一次调用时组装并缓存**(`SystemPromptPlugin._cached_content`),此后不再重算。因此 skill 目录清单适合做成"会话启动时快照一次",不需要每步重算的机制。
2. **`PluginManager.start_session` 把工具类的构造签名写死为 `cls(workspace_dir=...)`**,额外配置一律靠 `registry.py` 里已有的"`Configured*` 子类闭包"套路注入(`ConfiguredBashToolPlugin` 的写法),不改 `manager.py`。
3. **工具就是普通 plugin**:`llm_name` + 类属性 `schema` + `register(bus)` + `execute(payload) -> ToolCallResult`,走现成的 `ToolCallRequestEvent` 分发,`react_loop.py` 不需要为新工具做任何特化。
4. **`write_file`/`edit_file`/`read_file` 已经能读写 `<workspace>/...`**——凡是放在会话 workspace 目录里的东西,天然可被模型用现有工具增删改查。

结论:skill 系统不改动 `react_loop.py`/`bus.py`/`manager.py` 的 `start_session`,只新增两个工具插件 + 一个 system prompt section + 一个共享的纯函数模块。

## 3. SKILL.md 格式

比五家调研到的格式都精简,只保留三个字段:

```yaml
---
name: deploy
description: Deploy the current branch to staging. Use when the user asks to deploy or ship.
disable-model-invocation: false
---
Deploy the current branch:
1. Run the test suite
2. Build the application
3. Push to the deployment target

参考文件放同目录 references/*.md,load_skill(name, file_path=...) 按需读取。
```

- `name`(必填):目录名与之无关,catalog 与 `load_skill` 一律按这个字段查找(五家一致的做法)
- `description`(必填):同时说明做什么和何时用(Claude Code 的要求,直接照搬)
- `disable-model-invocation`(可选,默认 `false`):为 `true` 时该 skill 从 catalog 和 `list_skills` 结果中隐藏,`load_skill` 直接调用也会被拒绝——v1 没有斜杠命令系统,所以这个字段目前只有"对模型隐藏"这一层含义,没有"仅限人工触发"的对应通路

明确不引入的字段(理由见 §9):`allowed-tools`、`context: fork`、`agent`、`background`(Claude Code,需要 subagent,conic 还没有)、`metadata.*.requires`(OpenClaw 的 gating)、`version`/`author`/`license`/`platforms`/`tags`(Hermes,没有浏览/分类工具就没有意义)。

不支持 bundled 可执行脚本(`scripts/`):项目级 skill 目录在 session workspace 之外,`bash` 工具的执行边界还没有覆盖到那里,强行支持需要额外打通 bash 的路径边界,v1 不做,留在 §9。

## 4. 发现目录与优先级

两级目录,同名时会话级覆盖项目级:

| 优先级 | scope | 路径 | 用途 |
|---|---|---|---|
| 高 | `session` | `<workspace_dir>/skills/<name>/SKILL.md` | 会话私有;天然可被 `write_file`/`edit_file` 写入,为后续"自主创建"铺路,但本次不加任何鼓励模型去写的 prompt 文案 |
| 低 | `project` | `<project_root>/skills/<name>/SKILL.md` | 人写、随仓库提交、所有会话共享 |

不引入用户级(`~`)、bundled、插件级目录——conic 是单一部署的 Discord bot,不是多租户 CLI,这些目录在五家调研里对应的是"多用户/插件市场"场景,与 conic 现状不符(YAGNI)。

两个目录都不存在或都为空时,`list_skills` 返回"没有可用 skill"的提示,`load_skill` 对任何名字返回未找到错误,system prompt 不出现 `skills` 章节——不需要任何开关配置。

## 5. 架构:三个新模块

```
src/conic/plugins/skills.py              # 纯函数:发现 + 解析 + 路径解析,无 bus 依赖
src/conic/plugins/tools/skills.py        # ListSkillsToolPlugin / LoadSkillToolPlugin
src/conic/plugins/context/sections/skills.py  # SkillsSectionPlugin
```

`skills.py`(纯函数层,两个 tool 插件和 section 插件都依赖它,互相之间零耦合):

```python
@dataclass
class SkillEntry:
    name: str
    description: str
    disable_model_invocation: bool
    scope: str          # "project" | "session"
    dir: Path           # SKILL.md 所在目录


class SkillFormatError(Exception):
    pass


def discover_skills(workspace_dir: str, project_root: str) -> list[SkillEntry]:
    ...  # 扫描两个 root,同名 session 覆盖 project,按 name 排序;单个 SKILL.md 解析失败只 logger.warning 并跳过,不抛出


def visible_to_model(entries: list[SkillEntry]) -> list[SkillEntry]:
    return [e for e in entries if not e.disable_model_invocation]


def parse_skill_file(path: Path) -> tuple[dict, str]:
    ...  # 用 pyyaml 解析 frontmatter,返回 (frontmatter dict, body markdown)


def resolve_skill_reference(entry: SkillEntry, file_path: str) -> Path:
    return resolve_within_workspace(str(entry.dir), file_path)  # 复用 tools/base.py 的边界检查,root 换成 skill 自己的目录
```

`resolve_within_workspace` 的实现本来就与"workspace"这个名字无关(只是一个"路径必须落在给定 root 内"的通用检查),直接对 skill 自己的目录复用,不需要新写一个边界检查函数。

frontmatter 的 key 一律用连字符(`disable-model-invocation`,与 agentskills.io 及五家保持一致);`parse_skill_file` 只负责把 YAML 解析成原始 dict,把连字符 key 转换成 `SkillEntry` 的下划线字段是 `discover_skills` 的职责。`name` 字段不做字符集校验(不照抄 Claude Code 的 64 字符/小写/连字符限制)——v1 信任本地文件作者,原样使用 frontmatter 里的字符串作为 catalog 展示与 `load_skill` 查找 key。

新增依赖:`pyyaml`(frontmatter 是标准 YAML,手撸解析器容易在引号/多行 description 上出错,不如显式声明这个标准库——参照 `web-tools-design.md` "即使可能被间接依赖也显式声明"的既有原则)。

## 6. System Prompt 注入

新增 `SkillsSectionPlugin`,插入 `SECTION_ORDER`(`system_prompt.py`)的 `tooling` 之后:

```python
SECTION_ORDER = ["identity", "tooling", "skills", "workspace", "runtime", "execution"]
```

会话启动时(即 `SystemPromptPlugin` 第一次组装并缓存时)调用一次 `discover_skills()`,之后不再重算——与既有的"system prompt 只算一次"的缓存语义完全一致,零新机制。没有可见 skill 时不贡献该 section(照抄 `ToolingSectionPlugin` 的 `if not ...: return msg` 写法)。

文案融合五家的三点(不采用 Hermes"MUST/err on the side of loading"式的高压措辞——面向单人使用的小项目不需要那种合规压力):

```
## skills
Skills are reusable, task-specific instructions stored on disk. This list is
a snapshot from session start; call list_skills for the current state.

<available_skills>
- deploy (project): Deploy the current branch to staging. Use when the user asks to deploy or ship.
- code-review (session): Run this project's PR review checklist.
</available_skills>

These are summaries only, not instructions -- call load_skill with the exact
name before acting on one, even if you think you already know how to do the
task; it may encode this project's specific conventions.
```

来源对照:`<available_skills>` 纯 XML 清单取自 OpenClaw;"summaries only, not instructions"取自 DeepSeek Harness 的"加载前不要照做";"even if you think you already know"取自 Hermes 但去掉了强制语气;"snapshot from session start; call list_skills for current state"是 conic 自己的补充,因为 conic 独有的两级视图(静态快照 + 实时 `list_skills`)在五家里都没有直接对应。

## 7. 两个工具

### list_skills

```json
{
  "type": "function",
  "function": {
    "name": "list_skills",
    "description": "List available skills as name + description pairs. Rescans disk on every call, so it reflects skills added after this session started. Call load_skill with a name to get its full instructions.",
    "parameters": {"type": "object", "properties": {}, "required": []}
  }
}
```

行为:每次调用都重新 `discover_skills()`(不读 system prompt 的缓存快照)——这是它存在的意义:system prompt 清单是启动时快照,`list_skills` 是"磁盘上现在到底有什么"的实时来源,顺带覆盖了会话中途新增 skill 的场景。输出:

```
2 skill(s) available:
- deploy (project): Deploy the current branch to staging. Use when the user asks to deploy or ship.
- code-review (session): Run this project's PR review checklist.
```

无可见 skill 时输出 `No skills available.`。

### load_skill

```json
{
  "type": "function",
  "function": {
    "name": "load_skill",
    "description": "Load the full instructions for a skill by name. Returns the SKILL.md body (not just the summary from list_skills or the system prompt) -- call this before acting on a skill, even if you think you already know how to do the task. If the skill has reference files, the result lists their names; pass one as file_path to read it.",
    "parameters": {
      "type": "object",
      "properties": {
        "name": {"type": "string", "description": "Exact skill name, as shown by list_skills or in the system prompt catalog."},
        "file_path": {"type": "string", "description": "Optional. A reference file listed in a previous load_skill result for this skill, e.g. 'references/rollback.md'."}
      },
      "required": ["name"]
    }
  }
}
```

行为:

- 找不到 `name` → `ToolCallResult(error="skill '<name>' not found")`
- 找到但 `disable_model_invocation` 为真 → `ToolCallResult(error="skill '<name>' is not available for model invocation")`
- 未传 `file_path`:返回去掉 frontmatter 的正文,包成 `<skill name="...">...</skill>`(风格参照 conic 已有的 `wrap_untrusted` 标签包裹惯例,而非直接照抄 DeepSeek 的 `<skill_content>`);若该 skill 目录下有除 `SKILL.md` 外的其他文件,末尾附一行可用文件名提示
- 传了 `file_path`:在该 skill 自己的目录内解析(`resolve_skill_reference`),越界或不存在 → `ToolCallResult(error=...)`;成功则返回文件原文,包成 `<skill_reference name="..." file="...">...</skill_reference>`

两个工具的 payload:

```python
@dataclass
class ListSkillsCall:
    pass

@dataclass
class LoadSkillCall:
    name: str
    file_path: str = ""
```

两个插件构造签名均为 `__init__(self, workspace_dir: str, project_root: str)`,`register(bus)` 挂 `on_request(ToolCallRequestEvent, execute)`;`ListSkillsToolPlugin` 不需要贡献 system prompt section(那是 `SkillsSectionPlugin` 的职责)。

## 8. registry.py 接线

`build_plugin_set` 里新增(套用 `ConfiguredBashToolPlugin` 的闭包子类模式,`manager.py` 零改动):

```python
class ConfiguredListSkillsToolPlugin(ListSkillsToolPlugin):
    def __init__(self, workspace_dir: str):
        super().__init__(workspace_dir=workspace_dir, project_root=config.project_root)

class ConfiguredLoadSkillToolPlugin(LoadSkillToolPlugin):
    def __init__(self, workspace_dir: str):
        super().__init__(workspace_dir=workspace_dir, project_root=config.project_root)
```

追加进 `tool_classes` 列表(无条件注册,不像 web tools 那样依赖 API key 开关——没有 skill 时两个工具只是返回空结果,不需要开关)。`context_plugins` 里的 `SystemPromptPlugin([...])` 列表里,在 `ToolingSectionPlugin(schemas)` 之后插入 `SkillsSectionPlugin(ws, config.project_root)`。

不新增任何 `Config` 字段——发现目录完全由已有的 `config.project_root` 和 per-session `workspace_dir` 推导得出。

## 9. v1 范围外(留给两份调研文档的"中长期"结论)

- agent 自主创建 skill 的引导性 prompt(Hermes 的 `skill_manage`/`/learn` 等价物)——会话级目录已经可写,但这次不在 prompt 里主动引导模型去写
- 安全扫描(Hermes 的 ~120 条威胁正则)
- 条件 gating(OpenClaw 的 `requires.bins/env/config`)
- bundled 可执行脚本(`scripts/`)——项目级目录在 workspace 边界之外,`bash` 工具够不到
- `allowed-tools`/`context: fork`——依赖 conic 目前没有的 subagent 机制
- 用户级/插件级发现目录、slash 命令直派发(`command-dispatch: tool`)

## 10. 测试策略

照 `tests/plugins/tools/` 与 `tests/plugins/context/` 现有风格,直接调 `execute()`/`contribute()`,用 `tmp_path` 构造真实目录结构(不 mock 文件系统):

- `tests/plugins/test_skills.py`(`discover_skills`/`parse_skill_file`/`visible_to_model`/`resolve_skill_reference`):项目级与会话级各一个 skill、同名时会话级覆盖、缺 frontmatter 分隔符/缺 `name`/`description`/非法 YAML 时跳过并不抛出、两个目录都不存在返回空列表、`disable_model_invocation` 默认 false 且能从 `"true"`/`true` 等 YAML 布尔正确解析、`resolve_skill_reference` 拒绝 `../` 越界
- `tests/plugins/tools/test_skills.py`(`ListSkillsToolPlugin`/`LoadSkillToolPlugin`):`list_skills` 输出格式与数量、`list_skills` 隐藏 `disable_model_invocation` 的 skill、`list_skills` 空目录返回提示文案、`list_skills` 每次调用重新扫描(会话中途新建文件后再调用能看到)、`load_skill` 返回正文并去除 frontmatter、`load_skill` 提示可用引用文件、`load_skill(file_path=...)` 读取引用文件、`load_skill` 未找到/越界/disable_model_invocation 三种错误路径
- `tests/plugins/context/test_skills_section.py`(`SkillsSectionPlugin`):有 skill 时贡献 `skills` section 且包含 `<available_skills>`、无 skill 时不贡献该 key、catalog 文本标注 `(project)`/`(session)`、`disable_model_invocation` 的 skill 不出现在 catalog 里
- `tests/plugins/context/test_system_prompt.py`(追加):`SECTION_ORDER` 包含 `"skills"` 且顺序在 `"tooling"` 之后
- `tests/plugins/test_registry.py`(追加):`build_plugin_set` 的 `tool_classes` 包含 `ListSkillsToolPlugin`/`LoadSkillToolPlugin` 子类;两个工具实例能拿到正确的 `workspace_dir`/`project_root`

## 附录:五家做法与本设计的取舍对照

| 维度 | Claude Code | Codex | OpenClaw | Hermes Agent | DeepSeek Harness(存疑,见下) | Conic v1 |
|---|---|---|---|---|---|---|
| 清单常驻位置 | system prompt(启动时) | developer message(每轮) | system prompt(XML) | system prompt(每轮,高压措辞) | catalog 消息(存疑) | system prompt(启动时快照,不逐轮重算) |
| 加载工具数量 | 0(自动触发) | 2(`skills.list`/`skills.read`) | 0(用通用 `read`) | 3(`skills_list`/`skill_view`/`skill_manage`) | 1(`skill`) | 2(`list_skills`/`load_skill`),但 `list_skills` 实时重扫,与静态快照分工明确 |
| 自主创建 | 否 | 否 | 草稿队列 | 是(`skill_manage`+`/learn`) | 未知 | 否(但会话级目录已可写,留作后续) |
| 发现目录数 | 5+ | 3 域 | 7 级 | 多目录 | 6 级(存疑) | 2 级(project/session) |

DeepSeek Harness 一行的调研结果曾被内容安全层标记为"疑似指令注入内容"(见对话历史,`deepseek-ai/deepseek-harness` 的自动调研返回了刻意模仿 Claude Code 内部 `<system-reminder>` 标签、且巧合地点名了本次五方对比对象的"设计笔记"),本设计对它的引用仅作为背景参考,未作为任何决策依据。
