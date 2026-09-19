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
- **工具集**：v1 档位——`bash` / `read_file` / `write_file` / `edit_file` / `web_search`（Tavily）/ `web_fetch`（Firecrawl）。web_search 和 web_fetch 通过 HTTP API 直接调用 provider，API key 各自独立配置，任一 key 未配置时对应工具不注册。详见 `docs/superpowers/specs/2026-09-18-web-tools-design.md`。
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

**消息类型判定 = 字符串 topic + payload 的 Python 类型**。同一个字符串 topic 下可以有多种不同 payload 类型分别对应不同 handler。

`chain()` 对每个已注册 handler 先做 `isinstance(payload, payload_cls)` 判断，只有类型匹配才会真正调用；`payload_cls` 只来自 handler 参数的类型注解，跟返回值类型无关——框架不检查"参数类型和返回值类型是否一致"，这是靠约定维持的：同一条概念上的链（比如所有订阅 `BeforeModelCallEvent` 的 handler 都该收/发 `BeforeModelCall`）如果某个 handler 返回了别的类型，链上后面注册的同类型 handler 会被 `isinstance` 检查悄悄跳过，不报错。`isinstance` 检查为 False 时 `chain()` 会打一条 `logger.info("chain interrupted: topic={} handler={} expected={} got={}", ...)`——这不代表出错（同一 topic 下多种 payload 类型互相跳过是设计内的正常情况），但如果它出现在你以为都是同一类型的一条链里，就是排查"payload 类型被意外换掉"这类隐蔽 bug 的信号。

