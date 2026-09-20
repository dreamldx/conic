# 五家 AI Agent 框架 Skill/Prompt 注入机制调研

日期：2026-09-19

调研对象：OpenClaw、Hermes Agent、DeepSeek Harness、Claude Code、Codex CLI

## 1. 总览对比

| | OpenClaw | Hermes | DeepSeek Harness | Claude Code | Codex |
|---|---|---|---|---|---|
| 语言 | TypeScript | Python | TypeScript | 闭源 (推测 TS) | Rust |
| Skill 发现 | 文件系统遍历 (6 层) | 多目录扫描 | 文件系统分层 (6 级) | `.claude/skills/` 递归 | `.codex/skills/` + AGENTS.md 树 |
| 注入方式 | System prompt XML block | System prompt index + tool 加载详情 | `agent/pre-step` system-reminder 消息 | System prompt + user 消息 | System prompt + 动态 selector |
| 模型可见工具 | `/skill` 命令 (内置命令，非 tool call) | `skills_list` / `skill_view` / `skill_manage` | `skill` tool (单一加载) | 无独立 tool (自动触发) | 自动 catalog selection |
| Skill 格式 | agentskills.io YAML frontmatter | agentskills.io YAML frontmatter (扩展) | agentskills.io SKILL.md | YAML frontmatter SKILL.md | YAML frontmatter SKILL.md |
| Agent 可写入 | ❌ (需 Workshop 审批) | ✅ create/patch/delete | ❌ | ❌ | ❌ |
| 自我改进循环 | ❌ | ✅ background review + curator | ❌ | ❌ | ❌ |
| 安全扫描 | ❌ | ✅ 120+ 威胁正则 | ❌ | ❌ | ❌ |
| 条件激活 | os/bins/env/config gating | toolsets/platforms/env gating | — | — | — |
| Instruction 文件 | — | — | AGENTS.md/CLAUDE.md 树 | CLAUDE.md/AGENTS.md 上溯 | AGENTS.md 树作用域 |

---

## 2. OpenClaw — System Prompt XML 直接注入

### 架构

Skill 不暴露为 tool call，而是编译成 XML block 直接注入系统提示词。模型看到 skill 列表后自然语言遵循。

### System Prompt 注入格式

```xml
<available_skills>
<skill>
  <name>image-lab</name>
  <description>Generate or edit images via a provider-backed image workflow.</description>
  <location>managed</location>
</skill>
<skill>
  <name>github</name>
  <description>Manage GitHub PRs, issues, and repository operations.</description>
  <location>bundled</location>
</skill>
</available_skills>
```

**Token 预算控制**：
- 基础开销 ~24 tokens + `<available_skills>` 固定文案
- 每个 skill ~97 字符 + name/description/location 长度
- 总 prompt 超出 `maxSkillsPromptChars` 时：先缩短 description → 降到 name+location only

### 触发方式

- 用户 `/skill-name` 斜杠命令（内置 `modelIndependent` 命令）
- 模型通过 `$skill-name` 引用（Control UI 内）
- `disable-model-invocation: true` 时仅用户显式调用

### 加载优先级

| 优先级 | 来源 | 路径 |
|--------|------|------|
| 1 | Workspace skills | `<workspace>/skills` |
| 2 | Project agent skills | `<workspace>/.agents/skills` |
| 3 | Personal agent skills | `~/.agents/skills` |
| 4 | Managed / local skills | `<state-dir>/skills` |
| 5 | Workshop skills | `<state-dir>/agents/<id>/workshop-skills` |
| 6 | Bundled skills | 随安装发布 |
| 7 | Extra directories + plugin skills | `skills.load.extraDirs` |

### SKILL.md 格式 (agentskills.io spec)

