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

`PluginManager.start_session()` 内部按固定顺序把插件挂到新建的 `MessageBus` 上：4 个 ToolPlugin（各自绑定 `workspace_dir`）→ BackendPlugin → context 插件链（system_prompt → truncator → token_budget）→ policy 插件（permission → step_limit）→ SummarizerPlugin → ReactLoopPlugin → channel 插件。`PluginSet.backend` 和其余插件一样也是**工厂闭包**，确保每会话独立实例——`OpenRouterBackendPlugin.register()` 会捕获本会话的 `bus`（流式分支要用它 emit `MessageDeltaUpdateEvent`），如果跨会话共享同一个实例，后一个会话的 `register()` 会覆盖前一个会话捕获的 `bus`，导致流式 token 错发到别的会话/线程（曾经的真实 bug，已修复）。`registry.py` 里这个工厂闭包共享同一个 `AsyncOpenAI` 连接实例，只是插件对象本身（连同它捕获的 `bus`）各会话独立，避免为每个会话重复建立 HTTP 连接。`SessionScope` 额外持有一把 `asyncio.Lock`：`DiscordGateway.handle_message()` 在 `async with scope.lock` 内才 `emit(UserInputEvent, ...)`，避免同一线程内并发消息互相打断同一个 Turn；`handle_stop_command()`（`agent_stop` 命令）同样在 pop 掉路由表条目后用同一把锁包住 `emit(SessionStopEvent) + stop_session()`，确保它会等一个正在跑的 Turn 释放锁之后才停止/归档会话，而不是与之竞态。

### 5.1 进程启动与会话恢复流程

`Gateway` 通用接口（放在 `types/gateway.py`）：
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
| `InputEvent` | emit | `Input` | 无默认订阅者（拦截点，供未来文本命令插件使用） |
| `UserInputEvent` | emit | `UserInput` | LoopPlugin |
| `SessionStartEvent` | emit | `SessionStart` | 无默认订阅者（会话开始通知，reason: new/resume） |
| `TurnStartEvent` | emit | `TurnStart` | TurnVariableUpdaterPlugin（注入 `now` 到 `variables["turn"]`）→ DiscordThreadPlugin（typing indicator + 发送占位状态消息） |
| `StepStartEvent` | emit | `StepStart` | StepLimitPlugin、DiscordThreadPlugin（续接 typing indicator） |
| `BeforeModelCallEvent` | emit | `BeforeModelCall` | SystemPromptPlugin → TruncatorPlugin → TokenBudgetPlugin |
| `BeforeSummarizeEvent` | emit | `BeforeSummarize` | 无默认订阅者（可改写 `instructions` 或置 `cancelled=True`） |
| `SummarizeEvent` | request | `SummarizeRequest` → `SummarizeResult` | SummarizerPlugin |
| `SummarizeDoneEvent` | emit | `SummarizeDone` | 无默认订阅者 |
| `SummarizeFailedEvent` | emit | `SummarizeFailed` | 无默认订阅者（emit 后异常仍会重新抛出） |
| `ModelRequestEvent` | request | `ModelRequest` → `ModelResponse` | OpenRouterBackendPlugin |
| `MessageDeltaUpdateEvent` | emit | `MessageDeltaUpdate` | DiscordThreadPlugin（追加缓冲区 + 节流 edit）；仅当 `ModelRequest.stream_updates=True` 时由 OpenRouterBackendPlugin 逐 chunk emit |
| `ModelResponseEvent` | emit | `ModelResponse` | 无默认订阅者 |
| `ToolCallEvent` | emit | `ToolCall` | PermissionPolicyPlugin |
| `ToolExecutionStartEvent` | emit | `ToolExecutionStart` | 无默认订阅者；ReactLoopPlugin 紧接着 emit `MessageUpdateEvent`（工具状态行） |
| `MessageUpdateEvent` | emit | `MessageUpdate` | DiscordThreadPlugin（整体替换缓冲区 + 立即 edit，不节流） |
| `ToolCallRequestEvent` | request | tool payload → `ToolCallResult` | 对应 ToolPlugin |
| `ToolExecutionEndEvent` | emit | `ToolExecutionEnd` | 无默认订阅者（携带派发得到的原始 `ToolCallResult`） |
| `ToolCallResultEvent` | emit | `ToolCallResult` | 无默认订阅者 |
| `StepEndEvent` | emit | `StepEnd` | 无默认订阅者 |
| `AssistantMessageEvent` | emit | `AssistantMessage` | DiscordThreadPlugin |
| `ErrorEvent` | emit | `Error` | DiscordThreadPlugin |
| `TurnEndEvent` | emit | `TurnEnd` | DiscordThreadPlugin（停止 typing） |
| `BuildSystemPromptEvent` | emit | `BuildSystemPrompt` | Section 插件链 |
| `SessionStopEvent` | emit | `TurnEnd` | DiscordThreadPlugin（标记停止，先于 `SessionEndEvent`） |
| `SessionEndEvent` | emit | `SessionEnd` | 无默认订阅者（会话结束通知，目前 reason 只有 `user_stop`） |

