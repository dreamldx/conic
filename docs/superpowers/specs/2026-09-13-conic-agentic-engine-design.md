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
- **工具集**：v1 档位——`bash` / `read_file` / `write_file` / `edit_file` / `web_search`（Tavily）/ `web_fetch`（Firecrawl）。web_search 和 web_fetch 通过 HTTP API 直接调用 provider，API key 各自独立配置；是否启用由 `plugins.yaml` 显式声明，声明了但对应 key 未配置时启动失败报错，而不是静默跳过。详见 `docs/superpowers/specs/2026-09-18-web-tools-design.md` 和 `docs/superpowers/specs/2026-09-22-yaml-plugin-config-design.md`。
- **架构要求**：AI 后端、渠道、工具三者都做成插件；agent loop 本身也是插件；插件之间只通过消息机制通信，不做直接函数调用（持久化存储、渠道网关连接等进程级 Core Service 除外，见第 3 节）

使用场景：小型可信团队内部使用，工具具备真实的本地执行能力（bash/文件读写），因此需要工作区沙箱边界，但不需要面向不可信公网用户的重度隔离。

## 2. 核心术语

- **Turn（轮）**：从 loop 消费一批 steering 输入（通常是一条用户消息）到产生一次 `AssistantMessage`（或 `Error`）为止的完整交换。用户发消息、收到最终回复，中间无论循环多少次都算同一轮。
- **Step（步）**：Turn 内部 `while` 循环体的一次迭代——一次 `model_request`/`model_response`，加上该次响应里的所有工具调用。一个 Turn 由 1 个或多个 Step 组成，直到某个 Step 的模型响应不再包含工具调用为止。Step 数量不确定，由模型行为决定，因此需要 `step_limit` 策略插件（`max_steps` 参数，`plugins.yaml` 声明）兜底。

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

## 4. MessageBus：两套注册表 + 邮箱，语义不同

```python
class MessageBus:
    def on_chain(self, type_name: str, handler) -> None: ...
    async def chain(self, type_name: str, payload: Any) -> Any: ...

    def on_request(self, type_name: str, handler) -> None: ...
    async def request(self, type_name: str, payload: Any) -> Any: ...

    def create_mailbox(self, name: str, payload_type: type) -> None: ...
    async def post(self, name: str, payload: Any) -> None: ...
    async def drain(self, name: str) -> list[Any]: ...
    async def wait_multiply_mailbox(self, *names: str) -> None: ...
    async def close(self) -> None: ...
```

| | `on_chain` / `chain` | `on_request` / `request` | mailbox |
|---|---|---|---|
| 订阅者数量 | 0 个或多个，按注册顺序依次处理，每个可返回修改后的 payload 传给下一个，也可 `raise AbortTurn` 中止 | 恰好 1 个；注册第二个同 `(type_name, payload类型)` 的会报错 | 每个 mailbox 名称恰好一个消费者创建，任意生产者可 `post` |
| 用途 | 通知 + 链式加工（前置/后置钩子、审核、渠道输出） | 问答（唯一确定答案：模型补全结果、某个工具的执行结果、摘要结果） | 会话内排队输入与异步唤醒；当前用于 `steering.high` / `steering.low` |

（历史命名：这两套原语最初叫 `on`/`emit`，后改名为 `on_chain`/`chain` 以和第三种原语区分；本文档正文里泛指"发出某事件"时仍会用 emit 作动词。）

**Mailbox（邮箱）是第三种原语**，语义与前两者都不同：生产者随时 `post`，**唯一消费者**在自己方便的时机 `drain` 批量取走——投递和消费在时间上解耦，这正是链式调用给不了的。`create_mailbox(name, payload_type)` 由消费者注册（同名重复注册抛 `DuplicateMailboxError`）；`post` 校验 payload 类型后入队（bus 已 `close()` 后 post 是 no-op，只打 warning，不抛错——进程收尾阶段的迟到投递不该炸掉调用方）；`drain` 一次性取空并复位；`wait_multiply_mailbox(*names)` 挂起直到任意一个邮箱非空或 bus 被 `close()`。目前唯一的邮箱消费者是 `ReactLoopPlugin`，它在 `register()` 里注册 `steering.high`/`steering.low` 两个邮箱（payload 基类 `SteeringItem`，见 `types/steering.py`）；完整动机与设计见 `../../steering-mailbox-design.md`。

**消息类型判定 = 字符串 topic + payload 的 Python 类型**。同一个字符串 topic 下可以有多种不同 payload 类型分别对应不同 handler。

`chain()` 对每个已注册 handler 先做 `isinstance(payload, payload_cls)` 判断，只有类型匹配才会真正调用；`payload_cls` 只来自 handler 参数的类型注解，跟返回值类型无关——框架不检查"参数类型和返回值类型是否一致"，这是靠约定维持的：同一条概念上的链（比如所有订阅 `BeforeModelCallEvent` 的 handler 都该收/发 `BeforeModelCall`）如果某个 handler 返回了别的类型，链上后面注册的同类型 handler 会被 `isinstance` 检查悄悄跳过，不报错。`isinstance` 检查为 False 时 `chain()` 会打一条 `logger.info("chain interrupted: topic={} handler={} expected={} got={}", ...)`——这不代表出错（同一 topic 下多种 payload 类型互相跳过是设计内的正常情况），但如果它出现在你以为都是同一类型的一条链里，就是排查"payload 类型被意外换掉"这类隐蔽 bug 的信号。

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
    reason="new",  # 或 "resume"，透传给 SessionStartEvent
)
```

`PluginManager.start_session()` 内部按固定顺序把插件挂到新建的 `MessageBus` 上：当前配置启用的 ToolPlugin（各自绑定 `workspace_dir`）→ BackendPlugin → context 插件链（variables → system_prompt → truncator → token_budget → extra_prompt）→ policy 插件（permission → step_limit）→ SummarizerPlugin → ReactLoopPlugin → channel 插件 → `SessionGatewayPlugin`。ToolPlugin 数量不是固定 6 个：`bash/read_file/write_file/edit_file` 总是注册，`web_search` 仅在 `TAVILY_API_KEY` 存在时注册，`web_fetch` 仅在 `FIRECRAWL_API_KEY` 存在时注册。`PluginSet.backend` 和其余插件一样也是**工厂闭包**，确保每会话独立实例——`OpenRouterModelPlugin.register()` 会捕获本会话的 `bus`（流式分支要用它 chain `MessageDeltaUpdateEvent`），如果跨会话共享同一个实例，后一个会话的 `register()` 会覆盖前一个会话捕获的 `bus`，导致流式 token 错发到别的会话/线程（曾经的真实 bug，已修复）。`registry.py` 里这个工厂闭包共享同一个 `AsyncOpenAI` 连接实例，只是插件对象本身（连同它捕获的 `bus`）各会话独立，避免为每个会话重复建立 HTTP 连接。

插件全部挂好后，`start_session()` 自己收尾三件事：

1. `await bus.chain(SessionStartEvent, SessionStart(reason=reason))`——`reason`（"new"/"resume"）由调用方（Gateway）透传进来，所以生命周期事件可以由 `PluginManager` 统一发出，不需要每个渠道 Gateway 自己记得发。
2. 同步注册一个 `SessionEndEvent` 标记 handler（记录"本会话是否已经有人发过 SessionEnd"），必须在任何后台任务起跑之前注册，否则可能漏记一次与 `join()` 竞态的 SessionEnd。
3. 在 `scope.tasks` 里创建两个**每会话常驻协程**：`loop_plugin.run_loop()`（消费 steering 邮箱、驱动 Turn，见 8.1）和 `session_gateway.run()`（消费 `scope.queue`、跑 `InputEvent` 拦截链，见下），再挂一个 `_join_and_cleanup` 后台任务：`gather` 等两个常驻任务都退出后，若期间没人发过 `SessionEndEvent` 则补发 `SessionEnd(reason="unexpected_exit")`，把 storage 里该会话置为 `ended`，最后 `bus.close()`。

`SessionGatewayPlugin`（`core/session_gateway.py`）是**渠道无关**的每会话协程：轮询 `scope.queue`（由渠道 Gateway 的 `handle_message` 投喂原始文本；轮询而非永久阻塞在 `queue.get()`，是因为 asyncio 没法从外部唤醒一个卡在 await 里的协程，`scope.closing` 只能靠超时 tick 检查到），对每条文本先跑 `chain(InputEvent, Input(text))` 拦截链（钩子可改写 `text` 或置 `handled=True` 完全拦下），未被拦下的包成 `SteeringUserMessage` `post` 进 `steering.high` 邮箱。并发控制不再靠锁：早期版本 `SessionScope` 里有一把 `asyncio.Lock` 串行化 `handle_message`/停止命令，mailbox 重构后消息只是入队，什么时候消费、一次消费几条由 `ReactLoopPlugin.run_loop()` 的检查点决定（见 8.1 和 `../../steering-mailbox-design.md`），锁已删除。

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
3. 查 storage 里 `channel="discord"` 且 `status="active"` 的会话，逐个恢复，填进内存路由表 `{thread_id: SessionScope}`；如果远端 thread 已丢失或已归档，则直接把该 session 标成 `ended`
4. 阻塞进入 discord.py 事件循环

## 6. 完整消息生命周期

| Topic / channel | Verb | Payload | 处理者 |
|---|---|---|---|
| `InputEvent` | chain | `Input` | 无默认订阅者（拦截点：`SessionGatewayPlugin` 在把文本 post 进 `steering.high` 之前发出，钩子可改写 `text` 或置 `handled=True` 拦下） |
| `steering.high` | mailbox | `SteeringItem` | `ReactLoopPlugin.run_loop()`；当前接收 `SteeringUserMessage` 和 `SteeringStopCommand` |
| `steering.low` | mailbox | `SteeringItem` | `ReactLoopPlugin.run_loop()`；当前供后台结果等低优先级输入预留 |
| `SessionStartEvent` | chain | `SessionStart` | `PluginManager.start_session()` 注册完插件后发出；默认无订阅者（会话开始通知，reason: new/resume） |
| `TurnStartEvent` | chain | `TurnStart` | TurnVariableUpdaterPlugin（注入 `now` 到 `variables["turn"]`）→ DiscordThreadPlugin（typing indicator + 发送占位状态消息） |
| `StepStartEvent` | chain | `StepStart` | StepLimitPlugin、DiscordThreadPlugin（续接 typing indicator） |
| `BeforeModelCallEvent` | chain | `BeforeModelCall` | SystemPromptPlugin → TruncatorPlugin → TokenBudgetPlugin → ExtraPromptPlugin |
| `BeforeSummarizeEvent` | chain | `BeforeSummarize` | 无默认订阅者（可改写 `instructions` 或置 `cancelled=True`） |
| `SummarizeEvent` | request | `SummarizeRequest` → `SummarizeResult` | SummarizerPlugin |
| `SummarizeDoneEvent` | chain | `SummarizeDone` | 无默认订阅者 |
| `SummarizeFailedEvent` | chain | `SummarizeFailed` | 无默认订阅者（chain 后异常仍会重新抛出） |
| `ModelRequestEvent` | request | `ModelRequest` → `ModelResponse` | OpenRouterModelPlugin |
| `SwitchModelRequestEvent` | request | `SwitchModelRequest` → `SwitchModelResult` | OpenRouterModelPlugin（见 8.2）；目前没有默认的发送方 |
| `MessageDeltaUpdateEvent` | chain | `MessageDeltaUpdate` | DiscordThreadPlugin（追加缓冲区 + 节流 edit）；仅当 `ModelRequest.stream_updates=True` 时由 OpenRouterModelPlugin 逐 chunk chain |
| `ModelResponseEvent` | chain | `ModelResponse` | 无默认订阅者 |
| `ToolCallEvent` | chain | `ToolCall` | PermissionPolicyPlugin |
| `ToolExecutionStartEvent` | chain | `ToolExecutionStart` | 无默认订阅者；ReactLoopPlugin 紧接着 chain `MessageUpdateEvent`（工具状态行） |
| `MessageUpdateEvent` | chain | `MessageUpdate` | DiscordThreadPlugin（整体替换缓冲区 + 立即 edit，不节流） |
| `ToolCallRequestEvent` | request | tool payload → `ToolCallResult` | 对应 ToolPlugin |
| `ToolExecutionEndEvent` | chain | `ToolExecutionEnd` | 无默认订阅者（携带派发得到的原始 `ToolCallResult`） |
| `ToolCallResultEvent` | chain | `ToolCallResult` | 无默认订阅者 |
| `StepEndEvent` | chain | `StepEnd` | 无默认订阅者 |
| `AssistantMessageEvent` | chain | `AssistantMessage` | DiscordThreadPlugin |
| `ErrorEvent` | chain | `Error` | DiscordThreadPlugin |
| `TurnEndEvent` | chain | `TurnEnd` | DiscordThreadPlugin（停止 typing） |
| `BuildSystemPromptEvent` | chain | `BuildSystemPrompt` | Section 插件链（只在每 session 第一次 `BeforeModelCallEvent` 时 chain 一次，结果被 `SystemPromptPlugin` 缓存，见 7.1） |
| `BuildDynamicPromptEvent` | chain | `BuildDynamicPrompt` | `DynamicStateSectionPlugin`（只在每个 Turn 的 Step 0 chain 一次，不缓存但也不逐 Step 重复，见 7.1） |
| `SessionEndEvent` | chain | `SessionEnd` | DiscordThreadPlugin（置 `_stopped` 并停止 typing，此后忽略一切消息输出）、`PluginManager` 内部标记 handler。发出者有两个：`ReactLoopPlugin._finalize_session()`（reason `agent_stop`，`/agent_stop` 或会话级 AbortTurn 触发）、`PluginManager._join_and_cleanup()` 兜底（reason `unexpected_exit`，常驻任务退出但没人发过 SessionEnd 时补发） |

## 7. 系统提示词（System Prompt）

系统提示词通过 bus 消息 `BuildSystemPromptEvent` 动态组装。`SystemPromptPlugin` chain 该消息时携带空的 `sections: dict[str, str]`，各 section 插件依次填充：

| Section | 插件 | 来源 |
|---------|------|------|
| identity | `IdentitySectionPlugin` | `prompts/identity.md`（启动时加载进内存） |
| tooling | `ToolingSectionPlugin` | 当前会话的 tool schemas |
| workspace | `WorkspaceSectionPlugin` | 会话 workspace_dir |
| runtime | `RuntimeSectionPlugin` | 纯 Jinja2 占位符文本，只引用 `global.*`（`{{ global.platform }}`/`{{ global.shell }}`/`{{ global.model }}`/`{{ global.timezone }}`）——这是它能被 `SystemPromptPlugin` 缓存、只渲染一次的前提，见 7.1 |
| execution | `ExecutionBiasSectionPlugin` | `prompts/execution.md`（启动时加载进内存） |
| output | `DiscordThreadPlugin`（渠道插件） | 硬编码常量 `OUTPUT_REQUIREMENTS`（`plugins/channels/discord.py`） |
| bash | `BashToolPlugin`（工具插件） | 动态拼接，携带当前会话的 `self._timeout` 和 `self._workspace_dir`（`plugins/tools/bash.py`） |
| read_file | `ReadFileToolPlugin`（工具插件） | 动态拼接，携带当前会话的 `self._workspace_dir`（`plugins/tools/read_file.py`） |
| write_file | `WriteFileToolPlugin`（工具插件） | 动态拼接，携带当前会话的 `self._workspace_dir`（`plugins/tools/write_file.py`） |
| edit_file | `EditFileToolPlugin`（工具插件） | 动态拼接，携带当前会话的 `self._workspace_dir`（`plugins/tools/edit_file.py`） |
| web_search | `WebSearchToolPlugin`（工具插件） | 模块常量 `WEB_SEARCH_SECTION`，含 `{{ turn.now }}`、topic/country/exact_match/include_domains/include_answer 等参数的使用引导（`plugins/tools/web_search.py`） |
| web_fetch | `WebFetchToolPlugin`（工具插件） | 模块常量 `WEB_FETCH_SECTION`，含 fetch 节制指引及不可信内容警告（`plugins/tools/web_fetch.py`） |

任何插件都可以 hook `BuildSystemPromptEvent` 注入自定义 section。`SystemPromptPlugin._assemble()` 按 `SECTION_ORDER` 拼接为最终系统消息；不在 `SECTION_ORDER` 里的 section（未来插件新增的）会追加在已知 section 之后，不会丢失——`output` 就是这样一个例子：由渠道插件（而不是 `context_plugins` 里的固定 section 插件）贡献，告诉模型当前输出渠道（Discord）的格式限制（不渲染 markdown 表格、标题只支持到 `###`）、流式渲染方式（同一条消息逐 token 编辑，不需要模型自己分段）、以及要求回复不超过单条消息字符数上限（2000）。这也是"渠道相关的输出要求应该由渠道插件自己声明，而不是写死在 core prompt 里"这一设计意图的落地。`bash` 是同一模式在工具侧的例子：`BashToolPlugin.register()` 同时 hook `ToolCallRequestEvent`（真正执行命令）和 `BuildSystemPromptEvent`（告诉模型"超过 `self._timeout` 秒的命令会被 kill 并报错，不要跑长期运行/阻塞/交互式命令"），把"这个工具有什么限制"和"工具本身怎么实现"放在同一个文件里维护，且提示词里的超时数字直接读 `self._timeout`，配置改了不会和提示词文字脱节。

