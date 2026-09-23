# YAML 驱动的插件配置

日期：2026-09-22
状态：已实现

## 1. 背景与目标

`registry.py::build_plugin_set()` 现在是纯 Python 硬编码装配：哪些工具存在、什么顺序、每个插件的非敏感参数（`bash` 的 `timeout`、`truncator` 的 `keep_last_n` 等），全部写死在这一个文件的 `if`/`class`/`tuple` 字面量里。改一个插件集合（加/删工具、调顺序、改参数）必须改这个 Python 文件。

目标：把"有哪些插件、什么顺序、各自的非敏感参数"这部分变成一份可读、可 diff、可进 git 版本控制的 YAML 文件，`build_plugin_set()` 解析这份 YAML 构造出跟今天完全等价的 `PluginSet`。

参考：`docs/superpowers/specs/2026-09-13-conic-agentic-engine-design.md` 第 8.7 节（Context 插件链现状）、第 15 节（这次讨论的存档，本文档是它的正式化版本）。

## 2. 范围

**included**：`tool_classes`、`context_plugins`（含 `system_prompt`/`extra_prompt` 内部的 section 插件列表）、`policy_plugins`、`summarizer`、`backend` 的选择 —— 即 `PluginSet` 六个字段全部由 YAML 驱动构造。

**not included**（明确排除，理由见括号）：
- 插件自注册/装饰器扫描式的动态发现（v1 范围本来就不做，见主设计文档第 14 节；固定的 name→builder 映射表足够覆盖 6~8 个工具）
- 运行时热重载 YAML（改配置需要重启进程，跟现在改 `.env` 需要重启是一致的）
- `loop_factory`（`ReactLoopPlugin` 本身）可替换 —— 目前只有一种 Loop 实现，不做成可配置项，字段不放进 YAML schema

## 3. YAML Schema

```yaml
tools:
  - bash: {timeout: 60}
  - read_file
  - write_file
  - edit_file
  - list_skills
  - load_skill
  - web_search: {timeout: 30}
  - web_fetch: {timeout: 60, max_chars: 15000, summary_model: ""}

context:
  - turn_variables
  - system_prompt:
      sections: [identity, tooling, skills, workspace, runtime, execution]
  - truncator: {keep_last_n: 40}
  - token_budget: {budget_tokens: 50000}
  - extra_prompt:
      sections: [dynamic_state]

policy:
  - permission
  - step_limit: {max_steps: 25}

summarizer: default
backend: openrouter
```

**列表项的两种写法**：裸字符串（`read_file`，无参数）或单键映射（`bash: {timeout: 60}`，键是插件名、值是参数字典）。`context`/`policy` 同一套写法。`summarizer`/`backend` 是标量字符串（目前各自只有一个可选值：`default`/`openrouter`，为将来可能的多选项留了口子，不是当前就要支持多选）。

**`web_search`/`web_fetch` 只有显式出现在 `tools` 列表里才会被装**（不像现在靠 `TAVILY_API_KEY`/`FIRECRAWL_API_KEY` 是否非空隐式决定），见第 5 节错误处理。

## 4. 跟 `.env`/`Config` 的分工

- **`.env`/`Config` 保留**：所有密钥（`OPENROUTER_API_KEY`/`DISCORD_BOT_TOKEN`/`TAVILY_API_KEY`/`FIRECRAWL_API_KEY`）、`OPENROUTER_MODEL`、`OPENROUTER_PROVIDER_BLACKLIST`、`PROJECT_NAME`、`LOG_LEVEL`、`PROJECT_ROOT`/`WORKSPACE_ROOT`/`DUCKDB_PATH`（路径类，不是插件参数）、新增的 `PLUGINS_CONFIG_PATH`（见第 6 节）。
- **从 `Config` 移进 YAML、`Config` 里对应字段删除**：`bash_timeout`、`max_steps_per_turn`、`context_token_budget`、`truncate_keep_last_n`、`web_search_timeout`、`web_fetch_timeout`、`web_fetch_max_chars`、`web_fetch_summary_model`。这些都是"某个插件的非敏感构造参数"，YAML 里各自插件条目的 `params` 就是唯一来源，不再有 `Config` 字段这个平行的第二来源（避免两个来源不一致时不知道听谁的）。

`Config` 类定义需要同步删掉这 8 个字段（`config.py`），相应地 `tests/test_config.py` 里引用这些字段的断言要删或改。

## 5. 名字 → 构造逻辑：builder 映射

新文件 `src/conic/plugins/plugin_config.py`：