## 7. 系统提示词（System Prompt）

系统提示词通过 bus 消息 `BuildSystemPromptEvent` 动态组装。`SystemPromptPlugin` emit 该消息时携带空的 `sections: dict[str, str]`，各 section 插件依次填充：

| Section | 插件 | 来源 |
|---------|------|------|
| identity | `IdentitySectionPlugin` | `prompts/identity.md`（启动时加载进内存） |
| tooling | `ToolingSectionPlugin` | 当前会话的 tool schemas |
| workspace | `WorkspaceSectionPlugin` | 会话 workspace_dir |
| extra | `ExtraPromptPlugin` | 纯 Jinja2 占位符文本（`{{ global.platform }}`/`{{ global.model }}`/`{{ global.timezone }}`/`{{ turn.now }}`/`{{ turn.step_count }}`/`{{ session.tokens_used }}`/`{{ session.turn_count }}`），不在 `BuildSystemPromptEvent` 时求值，留给 `SystemPromptPlugin.apply()` 之后统一渲染（见 7.1） |
| execution | `ExecutionBiasSectionPlugin` | `prompts/execution.md`（启动时加载进内存） |
| output | `DiscordThreadPlugin`（渠道插件） | 硬编码常量 `OUTPUT_REQUIREMENTS`（`plugins/channels/discord.py`） |
| bash | `BashToolPlugin`（工具插件） | 动态拼接，携带当前会话的 `self._timeout` 和 `self._workspace_dir`（`plugins/tools/bash.py`） |
| read_file | `ReadFileToolPlugin`（工具插件） | 动态拼接，携带当前会话的 `self._workspace_dir`（`plugins/tools/read_file.py`） |
| write_file | `WriteFileToolPlugin`（工具插件） | 动态拼接，携带当前会话的 `self._workspace_dir`（`plugins/tools/write_file.py`） |
| edit_file | `EditFileToolPlugin`（工具插件） | 动态拼接，携带当前会话的 `self._workspace_dir`（`plugins/tools/edit_file.py`） |

任何插件都可以 hook `BuildSystemPromptEvent` 注入自定义 section。`SystemPromptPlugin._assemble()` 按 `SECTION_ORDER` 拼接为最终系统消息；不在 `SECTION_ORDER` 里的 section（未来插件新增的）会追加在已知 section 之后，不会丢失——`output` 就是这样一个例子：由渠道插件（而不是 `context_plugins` 里的固定 section 插件）贡献，告诉模型当前输出渠道（Discord）的格式限制（不渲染 markdown 表格、标题只支持到 `###`）、流式渲染方式（同一条消息逐 token 编辑，不需要模型自己分段）、以及要求回复不超过单条消息字符数上限（2000）。这也是"渠道相关的输出要求应该由渠道插件自己声明，而不是写死在 core prompt 里"这一设计意图的落地。`bash` 是同一模式在工具侧的例子：`BashToolPlugin.register()` 同时 hook `ToolCallRequestEvent`（真正执行命令）和 `BuildSystemPromptEvent`（告诉模型"超过 `self._timeout` 秒的命令会被 kill 并报错，不要跑长期运行/阻塞/交互式命令"），把"这个工具有什么限制"和"工具本身怎么实现"放在同一个文件里维护，且提示词里的超时数字直接读 `self._timeout`，配置改了不会和提示词文字脱节。

`registry.py` 里的 `_load_prompts()` 并不是只认 `identity.md`/`execution.md` 两个硬编码文件名，而是遍历 `prompts/*.md` 下所有文件，以文件名（去掉 `.md`）为 key 存进字典；`build_plugin_set()` 目前只取 `identity`/`execution` 两个 key 使用，往 `prompts/` 下新增 `.md` 文件不会自动被消费，需要相应 section 插件去 `prompts.get(...)`。

### 7.1 变量注入与 Jinja2 模板渲染

模板变量分三层作用域，各自的生命周期和来源不同，渲染时通过命名空间区分，不做扁平合并：