`registry.py` 里的 `_load_prompts()` 并不是只认 `identity.md`/`execution.md` 两个硬编码文件名，而是遍历 `prompts/*.md` 下所有文件，以文件名（去掉 `.md`）为 key 存进字典；`build_plugin_set()` 目前只取 `identity`/`execution` 两个 key 使用，往 `prompts/` 下新增 `.md` 文件不会自动被消费，需要相应 section 插件去 `prompts.get(...)`。

### 7.1 变量注入与 Jinja2 模板渲染

模板变量分三层作用域，各自的生命周期和来源不同，渲染时通过命名空间区分，不做扁平合并：

| 作用域 | 生命周期 | 建立时机 | 键 | 更新者 |
|---|---|---|---|---|
| `global` | 与进程/`PluginSet` 同寿命，所有 session 共享同一份引用 | `build_plugin_set()` 调用时 | `model`（`config.openrouter_model`）、`platform`（`platform.system() + platform.release()`）、`shell`（`_detect_shell()`，见下）、`timezone`（`datetime.now().astimezone().tzinfo`）、`model_context_length`（当前配置模型的上下文窗口大小，见下），加上调用方可选传入的 `global_variables: dict`（覆盖同名默认键） | `registry.py::build_plugin_set()`，一次性计算，之后只读 |
| `session` | 与一次 Discord 会话（一个 `ReactLoopPlugin` 实例）同寿命 | `ReactLoopPlugin.__init__()` 构造时 | `workspace_dir`；`tokens_used`（初值 0）；`turn_count`（初值 0）；`context_usage`（本 session 迄今为止最大的一次 prompt token 数，见下） | `workspace_dir` 由 `__init__` 一次性写入；`tokens_used` 由 `OpenRouterModelPlugin` 在每次 `ModelRequestEvent` 完成后累加（阻塞/流式路径都支持，流式通过 `stream_options={"include_usage": True}` 从最后一个 chunk 拿 usage），跨 Turn 累计不清零；`turn_count` 由 `ReactLoopPlugin._run_turn()` 在方法一开始（`variables` 字典构造之前）`self._session_variables["turn_count"] += 1`，因此本 Turn 内读到的值就是"这是第几轮对话"（1-based），累计不清零；`context_usage` 由 `OpenRouterModelPlugin._record_usage()` 每次模型响应后取 `max(旧值, usage.prompt_tokens)` 写回，见下 |
| `turn` | 一次 Turn（一次 `_run_turn()` 调用） | 每次 `_run_turn()` 开始时新建空 dict | `now`（UTC ISO8601，精确到秒，由 `TurnVariableUpdaterPlugin` 在 `TurnStartEvent` 上写入）；`step_count`（当前 Step 序号，0-based，由 `ReactLoopPlugin` 在每次进入 while 循环体时写入，与该次 `StepStart.step_index` 一致）；`steering_count`（这轮 steering 注入产生的 history 条目数，由 `ReactLoopPlugin._run_turn()` 在 `_inject([*high, *low], turn_id)` 时写入，供 `ExtraPromptPlugin` 计算插入位置，见 7.1 后文） | `TurnVariableUpdaterPlugin`（`now`）、`ReactLoopPlugin._run_turn()`（`step_count`/`steering_count`）；任何 Turn 内事件的订阅者都可以继续往 `turn` 里写 |

`ReactLoopPlugin._run_turn()` 在 Turn 开始时创建：

```python
variables: dict = {
    "global": self._global_variables,   # 引用，跨 Turn/跨 session 不变
    "session": self._session_variables, # 引用，跨 Turn 不变，随 loop 实例存在
    "turn": {},                         # 每个 Turn 新建
}
```

并把同一个 `variables` 引用传给该 Turn 内所有 Turn/Step/工具生命周期事件的 payload（`TurnStart`/`TurnEnd`/`StepStart`/`StepEnd`/`BeforeModelCall`/`ToolCall`/`ToolExecutionStart`/`ToolExecutionEnd`/`AssistantMessage`/`Error`，均带 `variables: dict = field(default_factory=dict)` 字段），以及 `ModelRequest`（同样带 `variables` 字段，专门为了让 `OpenRouterModelPlugin` 能回写 `session.tokens_used`，见下）。因为 `global`/`session`/`turn` 三个子 dict 都是引用（不是逐次拷贝），任何事件订阅者往 `msg.variables["turn"]` 里写入的键，本 Turn 后续事件的订阅者都能读到；`global`/`session` 也会被订阅者原地修改——`OpenRouterModelPlugin` 就是这么更新 `session["tokens_used"]` 的（见下）。除 `ModelRequest` 外，其余请求/响应类 payload（`ModelResponse`/`ToolCallSpec`/`ToolCallResult`/`SummarizeRequest`/`SummarizeResult` 等）以及非 Turn 生命周期事件（`BuildSystemPrompt` 等）仍不携带 `variables`。

`TurnVariableUpdaterPlugin`（`plugins/context/variables.py`，无构造参数）订阅 `TurnStartEvent`，只做一件事：`msg.variables["turn"]["now"] = ...`。它在 `registry.py` 的 `context_plugins` 元组里排第一位，保证同一 Turn 后续任何事件读取 `variables["turn"]["now"]` 时这个键已经存在。`turn.step_count` 不经过插件，直接由 `ReactLoopPlugin._run_turn()` 在 while 循环体顶部（`this_step = step_index` 之后）写入 `variables["turn"]["step_count"] = this_step`，与该次 `StepStartEvent` 的 `step_index` 保持一致；`ExtraPromptPlugin` 正是靠这个值判断"是不是这个 Turn 的第一个 Step"，只在 `step_count == 0` 时产生动态内容（见 7.1 后文）。`turn.steering_count`（同样由 `_run_turn()` 写入，`_inject([*high, *low], turn_id)` 的返回值）记录这轮 steering 注入了多少条 history，供 `ExtraPromptPlugin` 计算插入位置。`SystemPromptPlugin` 的静态系统提示词只在本 session 第一次 `BeforeModelCallEvent` 时渲染并缓存，不会随后续 Step 重新渲染 `turn.*`。

**踩过的坑，记录一下以免以后重踩**：曾经尝试过在 `turn` 里加一个 `context_length`，本地用 `estimate_tokens(ctx.messages)` 估算"这次发给模型的上下文大概多大"，还想把它渲染进 `state` 段给模型自己看。结果撞上先有鸡还是先有蛋的问题——`state` 段本身是 `ExtraPromptPlugin` 在 `BeforeModelCallEvent` 链的最后一步插入的，如果要在这段文字里报告"这次发了多大"，这个数字理论上得等 state 段插入完才能精确算出来，但那时候模板已经渲染完了。当时的权宜解法是接受近似（在插入 state 消息之前，用不含它自己的 `ctx.messages` 先估一个近似值），但已经整体移除了：改成更直接的方式——见下文 `session.context_usage`，直接用模型响应里 provider 自己报出来的精确 `usage.prompt_tokens`，不再本地估算，也不再费劲塞进生成前的提示词里。