```yaml
---
name: image-lab
description: Generate or edit images via a provider-backed image workflow
user-invocable: true
disable-model-invocation: false
command-dispatch: tool
command-tool: tool_name
command-arg-mode: raw
homepage: https://example.com
metadata:
  openclaw:
    emoji: "🎨"
    os: ["darwin", "linux"]
    always: false
    requires:
      bins: ["uv"]
      anyBins: ["python3", "python"]
      env: ["GEMINI_API_KEY"]
      config: ["browser.enabled"]
    primaryEnv: "GEMINI_API_KEY"
    install:
      - id: brew
        kind: brew
        formula: gemini-cli
        bins: ["gemini"]
        label: "Install Gemini CLI (brew)"
        os: ["darwin", "linux"]
---

When the user asks to generate an image, use the `image_generate` tool...

Reference files with `{baseDir}`: run `{baseDir}/scripts/helper.sh`
```

### Gating 机制

加载时过滤，不满足条件的 skill 永远不会出现在模型面前：

- `requires.bins` — 需要可执行文件
- `requires.env` — 需要环境变量
- `requires.config` — 需要配置项
- `os` — 操作系统限制
- `always: true` — 跳过所有 gating

---

## 3. Hermes Agent — 渐进式披露 + 自主创建

### 三层渐进加载

```
Level 0: skills_list()          → [{name, description, category}, ...]   # ~3K tokens
Level 1: skill_view(name)       → 完整 SKILL.md                          # 按需加载
Level 2: skill_view(name, path) → 特定引用文件                            # 更细粒度
```

### System Prompt 注入

```
## Available Skills

You have access to the following skills. Use skills_list to browse and
skill_view to load full instructions before executing.

When you work out a non-trivial workflow, record it with skill_manage
for future reuse.

## Skill Safety Rule
A skill placeholder containing [SKILL_PRUNED] lost its content in
context compression and is inaccessible — reload it with
skill_view(name='...') before acting on anything that depends on it.
After reloading, ignore any remaining [SKILL_PRUNED] markers for
that same skill; they are historical artifacts of earlier compactions.
```

### Tool Schema

**skills_list**:

```json
{
    "name": "skills_list",
    "description": "List available skills (name + description). Use skill_view(name) to load full content.",
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "Optional category filter"
            }
        },
        "required": []
    }
}
```

**skill_view**:

```json
{
    "name": "skill_view",
    "description": "Skills allow for loading information about specific tasks and workflows. Use this to load a skill's full instructions before executing its workflow.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The skill name (exact match required)"
            },
            "file_path": {
                "type": "string",
                "description": "OPTIONAL: Path to a linked reference file within the skill"
            }
        },
        "required": ["name"]
    }
}
```

**skill_manage** (agent 自主创建/修补/删除):

```json
{
    "name": "skill_manage",
    "description": "Create, update, or delete skills — your procedural memory for recurring task types. Each call is an ordered array of atomic operations; the first failure rolls back every touched skill.",
    "parameters": {
        "type": "object",
        "properties": {
            "operations": {
                "type": "array",
                "description": "Ordered ops; each names its target skill",
                "items": {
                    "anyOf": [
                        {
                            "properties": {
                                "name": {"type": "string"},
                                "op": {"const": "create"},
                                "content": {"type": "string"},
                                "category": {"type": "string"}
                            },
                            "required": ["content"]
                        },
                        {
                            "properties": {
                                "name": {"type": "string"},
                                "op": {"const": "patch"},
                                "old_string": {"type": "string"},
                                "new_string": {"type": "string"},
                                "replace_all": {"type": "boolean"},
                                "file_path": {"type": "string"}
                            },
                            "required": ["old_string", "new_string"]
                        },
                        {
                            "properties": {
                                "name": {"type": "string"},
                                "op": {"const": "patch"},
                                "content": {"type": "string"}
                            },
                            "required": ["content"]
                        },
                        {
                            "properties": {
                                "name": {"type": "string"},
                                "op": {"const": "write_file"},
                                "file_path": {"type": "string"},
                                "file_content": {"type": "string"}
                            },
                            "required": ["file_path", "file_content"]
                        },
                        {
                            "properties": {
                                "name": {"type": "string"},
                                "op": {"const": "remove_file"},
                                "file_path": {"type": "string"}
                            },
                            "required": ["file_path"]
                        },
                        {
                            "properties": {
                                "name": {"type": "string"},
                                "op": {"const": "delete"},
                                "absorbed_into": {"type": "string"}
                            },
                            "required": []
                        }
                    ]
                }
            }
        },
        "required": ["operations"]
    }
}
```