Mailbox 是总线里的第三种通信形态。`ReactLoopPlugin.register()` 创建 `steering.high` / `steering.low` 两个 mailbox，payload 类型都是 `SteeringItem`。`SessionGatewayPlugin` 把用户输入转成 `SteeringUserMessage` 投递到 `steering.high`；`/agent_stop` 把 `SteeringStopCommand` 投递到 `steering.high`；Loop 协程通过 `wait_multiply_mailbox()` 被任一 mailbox 唤醒，再 `drain()` 批量消费。`MessageBus.close()` 会唤醒等待中的 loop，使会话后台任务能结束。

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
    reason="new",
)
```

`PluginManager.start_session()` 内部按固定顺序把插件挂到新建的 `MessageBus` 上：当前配置启用的 ToolPlugin（各自绑定 `workspace_dir`）→ BackendPlugin → context 插件链（variables → system_prompt → truncator → token_budget → extra_prompt）→ policy 插件（permission → step_limit）→ SummarizerPlugin → ReactLoopPlugin → channel 插件 → `SessionGatewayPlugin`。ToolPlugin 数量不是固定 6 个：`bash/read_file/write_file/edit_file` 总是注册，`web_search` 仅在 `TAVILY_API_KEY` 存在时注册，`web_fetch` 仅在 `FIRECRAWL_API_KEY` 存在时注册。`PluginSet.backend` 和其余插件一样也是**工厂闭包**，确保每会话独立实例——`OpenRouterModelPlugin.register()` 会捕获本会话的 `bus`（流式分支要用它 chain `MessageDeltaUpdateEvent`），如果跨会话共享同一个实例，后一个会话的 `register()` 会覆盖前一个会话捕获的 `bus`，导致流式 token 错发到别的会话/线程（曾经的真实 bug，已修复）。`registry.py` 里这个工厂闭包共享同一个 `AsyncOpenAI` 连接实例，只是插件对象本身（连同它捕获的 `bus`）各会话独立，避免为每个会话重复建立 HTTP 连接。

`PluginManager.start_session()` 在所有插件注册完成后立即 `chain(SessionStartEvent, SessionStart(reason=reason))`，然后启动两个后台任务：`ReactLoopPlugin.run_loop()` 和 `SessionGatewayPlugin.run()`。`SessionScope` 当前持有 `bus`、持久化 session row、`closing` 标记、一个 `asyncio.Queue[str]` 以及后台 task 集合；它不再用 per-session lock 直接包住每条输入。`DiscordGateway.handle_message()` 只负责把 Discord 文本放入 `scope.queue`。`SessionGatewayPlugin` 串行消费 queue，先链式运行 `InputEvent(Input(text=...))` 拦截/改写，再把未处理的文本作为 `SteeringUserMessage` 投递到 `steering.high`。`ReactLoopPlugin.run_loop()` 是唯一消费 `steering.high` / `steering.low` 的协程，因此同一会话的 Turn 自然串行执行。

停止流程同样走 mailbox：`DiscordGateway.handle_stop_command()` 向 `steering.high` 投递 `SteeringStopCommand`，设置 `scope.closing=True`，从路由表移除该 session，并归档/锁定线程。Loop 在下一次安全 checkpoint 消费到 stop 命令后发出 `SessionEndEvent(reason="agent_stop")`；如果后台任务异常退出且此前没有发过 `SessionEndEvent`，`PluginManager._join_and_cleanup()` 会补发 `SessionEndEvent(reason="unexpected_exit")`，随后把 session 状态置为 `ended` 并关闭 bus。

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
| `InputEvent` | chain | `Input` | 无默认订阅者（`SessionGatewayPlugin` 在投递用户输入前触发的拦截点，未来文本命令插件可改写 `text` 或置 `handled=True`） |
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
| `BuildDynamicPromptEvent` | chain | `BuildDynamicPrompt` | `DynamicStateSectionPlugin`（每次 `BeforeModelCallEvent` 都重新 chain，不缓存，见 7.1） |
| `SessionEndEvent` | chain | `SessionEnd` | DiscordThreadPlugin（标记停止并停止 typing）以及 PluginManager 内部的结束标记 handler；reason 可能是 `agent_stop` / `unexpected_exit` 等 |

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
| `global` | 与进程/`PluginSet` 同寿命，所有 session 共享同一份引用 | `build_plugin_set()` 调用时 | `model`（`config.openrouter_model`）、`platform`（`platform.system() + platform.release()`）、`shell`（`_detect_shell()`，见下）、`timezone`（`datetime.now().astimezone().tzinfo`），加上调用方可选传入的 `global_variables: dict`（覆盖同名默认键） | `registry.py::build_plugin_set()`，一次性计算，之后只读 |
| `session` | 与一次 Discord 会话（一个 `ReactLoopPlugin` 实例）同寿命 | `ReactLoopPlugin.__init__()` 构造时 | `workspace_dir`；`tokens_used`（初值 0）；`turn_count`（初值 0） | `workspace_dir` 由 `__init__` 一次性写入；`tokens_used` 由 `OpenRouterModelPlugin` 在每次 `ModelRequestEvent` 完成后累加（阻塞/流式路径都支持，流式通过 `stream_options={"include_usage": True}` 从最后一个 chunk 拿 usage），跨 Turn 累计不清零；`turn_count` 由 `ReactLoopPlugin._run_turn()` 在方法一开始（`variables` 字典构造之前）`self._session_variables["turn_count"] += 1`，因此本 Turn 内读到的值就是"这是第几轮对话"（1-based），累计不清零 |
| `turn` | 一次 Turn（一次 `_run_turn()` 调用） | 每次 `_run_turn()` 开始时新建空 dict | `now`（UTC ISO8601，精确到秒，由 `TurnVariableUpdaterPlugin` 在 `TurnStartEvent` 上写入）；`step_count`（当前 Step 序号，0-based，由 `ReactLoopPlugin` 在每次进入 while 循环体时写入，与该次 `StepStart.step_index` 一致） | `TurnVariableUpdaterPlugin`（`now`）、`ReactLoopPlugin._run_turn()`（`step_count`）；任何 Turn 内事件的订阅者都可以继续往 `turn` 里写 |

`ReactLoopPlugin._run_turn()` 在 Turn 开始时创建：

```python
variables: dict = {
    "global": self._global_variables,   # 引用，跨 Turn/跨 session 不变
    "session": self._session_variables, # 引用，跨 Turn 不变，随 loop 实例存在
    "turn": {},                         # 每个 Turn 新建
}
```

并把同一个 `variables` 引用传给该 Turn 内所有 Turn/Step/工具生命周期事件的 payload（`TurnStart`/`TurnEnd`/`StepStart`/`StepEnd`/`BeforeModelCall`/`ToolCall`/`ToolExecutionStart`/`ToolExecutionEnd`/`AssistantMessage`/`Error`，均带 `variables: dict = field(default_factory=dict)` 字段），以及 `ModelRequest`（同样带 `variables` 字段，专门为了让 `OpenRouterModelPlugin` 能回写 `session.tokens_used`，见下）。因为 `global`/`session`/`turn` 三个子 dict 都是引用（不是逐次拷贝），任何事件订阅者往 `msg.variables["turn"]` 里写入的键，本 Turn 后续事件的订阅者都能读到；`global`/`session` 也会被订阅者原地修改——`OpenRouterModelPlugin` 就是这么更新 `session["tokens_used"]` 的（见下）。除 `ModelRequest` 外，其余请求/响应类 payload（`ModelResponse`/`ToolCallSpec`/`ToolCallResult`/`SummarizeRequest`/`SummarizeResult` 等）以及非 Turn 生命周期事件（`BuildSystemPrompt` 等）仍不携带 `variables`。

`TurnVariableUpdaterPlugin`（`plugins/context/variables.py`，无构造参数）订阅 `TurnStartEvent`，只做一件事：`msg.variables["turn"]["now"] = ...`。它在 `registry.py` 的 `context_plugins` 元组里排第一位，保证同一 Turn 后续任何事件读取 `variables["turn"]["now"]` 时这个键已经存在。`turn.step_count` 不经过插件，直接由 `ReactLoopPlugin._run_turn()` 在 while 循环体顶部（`this_step = step_index` 之后）写入 `variables["turn"]["step_count"] = this_step`，与该次 `StepStartEvent` 的 `step_index` 保持一致，因此 `ExtraPromptPlugin` 每个 Step 渲染动态提示词时总能读到当前 Step 序号。`SystemPromptPlugin` 的静态系统提示词只在本 session 第一次 `BeforeModelCallEvent` 时渲染并缓存，不会随后续 Step 重新渲染 `turn.*`。

`global`/`session` 两层的建立入口不在 `TurnVariableUpdaterPlugin` 里，而是：
- `registry.py::build_plugin_set(config, global_variables=None)` 计算 `resolved_global_variables = {"model": ..., "platform": ..., "shell": ..., "timezone": ..., **(global_variables or {})}`，通过闭包捕获进 `loop_factory`。`shell` 由私有函数 `_detect_shell()` 算出，故意不探测 conic 进程自己跑在哪个交互式 shell 下（那和实际执行工具调用的解释器是两回事），而是直接对齐 `BashToolPlugin.execute()` 底层 `asyncio.create_subprocess_shell`（`shell=True`）真正会 spawn 的程序：Windows 读 `ComSpec` 环境变量取文件名（未设置时回退 `"C:\Windows\System32\cmd.exe"` 的文件名 `"cmd.exe"`——CPython `subprocess.py` 的 Windows `_execute_child` 在 `shell=True` 时就是这么解析 `comspec` 并拼成 `"{comspec} /c \"{args}\""` 的，逐行读源码 + 用 `echo %ComSpec%` 这种只有 cmd.exe 才会展开 `%VAR%` 的探测命令实测验证过），其他平台固定 `"/bin/sh"`（CPython 同一份 `_execute_child` 的 POSIX 分支里 `shell=True` 固定 `args = ["/bin/sh", "-c"] + args`，不读 `$SHELL`）。`RuntimeSectionPlugin` 早期版本硬编码过 "PowerShell 5.1"，这条提示词本来就是错的（Windows 上 `shell=True` 走的是 `ComSpec`／通常是 `cmd.exe`，不是 PowerShell）；中途还试过用第三方库 `shellingham` 探测父进程链识别的交互式 shell，但那反映的是"conic 进程本身在哪个 shell 里启动"，跟"`BashToolPlugin` 的每条命令实际被哪个解释器执行"是两个不同的问题——已改回直接对齐后者；
- `PluginSet.loop_factory` 签名是 `Callable[[handle, tool_schemas, tool_payload_map, workspace_dir, persisted_session_variables], object]`（比原来多了 `workspace_dir` 和 `persisted_session_variables` 两个参数），`core/manager.py::PluginManager.start_session()` 调用时传入 `row.workspace_dir` 和 `row.variables`（后者来自 storage，见下）；
- `ReactLoopPlugin.__init__(..., workspace_dir="", global_variables=None, persisted_session_variables=None)` 建立 `self._session_variables = {"tokens_used": 0, "turn_count": 0, **(persisted_session_variables or {}), "workspace_dir": workspace_dir}`——先给默认值，再用持久化值覆盖（找回上次的 `tokens_used`/`turn_count` 等），最后强制用本次构造传入的 `workspace_dir` 覆盖（不信任持久化里的旧路径，永远以当前会话的实际路径为准）；
- `OpenRouterModelPlugin.complete()`（阻塞与流式两条路径都会调用同一个 `_record_usage(msg, usage)` 辅助方法）在拿到模型响应的 `usage`（阻塞路径读 `response.usage`；流式路径给 `create()` 传 `stream_options={"include_usage": True}`，从不含 `choices` 的最后一个 chunk 读 `chunk.usage`）后，把 `usage.total_tokens` 累加进 `msg.variables["session"]["tokens_used"]`——因为 `session` 子 dict 和 `ReactLoopPlugin` 持有的是同一个引用，这个累加值跨 Turn 持续到会话结束都不会被重置。`usage` 为 `None`（如 fake/未启用用量统计的响应）或 `variables` 里没有 `session` key 时静默跳过，不抛异常。

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

**会变的信息不再混进 system 消息，改成 `ExtraPromptPlugin` 拼进最后一条已有消息的 `content` 里**（`plugins/context/extra_prompt.py`，直接订阅 `BeforeModelCallEvent`，不再是 `BuildSystemPromptEvent` 的 section 贡献者）。它的收集机制和 `SystemPromptPlugin` 是同一套模式的镜像：`SystemPromptPlugin` 构造时接收一份 `section_plugins` 列表，chain `BuildSystemPromptEvent` 收集只读一次的 `global` 级 section；`ExtraPromptPlugin` 同样构造时接收一份 `section_plugins` 列表（目前只有 `DynamicStateSectionPlugin` 一个），但它在**每个 Step** 都重新 chain 新引入的 `BuildDynamicPromptEvent`（`meta.py`）收集 `session`/`turn` 级、逐 Step 会变的 section：

```python
class ExtraPromptPlugin:
    def __init__(self, section_plugins: list):
        self._section_plugins = section_plugins
        self._bus = None

    def register(self, bus) -> None:
        self._bus = bus
        for plugin in self._section_plugins:
            plugin.register(bus)
        bus.on_chain(meta.BeforeModelCallEvent, self.apply)

    async def apply(self, ctx: BeforeModelCall) -> BeforeModelCall | None:
        if not ctx.messages:
            return None
        sections_msg = await self._bus.chain(meta.BuildDynamicPromptEvent, BuildDynamicPrompt(sections={}))
        text = self._assemble(sections_msg.sections)
        text = Template(text).render(**ctx.variables)
        if not text:
            return None
        *rest, last = ctx.messages
        content = last.get("content") or ""
        appended = {**last, "content": f"{content}\n\n{text}"}
        return BeforeModelCall(messages=[*rest, appended], tools=ctx.tools, variables=ctx.variables)