| 作用域 | 生命周期 | 建立时机 | 键 | 更新者 |
|---|---|---|---|---|
| `global` | 与进程/`PluginSet` 同寿命，所有 session 共享同一份引用 | `build_plugin_set()` 调用时 | `model`（`config.openrouter_model`）、`platform`（`platform.system() + platform.release()`）、`timezone`（`datetime.now().astimezone().tzinfo`），加上调用方可选传入的 `global_variables: dict`（覆盖同名默认键） | `registry.py::build_plugin_set()`，一次性计算，之后只读 |
| `session` | 与一次 Discord 会话（一个 `ReactLoopPlugin` 实例）同寿命 | `ReactLoopPlugin.__init__()` 构造时 | `workspace_dir`；`tokens_used`（初值 0）；`turn_count`（初值 0） | `workspace_dir` 由 `__init__` 一次性写入；`tokens_used` 由 `OpenRouterBackendPlugin` 在每次 `ModelRequestEvent` 完成后累加（阻塞/流式路径都支持，流式通过 `stream_options={"include_usage": True}` 从最后一个 chunk 拿 usage），跨 Turn 累计不清零；`turn_count` 由 `ReactLoopPlugin.handle_user_input()` 在方法一开始（`variables` 字典构造之前）`self._session_variables["turn_count"] += 1`，因此本 Turn 内读到的值就是"这是第几轮对话"（1-based），累计不清零 |
| `turn` | 一次 Turn（一次 `handle_user_input()` 调用） | 每次 `handle_user_input()` 开始时新建空 dict | `now`（UTC ISO8601，精确到秒，由 `TurnVariableUpdaterPlugin` 在 `TurnStartEvent` 上写入）；`step_count`（当前 Step 序号，0-based，由 `ReactLoopPlugin` 在每次进入 while 循环体时写入，与该次 `StepStart.step_index` 一致） | `TurnVariableUpdaterPlugin`（`now`）、`ReactLoopPlugin.handle_user_input()`（`step_count`）；任何 Turn 内事件的订阅者都可以继续往 `turn` 里写 |

`ReactLoopPlugin.handle_user_input()` 在 Turn 开始时创建：

```python
variables: dict = {
    "global": self._global_variables,   # 引用，跨 Turn/跨 session 不变
    "session": self._session_variables, # 引用，跨 Turn 不变，随 loop 实例存在
    "turn": {},                         # 每个 Turn 新建
}
```

并把同一个 `variables` 引用传给该 Turn 内所有 Turn/Step/工具生命周期事件的 payload（`TurnStart`/`TurnEnd`/`StepStart`/`StepEnd`/`BeforeModelCall`/`ToolCall`/`ToolExecutionStart`/`ToolExecutionEnd`/`AssistantMessage`/`Error`，均带 `variables: dict = field(default_factory=dict)` 字段），以及 `ModelRequest`（同样带 `variables` 字段，专门为了让 `OpenRouterBackendPlugin` 能回写 `session.tokens_used`，见下）。因为 `global`/`session`/`turn` 三个子 dict 都是引用（不是逐次拷贝），任何事件订阅者往 `msg.variables["turn"]` 里写入的键，本 Turn 后续事件的订阅者都能读到；`global`/`session` 也会被订阅者原地修改——`OpenRouterBackendPlugin` 就是这么更新 `session["tokens_used"]` 的（见下）。除 `ModelRequest` 外，其余请求/响应类 payload（`ModelResponse`/`ToolCallSpec`/`ToolCallResult`/`SummarizeRequest`/`SummarizeResult` 等）以及非 Turn 生命周期事件（`BuildSystemPrompt` 等）仍不携带 `variables`。

`TurnVariableUpdaterPlugin`（`plugins/context/variables.py`，无构造参数）订阅 `TurnStartEvent`，只做一件事：`msg.variables["turn"]["now"] = ...`。它在 `registry.py` 的 `context_plugins` 元组里排第一位，保证同一 Turn 后续任何事件读取 `variables["turn"]["now"]` 时这个键已经存在。`turn.step_count` 不经过插件，直接由 `ReactLoopPlugin.handle_user_input()` 在 while 循环体顶部（`this_step = step_index` 之后）写入 `variables["turn"]["step_count"] = this_step`，与该次 `StepStartEvent` 的 `step_index` 保持一致，因此 `BeforeModelCallEvent` 渲染系统提示词时总能读到当前 Step 序号。