`global`/`session` 两层的建立入口不在 `TurnVariableUpdaterPlugin` 里，而是：
- `registry.py::build_plugin_set(config, global_variables=None)` 计算 `resolved_global_variables = {"model": ..., "platform": ..., "shell": ..., "timezone": ..., **(global_variables or {})}`，通过闭包捕获进 `loop_factory`。`shell` 由私有函数 `_detect_shell()` 算出，故意不探测 conic 进程自己跑在哪个交互式 shell 下（那和实际执行工具调用的解释器是两回事），而是直接对齐 `BashToolPlugin.execute()` 底层 `asyncio.create_subprocess_shell`（`shell=True`）真正会 spawn 的程序：Windows 读 `ComSpec` 环境变量取文件名（未设置时回退 `"C:\Windows\System32\cmd.exe"` 的文件名 `"cmd.exe"`——CPython `subprocess.py` 的 Windows `_execute_child` 在 `shell=True` 时就是这么解析 `comspec` 并拼成 `"{comspec} /c \"{args}\""` 的，逐行读源码 + 用 `echo %ComSpec%` 这种只有 cmd.exe 才会展开 `%VAR%` 的探测命令实测验证过），macOS（Darwin）固定返回 `"/bin/bash"`——`create_subprocess_shell` spawn 的确实是 `/bin/sh`，但 macOS 的 `/bin/sh` 经 `/var/select/sh` 实际就是 bash（3.2，sh 兼容模式），直接把真实实现告诉模型；其余 POSIX 平台固定 `"/bin/sh"`（CPython 同一份 `_execute_child` 的 POSIX 分支里 `shell=True` 固定 `args = ["/bin/sh", "-c"] + args`，不读 `$SHELL`）。`RuntimeSectionPlugin` 早期版本硬编码过 "PowerShell 5.1"，这条提示词本来就是错的（Windows 上 `shell=True` 走的是 `ComSpec`／通常是 `cmd.exe`，不是 PowerShell）；中途还试过用第三方库 `shellingham` 探测父进程链识别的交互式 shell，但那反映的是"conic 进程本身在哪个 shell 里启动"，跟"`BashToolPlugin` 的每条命令实际被哪个解释器执行"是两个不同的问题——已改回直接对齐后者；
- `PluginSet.loop_factory` 签名是 `Callable[[handle, tool_schemas, tool_payload_map, workspace_dir, persisted_session_variables], object]`（比原来多了 `workspace_dir` 和 `persisted_session_variables` 两个参数），`core/manager.py::PluginManager.start_session()` 调用时传入 `row.workspace_dir` 和 `row.variables`（后者来自 storage，见下）；
- `model_context_length` 由 `entry.py::build_app()` 在调 `build_plugin_set()` 之前算好、通过 `global_variables={"model_context_length": ...}` 传入（不是 `build_plugin_set()` 自己算的，因为它需要查 `StorageService`，而 `build_plugin_set()` 本身不持有 storage 引用）。`build_app()` 现在是 **`async` 函数**：`storage.startup()` 之后，先 `await sync_once(storage, config.openrouter_api_key)`（`sync_once` 参数默认是 `conic/openrouter/catalog.py::sync_catalog_once()`，见 9.1 节），再调 `storage.get_model_context_length(config.openrouter_model)`（`services/storage.py`）按 id 精确查刚同步好的 `model_catalog` 表。这个顺序是特意的：**先联网同步一次目录，再读模型元数据**——原因写在 `entry.py` 那行代码正上方的注释里，就是为了避免"库是空的，只能拿 `DEFAULT_MODEL_CONTEXT_LENGTH = 65535` 兜底"这个问题只在进程重启过至少一次之后才会消失，而是从第一次启动、第一个 session 建立之前就已经是准确值。查询仍然可能查不到（这次同步本身失败了，比如启动时没网；或者配置的模型确实不在 OpenRouter 目录里）——这种情况下才会退回 `DEFAULT_MODEL_CONTEXT_LENGTH = 65535`，bot 依然能正常启动，之后靠后台 `run_periodic_sync` 每小时重试。**这意味着 `build_app()` 不再是"零网络调用"的了**——下文（8.2 节 App 归属那段）曾经写过"build_app() 本身不做任何网络调用"这个不变式，现在不成立了，已经改写，见那里的说明。`DynamicStateSectionPlugin` 把它渲染进 `state` 段的 `Model context window: {{ global.model_context_length }} tokens` 一行。**⚠️ 待办／已知限制：这个值只在进程启动时算一次，此后不会自动刷新**（哪怕后台 `run_periodic_sync` 每小时都在更新 `model_catalog` 表，`global_variables["model_context_length"]` 这个已经算好、塞进内存的值不会跟着表一起变）——跟 `tokens_used` 那种每次读写都实时更新的 `session` 变量不同，`global_variables` 目前全程只读（见上表"更新者"一列：`build_plugin_set()` 一次性计算，之后只读）。"运行时切换模型"现在已经有了消息（`SwitchModelRequestEvent`，见 8.2），但**没有做这件事**——切换时**必须同步更新 `global_variables["model_context_length"]`**，否则 state 段会一直显示旧模型的上下文窗口大小，误导模型对自己实际可用上下文的判断。好消息是这一处比 `global.model`（被 `identity`/`runtime` 两个**静态、只渲染一次就缓存**的 system prompt section 引用，见下文"system 消息只渲染一次"）好改——`model_context_length` 只出现在 `DynamicStateSectionPlugin` 贡献的**动态**段里，每个 Turn 的 Step 0 都会用当前的 `ctx.variables["global"]` 重新渲染一次，所以切换模型时只要把 `self._global_variables`（`ReactLoopPlugin` 持有的那个引用）原地更新，下一次渲染就会自动生效，不需要额外的缓存失效逻辑；但 `global.model` 本身要是也要跟着切换模型变，则必须同时处理 `SystemPromptPlugin._cached_content` 的失效，是两个不同量级的问题；
- `ReactLoopPlugin.__init__(..., workspace_dir="", global_variables=None, persisted_session_variables=None)` 建立 `self._session_variables = {"tokens_used": 0, "turn_count": 0, **(persisted_session_variables or {}), "workspace_dir": workspace_dir}`——先给默认值，再用持久化值覆盖（找回上次的 `tokens_used`/`turn_count` 等），最后强制用本次构造传入的 `workspace_dir` 覆盖（不信任持久化里的旧路径，永远以当前会话的实际路径为准）；
- `OpenRouterModelPlugin.complete()`（阻塞与流式两条路径都会调用同一个 `_record_usage(msg, usage)` 辅助方法）在拿到模型响应的 `usage`（阻塞路径读 `response.usage`；流式路径给 `create()` 传 `stream_options={"include_usage": True}`，从不含 `choices` 的最后一个 chunk 读 `chunk.usage`）后写两个字段：把 `usage.total_tokens` 累加进 `msg.variables["session"]["tokens_used"]`——因为 `session` 子 dict 和 `ReactLoopPlugin` 持有的是同一个引用，这个累加值跨 Turn 持续到会话结束都不会被重置；同时用 `usage.prompt_tokens`（provider 自己数出来的、这次请求 prompt 部分的精确 token 数，不是本地估算）更新 `msg.variables["session"]["context_usage"] = max(session.get("context_usage", 0), usage.prompt_tokens)`——取**本 session 迄今为止见过的最大值**，不是简单覆盖也不是累加：如果这次请求比之前任何一次都大，它就变大；如果这次因为刚发生过截断/摘要而变小了，之前记录的峰值不会被抹掉。目的是追踪"这个 session 历史上最接近过模型上下文上限的程度"，跟 `tokens_used`（累计用量，衡量成本）和 `global.model_context_length`（上限）是三个不同维度的量——`context_usage` 这个名字特意跟 `model_context_length` 区分开，避免让人误以为它也是某种"窗口/容量"而不是"用量峰值"（早期版本曾经也叫 `context_length`，命名上跟 `global.model_context_length` 太像、容易混淆，已经改掉）。时机上总是**慢一拍**：只有等模型响应回来才知道那次请求真实发了多少 token，所以它反映的是"已经发生过的调用里峰值是多少"，不是"这次即将发送的会有多大"——`DynamicStateSectionPlugin` 仍然把它渲染进 `state` section（见 7.1 后文），模型看到的是"目前为止见过的峰值"，不是这次请求本身的大小。`usage` 为 `None`（如 fake/未启用用量统计的响应）或 `variables` 里没有 `session` key 时两个字段都静默跳过，不抛异常。**⚠️ 待办**：`context_usage` 现在只涨不跌——一旦 `TokenBudgetPlugin` 的摘要/压缩真的把 history 缩小了（见 8.7/8.8 节），这个峰值不会跟着下降。等压缩逻辑真正落地时需要决定：`context_usage` 是继续保持"历史峰值"这个语义（当前行为），还是需要一个显式的重置/调整钩子，让它也能反映压缩后的真实大小（代码里 `_record_usage()` 旁边留了同样内容的 TODO 注释）。

**持久化：** `session` 变量每个 Turn 结束都会写入 storage，会话恢复时从 storage 读回，跨进程重启也不丢：
- Schema：`sessions` 表新增 `variables VARCHAR DEFAULT '{}'` 列（JSON 序列化的 `dict`）。新建表（`queries.create_sessions_table_sql()`）直接带这一列；已存在的旧库靠 `StorageService.startup()` 里额外执行的 `queries.add_sessions_variables_column_sql()`（`ALTER TABLE sessions ADD COLUMN IF NOT EXISTS variables VARCHAR DEFAULT '{}'`，DuckDB 支持该语法，幂等）补齐。
- 读取：`services/models.py::Session` 新增 `variables: dict` 字段；`StorageService._session_from_row()` 把该列 `json.loads()` 回 dict（空/`None` 时给 `{}`）；`get_or_create()` 新建会话时 `variables={}`。
- 写入：`SessionHandle.save_variables(variables: dict)`（`services/storage.py`）把整个 dict `json.dumps()` 后 `UPDATE sessions SET variables = ? WHERE session_key = ?`（`queries.set_session_variables_sql`），整体替换而不是合并。
- 调用时机：`ReactLoopPlugin._run_turn()` 把 while 循环体（含两个 `except`）包在一个 `try/finally` 里，`finally: self._storage.save_variables(self._session_variables)`——无论 Turn 是正常走到 `break` 后触发 `TurnEndEvent`、被 `AbortTurn` 中止、还是被普通异常中止，这一行都会执行且只执行一次，保证"每次 Turn 结束都持久化"覆盖全部三种收尾路径。因为 `self._session_variables` 就是 `variables["session"]` 的同一个引用，Turn 期间任何写入（例如 `OpenRouterModelPlugin` 累加的 `tokens_used`）在持久化时都已经生效。
- 恢复时机：`PluginManager.start_session()` 每次都会调用 `storage.get_or_create()`（新会话或恢复已有会话都走这条路径），把拿到的 `row.variables` 传给 `loop_factory` 再传给 `ReactLoopPlugin.__init__`；对新会话这就是 `{}`（用默认值 `tokens_used=0`），对恢复的会话则是上次持久化的值。

模板渲染发生在 `SystemPromptPlugin.apply()`（`BeforeModelCallEvent` 的第一个订阅者）里，在 `_assemble()` 把所有 section 拼成纯文本之后：

```python
if self._cached_content is None:
    sections_msg = await self._bus.chain(meta.BuildSystemPromptEvent, BuildSystemPrompt(sections={}))
    system_text = self._assemble(sections_msg.sections)
    self._cached_content = Template(system_text).render(**ctx.variables)
```

`**ctx.variables` 把 `global`/`session`/`turn` 三个 key 展开成同名关键字参数传给 `render()`（Jinja2 不受 Python `global` 关键字保留限制），因此各 section 插件（`identity`/`execution.md` 等）里的占位符要写成带命名空间前缀的形式，例如 `prompts/identity.md` 现在写的是 `"You are Conic, a helpful coding agent running on {{ global.model }}."`。

**system 消息只渲染一次、之后全 session 复用（`SystemPromptPlugin.__init__` 里的 `self._cached_content: str | None = None`）**：`SystemPromptPlugin` 是每 session 一个新实例（`context_plugins` 工厂闭包保证），所以这个缓存天然是 session 级的——第一次 `apply()`（本 session 第一个 Step）才会真正 chain `BuildSystemPromptEvent` + `_assemble()` + `Template(...).render()`，之后每个 Step 的 `apply()` 直接复用 `self._cached_content`，不再重新收集 section、不再重新渲染。这要求参与这次渲染的所有 section **只能引用 `global.*`**——`identity`（`global.model`）、`tooling`/`workspace`（不含模板语法，构造时就是定值）、`runtime`（`global.platform`/`global.shell`/`global.model`/`global.timezone`）、`execution`、各工具的静态 section 都满足这一条件，因此把它们缓存下来是安全的：同一个 session 里 `global` 不会变，缓存的渲染结果自然也不会过期。缓存的直接收益是给 OpenRouter/DeepSeek 之类支持 prompt 前缀缓存的后端一个**跨请求完全不变的 system 消息**，不会像"每个 Step 都带一份不同的当前时间/step 数"那样把缓存前缀每次都打断。`SystemPromptPlugin.apply()` 返回的新 `BeforeModelCall` 会把 `variables=ctx.variables` 原样带上。**`TruncatorPlugin`/`TokenBudgetPlugin` 在它们真正改写消息列表时也必须转发 `variables=ctx.variables`**——`ExtraPromptPlugin` 排在它们之后，会用 `ctx.variables` 渲染动态 section，如果这两者中任何一个在构造自己的 `BeforeModelCall` 时漏掉 `variables`（默认值是空 dict `{}`），`ExtraPromptPlugin` 拿到的 `ctx.variables` 就不含 `global`/`session`/`turn` 任何一个 key，`Template(text).render(**ctx.variables)` 渲染 `{{ turn.now }}` 会直接抛出 `jinja2.exceptions.UndefinedError: 'turn' is undefined`（真实出现过的 bug：`TruncatorPlugin.apply()`/`TokenBudgetPlugin.apply()` 早期实现里 `return BeforeModelCall(...)` 都没带 `variables`，因为写这段代码时 `ExtraPromptPlugin` 还没引入、链上确实没人在它们之后读 `variables`；后来把 `ExtraPromptPlugin` 加到链尾读 `ctx.variables` 时，这两处忘了同步补上）。`tests/plugins/context/test_truncator.py::test_forwards_variables_when_truncating`、`test_token_budget.py::test_forwards_variables_when_summarizing` 和 `test_extra_prompt.py` 里两个 `test_survives_after_*_in_the_real_registry_chain_order` 端到端测试专门覆盖这一点。