```

**第一版实现曾经把这段动态内容作为一条新的 `{"role": "system", ...}` 消息追加在 `messages` 最末尾，实测会让模型（DeepSeek chat/agent 系）回答错乱。** 原因是 OpenAI 兼容的 chat completions 接口对角色顺序有隐性预期——尤其是"assistant(tool_calls) → tool(reply) → 下一个 assistant" 这种紧邻结构，很多按此结构训练/微调过的模型（包括工具调用场景）没见过在这中间or末尾插进来一条陌生的 `system` 消息，会导致输出不稳定。修复方式是**不新增消息，把渲染好的动态内容直接拼接到 `ctx.messages` 最后一条消息（不论其 role 是 `user` 还是 `tool`）的 `content` 末尾**——消息条数、每条消息的 role 完全不变，只是最后一条消息的文本变长了，这就规避了角色顺序被打断的问题，同时依然保留"离生成最近"这个位置优势。

拼接对象是 `user` 消息时问题不大，但**当最后一条消息是 `tool` 角色的返回值时**，拼进去的动态信息和工具真实输出混在同一个 `content` 字符串里——如果分隔符长得像工具会自然产出的文本（比如 markdown 的 `## key` 标题），模型有极小概率把它误当成工具输出的一部分去解读。因此 `ExtraPromptPlugin._assemble()` 用 XML 风格标签包裹每个 section，外层再套一层 `<context_state>`：