`global`/`session` 两层的建立入口不在 `TurnVariableUpdaterPlugin` 里，而是：
- `registry.py::build_plugin_set(config, global_variables=None)` 计算 `resolved_global_variables = {"model": ..., "platform": ..., "timezone": ..., **(global_variables or {})}`，通过闭包捕获进 `loop_factory`；
- `PluginSet.loop_factory` 签名是 `Callable[[handle, tool_schemas, tool_payload_map, workspace_dir, persisted_session_variables], object]`（比原来多了 `workspace_dir` 和 `persisted_session_variables` 两个参数），`core/manager.py::PluginManager.start_session()` 调用时传入 `row.workspace_dir` 和 `row.variables`（后者来自 storage，见下）；
- `ReactLoopPlugin.__init__(..., workspace_dir="", global_variables=None, persisted_session_variables=None)` 建立 `self._session_variables = {"tokens_used": 0, "turn_count": 0, **(persisted_session_variables or {}), "workspace_dir": workspace_dir}`——先给默认值，再用持久化值覆盖（找回上次的 `tokens_used`/`turn_count` 等），最后强制用本次构造传入的 `workspace_dir` 覆盖（不信任持久化里的旧路径，永远以当前会话的实际路径为准）；
- `OpenRouterBackendPlugin.complete()`（阻塞与流式两条路径都会调用同一个 `_record_usage(msg, usage)` 辅助方法）在拿到模型响应的 `usage`（阻塞路径读 `response.usage`；流式路径给 `create()` 传 `stream_options={"include_usage": True}`，从不含 `choices` 的最后一个 chunk 读 `chunk.usage`）后，把 `usage.total_tokens` 累加进 `msg.variables["session"]["tokens_used"]`——因为 `session` 子 dict 和 `ReactLoopPlugin` 持有的是同一个引用，这个累加值跨 Turn 持续到会话结束都不会被重置。`usage` 为 `None`（如 fake/未启用用量统计的响应）或 `variables` 里没有 `session` key 时静默跳过，不抛异常。

**持久化：** `session` 变量每个 Turn 结束都会写入 storage，会话恢复时从 storage 读回，跨进程重启也不丢：
- Schema：`sessions` 表新增 `variables VARCHAR DEFAULT '{}'` 列（JSON 序列化的 `dict`）。新建表（`queries.create_sessions_table_sql()`）直接带这一列；已存在的旧库靠 `StorageService.startup()` 里额外执行的 `queries.add_sessions_variables_column_sql()`（`ALTER TABLE sessions ADD COLUMN IF NOT EXISTS variables VARCHAR DEFAULT '{}'`，DuckDB 支持该语法，幂等）补齐。
- 读取：`services/models.py::Session` 新增 `variables: dict` 字段；`StorageService._session_from_row()` 把该列 `json.loads()` 回 dict（空/`None` 时给 `{}`）；`get_or_create()` 新建会话时 `variables={}`。
- 写入：`SessionHandle.save_variables(variables: dict)`（`services/storage.py`）把整个 dict `json.dumps()` 后 `UPDATE sessions SET variables = ? WHERE session_key = ?`（`queries.set_session_variables_sql`），整体替换而不是合并。
- 调用时机：`ReactLoopPlugin.handle_user_input()` 把 while 循环体（含两个 `except`）包在一个 `try/finally` 里，`finally: self._storage.save_variables(self._session_variables)`——无论 Turn 是正常走到 `break` 后触发 `TurnEndEvent`、被 `AbortTurn` 中止、还是被普通异常中止，这一行都会执行且只执行一次，保证"每次 Turn 结束都持久化"覆盖全部三种收尾路径。因为 `self._session_variables` 就是 `variables["session"]` 的同一个引用，Turn 期间任何写入（例如 `OpenRouterBackendPlugin` 累加的 `tokens_used`）在持久化时都已经生效。
- 恢复时机：`PluginManager.start_session()` 每次都会调用 `storage.get_or_create()`（新会话或恢复已有会话都走这条路径），把拿到的 `row.variables` 传给 `loop_factory` 再传给 `ReactLoopPlugin.__init__`；对新会话这就是 `{}`（用默认值 `tokens_used=0`），对恢复的会话则是上次持久化的值。

真正的模板渲染发生在 `SystemPromptPlugin.apply()`（`BeforeModelCallEvent` 的第一个订阅者）里，在 `_assemble()` 把所有 section 拼成纯文本之后：

```python
system_text = self._assemble(sections_msg.sections)
system_text = Template(system_text).render(**ctx.variables)
```

`**ctx.variables` 把 `global`/`session`/`turn` 三个 key 展开成同名关键字参数传给 `render()`（Jinja2 不受 Python `global` 关键字保留限制），因此各 section 插件（`identity`/`execution.md` 等）里的占位符要写成带命名空间前缀的形式，例如 `prompts/identity.md` 现在写的是 `"You are Conic, a helpful coding agent running on {{ global.model }}."`；`session` 级变量用 `{{ session.workspace_dir }}`，`turn` 级变量用 `{{ turn.now }}`。渲染时机在"所有 section 收集完成之后、拼成最终 system 消息之前"一次性完成，而不是每个 section 插件各自渲染。`SystemPromptPlugin.apply()` 返回的新 `BeforeModelCall` 会把 `variables=ctx.variables` 原样带上，但由于它是 `BeforeModelCallEvent` 链上唯一读取/渲染 `variables` 的订阅者，链上后续的 `TruncatorPlugin`/`TokenBudgetPlugin` 即使构造自己的 `BeforeModelCall` 时不传 `variables`（用默认空 dict）也不影响结果——本次事件周期内不会再有人读它。