**会变的信息不再混进 system 消息，改成 `ExtraPromptPlugin` 在每个 Turn 的 Step 0 插入一条独立的 `user`-role 消息**（`plugins/context/extra_prompt.py`，直接订阅 `BeforeModelCallEvent`，不再是 `BuildSystemPromptEvent` 的 section 贡献者）。它的收集机制和 `SystemPromptPlugin` 是同一套模式的镜像：`SystemPromptPlugin` 构造时接收一份 `section_plugins` 列表，chain `BuildSystemPromptEvent` 收集只读一次的 `global` 级 section；`ExtraPromptPlugin` 同样构造时接收一份 `section_plugins` 列表（目前只有 `DynamicStateSectionPlugin` 一个），chain 新引入的 `BuildDynamicPromptEvent`（`meta.py`）收集 `session`/`turn` 级、会变的 section——但只在 **Turn 的第一个 Step**（`turn.step_count == 0`）做一次，后续同一 Turn 内的 Step 完全不重复：

```python
async def apply(self, ctx: BeforeModelCall) -> BeforeModelCall | None:
    if not ctx.messages:
        return None
    if ctx.variables.get("turn", {}).get("step_count") != 0:
        return None

    sections_msg = await self._bus.chain(meta.BuildDynamicPromptEvent, BuildDynamicPrompt(sections={}))
    text = self._assemble(sections_msg.sections)
    text = Template(text).render(**ctx.variables)
    if not text:
        return None

    steering_count = ctx.variables.get("turn", {}).get("steering_count", 0)
    insert_at = max(0, len(ctx.messages) - steering_count)
    messages = [*ctx.messages[:insert_at], {"role": "user", "content": text}, *ctx.messages[insert_at:]]
    return BeforeModelCall(messages=messages, tools=ctx.tools, variables=ctx.variables)
```

**这个设计经过两次修正，两次都是靠实测（不是猜测）定下来的。**

*第一版*把这段动态内容拼进最后一条已有消息的 `content` 里，不新增消息——因为再往前一版曾经把它当一条新的 `{"role": "system", ...}` 消息追加在 `messages` 最末尾，实测会让 DeepSeek chat/agent 系模型回答错乱，所以改成"不新增消息、只拼内容"来规避。

*第二版（当前实现）*：因为要求变成"每 Turn 只在 Step 0 出现一次、且必须插在这轮 steering 消息之前"（而不是每个 Step 都拼在当时最后一条消息后面），"拼进已有消息 content"这个技巧不再适用——Step 0 时"这轮 steering 消息之前"和"这轮 steering 消息之后"是完全不同的两条消息，没法靠"改内容"实现，必须真的插入一条新消息。这就重新把"新消息用什么 role"这个问题摆到台面上。这次没有凭经验猜，而是：

1. 直接抓取 HuggingFace 上 `deepseek-ai/DeepSeek-R1` 的 `tokenizer_config.json`，确认了它的 Jinja2 chat template 会把 **所有** `role == "system"` 的消息（不管在 `messages` 数组里处于什么位置）统一收集、用 `\n\n` 拼接，渲染在 `<bos>` 之后、第一条 user 消息之前：
   ```jinja2
   {%- for message in messages %}
     {%- if message['role'] == 'system' %}
       {%- if ns.is_first_sp %}
         {% set ns.system_prompt = ns.system_prompt + message['content'] %}
       {%- else %}
         {% set ns.system_prompt = ns.system_prompt + '\n\n' + message['content'] %}
   ...
   {{ bos_token }}{{ ns.system_prompt }}
   ```
   也就是说，不管把 `system` 消息插在 API 请求 `messages` 数组的哪个位置，DeepSeek 模板都会把它拽走、跟静态 system prompt 混在一起——`system` role 对这个用例在架构层面就不可行，不是"训练分布没见过"这种软风险。
2. 用项目实际配置的 OpenRouter key，对 deepseek/qwen/glm/claude/openai 五个厂商做了真实请求的 A/B 测试（多轮工具调用场景：`assistant(上轮答案) → user(这轮输入) → 插入的消息 → 生成`，预期模型正确调用工具）。结果：`system` role 在 DeepSeek 上约 4/5 的请求直接把 `tool_calls` 吞掉（`finish_reason: "stop"`，空响应）——跟第 1 点的模板行为吻合；`user` role 在同样场景下 5/5 稳定，且其余四个厂商无论哪种 role 都没出现问题。额外做了"recall"测试（追加一条只问"state 里的 tokens_used/turn_count 是多少"的问题，不带工具）验证插入的信息确实被模型读到、用到，五个厂商全部正确报出数值。

结论：**用 `user` role**，因为它不会被任何测试过的厂商模板重新收集/挪位置，始终保持在数组里的真实位置。

`ExtraPromptPlugin._assemble()` 仍然用 XML 风格标签包裹每个 section、外层套一层 `<context_state>`（不是必须，但保持内容结构化、不是一坨纯文本，是良好实践）：

```python
@staticmethod
def _assemble(sections: dict[str, str]) -> str:
    if not sections:
        return ""
    body = "\n\n".join(f"<{key}>\n{content}\n</{key}>" for key, content in sections.items())
    return f"<context_state>\n{body}\n</context_state>"
```

实际插入效果（Turn 开始、Step 0，这轮只有一条 steering 消息）：

```
...
assistant: 上一轮的最终答案
user: <context_state>
<state>
Current time: 2026-09-16T06:12:30+00:00
Current step: 0
Current turns: 1
Session total tokens used: 0 tokens
Context window used: 0 tokens
Model context window: 1310720 tokens
</state>
</context_state>
user: 这轮用户真正的新输入        <- 这轮的 steering 消息，排在插入内容之后
[Step 0 在这里请求模型]
```

**`turn.steering_count`**（`ReactLoopPlugin._run_turn()` 写入）是这轮 steering 注入产生的 history 条目数：`_inject()` 现在返回它实际写入了多少条（`for item in items: for entry in item.to_history_entries(): ...; count += 1`），`_run_turn()` 在调用 `self._inject([*high, *low], turn_id)` 时把返回值存进 `variables["turn"]["steering_count"]`。`ExtraPromptPlugin` 用它计算插入点：`ctx.messages` 里从末尾往前数 `steering_count` 条就是"这轮 steering 消息"，插入点定在它们前面（`insert_at = len(ctx.messages) - steering_count`）。Step 0 时本轮还没有任何 assistant/tool 消息（那些要等模型响应后才追加），所以这轮 steering 消息必然正好是 `ctx.messages` 末尾那几条，这个位置计算是准确的；`steering_count` 缺失或为 0 时退化成"插在末尾"（没有 steering 消息可以插在其前面）。

`DynamicStateSectionPlugin`（`plugins/context/sections/dynamic_state.py`）是目前唯一的 `BuildDynamicPromptEvent` 订阅者，贡献一个 `state` section，包含六个值：`{{ turn.now }}`/`{{ turn.step_count }}`/`{{ session.turn_count }}`/`{{ session.tokens_used }}`/`{{ session.context_usage }}`/`{{ global.model_context_length }}`。`session.context_usage`（模型 usage 里报出来的峰值 prompt token 数，见上文 8.2 节）虽然语义上反映的是"已经发生过的调用"而不是"这次即将发送的内容"，但仍然渲染进了这个 section——跟 `{{ global.model_context_length }}`（上限）放在一起，让模型能直接看到"历史峰值 vs 总上限"这组对比，即使峰值本身是慢一拍的历史数据。往这条动态 prompt 里加新内容，只需要写一个新的 section 插件订阅 `BuildDynamicPromptEvent`，塞进 `registry.py` 里 `ExtraPromptPlugin([...])` 的列表，不需要碰 `ExtraPromptPlugin` 或 `DynamicStateSectionPlugin` 本身。（早期版本这里还有一个 `{{ session.provider }}`，随 `_record_provider()` 一起被移除，见 8.2 节。）

`ExtraPromptPlugin` 在 `context_plugins` 元组里排在 **`TruncatorPlugin`/`TokenBudgetPlugin` 之后（最后一个）**，这个顺序依然是必须的——`TruncatorPlugin.apply()` 按 **role 分桶**重组消息列表（所有 `system` 角色的消息一律被搬到最前面，不看原始位置）：

```python
system = [m for m in ctx.messages if m.get("role") == "system"]
kept = non_system[cut_index:]
return BeforeModelCall(messages=[*system, *kept], tools=ctx.tools, variables=ctx.variables)
```

`ExtraPromptPlugin` 插入的是 `user`-role 消息，不受这条 role 分桶规则影响；但如果它排在 `TruncatorPlugin`/`TokenBudgetPlugin` 之前运行，`steering_count` 是按"注入时的原始 history 长度"算的，Truncator/TokenBudget 一旦先把消息列表整体换成裁剪/摘要后的新列表，`ctx.messages` 的长度和 `steering_count` 就对不上了，插入位置会算错。排在它们之后，保证 `ExtraPromptPlugin` 看到的永远是这一次实际发给模型、已经裁剪/摘要完毕的最终 `ctx.messages`，插入位置计算才是准确的。

## 8. 各插件详细设计

### 8.1 LoopPlugin — ReactLoopPlugin
本会话的编排者，持有 `storage_handle`，驱动整个 Turn/Step 流程。不再订阅任何"用户输入"事件——它是全系统唯一的**邮箱消费者**：`register()` 里注册 `steering.high`/`steering.low` 两个邮箱（payload 基类 `SteeringItem`），输入什么时候被消费由它自己的循环结构决定。

`run_loop()`（`PluginManager` 放进 `scope.tasks` 的常驻协程）的骨架：`wait_multiply_mailbox("steering.high", "steering.low")` 挂起等待 → 两个邮箱各 `drain` 一次 → 都为空（比如只是 bus 关闭唤醒）就回去继续等 → high 里有 `is_turn_abort()` 的条目（`SteeringStopCommand`，`/agent_stop` 投递）则 `_finalize_session()`（发 `SessionEnd(reason="agent_stop")`）并退出循环 → 否则 `_run_turn(high, low)`：先把两个邮箱 drain 到的 `SteeringItem` 按 `to_history_entries()` 注入历史，再发 `TurnStartEvent` 进入 Step 循环。`AbortTurn` 冒出 `_run_turn` 时看 `reason.ends_session`：为真（如 `USER_ABORT`）同样 `_finalize_session()` 退出；为假（如 `MODEL_TIMEOUT`，`_run_turn` 内部已发 `ErrorEvent`/`TurnEndEvent`）则回到循环顶部继续等下一条输入。

Turn 进行中新到的输入**不会打断正在跑的模型调用**，而是在两类检查点被 `drain("steering.high")`：

- 最终文本回答已经写入历史**之后**——此时 drain 出 turn-abort 就 `raise AbortTurn(USER_ABORT)`（答案已保住，不会被扔掉）；drain 出普通消息则注入历史、发 `StepEndEvent` 后 `continue`，同一个 Turn 直接接着跑下一个 Step，不用等用户下一次唤醒。
- 一个 Step 的**全部** tool_call 都拿到 tool_result 写回历史之后——绝不在 tool_calls/tool_result 配对中间检查，保证中止也不会留下孤儿 tool 消息。

`steering.low` 只在 Turn 正常收尾（`TurnEndEvent` 之后）才被 drain 注入，留给背景任务结果这类"不值得打断当前工作"的低优先级来源。模型调用本身包在 `asyncio.wait_for`（`model_timeout` 默认 120s）里，超时转成 `AbortTurn(MODEL_TIMEOUT)`。