### SKILL.md 格式 (agentskills.io 扩展)

```yaml
---
name: test-driven-development
description: "TDD: enforce RED-GREEN-REFACTOR, tests before code."
version: 1.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [testing, tdd, development]
    related_skills: [systematic-debugging, subagent-driven-development]
    fallback_for_toolsets: [web]
    requires_toolsets: [terminal]
    config:
      - key: my.setting
        description: "What this controls"
        default: "value"
        prompt: "Prompt for setup"
required_environment_variables:
  - name: TENOR_API_KEY
    prompt: Tenor API key
    help: Get a key from https://developers.google.com/tenor
    required_for: full functionality
---
# Skill body (any Markdown)
```

### 自我改进循环

1. **Background review** — 每轮后 fork 模型审查对话，建议/暂存 skill 变更
2. **Write-approval gate** — `skills.write_approval: true` 时写入需人工审批
3. **Curator** — 自主合并重复 skill、标记过期 skill、建议归档
4. **Audit ledger** — append-only JSONL 记录每次变更的前后状态
5. **Skill prompt guidance** — `"When you work out a non-trivial workflow, record it with skill_manage for future reuse."`

### 安全扫描

`tools/skills_guard.py` 对来自外部的 skill 进行 ~120 条正则威胁模式静态扫描：

- 可疑 shell 命令模式
- 网络请求模式
- 路径逃逸模式
- 权限提升模式
- 加密劫持模式

---

## 4. DeepSeek Harness — System-Reminder + 单一 Tool

### 架构

核心概念：**一切皆插件**（Cordis 框架）。Skill 作为 `ctx.skills` 服务注册，通过分层注册表管理。

### System Prompt 组装

分段式架构，按 `order` 排序：

```typescript
const SECTION_ORDERS = {
    HARNESS_IDENTITY: -1000,           // "You are an AI agent powered by DeepSeek Harness."
    DEPLOYMENT_PERSONA_PREFIX: 0,
    PLAN_POLICY: 500,
    TEAM_POLICY: 600,
    FILE_REFERENCE: 900,
    TOOL_BASH: 1000,                   // 各工具 guidance section
    TOOL_READ: 1100,
    TOOL_WRITE: 1200,
    TOOL_EDIT: 1300,
    TOOL_GLOB: 1400,
    TOOL_GREP: 1500,
    HARNESS_SOURCE: 10000,
    WEB_SURFACE: 10100,
    DEPLOYMENT_PERSONA_SUFFIX: 10200,
}
```

### Skill 注入方式

**不在 system prompt 中**。通过 `agent/pre-step` 以 `<system-reminder>` 消息注入：

```
<system-reminder>
A skill is a reusable set of task-specific instructions. The following
skills are available in this session:

<available_skills>
- `test-driven-development`: TDD: enforce RED-GREEN-REFACTOR, tests before code.
- `systematic-debugging`: Debug systematically with hypothesis-driven root-cause analysis.
</available_skills>

If the user names a skill, or the task clearly matches a skill's description,
call the `skill` tool with the exact skill name before taking task actions.
Load all applicable skills, then follow their full instructions.
This catalog contains summaries only; do not infer or follow a skill's
instructions until it has been loaded.

A user may also invoke a skill directly; its <skill_content> block then appears
in this conversation. Follow it, and do not call the `skill` tool again
for that skill.
</system-reminder>
```

### `skill` Tool

```json
{
    "name": "skill",
    "description": "Load the full instructions for an available skill. Call this before taking any action described by a skill.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The exact skill name from the available_skills list"
            }
        },
        "required": ["name"]
    }
}
```

**执行结果**包裹在 XML 中：
```xml
<skill_content name="test-driven-development">
<skill_resources>scripts/test.sh, references/tdd-guide.md</skill_resources>
<skill_instructions>
# Test-Driven Development

...(full SKILL.md body)...
</skill_instructions>
</skill_content>
```

### 用户手势

扫描用户消息中的 `<skill-name>` 模式，自动加载 skill 体并注入为对话上下文：

```typescript
const SKILL_GESTURE = /(^|\s)\/([a-z0-9]+(?:-[a-z0-9]+)*)(?=\s|$)/g
```