## 8. 各插件详细设计

### 8.1 LoopPlugin — ReactLoopPlugin
唯一订阅 `user_input` 的编排者，持有本会话的 `storage_handle`，驱动整个 Turn/Step 流程。

- 构造时除 `tool_schemas` 外还持有 `tool_payload_map: dict[str, type]`——工具的 `llm_name` → 该工具 `execute()` 参数类型（由 `PluginManager` 用 `infer_payload_type` 从签名自动推断），用于把模型返回的 `ToolCallSpec.args`（dict）转换成对应工具的 dataclass payload，再走 `bus.request(ToolCallRequestEvent, payload)`。
- 系统提示词不落库：每个 Step 都从 `storage.load_history()` 取纯对话历史（不含 system），再交给 `BeforeModelCallEvent` 链（`SystemPromptPlugin` 会重新拼一份 system 消息临时前置），因此 system prompt 内容可以随 workspace/runtime 等运行时信息逐 Step 刷新，但不会污染持久化历史。
- 单次工具调用若在 `ToolCallEvent`/`ToolCallRequestEvent`/`ToolCallResultEvent` 任一环节抛出普通异常，Loop 会捕获并把 `f"Error: {exc}"` 作为该 `tool_call_id` 的回复内容写回历史（保留原始 `call.id`，不中断整个 Turn）；`AbortTurn` 是这个局部 catch 的显式例外——即使在这三个环节里抛出（例如 `PermissionPolicyPlugin` 在 `before_tool_call` 里拒绝一次调用），也会先被 `except AbortTurn: raise` 放行，穿透到外层，和 `StepLimitPlugin` 那种在 `StepStartEvent` 抛出的 `AbortTurn` 一样，终止整个 Turn 并发 `ErrorEvent`。
- 每个 Step 结束时（不论走"最终回答"分支还是"处理完所有工具调用"分支）都会 emit `StepEndEvent`，`step_index` 与该 Step 的 `StepStartEvent` 一致；Step 因 `AbortTurn` 中止时不会补发（与 `TurnEndEvent` 在出错时也不补发、改由 `ErrorEvent` 收尾的既有约定一致）。
- `ToolExecutionStartEvent`/`ToolExecutionEndEvent` 括住 `bus.request(ToolCallRequestEvent, payload)` 这次实际派发：前者在 `before_tool_call` 策略检查通过、`payload` 构造完成后 emit；后者携带派发拿到的原始 `ToolCallResult`（在 `ToolCallResultEvent` 观察/改写链跑之前），用于区分"策略放行"与"真正开始执行"。两者都在同一个 `try` 块内，不改变 `AbortTurn`/普通异常的传播路径。
- **实时状态展示**：每次 `ToolExecutionStartEvent` 之后紧跟着 emit `MessageUpdateEvent(MessageUpdate(text=self._format_tool_status(call_ctx.call)))`，其中 `_format_tool_status` 是通用格式化（不区分具体工具）：`f"🔧 {call.name}(" + ", ".join(f"{k}={v!r}" for k, v in call.args.items()) + ")"`，例如 `🔧 bash(command='ls -la')`。这个 emit 只负责"当前状态该显示成什么文字"，具体怎么把文字落到 Discord 消息上是 `DiscordThreadPlugin`（8.5）的职责，Loop 本身不知道、也不关心渲染细节。**`StepStartEvent` 本身不会触发"🤔 思考中…"重置**——早期版本每个 Step 开始时都会把响应式消息打回"思考中"，但这会把上一个 Step 留下的、仍有意义的工具状态行/已流式输出文本无谓抹掉；"思考中"现在只在 `TurnStartEvent` 时由 `DiscordThreadPlugin` 发一次占位消息（见 8.5），之后完全靠 `MessageUpdateEvent`（工具状态）和 `MessageDeltaUpdateEvent`（模型流式文本）自然覆盖，Loop 不再主动"复位"。
- 请求模型时把 `ModelRequest.stream_updates` 显式设为 `True`（`ModelRequest(messages=ctx.messages, tools=ctx.tools, stream_updates=True)`），让 `OpenRouterBackendPlugin`（8.2）对本次调用走流式路径、逐 token emit `MessageDeltaUpdateEvent`。`AssistantMessageEvent`/`ErrorEvent`/`TurnEndEvent` 的 emit 时机和内容完全不变——Loop 不需要为"把消息编辑成最终答案"做任何特殊处理，这仍然是 `DiscordThreadPlugin` 订阅这两个既有事件后自己完成的。