- 构造时除 `tool_schemas` 外还持有 `tool_payload_map: dict[str, type]`——工具的 `llm_name` → 该工具 `execute()` 参数类型（由 `PluginManager` 用 `infer_payload_type` 从签名自动推断），用于把模型返回的 `ToolCallSpec.args`（dict）转换成对应工具的 dataclass payload，再走 `bus.request(ToolCallRequestEvent, payload)`。
- 系统提示词不落库：每个 Step 都从 `storage.load_history()` 取纯对话历史（不含 system），再交给 `BeforeModelCallEvent` 链（`SystemPromptPlugin` 会重新拼一份 system 消息临时前置），因此 system prompt 内容可以随 workspace/runtime 等运行时信息逐 Step 刷新，但不会污染持久化历史。
- 单次工具调用若在 `ToolCallEvent`/`ToolCallRequestEvent`/`ToolCallResultEvent` 任一环节抛出普通异常，Loop 会捕获并把 `f"Error: {exc}"` 作为该 `tool_call_id` 的回复内容写回历史（保留原始 `call.id`，不中断整个 Turn）；`AbortTurn` 是这个局部 catch 的显式例外——即使在这三个环节里抛出（例如 `PermissionPolicyPlugin` 在 `before_tool_call` 里拒绝一次调用），也会先被 `except AbortTurn: raise` 放行，穿透到外层，和 `StepLimitPlugin` 那种在 `StepStartEvent` 抛出的 `AbortTurn` 一样，终止整个 Turn 并发 `ErrorEvent`。
- 每个 Step 结束时（不论走"最终回答"分支还是"处理完所有工具调用"分支）都会 chain `StepEndEvent`，`step_index` 与该 Step 的 `StepStartEvent` 一致；Step 因 `AbortTurn` 中止时不会补发。
- `ToolExecutionStartEvent`/`ToolExecutionEndEvent` 括住 `bus.request(ToolCallRequestEvent, payload)` 这次实际派发：前者在 `before_tool_call` 策略检查通过、`payload` 构造完成后 chain；后者携带派发拿到的原始 `ToolCallResult`（在 `ToolCallResultEvent` 观察/改写链跑之前），用于区分"策略放行"与"真正开始执行"。两者都在同一个 `try` 块内，不改变 `AbortTurn`/普通异常的传播路径。
- **实时状态展示**：每次 `ToolExecutionStartEvent` 之后紧跟着 chain `MessageUpdateEvent(MessageUpdate(text=self._format_tool_status(call_ctx.call)))`，其中 `_format_tool_status` 是通用格式化（不区分具体工具）：`f"🔧 {call.name}(" + ", ".join(f"{k}={v!r}" for k, v in call.args.items() if v != "" and v != []) + ")"`，例如 `🔧 bash(command='ls -la')`。这个 chain 只负责"当前状态该显示成什么文字"，具体怎么把文字落到 Discord 消息上是 `DiscordThreadPlugin`（8.5）的职责，Loop 本身不知道、也不关心渲染细节。**`StepStartEvent` 本身不会触发"思考中"重置**——早期版本每个 Step 开始时都会把响应式消息打回"思考中"，但这会把上一个 Step 留下的、仍有意义的工具状态行/已流式输出文本无谓抹掉；"思考中"现在只在 `TurnStartEvent` 时由 `DiscordThreadPlugin` 发一次占位消息（见 8.5），之后完全靠 `MessageUpdateEvent`（工具状态）和 `MessageDeltaUpdateEvent`（模型流式文本）自然覆盖，Loop 不再主动"复位"。
- 请求模型时把 `ModelRequest.stream_updates` 显式设为 `True`（`ModelRequest(messages=ctx.messages, tools=ctx.tools, stream_updates=True)`），让 `OpenRouterModelPlugin`（8.2）对本次调用走流式路径、逐 token chain `MessageDeltaUpdateEvent`。`AssistantMessageEvent`/`ErrorEvent`/`TurnEndEvent` 的 chain 时机和内容完全不变——Loop 不需要为"把消息编辑成最终答案"做任何特殊处理，这仍然是 `DiscordThreadPlugin` 订阅这两个既有事件后自己完成的。

### 8.2 BackendPlugin — OpenRouterModelPlugin
唯一应答 `model_request`，用 openai SDK 调用 OpenRouter API。

`register()` 额外保存一份 `self._bus`（流式分支要用它 chain chunk）。`complete()` 按 `msg.stream_updates` 分两条路径：

- **`stream_updates=False`（默认）**：和原来完全一样的一次性阻塞调用，`SummarizerPlugin` 内部摘要用的 `ModelRequest` 从不设这个字段，永远走这条路径——摘要生成不会驱动任何 Discord 更新。
- **`stream_updates=True`（`ReactLoopPlugin` 每次真正驱动 Turn 的模型调用都会用到）**：改为 `stream=True` 调用 OpenAI SDK，边迭代 chunk 边组装：
  - 每个 chunk 的 `delta.content` 只要非空，就追加进 `content_parts` 并 `bus.chain(MessageDeltaUpdateEvent, MessageDeltaUpdate(text_delta=delta.content))`；工具调用参数的 JSON 片段（`delta.tool_calls[i].function.arguments`）**不会**触发这个事件，用户看不到原始参数流。
  - `tool_calls` 的增量按 `index` 累加进 `dict[int, dict]`（`id`/`name` 只在各自 index 的首个 chunk 出现一次，`arguments` 是要靠 index 拼接的 JSON 字符串片段），收尾按 index 排序、`json.loads` 解析出最终 `ToolCallSpec` 列表；某个工具调用全程没有 `arguments` 片段时用 `{}` 兜底，避免 `json.loads("")` 抛异常。
  - 组装出的 `raw_message` dict 只重建了 `role`/`content`/`tool_calls` 这个**兼容子集**，足以支撑会话历史回放，但并不需要（也做不到）和非流式分支的 `message.model_dump()` 逐字节一致——非流式分支的 `model_dump()` 会带上 provider 返回的所有字段（例如 `refusal`、`reasoning` 等 provider 专属字段），流式分支不会重建这些。`tool_calls` 字段是 `[{"id":.., "type": "function", "function": {"name":.., "arguments": json.dumps(tc.args)}}] or None`——注意这里要把 `ToolCallSpec.args`（解析后的 dict，方便 Loop 直接构造工具 payload）重新 `json.dumps` 回字符串，两种表示是反着来的。

**粘性路由（`session_id`，OpenRouter 原生功能，取代早期的手动 pin 方案）**：早期版本靠自己在收到响应后读 `openrouter_metadata` 里 `"selected": True` 的 provider、记进 `self._pinned_provider`、下次请求再用 `provider.order` 显式钉住这个字符串来实现"整个 session 尽量用同一个 provider"。这个手动方案已被移除，改用 OpenRouter 自己的 session-based sticky routing：`OpenRouterModelPlugin` 构造时接收 `session_id: str | None`，`_extra_headers()` 非空时把它塞进 `x-session-id` 请求头（OpenRouter 官方支持这个值作为 body 字段或者这个请求头，二选一即可；文档原文："a top-level request body field or through the `x-session-id` header"）。OpenRouter 收到这个头后自己维持"同一个 session_id 的请求尽量路由到同一个 provider"，不需要客户端自己记状态、自己拼 `provider.order`——**关键约束是绝对不能再传 `provider.order`**：OpenRouter 文档原文"if you set `provider.order` yourself, your order wins over sticky routing"，手动指定顺序会直接盖掉粘性路由，所以 `_extra_body()` 现在只处理黑名单，完全不再碰 `provider.order`。粘性路由本身的行为（10 分钟无活动过期、每次成功请求刷新计时、sticky provider 不可用时自动 fallback 到下一个可用 provider而不是报错）都是 OpenRouter 服务端的责任，客户端这边不用管。

`session_id` 的取值就是 Discord 的 session key（`f"{channel}:{native_id}"`，例如 `discord:123456789012345`），从 `PluginManager.start_session()` 里的 `row.session_key` 传下来——`PluginSet.backend` 的签名从零参 `Callable[[], object]` 改成了 `Callable[[str], object]`，`manager.py` 调用点相应改成 `self._plugin_set.backend(row.session_key).register(bus)`，`registry.py` 里 `backend=lambda session_key: OpenRouterModelPlugin(..., session_id=session_key)`。这个 session key 本来就满足 OpenRouter 对 `session_id` 的要求：稳定（同一个 Discord 帖子/会话全程不变）、代表"一次对话"而不是"一次请求"、远小于 256 字符上限。

**Provider 黑名单（`OPENROUTER_PROVIDER_BLACKLIST`，可选，与粘性路由正交）**：`registry.py` 把这个逗号分隔的环境变量字符串切分、去空白、丢弃空片段后，作为 `provider_blacklist: list[str]` 传给 `OpenRouterModelPlugin`。`_extra_body()` 只看 `self._provider_blacklist` 是否非空，非空才返回 `{"provider": {"ignore": [...]}}`，否则回到 `None`。黑名单不会干扰粘性路由——它不设置 `provider.order`，只是从"OpenRouter 可以选的候选里"提前排除掉几个，OpenRouter 依然会在剩下的候选里做粘性路由。

**不再观测/记录实际 provider**：`session_id` 换成 OpenRouter 服务端维持的粘性路由之后，紧接着又把"每次请求读 `openrouter_metadata`、记进 `session.provider`、打一条 info 日志"这层观测代码（`_record_provider()`、`X-OpenRouter-Metadata` 请求头、流式路径里的 `routing_metadata` 累积变量）整体删掉了——粘性路由是否生效完全由 OpenRouter 服务端负责，客户端不再需要为了"看一眼选中了谁"专门 opt-in 一个响应字段、每次请求解析它。`DynamicStateSectionPlugin` 的 `state` section 相应地去掉了 `Serving provider: {{ session.provider }}` 这一行（`session.provider` 不会再被任何代码写入，留着只会永远渲染成空字符串）——完整字段列表见 7.1 节末尾。

**App 归属（`HTTP-Referer` + `X-OpenRouter-Title`）**：按 OpenRouter 官方 app-attribution 文档（`https://openrouter.ai/docs/app-attribution`），`HTTP-Referer` 才是必需的那个头——它是"这次调用属于哪个 app"的唯一标识，决定要不要在 OpenRouter 后台建一个 app page；`X-OpenRouter-Title` 是可选的，只负责给那个 app page 起个显示名字，**单独发不会建立 app page**（文档原文："Does not create an app page on its own; requires HTTP-Referer pairing"）。据此两个头分工不同、来源也不同：

- `HTTP-Referer` 用一个固定值——`plugins/models/openrouter.py` 顶部的模块级常量 `APP_HTTP_REFERER = "https://github.com/dreamldx/conic"`（项目自己的 GitHub 仓库地址），`_extra_headers()` **无条件**带上它，不依赖任何运行时状态，也不是配置项（没有理由让它可配置，这个项目只有一个仓库地址）。
- `X-OpenRouter-Title` 依然从**运行中的 Discord bot 自己的 Application 名字**动态拿（这个名字本来就在 Discord Developer Portal 里登记过，不需要在 `.env` 里再抄一遍），机制不变：
  - `Config` 有字段 `app_name_holder: dict = Field(default_factory=lambda: {"name": None})`——每个 `Config` 实例自带一个属于自己的可变状态槽位，`default_factory`（而不是可变默认值）保证互不共享——`tests/test_config.py::test_app_name_holder_is_a_fresh_dict_per_config_instance` 测了这一点。
  - `entry.py::build_app()`/`registry.py::build_plugin_set()` 把 `config.app_name_holder` 原样转发到 `DiscordGateway(config, ...)` 和 `OpenRouterModelPlugin(app_name_holder=...)`，两边共享同一个 dict 引用。
  - `DiscordGateway._record_app_name()`：`self._client.application`（discord.py 的 `Client.application` 属性，在 `login()` 阶段就已经从 Discord API 填好，不需要额外发请求，也不需要等网关连上）非空时，把 `.name` 写进 `self._app_name_holder["name"]`。`on_ready` 一开始就调用它，保证在任何会话（新建或恢复）真正开始之前这个值就已经确定。
  - `OpenRouterModelPlugin._extra_headers()` **每次请求时**才读 `(self._app_name_holder or {}).get("name")`，非空才加 `X-OpenRouter-Title`；`HTTP-Referer`/`x-session-id`/`X-OpenRouter-Title` 三者互不冲突，可以同时出现在同一个请求里。

**这条不变式已经变了，记录一下变化**：`build_app()` 曾经是完全不发网络请求的（旧版 `test_build_app_wires_storage_and_gateway_without_connecting` 保护的就是这一点，用假 token 也能无网络地把对象图搭起来）。为了让 `model_context_length`（见上文）从第一次启动就是准确值，`build_app()` 现在会 `await sync_once(storage, config.openrouter_api_key)`（真实调用时是 OpenRouter 的 `/models` HTTP 请求），所以严格说它**不再是零网络调用**了。但 app name 依然不能在 `build_app()` 里同步抓取——原因没变：`build_app()` 从来不做 **Discord 登录**，Application 名字来自 `discord.py` 的 `Client.application`，只有在 `DiscordGateway.start()` 里 `login()` 完成之后才能读到，跟 catalog 同步是两个完全不相关的网络调用，谁先加进 `build_app()` 都不影响这个结论。所以 app name 只能继续靠这个"先占位、后填充、每次请求现读"的可变 dict 方案，不是把 `app_name` 当成构造时就能确定的普通字符串参数传下去。`build_app()` 新增的 `sync_once` 参数（默认 `sync_catalog_once`，见 9.1 节）就是为了让测试能注入一个不发真实请求的假实现，保住"测试套件本身不联网"这个更重要的不变式，同时不再要求 `build_app()` 在生产环境里也零网络——`tests/test_main.py` 现在传一个 `_noop_sync_once` 进去，`build_app()` 本身也从同步函数改成了 `async def`。