```python
class PluginSpec(BaseModel):
    name: str
    params: dict[str, Any] = {}

class PluginsConfig(BaseModel):
    tools: list[PluginSpec] = []
    context: list[PluginSpec] = []
    policy: list[PluginSpec] = []
    summarizer: str = "default"
    backend: str = "openrouter"

class PluginConfigError(Exception):
    pass

def load_plugins_config(path: str | Path) -> PluginsConfig: ...
```

`tools`/`context`/`policy` 在校验之前先做一步归一化（`field_validator(mode="before")`）：把裸字符串和单键映射两种写法统一成 `PluginSpec(name, params)`。非法条目（既不是字符串也不是单键映射）在这一步直接 `PluginConfigError`。

`registry.py` 里按插件类别各建一张模块级 `名字 -> Callable[[Config, dict, BuildContext], 类或工厂]` 映射表（`TOOL_BUILDERS`/`CONTEXT_BUILDERS`/`POLICY_BUILDERS`/`SECTION_BUILDERS`）。`BuildContext` 是一个小 dataclass，打包 builder 可能需要的、跟单个 Config 字段不对等的共享状态（`shared_client: AsyncOpenAI`、`prompts: dict[str, str]`），避免每个 builder 签名都不一样。

`build_plugin_set()` 的新流程：
1. `plugins_cfg = load_plugins_config(config.plugins_config_path)`
2. 对 `plugins_cfg.tools` 里每个 spec：查 `TOOL_BUILDERS`，查不到名字 -> `PluginConfigError(f"unknown tool: {spec.name}")`；查到了就调 `builder(config, spec.params, ctx)` 得到一个工具类，追加进 `tool_classes` 列表
3. `context`/`policy` 同理；`system_prompt`/`extra_prompt` 这两个 context builder 内部会读 `spec.params["sections"]`，对每个 section 名字查 `SECTION_BUILDERS`（同样查不到就报错），构造出 section 插件实例列表传给 `SystemPromptPlugin`/`ExtraPromptPlugin`
4. `summarizer`/`backend` 是标量，各自查一张单值映射表（`SUMMARIZER_BUILDERS`/`BACKEND_BUILDERS`）

保持 `PluginSet` 现有六个字段的**类型签名不变**（`tool_classes: tuple[type, ...]`、`context_plugins: tuple[Callable[[str, list[dict]], object], ...]` 等）——解析 YAML 只是换了一种"怎么把这六个字段的值拼出来"的方式，消费端（`core/manager.py`）不用感知这个变化。

## 6. `PLUGINS_CONFIG_PATH` 与默认文件

`Config` 新增字段：
```python
plugins_config_path: str = Field(default="", alias="PLUGINS_CONFIG_PATH")
```
`_resolve_paths` model_validator 里跟 `workspace_root`/`duckdb_path` 同样处理：未设置时默认为 `{project_root}/config/plugins.yaml`。

仓库 `config/` 目录下新增 `plugins.yaml`，内容是**当前硬编码 wiring 的忠实转录**（第 3 节的示例就是这份文件的内容，各插件参数取当前 `Config` 里对应字段的默认值）——保证迁移后开箱行为不变，不需要每个部署方额外准备 YAML 才能启动。（这份文件最初落在仓库根目录，后来统一迁到 `config/plugins.yaml`，与 `PLUGINS_CONFIG_PATH` 的默认值一起改动。）

## 7. 错误处理策略（相对现状的行为变化）

- **YAML 里出现 builder 映射表查不到的插件名** -> `PluginConfigError`，`build_plugin_set()` 不吞掉，直接向上抛，进程启动失败并带清晰报错（"unknown tool: xxx"），不是运行到一半才发现。
- **YAML 显式声明了 `web_search`/`web_fetch`，但对应 API key（`TAVILY_API_KEY`/`FIRECRAWL_API_KEY`）没在 `.env` 里配** -> `PluginConfigError`（"web_search requires TAVILY_API_KEY"）。跟现状不同：现状是 `logger.warning` 后静默跳过注册（`registry.py:87-107`），改成显式声明就必须配对 key，配置错误要在启动时暴露，不是运行时才发现工具缺失。
- **`PLUGINS_CONFIG_PATH` 指向的文件不存在，或 YAML 语法错误，或 pydantic 校验失败** -> `PluginConfigError`，包裹原始异常信息，进程启动失败。

以上全部是"启动时失败"（fail fast），没有"部分插件加载失败、其余插件继续跑"这种降级模式——插件集合被认为是一个整体配置，配错了就不该带着错误配置启动。

## 8. `instantiate_session` 闭包（顺带合并的改动）