### 8.2 BackendPlugin — OpenRouterBackendPlugin
唯一应答 `model_request`，用 openai SDK 调用 OpenRouter API。

`register()` 额外保存一份 `self._bus`（原来不需要，现在流式分支要用它 emit chunk）。`complete()` 按 `msg.stream_updates` 分两条路径：

- **`stream_updates=False`（默认）**：和原来完全一样的一次性阻塞调用，`SummarizerPlugin` 内部摘要用的 `ModelRequest` 从不设这个字段，永远走这条路径——摘要生成不会驱动任何 Discord 更新。
- **`stream_updates=True`（`ReactLoopPlugin` 每次真正驱动 Turn 的模型调用都会用到）**：改为 `stream=True` 调用 OpenAI SDK，边迭代 chunk 边组装：
  - 每个 chunk 的 `delta.content` 只要非空，就追加进 `content_parts` 并 `bus.emit(MessageDeltaUpdateEvent, MessageDeltaUpdate(text_delta=delta.content))`；工具调用参数的 JSON 片段（`delta.tool_calls[i].function.arguments`）**不会**触发这个事件，用户看不到原始参数流。
  - `tool_calls` 的增量按 `index` 累加进 `dict[int, dict]`（`id`/`name` 只在各自 index 的首个 chunk 出现一次，`arguments` 是要靠 index 拼接的 JSON 字符串片段），收尾按 index 排序、`json.loads` 解析出最终 `ToolCallSpec` 列表；某个工具调用全程没有 `arguments` 片段时用 `{}` 兜底，避免 `json.loads("")` 抛异常。
  - 组装出的 `raw_message` dict 只重建了 `role`/`content`/`tool_calls` 这个**兼容子集**，足以支撑会话历史回放，但并不需要（也做不到）和非流式分支的 `message.model_dump()` 逐字节一致——非流式分支的 `model_dump()` 会带上 provider 返回的所有字段（例如 `refusal`、`reasoning` 等 provider 专属字段），流式分支不会重建这些。`tool_calls` 字段是 `[{"id":.., "type": "function", "function": {"name":.., "arguments": json.dumps(tc.args)}}] or None`——注意这里要把 `ToolCallSpec.args`（解析后的 dict，方便 Loop 直接构造工具 payload）重新 `json.dumps` 回字符串，两种表示是反着来的。

### 8.3 ToolPlugin（4 个）
每个工具构造时绑定本会话 `workspace_dir`，任何解析后越出该目录的路径直接拒绝。路径校验逻辑集中在 `plugins/tools/base.py`：`resolve_within_workspace(workspace_dir, path)` 把相对路径解析到 `workspace_dir` 下并 `.resolve()`，若结果不在 workspace 内则 `raise WorkspaceEscapeError`；四个文件类工具都复用这一个函数，不各自实现越权检查。

四个工具的 `register()` 现在都额外 hook `BuildSystemPromptEvent`，各自贡献一段"我只能访问 `{workspace_dir}`"的 prompt section（见第 7 节表格），把代码层已经强制的越权拒绝也讲给模型听——目的是让模型一开始就不去尝试越权路径，而不是等工具报错才知道。三个文件工具（read/write/edit）的措辞可以是陈述句（"paths outside it are rejected"），因为 `resolve_within_workspace` 真的会拒绝；`BashToolPlugin` 的措辞是请求句（"stay inside it, don't cd out"），因为 bash 只是把 `cwd` 设到 `workspace_dir`，并没有在代码层阻止 `cd ..`/绝对路径逃逸——这段 prompt 是目前唯一的"软约束"，不是真正的沙箱，见 `docs/Improvement.md`"安全与隔离"一节。