**运行时切换模型（`SwitchModelRequestEvent`）**：`OpenRouterModelPlugin` 订阅这个 request 事件，payload 是 `SwitchModelRequest(model_id)`，返回 `SwitchModelResult(model, error)`。查找依赖构造时注入的 `catalog_lookup: Callable[[str], list[ModelCatalogEntry]]`（生产环境是 `StorageService.find_model_catalog_entries`，经 `entry.py → build_plugin_set(catalog_lookup=...) → BuildContext → _build_openrouter_backend` 一路传下来；不传时返回 `model catalog is not available`）。匹配规则在存储层的 SQL 里：不区分大小写、去掉首尾空格，查询词等于完整 slug，**或**等于"去掉 `~` 和厂商前缀之后的模型名"就命中，所以 `deepseek-v4-flash`、`deepseek/deepseek-v4-flash-0731`、`deepseek-v4-flash-0731`、`deepseek-v4-flash-latest`、`~deepseek/deepseek-flash-latest` 都能查到。命中多个时先取和完整 slug 完全相等的那个；仍有多个则返回 `ambiguous model: <id> matches a, b`，不随便挑一个；一个都没有返回 `model not found: <id>`；出错时 `self.model` 不变。找到后**用 `real_model` 而不是被查到的那条 slug** 去请求 OpenRouter（`~deepseek/deepseek-flash-latest` 别名会解析成它指向的 `deepseek/deepseek-v4.1-flash`，不带 `~`）。`:free`/`:batch` 变体不是别名，`real_model` 就是它们自己的 slug，需要显式传 `deepseek-r1:free` 才会命中。**范围与已知限制**：插件是每会话一个实例，所以切换只影响当前会话，且不持久化（进程重启/会话恢复回到配置的默认模型）；目前只有消息本身，没有触发入口（没有斜杠命令或工具会发这个请求）；切换**不会**更新 `global_variables["model_context_length"]`（见 7.1 的待办），也不会让 `SystemPromptPlugin` 缓存的 `global.model` 失效。

**模型目录提示**：插件还订阅 `BuildSystemPromptEvent`，贡献一个 `openrouter_models` 段，告诉模型 OpenRouter 模型信息（slug、vendor、real_model、name、description、上下文长度、价格、模态、支持的参数）以 JSON 存放在 `data/openrouter_models.json`（常量 `MODEL_CATALOG_PATH`，是给模型看的路径提示）。这个文件由每次目录同步生成，见 9.1。

### 8.3 ToolPlugin（6 个）
每个工具构造时绑定本会话 `workspace_dir`，任何解析后越出该目录的路径直接拒绝。路径校验逻辑集中在 `plugins/tools/base.py`：`resolve_within_workspace(workspace_dir, path)` 把相对路径解析到 `workspace_dir` 下并 `.resolve()`，若结果不在 workspace 内则 `raise WorkspaceEscapeError`；四个文件类工具都复用这一个函数，不各自实现越权检查。web_search/web_fetch 不访问本地文件，不依赖路径校验。

六个工具的 `register()` 现在都额外 hook `BuildSystemPromptEvent`，各自贡献一段 prompt section（见第 7 节表格），把代码层已经强制的越权拒绝也讲给模型听——目的是让模型一开始就不去尝试越权路径，而不是等工具报错才知道。三个文件工具（read/write/edit）的措辞可以是陈述句（"paths outside it are rejected"），因为 `resolve_within_workspace` 真的会拒绝；`BashToolPlugin` 的措辞是请求句（"stay inside it, don't cd out"），因为 bash 只是把 `cwd` 设到 `workspace_dir`，并没有在代码层阻止 `cd ..`/绝对路径逃逸——这段 prompt 是目前唯一的"软约束"，不是真正的沙箱，见 `../../Improvement-with-pi.md`"安全与隔离"一节。web_search 和 web_fetch 的 section 不涉及工作区约束，而是给模型提供参数使用指引（topic/country/domains 等）和 fetch 节制警告（"只取一两条最相关的 URL"）。

- `BashToolPlugin`：`asyncio.create_subprocess_shell` 在 `workspace_dir` 下执行，超时（`plugins.yaml` 里 `bash` 条目的 `timeout` 参数，仓库默认配置里是 60s）由 `asyncio.wait_for` 包裹 `proc.communicate()`；超时后 `proc.kill()` + `proc.wait()` 回收进程，返回 `ToolCallResult(error="command timed out after {timeout}s")`。超时值通过 `registry.py::_build_bash_tool()`（一个 builder 函数，读取 `PluginSpec.params["timeout"]`，动态生成一个绑定了该超时值的 `BashToolPlugin` 子类）注入——`tool_classes` 里的类要同时支持"当类用"（`cls.schema`/`cls.llm_name`/`cls.execute` 静态访问）和"当工厂用"（绑定运行时配置），子类化是能同时满足两者的最小改法。stdout/stderr 合并后按字节截断（默认 20000 字节，超出附加 `...[truncated]`），非零退出码作为 `error` 返回。
- `ReadFileToolPlugin`：`offset`/`limit`（默认 0 / 2000 行）按行切片，超出部分返回时附加总行数提示
- `WriteFileToolPlugin`：创建/覆盖文件，返回结果里报告新旧行数和 created/overwritten 状态
- `EditFileToolPlugin`：要求 `old_text` 在文件中**精确出现一次**，否则报错（未找到 / 不唯一），成功后只替换第一处匹配
- `WebSearchToolPlugin`：调用 Tavily Search API（`POST https://api.tavily.com/search`），`search_depth` 固定 `advanced`（2 credits/次），支持 `max_results`（1-10，默认 5）、`time_range`（day/week/month/year）、`topic`（general/news/finance，默认 general）、`country`（仅 topic=general 时生效）、`exact_match`、`include_domains`、`include_answer`（basic/advanced）。搜索结果以边界标记包裹（`<<<EXTERNAL_UNTRUSTED_CONTENT ...>>>`），返回时已剥离 LLM 特殊 token。仅当 `plugins.yaml` 声明了 `web_search` 时才注册；声明了但 `TAVILY_API_KEY` 未配置则启动时报错（`PluginConfigError`），而不是静默跳过。
- `WebFetchToolPlugin`：调用 Firecrawl Scrape API（`POST https://api.firecrawl.dev/v2/scrape`），`onlyMainContent`/`onlyCleanContent`/`skipTlsVerification` 均为 `true`，`proxy: auto`，`maxAge: 48h`，`timeout: 30s`。超过 `max_chars`（`plugins.yaml` 里 `web_fetch` 条目的参数，仓库默认配置里是 15000）时 head+tail 截断（75%/25%），完整 markdown 写入 workspace `web/<sha256>.md`。仅当 `plugins.yaml` 声明了 `web_fetch` 时才注册；声明了但 `FIRECRAWL_API_KEY` 未配置则启动时报错。详细设计见 `docs/superpowers/specs/2026-09-18-web-tools-design.md`。

### 8.4 DiscordGateway（Core Service，实现 `Gateway` Protocol）
进程级 discord.py 连接持有者，维护 `{thread_id: SessionScope}` 路由表。在 `conic/discord/gateway.py`。

- 会话生命周期事件不由 Gateway 发出：Gateway 只在 `handle_start_command`/`resume_active_sessions` 调用 `start_session(..., reason="new"/"resume")` 时把 reason 作为参数透传，`SessionStartEvent` 由 `PluginManager.start_session()` 自己 chain（见第 5 节）；`SessionEndEvent` 由 `ReactLoopPlugin`（`agent_stop`）或 `PluginManager._join_and_cleanup`（`unexpected_exit`）发出（见 8.1）。早期版本是 Gateway 在 `start_session()` 返回后自己 emit——mailbox 重构把 reason 变成参数后，这个"只有调用方知道 reason"的理由消失了，生命周期事件收归 `PluginManager`，渠道 Gateway 不再各自负责。
- `handle_message` 瘦成一行：查路由表拿到 `scope` 后 `await scope.queue.put(text)`，立即返回——不碰 bus、不做拦截。`InputEvent` 拦截链移到了渠道无关的 `SessionGatewayPlugin`（`core/session_gateway.py`，见第 5 节）里，Gateway 不需要知道文本最终会不会进 loop。
- `handle_stop_command`（`/agent_stop`）：`bus.post("steering.high", SteeringStopCommand())` → `scope.closing = True` → 从路由表 pop 掉条目 → `archive()`（归档并锁定 thread）。停止是**协作式**的：不打断正在跑的模型调用/工具调用，`ReactLoopPlugin` 在下一个检查点 drain 到这条 turn-abort 后自己以 `SessionEnd(reason="agent_stop")` 收尾（见 8.1），已完成的回答不会被扔掉。
- **@ 机器人**（`on_message`）：thread 里的消息走 `handle_message`，文本里的 `<@id>`/`<@!id>` 会先被去掉（`strip_bot_mention`），去掉后为空就不入队。普通服务器频道里的消息只有 @ 了机器人才处理（机器人自己的消息、私聊、没 @ 的消息一律忽略）：`handle_mention(create_thread, text, reply)` 用 `message.create_thread` 以输入的第一行（最多 90 字符，空则 `agent-session`）为标题新建公开 thread，经 `_start_thread_session`（和 `/agent_start` 共用）启动会话，再把去掉 @ 的文本放进 `scope.queue`。只 @ 不写内容时不建 thread，用 `reply` 在原频道发 `EMPTY_MENTION_ERROR`。
- **过期会话清理**：`close_stale_sessions(cutoff, fetch_thread)` 处理 `storage.stale_active_sessions("discord", cutoff)` 返回的会话，每个会话单独 try/except：路由表里还在的走 `handle_stop_command`（协作式停止，见上），不在的直接 `fetch_thread` 后归档并锁定 thread；归档失败只记警告；**最后总是把 storage 行置 `ended`**（thread 已删也一样），所以一个会话出问题不影响其他会话。"最后活动时间"取 `COALESCE(MAX(messages.created_at), sessions.created_at)`（用户和机器人的消息都算，从没有消息就看会话创建时间），早于 `cutoff` 即过期，SQL 在 `queries.list_stale_active_sessions_sql`。`sweep_stale_sessions(max_idle=STALE_SESSION_MAX_IDLE)` 先 `await client.wait_until_ready()`，再用 `now(UTC) - max_idle`（30 天）调 `close_stale_sessions`。
- `resume_active_sessions`：`fetch_thread` 失败（thread 已删）→ 直接把 storage 行置 `ended`，session 从未建起；fetch 成功但 `thread.archived` 为真 → 同样置 `ended` 跳过——这是 `/agent_stop` 已经 `archive()` 但进程在后台 cleanup 把状态写成 `ended` 之前就死掉留下的残行，fetch 成功不代表会话还活着。

- **定时清理任务**：`conic/discord/cleanup.py::run_periodic_cleanup(sweep, interval_seconds=3600, sleep=asyncio.sleep)` 是个 `while True` 循环，先立刻执行一次 `sweep()`（`entry.main()` 传的是 `gateway.sweep_stale_sessions`，它自己会等 Discord 连上），成功记一条 `closed N sessions`，任何异常记 `logger.exception` 后继续，然后 `sleep(3600)`；仿照 `run_periodic_sync` 的写法，作为 `main()` 里和目录同步并列的第二个后台 task，退出时一起 cancel。它属于 Discord 运行边界，所以 `pyproject.toml` 给它加了和 `gateway.py` 一样的 `BLE001` 豁免。30 天和 1 小时是代码常量，没做成配置项。

### 8.5 DiscordThreadPlugin（Plugin）
每会话一份，订阅 `AssistantMessageEvent`/`ErrorEvent`/`TurnStartEvent`/`StepStartEvent`/`MessageUpdateEvent`/`MessageDeltaUpdateEvent`/`TurnEndEvent`/`SessionEndEvent`/`BuildSystemPromptEvent`（贡献 `output` prompt section，见第 7 节）。TurnStart/StepStart 时启动或续接 typing indicator（`TYPING_INTERVAL=8s` 刷新一次 `thread.typing()`，`TYPING_TIMEOUT=20s` 兜底超时自动停止单段任务），TurnEnd/Error/SessionEnd 时停止。由于单段任务有 20s 上限，多 Step 的长 Turn 靠每个 `StepStartEvent` 重新拉起一个新任务（若旧任务已超时结束）来续接，避免指示器在 Turn 中途消失。在 `conic/plugins/channels/discord.py`。

- `SessionEndEvent`（不论 reason 是 `agent_stop` 还是 `unexpected_exit`）会把内部 `_stopped` 标记置位；之后任何 `AssistantMessageEvent`/`ErrorEvent`/编辑操作都会被忽略——防止会话已停止（thread 可能已被 archive/lock）后残余事件把消息发进已关闭的线程。
- `_send()` 按 `DISCORD_MESSAGE_LIMIT=2000` 字符切片分段发送，应对 Discord 单条消息长度限制。

**实时状态消息 + token 流式输出**：

内部状态：`_status_message`（占位消息对象，或 `None`）、`_buffer: str`（当前应显示的文字）、`_thinking_text: str`（本 Turn 选中的占位文案）、`_awaiting_first_delta: bool`、`_last_edit_time: float`，以及一个可注入的 `_clock`（默认 `time.monotonic`，测试时替换成假时钟，避免真实 sleep）。`STREAM_EDIT_INTERVAL = 1.0`（秒）。

`THINKING_TEXTS` 是模块级的 40 条占位文案元组——前 20 条风格偏俏皮（`"🤔 脑子在转，请稍等…"`/`"🧠 神经元触突中…"` 等），后 20 条风格偏"腹黑"（`"😏 已经想到答案了，先晾你一会儿…"`/`"😈 邪恶计划酝酿中…"` 等）。每次 `on_turn_start` 用 `random.choice(THINKING_TEXTS)` 从全部 40 条里随机挑一条存进 `self._thinking_text`——同一个 Turn 内如果后面需要"空 buffer 兜底"（见下），用的是这同一条，不会一个 Turn 里换来换去；不同 Turn 之间才会重新随机。

