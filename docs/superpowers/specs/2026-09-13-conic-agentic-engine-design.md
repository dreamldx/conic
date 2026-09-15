# Conic：插件化 Agentic Engine 设计

日期：2026-09-13
状态：已实现

## 1. 背景与目标

参考 [mini-Kode](https://github.com/shareAI-lab/mini-Kode)（原 mini-claude-code）的极简 agent loop 思路——

```
while True:
    response = model(messages, tools)
    if response.stop_reason != "tool_use": return response.text
    results = execute(response.tool_calls)
    messages.append(results)
```

实现一个通用的 agentic engine：

- **LLM 后端**：OpenRouter（OpenAI 兼容协议）
- **交互渠道**：Discord bot
- **工具集**：mini-Kode v1 档位——`bash` / `read_file` / `write_file` / `edit_file`
- **架构要求**：AI 后端、渠道、工具三者都做成插件；agent loop 本身也是插件；插件之间只通过消息机制通信，不做直接函数调用（持久化存储、渠道网关连接等进程级 Core Service 除外，见第 3 节）

使用场景：小型可信团队内部使用，工具具备真实的本地执行能力（bash/文件读写），因此需要工作区沙箱边界，但不需要面向不可信公网用户的重度隔离。

## 2. 核心术语

- **Turn（轮）**：从一次 `UserInput` 到产生一次 `AssistantMessage`（或 `Error`）为止的完整交换。用户发一条消息、收到最终回复，中间无论循环多少次都算同一轮。
- **Step（步）**：Turn 内部 `while` 循环体的一次迭代——一次 `model_request`/`model_response`，加上该次响应里的所有工具调用。一个 Turn 由 1 个或多个 Step 组成，直到某个 Step 的模型响应不再包含工具调用为止。Step 数量不确定，由模型行为决定，因此需要 `MAX_STEPS_PER_TURN` 兜底。

## 3. 实体分类：Core Service 与 Plugin

系统里的实体分两类，区分标准是**通信方式**和**生命周期范围**：

| | Plugin（插件） | Core Service（核心服务） |
|---|---|---|
| 通信方式 | 只通过 `MessageBus` 收发消息 | 被直接引用/直接函数调用 |
| 生命周期 | **随会话创建、随会话销毁**（`register(bus)` 挂到该会话专属总线上） | 进程级，跨会话存在，有 `startup()`/`shutdown()` 但不参与总线消息 |
| 成员 | LoopPlugin、BackendPlugin、ToolPlugin、DiscordThreadPlugin（每会话渠道适配器）、Section plugins、PolicyPlugin | `MessageBus` 本身、`PluginManager`、`StorageService`、`DiscordGateway` |

`StorageService`（持久化历史存储）**不是 Plugin**，原因（详见附录 A）：

1. **启动时序矛盾**：判断一个会话是新建还是恢复，必须先查存储；而这一步发生在该会话的 `MessageBus` 被创建**之前**——不可能"只通过还不存在的总线"去查它。
2. **生命周期矛盾**：Plugin 随会话销毁而销毁，但持久化的意义就是数据要在会话销毁后依然存在（甚至跨 bot 重启）。
3. **通信方式不匹配**：存储读写是 Loop 自己顺序控制流里的命令式操作（"给我历史""帮我写入"），不是"发生了某事，0个或多个订阅者反应"的广播场景。

**`DiscordGateway` 同理不是 Plugin，是 Core Service**：它拥有进程里唯一的 discord.py 连接，必须在任何会话存在之前就启动（登录、注册斜杠命令、扫描存储恢复历史活跃会话），并且要在收到外部 Discord 事件时主动决定"这条消息该桥接到哪个会话的总线"。真正按会话生命周期创建/销毁、只通过总线通信的，是 `DiscordThreadPlugin`（每会话一份实例）。

## 4. MessageBus：两套注册表，语义不同

```python
class MessageBus:
    def on(self, type_name: str, handler) -> None: ...
    async def emit(self, type_name: str, payload: Any) -> Any: ...

    def on_request(self, type_name: str, handler) -> None: ...
    async def request(self, type_name: str, payload: Any) -> Any: ...
```

| | `on` / `emit` | `on_request` / `request` |
|---|---|---|
| 订阅者数量 | 0 个或多个，按注册顺序依次处理，每个可返回修改后的 payload 传给下一个，也可 `raise AbortTurn` 中止 | 恰好 1 个；注册第二个同 `(type_name, payload类型)` 的会报错 |
| 用途 | 通知 + 链式加工（前置/后置钩子、审核、渠道输出） | 问答（唯一确定答案：模型补全结果、某个工具的执行结果、摘要结果） |

**消息类型判定 = 字符串 topic + payload 的 Python 类型**。同一个字符串 topic 下可以有多种不同 payload 类型分别对应不同 handler。

Payload 类型在注册时**自动从 handler 的类型注解推断**（用 `inspect.signature` + `typing.get_type_hints`），插件作者不需要重复声明。

Bus topic 名称通过 `src/conic/plugins/meta.py` 中的 `*Event` 常量集中管理，插件代码中禁止直接使用字符串字面量。

## 5. 会话作用域：不用 session_id，用总线实例隔离

消息不携带 `session_id`；会话身份靠**结构**体现：**每个会话拥有自己独立的 `MessageBus` 实例和一整套专属插件实例**。

`PluginManager` 本身完全渠道无关——不 import 任何 Discord 相关类型，具体渠道插件由调用方以工厂闭包的形式传入。`PluginSet` dataclass 携带所有插件类的工厂/实例，`start_session` 按工作区目录和工具 schema 组装。

`DiscordGateway` 调用：
```python
plugin_manager.start_session(
    channel="discord", native_id=str(discord_thread.id),
    channel_plugin_factory=lambda: DiscordThreadPlugin(discord_thread),
)
```

`PluginManager.start_session()` 内部按固定顺序把插件挂到新建的 `MessageBus` 上：4 个 ToolPlugin（各自绑定 `workspace_dir`）→ BackendPlugin → context 插件链（system_prompt → truncator → token_budget）→ policy 插件（permission → step_limit）→ SummarizerPlugin → ReactLoopPlugin → channel 插件。`PluginSet.backend` 是**单个共享实例**（不是工厂），因为它无会话状态（只持有 API client 和 model 名），直接在每个会话的 bus 上重复 `register()`；其余插件都以工厂闭包形式传入，确保每会话独立实例。`SessionScope` 额外持有一把 `asyncio.Lock`：`DiscordGateway.handle_message()` 在 `async with scope.lock` 内才 `emit(UserInputEvent, ...)`，避免同一线程内并发消息互相打断同一个 Turn；`handle_stop_command()`（`agent_stop` 命令）同样在 pop 掉路由表条目后用同一把锁包住 `emit(SessionStopEvent) + stop_session()`，确保它会等一个正在跑的 Turn 释放锁之后才停止/归档会话，而不是与之竞态。

### 5.1 进程启动与会话恢复流程

`Gateway` 通用接口（放在 `core/gateway.py`）：
```python
class Gateway(Protocol):
    name: str
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
```

`DiscordGateway.start()` 内部：
1. 登录 discord.py 网关连接
2. 注册 `/agent_start`、`/agent_stop` 斜杠命令
3. 查 storage 里 `channel="discord"` 且 `status="active"` 的会话，逐个恢复，填进内存路由表 `{thread_id: SessionScope}`
4. 阻塞进入 discord.py 事件循环

## 6. 完整消息生命周期

| Topic (meta.py) | Verb | Payload | 处理者 |
|---|---|---|---|
| `UserInputEvent` | emit | `UserInput` | LoopPlugin |
| `TurnStartEvent` | emit | `TurnStart` | DiscordThreadPlugin（typing indicator） |
| `StepStartEvent` | emit | `StepStart` | StepLimitPlugin |
| `BeforeModelCallEvent` | emit | `BeforeModelCall` | SystemPromptPlugin → TruncatorPlugin → TokenBudgetPlugin |
| `SummarizeEvent` | request | `SummarizeRequest` → `SummarizeResult` | SummarizerPlugin |
| `ModelRequestEvent` | request | `ModelRequest` → `ModelResponse` | OpenRouterBackendPlugin |
| `ModelResponseEvent` | emit | `ModelResponse` | 无默认订阅者 |
| `ToolCallEvent` | emit | `ToolCall` | PermissionPolicyPlugin |
| `ToolCallRequestEvent` | request | tool payload → `ToolCallResult` | 对应 ToolPlugin |
| `ToolCallResultEvent` | emit | `ToolCallResult` | 无默认订阅者 |
| `AssistantMessageEvent` | emit | `AssistantMessage` | DiscordThreadPlugin |
| `ErrorEvent` | emit | `Error` | DiscordThreadPlugin |
| `TurnEndEvent` | emit | `TurnEnd` | DiscordThreadPlugin（停止 typing） |
| `BuildSystemPromptEvent` | emit | `BuildSystemPrompt` | Section 插件链 |
| `SessionStopEvent` | emit | `TurnEnd` | DiscordThreadPlugin（标记停止） |

## 7. 系统提示词（System Prompt）

系统提示词通过 bus 消息 `BuildSystemPromptEvent` 动态组装。`SystemPromptPlugin` emit 该消息时携带空的 `sections: dict[str, str]`，各 section 插件依次填充：

| Section | 插件 | 来源 |
|---------|------|------|
| identity | `IdentitySectionPlugin` | `prompts/identity.md`（启动时加载进内存） |
| tooling | `ToolingSectionPlugin` | 当前会话的 tool schemas |
| workspace | `WorkspaceSectionPlugin` | 会话 workspace_dir |
| runtime | `RuntimeSectionPlugin` | 平台、模型等运行时信息 |
| execution | `ExecutionBiasSectionPlugin` | `prompts/execution.md`（启动时加载进内存） |

任何插件都可以 hook `BuildSystemPromptEvent` 注入自定义 section。`SystemPromptPlugin._assemble()` 按 `SECTION_ORDER` 拼接为最终系统消息；不在 `SECTION_ORDER` 里的 section（未来插件新增的）会追加在已知 section 之后，不会丢失。

`registry.py` 里的 `_load_prompts()` 并不是只认 `identity.md`/`execution.md` 两个硬编码文件名，而是遍历 `prompts/*.md` 下所有文件，以文件名（去掉 `.md`）为 key 存进字典；`build_plugin_set()` 目前只取 `identity`/`execution` 两个 key 使用，往 `prompts/` 下新增 `.md` 文件不会自动被消费，需要相应 section 插件去 `prompts.get(...)`。

## 8. 各插件详细设计

### 8.1 LoopPlugin — ReactLoopPlugin
唯一订阅 `user_input` 的编排者，持有本会话的 `storage_handle`，驱动整个 Turn/Step 流程。

- 构造时除 `tool_schemas` 外还持有 `tool_payload_map: dict[str, type]`——工具的 `llm_name` → 该工具 `execute()` 参数类型（由 `PluginManager` 用 `infer_payload_type` 从签名自动推断），用于把模型返回的 `ToolCallSpec.args`（dict）转换成对应工具的 dataclass payload，再走 `bus.request(ToolCallRequestEvent, payload)`。
- 系统提示词不落库：每个 Step 都从 `storage.load_history()` 取纯对话历史（不含 system），再交给 `BeforeModelCallEvent` 链（`SystemPromptPlugin` 会重新拼一份 system 消息临时前置），因此 system prompt 内容可以随 workspace/runtime 等运行时信息逐 Step 刷新，但不会污染持久化历史。
- 单次工具调用若在 `ToolCallEvent`/`ToolCallRequestEvent`/`ToolCallResultEvent` 任一环节抛出普通异常，Loop 会捕获并把 `f"Error: {exc}"` 作为该 `tool_call_id` 的回复内容写回历史（保留原始 `call.id`，不中断整个 Turn）；`AbortTurn` 是这个局部 catch 的显式例外——即使在这三个环节里抛出（例如 `PermissionPolicyPlugin` 在 `before_tool_call` 里拒绝一次调用），也会先被 `except AbortTurn: raise` 放行，穿透到外层，和 `StepLimitPlugin` 那种在 `StepStartEvent` 抛出的 `AbortTurn` 一样，终止整个 Turn 并发 `ErrorEvent`。

### 8.2 BackendPlugin — OpenRouterBackendPlugin
唯一应答 `model_request`，用 openai SDK 调用 OpenRouter API。

### 8.3 ToolPlugin（4 个）
每个工具构造时绑定本会话 `workspace_dir`，任何解析后越出该目录的路径直接拒绝。路径校验逻辑集中在 `plugins/tools/base.py`：`resolve_within_workspace(workspace_dir, path)` 把相对路径解析到 `workspace_dir` 下并 `.resolve()`，若结果不在 workspace 内则 `raise WorkspaceEscapeError`；四个文件类工具都复用这一个函数，不各自实现越权检查。

- `BashToolPlugin`：`asyncio.create_subprocess_shell` 在 `workspace_dir` 下执行，默认超时 60s（`asyncio.TimeoutError` 时 kill 进程），stdout/stderr 合并后按字节截断（默认 20000 字节，超出附加 `...[truncated]`），非零退出码作为 `error` 返回
- `ReadFileToolPlugin`：`offset`/`limit`（默认 0 / 2000 行）按行切片，超出部分返回时附加总行数提示
- `WriteFileToolPlugin`：创建/覆盖文件，返回结果里报告新旧行数和 created/overwritten 状态
- `EditFileToolPlugin`：要求 `old_text` 在文件中**精确出现一次**，否则报错（未找到 / 不唯一），成功后只替换第一处匹配

### 8.4 DiscordGateway（Core Service，实现 `Gateway` Protocol）
进程级 discord.py 连接持有者，维护 `{thread_id: SessionScope}` 路由表。在 `conic/discord/gateway.py`。

### 8.5 DiscordThreadPlugin（Plugin）
每会话一份，订阅 `AssistantMessageEvent`/`ErrorEvent`/`TurnStartEvent`/`StepStartEvent`/`TurnEndEvent`/`SessionStopEvent`。TurnStart/StepStart 时启动或续接 typing indicator（`TYPING_INTERVAL=8s` 刷新一次 `thread.typing()`，`TYPING_TIMEOUT=20s` 兜底超时自动停止单段任务），TurnEnd/Error/SessionStop 时停止。由于单段任务有 20s 上限，多 Step 的长 Turn 靠每个 `StepStartEvent` 重新拉起一个新任务（若旧任务已超时结束）来续接，避免指示器在 Turn 中途消失。在 `conic/plugins/channels/discord.py`。

- `SessionStopEvent`（`agent_stop` 命令触发）会把内部 `_stopped` 标记置位；之后任何 `AssistantMessageEvent`/`ErrorEvent` 都会被 `_send()` 直接忽略——防止会话已停止（thread 即将被 archive/lock）后模型仍在跑最后一个 Step 时把消息发进已关闭的线程。
- `_send()` 按 `DISCORD_MESSAGE_LIMIT=2000` 字符切片分段发送，应对 Discord 单条消息长度限制。

### 8.6 PolicyPlugin
- `PermissionPolicyPlugin`：v1 全部放行
- `StepLimitPlugin`：超过 `MAX_STEPS_PER_TURN` 时 `raise AbortTurn`

### 8.7 Context 插件链
- `SystemPromptPlugin`：emit `BuildSystemPromptEvent` 收集 sections 并组装系统消息；仅当 `ctx.messages[0]` 还不是 `system` 角色时才前置（防御性判断，正常流程下每个 Step 都会重新拼一份）
- `TruncatorPlugin`：非 system 消息数超过 `keep_last_n`（默认 40）时，从尾部保留最近 N 条，system 消息始终保留
- `TokenBudgetPlugin`：用 `core/tokencount.estimate_tokens`（`总字符数 // 4`的粗略估算，不依赖 tokenizer）统计 token 数，超预算时通过 `SummarizeEvent` 触发摘要

Truncator 和 Summarizer 的裁切点都要经过 `core/messagealign.align_cut(messages, cut_index)`：如果提议的裁切点落在一条 `role: "tool"` 消息上（即会把某个 `tool_calls` 消息和它对应的工具回复截断成孤儿），就把裁切点持续前移，直到落在完整的 assistant+tool回复 消息组之前，保证任何被保留的 `tool` 消息都能在保留区间内找到它所回复的 assistant 消息。

### 8.8 SummarizerPlugin
仅在 TokenBudgetPlugin 判断超限时通过 `SummarizeEvent`（request/response）被调用：
1. 从待压缩上下文里分离出 system 消息和其余消息；用 `align_cut` 找到"保留最近 `keep_recent`（默认 5）条、且不孤立 tool 回复"的裁切点
2. 把裁切点之前的历史拼成纯文本 transcript，通过 `bus.request(ModelRequestEvent, ...)`（复用同一个 backend）请求模型生成摘要——即摘要生成本身也是一次模型调用，走的是同一条 `model_request` 总线通道
3. 把摘要包装成新的 `{"role": "system", "content": "[Earlier conversation summary]\n..."}` 消息，与原 system 消息、最近保留的消息一起返回，替换原始的 `ctx.messages`（这次替换只影响本次模型调用的 payload，不写回持久化历史，因此下一 Step 若历史仍超预算会重新触发摘要）

## 9. StorageService（Core Service）

DuckDB schema 保持简单，两张表：

```sql
CREATE TABLE sessions (
    session_key VARCHAR PRIMARY KEY,   -- "{channel}:{native_id}"，如 "discord:123456"
    channel VARCHAR, native_id VARCHAR, workspace_dir VARCHAR,
    model VARCHAR, status VARCHAR,     -- status: "active" | "ended"
    created_at VARCHAR
)
CREATE TABLE messages (
    session_key VARCHAR, seq INTEGER,  -- 每会话独立递增（MAX(seq)+1），不用自增列
    role VARCHAR, content VARCHAR,     -- content 是整条消息序列化后的 JSON（含 tool_calls/tool_call_id 等原始字段，不是纯文本）
    created_at VARCHAR,
    PRIMARY KEY (session_key, seq)
)
```

SQL 语句集中在 `src/conic/services/queries.py` 中，每句包装为返回 `(sql, params)` 的函数。数据模型使用 pydantic `Session`/`Message` 类型（`src/conic/services/models.py`）。

`StorageService`（进程级单例，持有 DuckDB 连接）与 `SessionHandle`（`storage.handle_for(row)` 返回，仅包装 `session_key` + 连接引用）职责分离：前者管理会话行的创建/恢复/状态流转（`get_or_create`/`active_sessions`/`set_status`），后者是 `ReactLoopPlugin` 直接持有、每次读写历史消息时使用的窄接口（`append_message`/`load_history`），Loop 不直接接触 `StorageService` 或原始连接。`get_or_create()` 首次创建会话时会按 `{workspace_root}/{channel}/{native_id}` 派生并 `mkdir` 出 workspace 目录，写入 `workspace_dir` 字段供后续该会话所有 ToolPlugin 复用。

## 10. 配置项

使用 pydantic-settings `BaseSettings`，自动从 `.env` 和环境变量读取。

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `DISCORD_BOT_TOKEN` | 无（必填） | Discord bot token |
| `OPENROUTER_API_KEY` | 无（必填） | OpenRouter API key |
| `PROJECT_ROOT` | 无（必填） | 项目根目录，绝对路径。main.py 启动时会自动设为 `__file__` 所在目录；走 `conic` 控制台脚本入口时不会自动设置，必须显式提供（环境变量或 `.env`）——不做 `__file__`/cwd/`pyproject.toml` 猜测，因为那在打包/frozen 的 Release 场景下无法工作，会静默解析到错误目录 |
| `OPENROUTER_MODEL` | `anthropic/claude-sonnet-4.5` | 默认模型 |
| `WORKSPACE_ROOT` | `{PROJECT_ROOT}/workspace` | 工作区根目录 |
| `DUCKDB_PATH` | `{PROJECT_ROOT}/data/conic.duckdb` | 持久化文件路径 |
| `LOG_LEVEL` | `INFO` | loguru 日志级别 |
| `MAX_STEPS_PER_TURN` | `25` | 单轮最大 Step 数 |
| `CONTEXT_TOKEN_BUDGET` | `50000` | 触发摘要压缩的 token 阈值 |
| `TRUNCATE_KEEP_LAST_N` | `40` | Truncator 保留的最近消息数 |

`WORKSPACE_ROOT` 和 `DUCKDB_PATH` 默认为 PROJECT_ROOT 下的绝对路径，也可通过环境变量覆盖为自定义路径。

## 11. 目录结构

```
conic/                     # 项目根（main.py 与 pyproject.toml 同级，不在 src 下）
  main.py                # 入口：设置 PROJECT_ROOT 环境变量，调用 main()
  pyproject.toml         # console_script: conic = "conic.entry:main"
  prompts/
    identity.md           # 系统提示词 identity section
    execution.md          # 系统提示词 execution section
  src/conic/
    entry.py              # build_app() + main()
    config.py             # pydantic-settings Config, 自动加载 .env
    discord/
      gateway.py          # DiscordGateway (Core Service)
    core/
      bus.py              # MessageBus
      manager.py          # PluginManager, PluginSet
      gateway.py          # Gateway Protocol
      session.py          # SessionScope（含 per-session asyncio.Lock）
      messages.py         # 所有消息 dataclass
      errors.py           # AbortTurn、NoResponderError、DuplicateResponderError
      messagealign.py     # align_cut：截断对齐，防止 orphan tool replies
      tokencount.py       # estimate_tokens：字符数 // 4 的粗略估算
    plugins/
      meta.py             # 总线 topic 名称常量 (*Event)
      registry.py         # build_plugin_set()：组装 PluginSet，泛化加载 prompts/*.md
      channels/
        discord.py        # DiscordThreadPlugin (Plugin)
      loops/react_loop.py
      backends/openrouter.py
      tools/
        base.py           # resolve_within_workspace / WorkspaceEscapeError（4 个工具共用）
        bash.py / read_file.py / write_file.py / edit_file.py
      context/
        system_prompt.py  # 组装系统提示词
        truncator.py
        token_budget.py
        summarizer.py
        sections/         # 提示词 section 贡献插件
          identity.py
          tooling.py
          runtime.py
          workspace.py
          execution.py
      policy/{permission,step_limit}.py
    services/
      storage.py          # StorageService + SessionHandle (DuckDB)
      queries.py          # SQL 语句函数
      models.py           # Session/Message pydantic 模型
  tests/                  # 结构与 src/conic 镜像，另含 test_config.py、test_main.py
    core/
    discord/
    plugins/{backends,channels,context,loops,policy,tools}/
    plugins/test_registry.py
    services/
```

## 12. 日志

使用 `loguru`，级别通过 `LOG_LEVEL` 环境变量控制（默认 `INFO`）。`entry.py` 的 `build_app()` 中统一配置 `logger.remove()` + `logger.add(sys.stderr)`。各模块通过 `from loguru import logger` 获取全局 logger。

## 13. 错误处理

- 工具执行失败 → 返回 `ToolCallResult(error=...)`，反馈给模型
- 任意钩子 `raise AbortTurn` → Loop 捕获 → `bus.emit("error", ...)` → Turn 干净结束
- OpenRouter API 异常 → 转为 error 事件上报
- Discord 单条消息 2000 字符限制 → `DiscordThreadPlugin` 负责分段发送
- Typing indicator：TurnStart 启动，TurnEnd/Error 停止，60s 超时保护

## 14. v1 范围界定

**包含**：4 个核心工具、OpenRouter 单一 backend、Discord 单一 channel、DuckDB 持久化、完整的 ContextBuilder 链（含 Summarizer/TokenBudget）、composable system prompt、StepLimit 安全阀、typing indicator、loguru 日志。

**不包含（架构已预留空间）**：
- 多 channel 同时运行
- 子代理/Task 工具、Skill 工具
- 动态插件发现
- `model_response`/`tool_result` 的默认审核/脱敏钩子

## 附录 A：StorageService 不是 Plugin 的完整论证

见第 3 节引用的三条论据（启动时序矛盾、生命周期矛盾、通信方式不匹配）。结论：Storage 归类为 Core Service 是唯一不产生上述矛盾的分类方式。