```python
@staticmethod
def _assemble(sections: dict[str, str]) -> str:
    if not sections:
        return ""
    body = "\n\n".join(f"<{key}>\n{content}\n</{key}>" for key, content in sections.items())
    return f"<context_state>\n{body}\n</context_state>"
```

实际拼接效果（一条 `tool` 消息）：

```
ran ls -la

<context_state>
<state>
Current time: 2026-09-16T06:12:30+00:00
Current step: 0
Tokens used: 0
Turns so far this session: 1
</state>
</context_state>
```

这种带尖括号的标签结构不是常见 shell 命令/脚本输出会自然产生的形状，跟工具真实输出的区分度比 markdown 标题更高，也是 Cline/Aider 这类编码 agent 往工具结果里拼环境信息时的常见做法（例如 Cline 的 `<environment_details>`）。`SystemPromptPlugin._assemble()`（静态 system 消息用的那个同名方法，见上文）不受影响，仍然用 `## key` 标题风格——它只出现在一条独立的、明确是 system 角色的消息里，不会跟任何工具输出混在一起，没有这个顾虑。

`DynamicStateSectionPlugin`（`plugins/context/sections/dynamic_state.py`）是目前唯一的 `BuildDynamicPromptEvent` 订阅者，贡献一个 `state` section，包含四个值：`{{ turn.now }}`/`{{ turn.step_count }}`/`{{ session.tokens_used }}`/`{{ session.turn_count }}`。这套"通过事件收集 section、再统一渲染"的机制和静态 system 消息完全对称，唯一的区别是 `BuildSystemPromptEvent` 只在 `SystemPromptPlugin` 第一次 `apply()` 时 chain 一次（结果被缓存），而 `BuildDynamicPromptEvent` 每个 Step 都重新 chain、重新渲染（不缓存）——因为它存在的意义就是承载会变的值。往这条动态 prompt 里加新内容，只需要写一个新的 section 插件订阅 `BuildDynamicPromptEvent`，塞进 `registry.py` 里 `ExtraPromptPlugin([...])` 的列表，不需要碰 `ExtraPromptPlugin` 或 `DynamicStateSectionPlugin` 本身。（早期版本这里还有第五个值 `{{ session.provider }}`，随 `_record_provider()` 一起被移除，见 8.2 节。）