- `BashToolPlugin`：`asyncio.create_subprocess_shell` 在 `workspace_dir` 下执行，超时（`BASH_TIMEOUT`，默认 60s）由 `asyncio.wait_for` 包裹 `proc.communicate()`；超时后 `proc.kill()` + `proc.wait()` 回收进程，返回 `ToolCallResult(error="command timed out after {timeout}s")`。超时值通过 `registry.py` 里定义的 `ConfiguredBashToolPlugin`（`BashToolPlugin` 的一个薄子类，`__init__` 只接 `workspace_dir` 以匹配 `PluginManager` 对所有 `tool_classes` 统一的 `tool_cls(workspace_dir=...)` 实例化方式，内部把 `config.bash_timeout` 转发给父类）从 `Config` 注入——`tool_classes` 里的类要同时支持"当类用"（`cls.schema`/`cls.llm_name`/`cls.execute` 静态访问）和"当工厂用"（绑定运行时配置），子类化是能同时满足两者的最小改法。stdout/stderr 合并后按字节截断（默认 20000 字节，超出附加 `...[truncated]`），非零退出码作为 `error` 返回。
- `ReadFileToolPlugin`：`offset`/`limit`（默认 0 / 2000 行）按行切片，超出部分返回时附加总行数提示
- `WriteFileToolPlugin`：创建/覆盖文件，返回结果里报告新旧行数和 created/overwritten 状态
- `EditFileToolPlugin`：要求 `old_text` 在文件中**精确出现一次**，否则报错（未找到 / 不唯一），成功后只替换第一处匹配

### 8.4 DiscordGateway（Core Service，实现 `Gateway` Protocol）
进程级 discord.py 连接持有者，维护 `{thread_id: SessionScope}` 路由表。在 `conic/discord/gateway.py`。

- 会话生命周期事件由 **Gateway 自己 emit，不是 `PluginManager`**：`handle_start_command`/`resume_active_sessions` 在 `PluginManager.start_session()` 返回后立刻 `scope.bus.emit(SessionStartEvent, SessionStart(reason="new"/"resume"))`；`handle_stop_command` 在 `scope.lock` 内、`SessionStopEvent` 之后、`PluginManager.stop_session()` 之前 emit `SessionEndEvent(reason="user_stop")`。选择让 Gateway 而不是 `PluginManager` emit，是因为"reason"是调用方（Gateway）才知道的信息，这样 `PluginManager.start_session`/`stop_session` 保持同步、无需改造。`resume_active_sessions` 里线程确实丢失（`fetch_thread` 失败）的分支没有 bus 可 emit——session 从未真正建起。
- `handle_message` 在把文本转成 `UserInputEvent` 之前，先在同一把 `scope.lock` 内链式 emit `InputEvent(Input(text=text))`：钩子可返回改写过 `text` 的 `Input`（继续走 loop，但用新文本），或把 `handled` 置 `True` 完全拦下（`UserInputEvent` 不发出，方法直接返回）。

### 8.5 DiscordThreadPlugin（Plugin）
每会话一份，订阅 `AssistantMessageEvent`/`ErrorEvent`/`TurnStartEvent`/`StepStartEvent`/`MessageUpdateEvent`/`MessageDeltaUpdateEvent`/`TurnEndEvent`/`SessionStopEvent`/`BuildSystemPromptEvent`（贡献 `output` prompt section，见第 7 节）。TurnStart/StepStart 时启动或续接 typing indicator（`TYPING_INTERVAL=8s` 刷新一次 `thread.typing()`，`TYPING_TIMEOUT=20s` 兜底超时自动停止单段任务），TurnEnd/Error/SessionStop 时停止。由于单段任务有 20s 上限，多 Step 的长 Turn 靠每个 `StepStartEvent` 重新拉起一个新任务（若旧任务已超时结束）来续接，避免指示器在 Turn 中途消失。在 `conic/plugins/channels/discord.py`。

- `SessionStopEvent`（`agent_stop` 命令触发）会把内部 `_stopped` 标记置位；之后任何 `AssistantMessageEvent`/`ErrorEvent` 都会被忽略——防止会话已停止（thread 即将被 archive/lock）后模型仍在跑最后一个 Step 时把消息发进已关闭的线程。
- `_send()` 按 `DISCORD_MESSAGE_LIMIT=2000` 字符切片分段发送，应对 Discord 单条消息长度限制。

**实时状态消息 + token 流式输出**：

内部状态：`_status_message`（占位消息对象，或 `None`）、`_buffer: str`（当前应显示的文字）、`_awaiting_first_delta: bool`、`_last_edit_time: float`，以及一个可注入的 `_clock`（默认 `time.monotonic`，测试时替换成假时钟，避免真实 sleep）。`STREAM_EDIT_INTERVAL = 1.0`（秒）。