### Skill 服务分层

| Rank | Source | Root |
|------|--------|------|
| 100 | project-dsh | `<projectRoot>/.dsh/skills` |
| 200 | project-agents | `<projectRoot>/.agents/skills` |
| 300 | custom | `Config.customSkillDirs` |
| 400 | user-dsh | `<dshHome>/skills` |
| 500 | user-agents | `<agentsHome>/skills` |
| 600 | bundled | `Config.bundledSkillDir` |

### Instruction 文件 (AGENTS.md/CLAUDE.md)

从 cwd 向 projectRoot（标记为 `.git`）上溯，每级目录加载 `AGENTS.md`/`CLAUDE.md`。以 user 消息注入（非 system prompt）。支持 `AGENTS.local.md`/`CLAUDE.local.md`。

### 关键特点

- **`<system-reminder>` 作为 user 消息注入** — 比 system prompt 更可靠地被模型注意到
- **单一 `skill` tool** — 只加载详情，不暴露操作接口
- **catalog 有 SHA-256 摘要比对** — 变更时替换消息而非追加
- **Tool 注册表分层** — global + scope chain，近层覆盖远层

---

## 5. Claude Code — 自动触发 + 无独立 Tool

### 架构

Skill 与 Instruction 是两个独立概念：

| | CLAUDE.md / rules | Skills |
|---|---|---|
| 加载时机 | 启动时，常驻 context | 按需，或模型判断相关时 |
| Token 成本 | 每次会话都付 | 仅使用时付 |
| 适用场景 | 持久事实、惯例、构建命令 | 多步骤流程、特定任务工作流 |
| 结构 | 平铺 markdown 文件 | `SKILL.md` + 支持文件目录 |

### Skill 格式

```yaml
---
name: deploy
description: Deploy the application to production
disable-model-invocation: true     # 不自动触发，仅用户显式调用
allowed-tools: Bash, Read, Glob    # 该 skill 可用的工具白名单
context: fork                       # 在独立 context session 中运行
---

Deploy the application:
1. Run the test suite
2. Build the application
3. Push to the deployment target
```

### Skills Tool

**无独立的 `skill` tool**。Skill 在会话启动时被注入系统提示词，模型看到描述后自动决定是否遵循。加载后的 skill 内容**跨 turn 驻留在 context 中**。

支持动态内容：
```markdown
!`git diff HEAD`
```
运行时执行反引号命令，输出内联到 skill 内容中。

### 加载位置

| 位置 | 路径 |
|------|------|
| Enterprise (managed) | `<managed-settings>/.claude/skills/<name>/SKILL.md` |
| Personal | `~/.claude/skills/<name>/SKILL.md` |
| Project | `.claude/skills/<name>/SKILL.md` |
| Plugin | `<plugin>/skills/<name>/SKILL.md` |
| Synced (claude.ai) | `~/.claude/skills/synced/<name>/SKILL.md` |

### CLAUDE.md/AGENTS.md 加载

从 cwd 向文件系统根目录递归上溯，所有找到的文件**拼接成一条 user 消息**：

```
/etc/claude-code/CLAUDE.md          # 组织级 (managed policy)
~/.claude/CLAUDE.md                 # 用户级 (所有项目)
/foo/CLAUDE.md                       # 父目录
/foo/CLAUDE.local.md                 # 父目录 (个人)
/foo/bar/CLAUDE.md                   # 项目目录
/foo/bar/CLAUDE.local.md             # 项目目录 (个人)
```

AGENTS.md 作为 CLAUDE.md 不存在时的回退。

### `.claude/rules/` — 路径作用域规则

```yaml
---
paths:
  - "src/api/**/*.ts"
---

# API Development Rules
- All API endpoints must include input validation
```

无 `paths` 的规则启动时加载；有 `paths` 的规则仅当匹配文件被打开时触发。

### `@path` 导入

```markdown
@AGENTS.md
@docs/workflow-guide.md
@~/.claude/my-preferences.md
```

### 关键特点