`ExtraPromptPlugin` 在 `context_plugins` 元组里排在 **`TruncatorPlugin`/`TokenBudgetPlugin` 之后（最后一个）**，这个顺序依然是必须的——`TruncatorPlugin.apply()` 按 **role 分桶**重组消息列表（所有 `system` 角色的消息一律被搬到最前面，不看原始位置）：

```python
system = [m for m in ctx.messages if m.get("role") == "system"]
kept = non_system[cut_index:]
return BeforeModelCall(messages=[*system, *kept], tools=ctx.tools, variables=ctx.variables)
```

`ExtraPromptPlugin` 操作的是"当前 `ctx.messages` 的最后一条"，如果它排在 `TruncatorPlugin`/`TokenBudgetPlugin` 之前运行，后两者截断/摘要时完全可能把它刚拼接过的那条消息整个丢弃，或者（对 `TruncatorPlugin` 而言）如果拼接对象恰好是一条 `system` 消息，还会被按 role 分桶搬到最前面。排在它们之后，保证 `ExtraPromptPlugin` 拼接的永远是这一次实际发给模型的 `messages` 列表里真正的最后一条，不会被截断/摘要逻辑抢先处理掉或搬移位置。

## 8. 各插件详细设计

### 8.1 LoopPlugin — ReactLoopPlugin
唯一消费 `steering.high` / `steering.low` mailbox 的编排者，持有本会话的 `storage_handle`，驱动整个 Turn/Step 流程。

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

**不再观测/记录实际 provider**：`session_id` 换成 OpenRouter 服务端维持的粘性路由之后，紧接着又把"每次请求读 `openrouter_metadata`、记进 `session.provider`、打一条 info 日志"这层观测代码（`_record_provider()`、`X-OpenRouter-Metadata` 请求头、流式路径里的 `routing_metadata` 累积变量）整体删掉了——粘性路由是否生效完全由 OpenRouter 服务端负责，客户端不再需要为了"看一眼选中了谁"专门 opt-in 一个响应字段、每次请求解析它。`DynamicStateSectionPlugin` 的 `state` section 相应地去掉了 `Serving provider: {{ session.provider }}` 这一行（`session.provider` 不会再被任何代码写入，留着只会永远渲染成空字符串），现在只保留 `turn.now`/`turn.step_count`/`session.tokens_used`/`session.turn_count` 四个值。

**App 归属（`HTTP-Referer` + `X-OpenRouter-Title`）**：按 OpenRouter 官方 app-attribution 文档（`https://openrouter.ai/docs/app-attribution`），`HTTP-Referer` 才是必需的那个头——它是"这次调用属于哪个 app"的唯一标识，决定要不要在 OpenRouter 后台建一个 app page；`X-OpenRouter-Title` 是可选的，只负责给那个 app page 起个显示名字，**单独发不会建立 app page**（文档原文："Does not create an app page on its own; requires HTTP-Referer pairing"）。据此两个头分工不同、来源也不同：

- `HTTP-Referer` 用一个固定值——`plugins/models/openrouter.py` 顶部的模块级常量 `APP_HTTP_REFERER = "https://github.com/dreamldx/conic"`（项目自己的 GitHub 仓库地址），`_extra_headers()` **无条件**带上它，不依赖任何运行时状态，也不是配置项（没有理由让它可配置，这个项目只有一个仓库地址）。
- `X-OpenRouter-Title` 依然从**运行中的 Discord bot 自己的 Application 名字**动态拿（这个名字本来就在 Discord Developer Portal 里登记过，不需要在 `.env` 里再抄一遍），机制不变：
  - `Config` 有字段 `app_name_holder: dict = Field(default_factory=lambda: {"name": None})`——每个 `Config` 实例自带一个属于自己的可变状态槽位，`default_factory`（而不是可变默认值）保证互不共享——`tests/test_config.py::test_app_name_holder_is_a_fresh_dict_per_config_instance` 测了这一点。
  - `entry.py::build_app()`/`registry.py::build_plugin_set()` 把 `config.app_name_holder` 原样转发到 `DiscordGateway(config, ...)` 和 `OpenRouterModelPlugin(app_name_holder=...)`，两边共享同一个 dict 引用。
  - `DiscordGateway._record_app_name()`：`self._client.application`（discord.py 的 `Client.application` 属性，在 `login()` 阶段就已经从 Discord API 填好，不需要额外发请求，也不需要等网关连上）非空时，把 `.name` 写进 `self._app_name_holder["name"]`。`on_ready` 一开始就调用它，保证在任何会话（新建或恢复）真正开始之前这个值就已经确定。
  - `OpenRouterModelPlugin._extra_headers()` **每次请求时**才读 `(self._app_name_holder or {}).get("name")`，非空才加 `X-OpenRouter-Title`；`HTTP-Referer`/`x-session-id`/`X-OpenRouter-Title` 三者互不冲突，可以同时出现在同一个请求里。