- **`on_turn_start`**：除了原来的 `_ensure_typing()`，还把 `_buffer` 设成 `"🤔 思考中…"`、`_awaiting_first_delta` 置 `True`，并 `await self._thread.send(self._buffer)` 拿到 `_status_message`——这是本次改动新增的"Turn 一开始就发一条占位消息"的行为。
- **`on_message_update`**（新增）：`_buffer = msg.text`，`_awaiting_first_delta = True`，然后强制 `_apply_edit(force=True)`（不受节流影响，因为这类"整体替换"频率天然低）。
- **`on_message_delta_update`**（新增）：记下 `force = self._awaiting_first_delta`（复位前的值），如果为真则把 `_buffer` 清空、标记复位，再把 `msg.text_delta` 追加进 `_buffer`；然后 `_apply_edit(force=force)`。**这一条清空规则统一处理了所有"旧状态文字要被新内容取代"的场景**——不管是"思考中"要被工具状态取代，还是工具状态/思考中要被真正开始流式输出的模型文本取代（包括最终答案），都走同一条路径，不需要为"这是最后一步"单独判断。重置后的**第一个** delta 一定立即 edit（`force=True`），不受节流影响——这是从状态行切换到真实内容的关键一帧，值得立刻可见；同一次流式输出里后续的 delta 才会真正受节流约束。`_last_edit_time` 初始化成 `float("-inf")`（而不是 `0.0`）：配合测试用的假时钟从 `0.0` 起算，避免"重置后第一次 edit 恰好发生在 t=0"被误判成"刚 edit 过"而被节流。
- **`_apply_edit(force)`**：`_status_message is None` 或 `_stopped` 时直接跳过；非强制模式下，距上次真正调用 `.edit()` 不足 `STREAM_EDIT_INTERVAL` 秒就只更新内存里的 `_buffer`、不调用 Discord API，留给下一次 delta 或下一次状态切换去补上——因为 `_buffer` 在内存里永远是最新最全的，跳过的只是"现在要不要花一次 Discord API 调用"，不会丢内容。超过 `DISCORD_MESSAGE_LIMIT` 时，流式预览阶段展示末尾 2000 字符并加前缀 `…`（保证看到的是最新内容，而不是卡在开头）。
- **`_finalize(text)`**（`on_assistant_message`/`on_error` 内部复用）：`_status_message` 存在则把它 `.edit()` 成 `text` 的前 2000 字符，剩余部分交给既有的 `_send()` 按原逻辑分段续发；`_status_message` 为 `None`（理论上不会发生，防御性分支）则整段交给 `_send()`。结束后把 `_status_message` 置回 `None`。`on_error` 在调用 `_finalize` 前仍然先 `_stop_typing()`，行为不变。

这样一次 Turn 在 Discord 里呈现为**同一条消息**从头到尾的动态演进：占位 →（可能多轮）思考中/工具状态/流式模型文本 → 编辑成最终答案（或报错），而不是像改动前那样中途只有 typing indicator、结束时才突然冒出一条新消息。

### 8.6 PolicyPlugin
- `PermissionPolicyPlugin`：v1 全部放行
- `StepLimitPlugin`：超过 `MAX_STEPS_PER_TURN` 时 `raise AbortTurn`

### 8.7 Context 插件链
- `SystemPromptPlugin`：emit `BuildSystemPromptEvent` 收集 sections 并组装系统消息；仅当 `ctx.messages[0]` 还不是 `system` 角色时才前置（防御性判断，正常流程下每个 Step 都会重新拼一份）
- `TruncatorPlugin`：非 system 消息数超过 `keep_last_n`（默认 40）时，从尾部保留最近 N 条，system 消息始终保留
- `TokenBudgetPlugin`：用 `core/tokencount.estimate_tokens`（`总字符数 // 4`的粗略估算，不依赖 tokenizer）统计 token 数，超预算时先链式 emit `BeforeSummarizeEvent(BeforeSummarize(request, cancelled=False))`——钩子可改写 `request.instructions` 定制摘要提示词，或把 `cancelled` 置 `True` 跳过本次摘要（`apply()` 直接返回 `None`）；未取消则用（可能被改写的）`request` 发起 `SummarizeEvent`，成功后 emit `SummarizeDoneEvent(SummarizeDone(result))`，失败则先 emit `SummarizeFailedEvent(SummarizeFailed(exc))` 再重新抛出（摘要失败仍会中止整个 Turn，这一行为不变，只是失败前多了一次可观测通知）

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
| `BASH_TIMEOUT` | `60` | `bash` 工具单次命令执行超时（秒），超时后 kill 进程并返回 error |

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
      messagealign.py     # align_cut：截断对齐，防止 orphan tool replies
      tokencount.py       # estimate_tokens：字符数 // 4 的粗略估算
    types/                 # 纯类型定义（dataclass / Protocol / 异常类），无行为逻辑
      gateway.py          # Gateway Protocol
      session.py          # SessionScope（含 per-session asyncio.Lock）
      messages.py         # 所有消息 dataclass
      errors.py           # AbortTurn、NoResponderError、DuplicateResponderError
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
          extra.py
          workspace.py
          execution.py
        variables.py      # TurnVariableUpdaterPlugin (turn 级变量)
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