**动机**：`core/manager.py::PluginManager.start_session()` 现在直接写死"循环 `tool_classes`、注册 `backend`、循环 `context_plugins`/`policy_plugins`、造 `loop_plugin`"这段实例化逻辑。这段逻辑本质上是"把 `PluginSet` 的六个工厂字段变成真正注册到某个 `bus` 上的插件实例"，属于"插件怎么装配"这个关注点，放在 `registry.py` 里跟 YAML 解析逻辑放在一起更合适，而不是散在 `PluginManager`（一个完全插件无关的 Core Service）里。

**循环 import 问题**：`registry.py` 已经 `from conic.core.manager import PluginSet`。如果让 `manager.py` 反过来 `import` `registry.py` 的实例化函数，两个模块互相依赖，导入失败。

**解法**：`PluginSet` 增加第七个字段：
```python
instantiate_session: Callable[
    [MessageBus, str, str, SessionHandle, dict],  # bus, workspace_dir, session_key, handle, persisted_session_variables
    object,  # loop_plugin
]
```
`build_plugin_set()` 在组装好前六个字段后，在 `registry.py` 内部定义一个闭包函数赋给这第七个字段——函数体就是现在 `manager.py::start_session()` 里那段实例化循环，原样搬过来（用闭包捕获的 `tool_classes`/`context_plugins`/... 局部变量，不需要重新查 YAML）。

`manager.py::start_session()` 相应地瘦身：
```python
async def start_session(self, channel, native_id, channel_plugin_factory, reason="new") -> SessionScope:
    row = self._storage.get_or_create(channel=channel, native_id=native_id)
    bus = MessageBus()
    scope = SessionScope(bus=bus, row=row)
    handle = self._storage.handle_for(row)

    loop_plugin = self._plugin_set.instantiate_session(
        bus, row.workspace_dir, row.session_key, handle, row.variables
    )

    channel_plugin_factory().register(bus)
    session_gateway = SessionGatewayPlugin(scope)
    session_gateway.register(bus)

    await bus.chain(meta.SessionStartEvent, SessionStart(reason=reason))
    # ...（session_end_emitted / scope.tasks / _join_and_cleanup 不变）
```

`PluginManager` 依然不 import 任何具体插件类型，"完全插件无关"这条不变式不受影响（见主设计文档第 3 节）。

`PluginSet` 现有六个字段（`tool_classes`/`backend`/`context_plugins`/`policy_plugins`/`summarizer`/`loop_factory`）**保持不变，不删除、不合并进 `instantiate_session`**——`tests/plugins/test_registry.py` 里大量测试直接内省/调用这些字段（比如 `plugin_set.tool_classes[0]`、`plugin_set.backend("discord:1")`），拆分对测试友好，这次不破坏。

## 9. 测试改动

- **`tests/plugins/test_registry.py`**（~20 条）：`make_config()` 不再够用，需要配一份测试用的 `plugins.yaml`（写到 `tmp_path`，或者直接给 `PluginsConfig` 塞一个 dict 走 `PluginsConfig.model_validate(...)` 免文件 IO）。断言不变（`plugin_set.tool_classes[0]` 之类），前面加一步"先解析测试 YAML 再调 `build_plugin_set`"。
- 新增 `tests/plugins/test_plugin_config.py`：覆盖 `load_plugins_config()`（正常解析、裸字符串/单键映射两种写法、非法条目报错、YAML 语法错误报错）。
- 新增覆盖第 7 节错误场景的测试：未知插件名、`web_search` 缺 key、`web_fetch` 缺 key。
- `tests/test_config.py`：删除 8 个已移除字段相关的断言（`test_load_config_applies_defaults`/`test_load_config_reads_overrides` 里对应行）。
- `tests/core/test_manager.py`：如果现有测试直接构造 `PluginSet(...)` 传六个字段，需要补第七个 `instantiate_session` 字段（或给它一个默认值/测试专用的假实现）。

## 10. 迁移与向后兼容

- 不保留旧的纯 Python 硬编码路径——`build_plugin_set()` 只有 YAML 一条路径，没有"YAML 不存在就退回硬编码"这种双轨模式，避免两条路径分叉维护。
- `plugins.yaml` 默认值忠实转录现状，`git clone` 之后开箱即用；改行为需要显式编辑这份文件。
- `README.md`/`CONTRIB.md` 需要补一段"插件配置通过 `plugins.yaml`"的说明，指向这份新文件的位置和 schema。

## 11. 未决问题（实现时需要拍板，非阻塞）

- `SUMMARIZER_BUILDERS`/`BACKEND_BUILDERS` 目前各自只有一个选项，映射表机制在只有一个选项时略显多余，但为保持四类插件（tools/context/policy/summarizer+backend）走同一套"名字查表"机制的一致性，仍然这么做，而不是给这两个开特例。