因为 `build_app()` 本身不做任何网络调用（这是一个既有的、被 `test_build_app_wires_storage_and_gateway_without_connecting` 显式测试保护的不变式——用假 token 也能无网络地把对象图搭起来），app name 不能在 `build_app()` 里同步抓取，只能靠这个"先占位、后填充、每次请求现读"的可变 dict 方案，而不是把 `app_name` 当成构造时就能确定的普通字符串参数传下去。

### 8.3 ToolPlugin（6 个）
每个工具构造时绑定本会话 `workspace_dir`，任何解析后越出该目录的路径直接拒绝。路径校验逻辑集中在 `plugins/tools/base.py`：`resolve_within_workspace(workspace_dir, path)` 把相对路径解析到 `workspace_dir` 下并 `.resolve()`，若结果不在 workspace 内则 `raise WorkspaceEscapeError`；四个文件类工具都复用这一个函数，不各自实现越权检查。web_search/web_fetch 不访问本地文件，不依赖路径校验。

六个工具的 `register()` 现在都额外 hook `BuildSystemPromptEvent`，各自贡献一段 prompt section（见第 7 节表格），把代码层已经强制的越权拒绝也讲给模型听——目的是让模型一开始就不去尝试越权路径，而不是等工具报错才知道。三个文件工具（read/write/edit）的措辞可以是陈述句（"paths outside it are rejected"），因为 `resolve_within_workspace` 真的会拒绝；`BashToolPlugin` 的措辞是请求句（"stay inside it, don't cd out"），因为 bash 只是把 `cwd` 设到 `workspace_dir`，并没有在代码层阻止 `cd ..`/绝对路径逃逸——这段 prompt 是目前唯一的"软约束"，不是真正的沙箱，见 `docs/Improvement.md`"安全与隔离"一节。web_search 和 web_fetch 的 section 不涉及工作区约束，而是给模型提供参数使用指引（topic/country/domains 等）和 fetch 节制警告（"只取一两条最相关的 URL"）。

- `BashToolPlugin`：`asyncio.create_subprocess_shell` 在 `workspace_dir` 下执行，超时（`BASH_TIMEOUT`，默认 60s）由 `asyncio.wait_for` 包裹 `proc.communicate()`；超时后 `proc.kill()` + `proc.wait()` 回收进程，返回 `ToolCallResult(error="command timed out after {timeout}s")`。超时值通过 `registry.py` 里定义的 `ConfiguredBashToolPlugin`（`BashToolPlugin` 的一个薄子类，`__init__` 只接 `workspace_dir` 以匹配 `PluginManager` 对所有 `tool_classes` 统一的 `tool_cls(workspace_dir=...)` 实例化方式，内部把 `config.bash_timeout` 转发给父类）从 `Config` 注入——`tool_classes` 里的类要同时支持"当类用"（`cls.schema`/`cls.llm_name`/`cls.execute` 静态访问）和"当工厂用"（绑定运行时配置），子类化是能同时满足两者的最小改法。stdout/stderr 合并后按字节截断（默认 20000 字节，超出附加 `...[truncated]`），非零退出码作为 `error` 返回。
- `ReadFileToolPlugin`：`offset`/`limit`（默认 0 / 2000 行）按行切片，超出部分返回时附加总行数提示
- `WriteFileToolPlugin`：创建/覆盖文件，返回结果里报告新旧行数和 created/overwritten 状态
- `EditFileToolPlugin`：要求 `old_text` 在文件中**精确出现一次**，否则报错（未找到 / 不唯一），成功后只替换第一处匹配
- `WebSearchToolPlugin`：调用 Tavily Search API（`POST https://api.tavily.com/search`），`search_depth` 固定 `advanced`（2 credits/次），支持 `max_results`（1-10，默认 5）、`time_range`（day/week/month/year）、`topic`（general/news/finance，默认 general）、`country`（仅 topic=general 时生效）、`exact_match`、`include_domains`、`include_answer`（basic/advanced）。搜索结果以边界标记包裹（`<<<EXTERNAL_UNTRUSTED_CONTENT ...>>>`），返回时已剥离 LLM 特殊 token。仅在 `TAVILY_API_KEY` 配置后注册。
- `WebFetchToolPlugin`：调用 Firecrawl Scrape API（`POST https://api.firecrawl.dev/v2/scrape`），`onlyMainContent`/`onlyCleanContent`/`skipTlsVerification` 均为 `true`，`proxy: auto`，`maxAge: 48h`，`timeout: 30s`。超过 `WEB_FETCH_MAX_CHARS`（默认 15000）时 head+tail 截断（75%/25%），完整 markdown 写入 workspace `web/<sha256>.md`。仅在 `FIRECRAWL_API_KEY` 配置后注册。详细设计见 `docs/superpowers/specs/2026-09-18-web-tools-design.md`。