- **`on_turn_start`**：除了原来的 `_ensure_typing()`，还随机选一条占位文案存进 `_thinking_text`、把 `_buffer` 设成它、`_awaiting_first_delta` 置 `True`，并 `await self._thread.send(self._buffer)` 拿到 `_status_message`——这是"Turn 一开始就发一条占位消息"的行为。
- **`_apply_edit` 的空 buffer 兜底**：`content = self._buffer or self._thinking_text`——`on_message_update`/`on_message_delta_update` 传入空字符串（比如模型返回了空的 `MessageUpdate.text`，或第一个 delta 恰好是空串）时，Discord 的消息编辑接口不接受空 content，直接发空字符串会报错；用本 Turn 选中的占位文案兜底，而不是继续显示空白或让编辑静默失败。
- **`on_message_update`**（新增）：`_buffer = msg.text`，`_awaiting_first_delta = True`，然后强制 `_apply_edit(force=True)`（不受节流影响，因为这类"整体替换"频率天然低）。
- **`on_message_delta_update`**（新增）：记下 `force = self._awaiting_first_delta`（复位前的值），如果为真则把 `_buffer` 清空、标记复位，再把 `msg.text_delta` 追加进 `_buffer`；然后 `_apply_edit(force=force)`。**这一条清空规则统一处理了所有"旧状态文字要被新内容取代"的场景**——不管是"思考中"要被工具状态取代，还是工具状态/思考中要被真正开始流式输出的模型文本取代（包括最终答案），都走同一条路径，不需要为"这是最后一步"单独判断。重置后的**第一个** delta 一定立即 edit（`force=True`），不受节流影响——这是从状态行切换到真实内容的关键一帧，值得立刻可见；同一次流式输出里后续的 delta 才会真正受节流约束。`_last_edit_time` 初始化成 `float("-inf")`（而不是 `0.0`）：配合测试用的假时钟从 `0.0` 起算，避免"重置后第一次 edit 恰好发生在 t=0"被误判成"刚 edit 过"而被节流。
- **`_apply_edit(force)`**：`_status_message is None` 或 `_stopped` 时直接跳过；非强制模式下，距上次真正调用 `.edit()` 不足 `STREAM_EDIT_INTERVAL` 秒就只更新内存里的 `_buffer`、不调用 Discord API，留给下一次 delta 或下一次状态切换去补上——因为 `_buffer` 在内存里永远是最新最全的，跳过的只是"现在要不要花一次 Discord API 调用"，不会丢内容。超过 `DISCORD_MESSAGE_LIMIT` 时，流式预览阶段展示末尾 2000 字符并加前缀 `…`（保证看到的是最新内容，而不是卡在开头）。
- **`_finalize(text)`**（`on_assistant_message`/`on_error` 内部复用）：`_status_message` 存在则把它 `.edit()` 成 `text` 的前 2000 字符，剩余部分交给既有的 `_send()` 按原逻辑分段续发；`_status_message` 为 `None`（理论上不会发生，防御性分支）则整段交给 `_send()`。结束后把 `_status_message` 置回 `None`。`on_error` 在调用 `_finalize` 前仍然先 `_stop_typing()`，行为不变。

这样一次 Turn 在 Discord 里呈现为**同一条消息**从头到尾的动态演进：占位 →（可能多轮）思考中/工具状态/流式模型文本 → 编辑成最终答案（或报错），而不是像改动前那样中途只有 typing indicator、结束时才突然冒出一条新消息。

### 8.6 PolicyPlugin
- `PermissionPolicyPlugin`：v1 全部放行
- `StepLimitPlugin`：超过 `plugins.yaml` 里 `step_limit` 条目的 `max_steps` 参数（仓库默认配置里是 25）时 `raise AbortTurn`

### 8.7 Context 插件链
- `SystemPromptPlugin`：chain `BuildSystemPromptEvent` 收集 sections 并组装系统消息；仅当 `ctx.messages[0]` 还不是 `system` 角色时才前置。每 session 第一次收集并渲染后缓存静态 system 文本，后续 Step 复用缓存。
- `TruncatorPlugin`：非 system 消息数超过 `keep_last_n`（默认 40）时，从尾部保留最近 N 条，system 消息始终保留
- `TokenBudgetPlugin`：用 `core/tokencount.estimate_tokens`（`总字符数 // 4`的粗略估算，不依赖 tokenizer）统计 token 数，超预算时先链式 chain `BeforeSummarizeEvent(BeforeSummarize(request, cancelled=False))`——钩子可改写 `request.instructions` 定制摘要提示词，或把 `cancelled` 置 `True` 跳过本次摘要（`apply()` 直接返回 `None`）；未取消则用（可能被改写的）`request` 发起 `SummarizeEvent`，成功后 chain `SummarizeDoneEvent(SummarizeDone(result))`，失败则先 chain `SummarizeFailedEvent(SummarizeFailed(exc))` 再重新抛出（摘要失败仍会中止整个 Turn，这一行为不变，只是失败前多了一次可观测通知）
- `ExtraPromptPlugin`：排在链尾，只在每个 Turn 的 Step 0 chain 一次 `BuildDynamicPromptEvent`，把动态状态作为一条新的 `user`-role 消息插在这轮 steering 消息之前。

Truncator 和 Summarizer 的裁切点都要经过 `core/messagealign.align_cut(messages, cut_index)`：如果提议的裁切点落在一条 `role: "tool"` 消息上（即会把某个 `tool_calls` 消息和它对应的工具回复截断成孤儿），就把裁切点持续前移，直到落在完整的 assistant+tool回复 消息组之前，保证任何被保留的 `tool` 消息都能在保留区间内找到它所回复的 assistant 消息。

### 8.8 SummarizerPlugin
仅在 TokenBudgetPlugin 判断超限时通过 `SummarizeEvent`（request/response）被调用：
1. 从待压缩上下文里分离出 system 消息和其余消息；用 `align_cut` 找到"保留最近 `keep_recent`（默认 5）条、且不孤立 tool 回复"的裁切点
2. 把裁切点之前的历史拼成纯文本 transcript，通过 `bus.request(ModelRequestEvent, ...)`（复用同一个 backend）请求模型生成摘要——即摘要生成本身也是一次模型调用，走的是同一条 `model_request` 总线通道
3. 把摘要包装成新的 `{"role": "system", "content": "[Earlier conversation summary]\n..."}` 消息，与原 system 消息、最近保留的消息一起返回，替换原始的 `ctx.messages`（这次替换只影响本次模型调用的 payload，不写回持久化历史，因此下一 Step 若历史仍超预算会重新触发摘要）

## 9. StorageService（Core Service）

DuckDB schema 保持简单，三张表：

```sql
CREATE TABLE sessions (
    session_key VARCHAR PRIMARY KEY,   -- "{channel}:{native_id}"，如 "discord:123456"
    channel VARCHAR, native_id VARCHAR, workspace_dir VARCHAR,
    model VARCHAR, status VARCHAR,     -- status: "active" | "ended"
    created_at VARCHAR,
    variables VARCHAR DEFAULT '{}'
)
CREATE TABLE messages (
    session_key VARCHAR, seq INTEGER,  -- 每会话独立递增（MAX(seq)+1），不用自增列
    role VARCHAR, content VARCHAR,     -- content 是整条消息序列化后的 JSON（含 tool_calls/tool_call_id 等原始字段，不是纯文本）
    created_at VARCHAR,
    turn_id INTEGER DEFAULT 0,         -- 该消息产生于第几轮 Turn，独立列，不进 content 这份 JSON
    PRIMARY KEY (session_key, seq)
)
CREATE TABLE model_catalog (
    slug VARCHAR PRIMARY KEY,          -- OpenRouter 模型 id（原 id 列），如 "deepseek/deepseek-v4-flash-0731"，别名带 "~" 前缀
    vendor VARCHAR,                    -- slug 里斜杠前的部分，去掉 "~"，如 "deepseek"
    real_model VARCHAR,                -- 别名指向的真实模型 slug（API 的 alias_target.slug）；非别名等于自己的 slug
    name VARCHAR, description VARCHAR,
    context_length INTEGER,
    supports_tools BOOLEAN,
    pricing_prompt DOUBLE, pricing_completion DOUBLE,  -- 每 token 输入/输出单价
    input_modalities VARCHAR DEFAULT '[]',   -- JSON 数组，如 '["text"]'
    output_modalities VARCHAR DEFAULT '[]',  -- JSON 数组
    supported_parameters VARCHAR DEFAULT '[]', -- JSON 数组，完整能力列表（tools/reasoning/structured_outputs 等）
    fetched_at VARCHAR
)
```

SQL 语句集中在 `src/conic/services/queries.py` 中，每句包装为返回 `(sql, params)` 的函数。数据模型使用 pydantic `Session`/`Message`/`ModelCatalogEntry` 类型（`src/conic/services/models.py`）。`startup()` 会执行 `ALTER TABLE sessions ADD COLUMN IF NOT EXISTS variables VARCHAR DEFAULT '{}'`、`ALTER TABLE messages ADD COLUMN IF NOT EXISTS turn_id INTEGER DEFAULT 0`，以及 `model_catalog` 每个非主键列各一条 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`（`queries.add_model_catalog_extra_columns_sql()` 返回一组 `(sql, params)`，`startup()` 里循环执行），兼容旧库。

**`turn_id` 只为将来"按 Turn 分组裁剪/压缩"做准备，当前没有任何压缩逻辑读它**：`SessionHandle.append_message(message, turn_id)` 把 `turn_id` 存进独立的表列，而不是塞进 `content` 这份 JSON——`ReactLoopPlugin._run_turn()` 在方法开头 `turn_count` 自增后立即取值（`turn_id = self._session_variables["turn_count"]`），本 Turn 内所有 `append_message`/`_inject` 调用（steering 注入、assistant 消息、模型 `raw_message`、tool 结果）都带上这同一个 `turn_id`，因此同一轮里 `assistant(tool_calls) → tool(reply)` 这种必须成对出现的消息永远同属一个 turn_id，不会被裁剪逻辑从中间切开。`load_history_sql` 只 `SELECT content`，不选 `turn_id` 这一列，所以发给模型的 `messages` 列表里永远不会出现这个字段——不需要在 `OpenRouterModelPlugin` 或别处额外过滤。**迁移前的历史消息全部回填成 `turn_id = 0`**（DuckDB `ALTER TABLE ... ADD COLUMN ... DEFAULT` 对已有行的行为）：无法从 role 序列可靠反推原始轮次边界，所以不做回填脚本，这批老消息在未来的按 turn 分组逻辑里会被当作同属一组处理，等价于回退到当前的整体粒度。

`StorageService`（进程级单例，持有 DuckDB 连接）与 `SessionHandle`（`storage.handle_for(row)` 返回，仅包装 `session_key` + 连接引用）职责分离：前者管理会话行的创建/恢复/状态流转（`get_or_create`/`active_sessions`/`set_status`），后者是 `ReactLoopPlugin` 直接持有、每次读写历史消息时使用的窄接口（`append_message`/`load_history`/`save_variables`），Loop 不直接接触 `StorageService` 或原始连接。`get_or_create()` 首次创建会话时会按 `{workspace_root}/{channel}/{native_id}` 派生并 `mkdir` 出 workspace 目录，写入 `workspace_dir` 字段供后续该会话所有 ToolPlugin 复用。

### 9.1 Model Catalog 与后台定时同步

`model_catalog` 表存的是 OpenRouter `/api/v1/models` 返回的模型目录快照，消费者有三个：7.1 节提到的 `global.model_context_length`；`SwitchModelRequestEvent` 的模型查找（8.2，`find_model_catalog_entries`）；以及模型通过 `find-model` skill 读取每次同步生成的 `data/openrouter_models.json`（见下）。

- **抓取**：`conic/openrouter/catalog.py::fetch_openrouter_models(api_key, ...)` 直接 `GET https://openrouter.ai/api/v1/models`（带 `Authorization: Bearer <api_key>`），把每个模型的原始 JSON 映射成 `services/models.py::ModelCatalogEntry` 需要的扁平 dict（`_parse_model()`）：`id` 存成 `slug`，并派生 `vendor`（`vendor_of(slug)`：去掉 `~`、取斜杠前的部分）和 `real_model`（别名取 `alias_target.slug`，否则等于自己的 slug）；`context_length`/`supported_parameters` 缺失时安全降级为 `0`/`[]`；`supports_tools` 由 `"tools" in supported_parameters` 派生；`pricing.prompt`/`pricing.completion`（原始是字符串，如 `"0.00000004"`）转成 `float` 存进 `pricing_prompt`/`pricing_completion`；`architecture.input_modalities`/`output_modalities` 原样存成 JSON 数组。`session_factory` 参数（默认 `aiohttp.ClientSession`）是唯一的注入点，测试用假 session 伪造响应，不发真实请求。
- **旧表迁移**：`id` 改名为 `slug` 后，`startup()` 先查 `information_schema.columns`，发现旧表还有 `id` 列就 `DROP TABLE model_catalog` 再重建——这张表每次同步都是整表重写、只是缓存，直接重建比 `RENAME COLUMN`（主键列改名有限制）更稳，而且 `build_app()` 是先同步再读上下文长度，重建后立刻会被填满。
- **JSON 产物**：`sync_catalog_once(..., json_path=None)` 在保存进库之后，如果传了 `json_path`，就把同一批数据按 `slug` 排序写成 `<PROJECT_ROOT>/data/openrouter_models.json`（`write_catalog_json`：先写 `.tmp` 再 `os.replace`，读的人不会读到写一半的文件）。写文件失败（`OSError`）只记警告，同步仍算成功；同步失败则不动旧文件。`entry.py` 用 `config.project_root` 拼路径（不从 `__file__`/cwd 推），启动同步和每小时的后台同步都传它。**注意**：手动入口 `scripts/sync_model_catalog.py` 只写库，**不写这份 JSON**。
- **按名字查找**：`StorageService.find_model_catalog_entries(query)`（SQL 在 `queries.find_model_catalog_entries_sql`）返回所有满足 `lower(slug) = q` 或 `lower(regexp_replace(slug, '^~?[^/]*/', '')) = q` 的条目，按 slug 排序，供 8.2 的模型切换使用。
- **写入**：`StorageService.save_model_catalog(entries)` 每次都**整表替换**（先 `DELETE FROM model_catalog` 再逐条插入），不是按 id upsert——因为目录是从 OpenRouter 权威抓来的快照，模型下架后旧行不该继续留在本地目录里。
- **共享的单次同步原语**：`sync_catalog_once(storage, api_key, session_factory=...)` 把"抓取 + 保存 + 失败兜底"封成一个函数——`fetch_openrouter_models()` 拿到数据、`storage.save_model_catalog()` 存下去，成功 `logger.info` 返回 `True`，任何异常（网络错误、OpenRouter 挂了、解析失败等）都在这里被捕获、`logger.warning` 记日志、返回 `False`，不往外抛。这个文件因此加进了 `pyproject.toml` 的 `[tool.ruff.lint.per-file-ignores]`（`BLE001`，跟 `react_loop.py`/discord 网关同一条理由：这类顶层边界必须兜住任意异常，不能让一次抖动波及调用方）。它有两个调用方，各自决定"失败了怎么办"：
  - **后台循环** `run_periodic_sync(storage, api_key, interval_seconds=DEFAULT_SYNC_INTERVAL_SECONDS=3600, ...)`：一个 `while True` 循环，每次 `await sync_catalog_once(...)`（不管成功失败都继续）再 `await sleep(interval_seconds)`；失败了就等下一个 interval 自然重试，不做任何特殊处理。
  - **启动路径** `entry.py::build_app()`：在 `storage.startup()` 之后、读取任何模型元数据之前，**`await sync_catalog_once(storage, config.openrouter_api_key)` 一次并等它跑完**（`build_app()` 因此从普通函数改成了 `async def`）——目的是让 `storage.get_model_context_length(config.openrouter_model)` 从进程第一次启动、第一个 session 建立之前就能读到准确值，而不是依赖"上次进程运行时后台任务恰好同步过"这种偶然性。同步失败（比如启动时没网）不会阻止 bot 启动，只是这次 `get_model_context_length` 会退回 `DEFAULT_MODEL_CONTEXT_LENGTH = 65535`，等后台 `run_periodic_sync` 之后再补上。