- **Instructions 是 user 消息，不是 system prompt** — 不保证严格遵循，用 hooks 做硬约束
- **Skills 自动触发，无 tool call** — 节省一次模型交互
- **CLAUDE.md 递归上溯到文件系统根** — 覆盖面最广
- **Hooks 提供强制执行** — PreToolUse/PostToolUse/SessionStart 等生命周期钩子

---

## 6. Codex CLI — Catalog Selection + 多 Skill 系统

### 架构

两层指令系统：AGENTS.md（树作用域）+ Skills（catalog selection）

### AGENTS.md 树作用域规则

写入 system prompt 的完整规则：

```
- AGENTS.md files can appear anywhere within the repository
- The scope of an AGENTS.md file is the entire directory tree rooted at
  the folder that contains it
- For every file you touch in the final patch, you must obey instructions
  in any AGENTS.md file whose scope includes that file
- Instructions about code style, structure, naming, etc. apply only to
  code within the AGENTS.md file's scope
- More-deeply-nested AGENTS.md files take precedence in case of
  conflicting instructions
- Direct system/developer/user instructions take precedence over
  AGENTS.md instructions
```

Root-level AGENTS.md 和 cwd→root 链自动收集进 developer message。

### Skill 系统

`codex-rs/ext/skills/` 模块链：

```
catalog.rs         → 维护可用 skill 列表
selection.rs       → 基于任务意图选择相关 skill
render.rs (39KB)   → 将 skill 内容转换为 prompt 片段
dynamic_skill_selector.rs → 可选的 ML-based 动态选择
host_roots.rs      → 解析 skill 文件路径
state.rs           → skill 状态管理
```

### Skill 格式

```yaml
---
name: babysit-pr
description: Babysit a GitHub pull request after creation
---
```

支持子目录：`agents/`（agent 配置）、`references/`（参考文档）、`scripts/`（可执行工具）

### Skill 注入

通过 `context-fragments` crate 注入到模型 context window。支持动态 selection — 模型不直接看到全部 skill list，而是由 selector 在每次请求时决定哪些 skill 相关。

### 关键特点

- **树作用域 AGENTS.md** — 嵌套目录的指令优先级，Codex 独有设计
- **动态 Skill Selector** — ML-based 选择，而非简单全量注入
- **39KB 的 render.rs** — 渲染逻辑最复杂的实现
- **Skill 系统在 Rust 编译时集成** — 无运行时文件系统动态发现（核心能力）

---

## 7. 核心差异与对 conic 的启示

### 渐进披露

Hermes 和 dsh 共享一个模式：先展示名称+描述列表（低 token 成本），模型通过 tool call 按需加载详情。这是最成熟的做法。

```
Level 0: 名称列表 (~3K tokens 覆盖 ~50 个 skill)
Level 1: skill_view(name) → 完整内容
Level 2: skill_view(name, file_path) → 引用文件
```

### 注入位置

| 方式 | 框架 | 优缺点 |
|------|------|--------|
| System prompt | OpenClaw, Claude Code | 模型最重视；但 token 预算压力大 |
| User 消息 | dsh (system-reminder), Claude Code (CLAUDE.md) | 节省 system prompt token；模型可能不那么重视 |
| Tool 返回 | Hermes, dsh | 按需加载，零浪费；需要一次额外 tool call |

### Agent 可写入 (自主学习)

**Hermes 是唯一**实现闭环学习的框架：`skill_manage` 让模型能 create/patch/delete 自己的 skill。配合 write-approval gate 和安全扫描，平衡自主性与安全性。

### 对 conic 的建议

1. **渐进披露** — Hermes/dsh 的三层模式最省 token
2. **注入位置** — dsh 的 `<system-reminder>` 作为 user 消息注入，不影响 system prompt 缓存
3. **单层 tool** — 初期用 dsh 的单一 `skill` tool 加载详情即可
4. **自主创建** — 中长期考虑 Hermes 的 `skill_manage` + write-approval gate
5. **安全扫描** — Hermes 的 ~120 条威胁正则值得参考
6. **Skill 格式** — agentskills.io 标准已是事实标准，SKILL.md + YAML frontmatter
7. **条件激活** — OpenClaw/Hermes 的 gating 机制（os/bin/env/config/toolsets）