### 8.4 DiscordGateway（Core Service，实现 `Gateway` Protocol）
进程级 discord.py 连接持有者，维护 `{thread_id: SessionScope}` 路由表。在 `conic/discord/gateway.py`。

- 会话生命周期的 `SessionStartEvent` 由 `PluginManager.start_session()` 发出，`reason` 由调用方传入：`handle_start_command(..., reason="new")`，`resume_active_sessions(..., reason="resume")`。`DiscordGateway` 不再自己 chain `SessionStartEvent`。
- `handle_message` 只做路由：找到 `SessionScope` 后 `await scope.queue.put(text)`。后续由每会话的 `SessionGatewayPlugin.run()` 串行消费 queue、chain `InputEvent(Input(text=...))`，再把未处理输入转为 `SteeringUserMessage` 投递到 `steering.high`。
- `handle_stop_command` 向 `steering.high` 投递 `SteeringStopCommand()`，把 `scope.closing` 置为 `True`，从路由表移除 session，并归档/锁定 Discord thread。Loop 消费 stop 命令后通过 `SessionEndEvent(reason="agent_stop")` 结束 session；如果进程在归档后、状态落库前崩溃，下一次 `resume_active_sessions()` 看到远端 thread 已归档，会把该 session 标为 `ended`。

### 8.5 DiscordThreadPlugin（Plugin）
每会话一份，订阅 `SessionEndEvent`/`AssistantMessageEvent`/`ErrorEvent`/`TurnStartEvent`/`StepStartEvent`/`MessageUpdateEvent`/`MessageDeltaUpdateEvent`/`TurnEndEvent`/`BuildSystemPromptEvent`（贡献 `output` prompt section，见第 7 节）。TurnStart/StepStart 时启动或续接 typing indicator（`TYPING_INTERVAL=8s` 刷新一次 `thread.typing()`，`TYPING_TIMEOUT=20s` 兜底超时自动停止单段任务），TurnEnd/Error/SessionEnd 时停止。由于单段任务有 20s 上限，多 Step 的长 Turn 靠每个 `StepStartEvent` 重新拉起一个新任务（若旧任务已超时结束）来续接，避免指示器在 Turn 中途消失。在 `conic/plugins/channels/discord.py`。

- `SessionEndEvent` 会把内部 `_stopped` 标记置位；之后任何 `AssistantMessageEvent`/`ErrorEvent` 都会被忽略——防止会话已停止（thread 已被 archive/lock 或即将关闭）后模型仍在跑最后一个 Step 时把消息发进已关闭的线程。
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
- `StepLimitPlugin`：超过 `MAX_STEPS_PER_TURN` 时 `raise AbortTurn`