- **`build_app()` 因此不再是零网络调用**：这是个值得强调的架构变化，见 8.2 节 App 归属那段的详细说明——`build_app()` 新增了 `sync_once` 参数（默认 `sync_catalog_once`），测试通过传入一个不发真实请求的假实现（`tests/test_main.py::_noop_sync_once`）继续保住"测试套件不联网"，但生产路径上 `build_app()` 确确实实会先打一次 HTTP 请求。
- **接入进程生命周期**：`entry.py::main()` 里 `storage, gateway = await build_app()`（启动时的那次同步已经在里面发生了），随后 `asyncio.create_task(run_periodic_sync(storage, config.openrouter_api_key))` 把后台循环挂成独立 task，跟 `gateway.start()`（阻塞主循环）并行跑；`finally` 块里 `cancel()` 后 `await`（用 `contextlib.suppress(asyncio.CancelledError)` 包住），保证进程退出时这个任务被干净收尾，不留"Task was destroyed but it is pending"之类的警告。
- **独立脚本**：`scripts/sync_model_catalog.py` 是同一套抓取/写入逻辑的一次性手动入口（`uv run python scripts/sync_model_catalog.py`），读 `.env` 里的配置，跑一次就退出，不进后台循环——用于手动补数据或调试，跟 `run_periodic_sync`/`build_app()` 内部调的是同一套 `fetch_openrouter_models`/`save_model_catalog`（目前直接用 `fetch_openrouter_models` + `ModelCatalogEntry` 手动拼，没有复用 `sync_catalog_once`，属于可以顺手统一但不影响正确性的小重复），没有另外重新实现一遍抓取/解析逻辑。

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
| `PLUGINS_CONFIG_PATH` | `{PROJECT_ROOT}/config/plugins.yaml` | 声明式插件配置文件路径，见第 15 节 |
| `TAVILY_API_KEY` | 无 | Tavily API key。`plugins.yaml` 声明了 `web_search` 但这个 key 未设置时启动报错 |
| `FIRECRAWL_API_KEY` | 无 | Firecrawl API key。`plugins.yaml` 声明了 `web_fetch` 但这个 key 未设置时启动报错 |

`WORKSPACE_ROOT` 和 `DUCKDB_PATH` 默认为 PROJECT_ROOT 下的绝对路径，也可通过环境变量覆盖为自定义路径。

单轮最大 Step 数、摘要压缩的 token 阈值、Truncator 保留消息数、各工具超时/字符预算等——原先的 8 个环境变量（`MAX_STEPS_PER_TURN`/`CONTEXT_TOKEN_BUDGET`/`TRUNCATE_KEEP_LAST_N`/`BASH_TIMEOUT`/`WEB_SEARCH_TIMEOUT`/`WEB_FETCH_TIMEOUT`/`WEB_FETCH_MAX_CHARS`/`WEB_FETCH_SUMMARY_MODEL`）已从 `Config` 中移除，改为 `plugins.yaml` 里对应插件条目的参数，见第 15 节。

## 11. 目录结构

```
conic/                     # 项目根（main.py 与 pyproject.toml 同级，不在 src 下）
  main.py                # 入口：设置 PROJECT_ROOT 环境变量，调用 main()
  pyproject.toml         # console_script: conic = "conic.entry:main"
  scripts/
    sync_model_catalog.py # 手动一次性入口：拉取 + 保存 model_catalog，不进后台循环（见 9.1）
  data/                    # 运行时状态（.gitignore）：conic.duckdb、openrouter_models.json（每次目录同步重新生成）
  skills/                  # 项目级 skill（SKILL.md + scripts/），如 find-model；发现规则见 skill 系统 spec
  prompts/
    identity.md           # 系统提示词 identity section
    execution.md          # 系统提示词 execution section
  src/conic/
    entry.py              # async build_app()（启动时同步一次 model catalog）+ main()，main() 里挂了 run_periodic_sync 后台 task（见 9.1）
    config.py             # pydantic-settings Config, 自动加载 .env
    discord/
      gateway.py          # DiscordGateway (Core Service)：@ 触发建 thread、过期会话清理（8.4）
      cleanup.py          # run_periodic_cleanup：每小时扫一次过期会话（8.4）
    openrouter/
      catalog.py          # fetch_openrouter_models / sync_catalog_once / run_periodic_sync / write_catalog_json（见 9.1）
    core/
      bus.py              # MessageBus（chain/request/mailbox 三套原语）
      manager.py          # PluginManager, PluginSet
      session_gateway.py  # SessionGatewayPlugin：渠道无关的输入拦截 + steering 投递协程
      messagealign.py     # align_cut：截断对齐，防止 orphan tool replies
      tokencount.py       # estimate_tokens：字符数 // 4 的粗略估算
    types/                 # 纯类型定义（dataclass / Protocol / 异常类），无行为逻辑
      gateway.py          # Gateway Protocol
      session.py          # SessionScope（bus/row/closing/queue/tasks）
      messages.py         # 所有消息 dataclass
      steering.py         # SteeringItem 层级（UserMessage/BackgroundResult/StopCommand）
      errors.py           # AbortTurn（含 AbortReason）、总线/邮箱异常类
    plugins/
      meta.py             # 总线 topic 名称常量 (*Event)
      registry.py         # build_plugin_set()：组装 PluginSet，泛化加载 prompts/*.md
      channels/
        discord.py        # DiscordThreadPlugin (Plugin)
      loops/react_loop.py
      models/openrouter.py
      tools/
        base.py           # resolve_within_workspace / strip_special_tokens / wrap_untrusted（通用辅助）
        bash.py / read_file.py / write_file.py / edit_file.py
        web_search.py     # Tavily Search API
        web_fetch.py      # Firecrawl Scrape API
      context/
        system_prompt.py  # 组装系统提示词，缓存渲染结果
        extra_prompt.py   # ExtraPromptPlugin，Turn 的 Step 0 把动态状态插成一条新的 user 消息
        truncator.py
        token_budget.py
        summarizer.py
        sections/         # BuildSystemPromptEvent 贡献插件（均可被 SystemPromptPlugin 缓存）
          identity.py
          tooling.py
          runtime.py
          workspace.py
          execution.py
          dynamic_state.py  # DynamicStateSectionPlugin — 订阅 BuildDynamicPromptEvent，不属于可缓存的静态 section
        variables.py      # TurnVariableUpdaterPlugin (turn 级变量)
      policy/{permission,step_limit}.py
    services/
      storage.py          # StorageService + SessionHandle (DuckDB)
      queries.py          # SQL 语句函数
      models.py           # Session/Message/ModelCatalogEntry pydantic 模型
    utils/
      net.py              # http_post_json / validate_web_url / ProviderResponseError（HTTP 调用辅助）
  tests/                  # 结构与 src/conic 镜像，另含 test_config.py、test_main.py
    core/
    discord/
    openrouter/
    plugins/{models,channels,context,loops,policy,tools}/
    plugins/test_registry.py
    services/
```

## 12. 日志

使用 `loguru`，级别通过 `LOG_LEVEL` 环境变量控制（默认 `INFO`）。`entry.py` 的 `build_app()` 中统一配置 `logger.remove()` + `logger.add(sys.stderr)`。各模块通过 `from loguru import logger` 获取全局 logger。

## 13. 错误处理

- 工具执行失败 → 返回 `ToolCallResult(error=...)`，反馈给模型
- 任意钩子 `raise AbortTurn` → Loop 捕获 → 按 `AbortReason` 分流（`USER_ABORT` 只发 `TurnEndEvent`；`MODEL_TIMEOUT` 发 `ErrorEvent` + `TurnEndEvent` 后回到 idle 继续等下一条输入；`reason.ends_session` 为真的则整个会话以 `SessionEnd(reason="agent_stop")` 收尾）
- OpenRouter API 异常 → 转为 error 事件上报
- Discord 单条消息 2000 字符限制 → `DiscordThreadPlugin` 负责分段发送
- Typing indicator：TurnStart/StepStart 启动或续接，TurnEnd/Error/SessionEnd 停止，20s 单段超时保护

## 14. v1 范围界定

**包含**：4 个本地核心工具（bash/read_file/write_file/edit_file）和按 `plugins.yaml` 声明注册的 web_search/web_fetch（声明了但对应 API key 未配置则启动报错）、OpenRouter 单一 backend、Discord 单一 channel、DuckDB 持久化、完整的 ContextBuilder 链（含 Summarizer/TokenBudget/ExtraPrompt）、composable system prompt、StepLimit 安全阀、typing indicator、loguru 日志、YAML 驱动的插件配置（见第 15 节）。

**不包含（架构已预留空间）**：
- 多 channel 同时运行
- 子代理/Task 工具、Skill 工具
- 动态插件发现
- `model_response`/`tool_result` 的默认审核/脱敏钩子

## 15. YAML 驱动的插件配置（已实现，详见独立 spec）

`registry.py::build_plugin_set()` 不再是纯 Python 硬编码装配，而是从 `plugins.yaml`（路径由 `Config.plugins_config_path` 决定，默认 `{PROJECT_ROOT}/config/plugins.yaml`）声明式地构造 `PluginSet`。完整设计（schema、名字 → 构造逻辑的 builder 映射表机制、错误处理策略、跟 `.env`/`Config` 的分工、`PluginSet.instantiate_session` 闭包顺带完成的 `core/manager.py` 瘦身）和实现细节见：
- `docs/superpowers/specs/2026-09-22-yaml-plugin-config-design.md`（设计）
- `docs/superpowers/plans/2026-09-22-yaml-plugin-config.md`（实现计划，9 个 task）

这里只留一句指路，不重复维护两份内容。

## 附录 A：StorageService 不是 Plugin 的完整论证

见第 3 节引用的三条论据（启动时序矛盾、生命周期矛盾、通信方式不匹配）。结论：Storage 归类为 Core Service 是唯一不产生上述矛盾的分类方式。