### 8.7 Context 插件链
- `SystemPromptPlugin`：chain `BuildSystemPromptEvent` 收集 sections 并组装系统消息；仅当 `ctx.messages[0]` 还不是 `system` 角色时才前置。每 session 第一次收集并渲染后缓存静态 system 文本，后续 Step 复用缓存。
- `TruncatorPlugin`：非 system 消息数超过 `keep_last_n`（默认 40）时，从尾部保留最近 N 条，system 消息始终保留
- `TokenBudgetPlugin`：用 `core/tokencount.estimate_tokens`（`总字符数 // 4`的粗略估算，不依赖 tokenizer）统计 token 数，超预算时先链式 chain `BeforeSummarizeEvent(BeforeSummarize(request, cancelled=False))`——钩子可改写 `request.instructions` 定制摘要提示词，或把 `cancelled` 置 `True` 跳过本次摘要（`apply()` 直接返回 `None`）；未取消则用（可能被改写的）`request` 发起 `SummarizeEvent`，成功后 chain `SummarizeDoneEvent(SummarizeDone(result))`，失败则先 chain `SummarizeFailedEvent(SummarizeFailed(exc))` 再重新抛出（摘要失败仍会中止整个 Turn，这一行为不变，只是失败前多了一次可观测通知）
- `ExtraPromptPlugin`：排在链尾，每个 Step chain `BuildDynamicPromptEvent`，把动态状态拼进最终发给模型的最后一条消息 content，不新增 message role。

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
    created_at VARCHAR,
    variables VARCHAR DEFAULT '{}'
)
CREATE TABLE messages (
    session_key VARCHAR, seq INTEGER,  -- 每会话独立递增（MAX(seq)+1），不用自增列
    role VARCHAR, content VARCHAR,     -- content 是整条消息序列化后的 JSON（含 tool_calls/tool_call_id 等原始字段，不是纯文本）
    created_at VARCHAR,
    PRIMARY KEY (session_key, seq)
)
```

SQL 语句集中在 `src/conic/services/queries.py` 中，每句包装为返回 `(sql, params)` 的函数。数据模型使用 pydantic `Session`/`Message` 类型（`src/conic/services/models.py`）。`startup()` 会执行 `ALTER TABLE sessions ADD COLUMN IF NOT EXISTS variables VARCHAR DEFAULT '{}'`，兼容旧库。

`StorageService`（进程级单例，持有 DuckDB 连接）与 `SessionHandle`（`storage.handle_for(row)` 返回，仅包装 `session_key` + 连接引用）职责分离：前者管理会话行的创建/恢复/状态流转（`get_or_create`/`active_sessions`/`set_status`），后者是 `ReactLoopPlugin` 直接持有、每次读写历史消息时使用的窄接口（`append_message`/`load_history`/`save_variables`），Loop 不直接接触 `StorageService` 或原始连接。`get_or_create()` 首次创建会话时会按 `{workspace_root}/{channel}/{native_id}` 派生并 `mkdir` 出 workspace 目录，写入 `workspace_dir` 字段供后续该会话所有 ToolPlugin 复用。

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
| `TAVILY_API_KEY` | 无 | Tavily API key。未设置时 `web_search` 不注册 |
| `FIRECRAWL_API_KEY` | 无 | Firecrawl API key。未设置时 `web_fetch` 不注册 |
| `WEB_SEARCH_TIMEOUT` | `30` | `web_search` 请求超时（秒） |
| `WEB_FETCH_TIMEOUT` | `60` | `web_fetch` 请求超时（秒） |
| `WEB_FETCH_MAX_CHARS` | `15000` | fetch 内联返回的字符预算，超长时 head+tail 截断落盘 |
| `WEB_FETCH_SUMMARY_MODEL` | 无 | 两段式 fetch 的小模型 id（OpenRouter），设置后 `web_fetch` 暴露 `prompt` 参数 |

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
      session_gateway.py  # SessionGatewayPlugin：queue → InputEvent → steering.high
      messagealign.py     # align_cut：截断对齐，防止 orphan tool replies
      tokencount.py       # estimate_tokens：字符数 // 4 的粗略估算
    types/                 # 纯类型定义（dataclass / Protocol / 异常类），无行为逻辑
      gateway.py          # Gateway Protocol
      session.py          # SessionScope（bus / row / closing / queue / tasks）
      messages.py         # 所有消息 dataclass
      errors.py           # AbortTurn、NoResponderError、DuplicateResponderError、mailbox 相关错误
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
        extra_prompt.py   # ExtraPromptPlugin，把逐 Step 变化的信息拼进最后一条消息的 content
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
      models.py           # Session/Message pydantic 模型
    utils/
      net.py              # http_post_json / validate_web_url / ProviderResponseError（HTTP 调用辅助）
  tests/                  # 结构与 src/conic 镜像，另含 test_config.py、test_main.py
    core/
    discord/
    plugins/{models,channels,context,loops,policy,tools}/
    plugins/test_registry.py
    services/
```

## 12. 日志

使用 `loguru`，级别通过 `LOG_LEVEL` 环境变量控制（默认 `INFO`）。`entry.py` 的 `build_app()` 中统一配置 `logger.remove()` + `logger.add(sys.stderr)`。各模块通过 `from loguru import logger` 获取全局 logger。

## 13. 错误处理

- 工具执行失败 → 返回 `ToolCallResult(error=...)`，反馈给模型
- 任意钩子 `raise AbortTurn` → Loop 捕获 → `bus.chain(ErrorEvent, ...)` → Turn 干净结束；`USER_ABORT` 会结束整个 session
- OpenRouter API 异常 → 转为 error 事件上报
- Discord 单条消息 2000 字符限制 → `DiscordThreadPlugin` 负责分段发送
- Typing indicator：TurnStart/StepStart 启动或续接，TurnEnd/Error/SessionEnd 停止，20s 单段超时保护

## 14. v1 范围界定

**包含**：4 个本地核心工具（bash/read_file/write_file/edit_file）和按 API key 条件注册的 web_search/web_fetch、OpenRouter 单一 backend、Discord 单一 channel、DuckDB 持久化、完整的 ContextBuilder 链（含 Summarizer/TokenBudget/ExtraPrompt）、composable system prompt、StepLimit 安全阀、typing indicator、loguru 日志。

**不包含（架构已预留空间）**：
- 多 channel 同时运行
- 子代理/Task 工具、Skill 工具
- 动态插件发现
- `model_response`/`tool_result` 的默认审核/脱敏钩子

## 附录 A：StorageService 不是 Plugin 的完整论证

见第 3 节引用的三条论据（启动时序矛盾、生命周期矛盾、通信方式不匹配）。结论：Storage 归类为 Core Service 是唯一不产生上述矛盾的分类方式。
