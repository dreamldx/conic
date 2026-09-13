# Conic：插件化 Agentic Engine 设计

日期：2026-09-13
状态：待评审

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
| 成员 | LoopPlugin、BackendPlugin、ToolPlugin、`DiscordThreadPlugin`（每会话渠道适配器）、ContextBuilderPlugin、PolicyPlugin | `MessageBus` 本身、`PluginManager`、`StorageService`、`DiscordGateway` |

`StorageService`（持久化历史存储）**不是 Plugin**，原因（详见附录 A）：

1. **启动时序矛盾**：判断一个会话是新建还是恢复，必须先查存储；而这一步发生在该会话的 `MessageBus` 被创建**之前**——不可能"只通过还不存在的总线"去查它。
2. **生命周期矛盾**：Plugin 随会话销毁而销毁，但持久化的意义就是数据要在会话销毁后依然存在（甚至跨 bot 重启）。
3. **通信方式不匹配**：存储读写是 Loop 自己顺序控制流里的命令式操作（"给我历史""帮我写入"），不是"发生了某事，0个或多个订阅者反应"的广播场景，套用总线只是给普通函数调用披皮，不会带来真实的多实现/多响应者收益。

**`DiscordGateway`（Discord 网关连接）同理也不是 Plugin，是 Core Service**：它拥有进程里唯一的 discord.py 连接，必须在任何会话存在之前就启动（登录、注册斜杠命令、扫描存储恢复历史活跃会话），并且要在收到外部 Discord 事件时主动决定"这条消息该桥接到哪个会话的总线"——这和 Storage 面临的是同一类"先于会话存在、做启动期决策"的结构性问题（详见 5.1 节和附录 A）。真正按会话生命周期创建/销毁、只通过总线通信的，是 `DiscordThreadPlugin`（每个会话一份实例）。

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

**消息类型判定 = 字符串 topic + payload 的 Python 类型**（不是纯字符串，也不是纯类型）。同一个字符串 topic 下可以有多种不同 payload 类型分别对应不同 handler（例如 `"tool_call"` 这个 request topic 下，`BashCall`/`ReadFileCall`/`WriteFileCall`/`EditFileCall` 各自对应各自的工具）。

Payload 类型在注册时**自动从 handler 的类型注解推断**（用 `inspect.signature` + `typing.get_type_hints`），插件作者不需要重复声明：

```python
def _infer_payload_type(handler) -> type:
    hints = get_type_hints(handler)
    params = [p for p in inspect.signature(handler).parameters if p != "self"]
    return hints[params[0]]

class BashToolPlugin:
    def register(self, bus: MessageBus):
        bus.on_request("tool_call", self.execute)   # 自动推断出 BashCall

    async def execute(self, call: BashCall) -> ToolCallResult: ...
```

`emit`/`request` 内部各自是独立的 dict（按 `type_name` 索引，值是 `(payload_cls, handler)` 列表），不会互相冲突，但同一字符串在两套注册表里代表不同语义时容易让人误解，因此**同一字符串只用于一种 verb**（见第 6 节消息总表，`before_tool_call` 与 `tool_call` 特意分开命名）。

## 5. 会话作用域：不用 session_id，用总线实例隔离

消息不携带 `session_id` 或整个 `Session` 对象；会话身份靠**结构**体现：**每个会话拥有自己独立的 `MessageBus` 实例和一整套专属插件实例**。

`/agent start`（Discord slash command）触发时，`PluginManager` 现场组装。**`PluginManager` 本身完全渠道无关**——不 import 任何 Discord 相关类型，具体渠道插件由调用方以工厂闭包的形式传入：

```python
def start_session(channel: str, native_id: str,
                   channel_plugin_factory: Callable[[], ChannelPlugin]) -> SessionScope:
    row = storage.get_or_create(channel=channel, native_id=native_id)  # 直接函数调用
    bus = MessageBus()

    for tool_cls in (BashToolPlugin, ReadFileToolPlugin, WriteFileToolPlugin, EditFileToolPlugin):
        tool_cls(workspace_dir=row.workspace_dir).register(bus)   # 每会话一份实例，绑定 workspace_dir

    backend.register(bus)                        # 进程级单例，注册到本会话总线
    for ctx_plugin in (system_prompt, truncator, token_budget):
        ctx_plugin.register(bus)                  # 进程级单例（无状态）
    for policy in (permission_policy, step_limit):
        policy.register(bus)
    summarizer.register(bus)

    ReactLoopPlugin(storage_handle=storage.handle_for(row)).register(bus)  # 每会话一份，持有本会话存储句柄
    channel_plugin_factory().register(bus)        # 渠道细节完全由调用方（某个 Gateway）通过闭包提供

    return SessionScope(bus=bus, row=row)

def stop_session(scope: SessionScope) -> None:
    storage.handle_for(scope.row).set_status("ended")
```

`DiscordGateway` 这样调用（`channel_plugin_factory` 闭包里才第一次出现 `DiscordThreadPlugin`/`discord_thread` 这些 Discord 专属类型）：
```python
plugin_manager.start_session(
    channel="discord", native_id=str(discord_thread.id),
    channel_plugin_factory=lambda: DiscordThreadPlugin(discord_thread),
)
```

消息类型本身只携带"纯语义内容"，不含任何路由/归属字段，例如：

```python
@dataclass
class BashCall:
    command: str

@dataclass
class AssistantMessage:
    text: str
```

`Loop` emit 一个 `AssistantMessage(text=...)`，本会话总线上**只有一个** `DiscordThreadPlugin` 实例订阅了它，天然知道发到哪个 thread，不需要判断"是不是我的会话"。

**例外说明**：Discord 的 `thread.id` 仍然是必要的外部标识，用来在 bot 重启后"找回"会话（`storage.get_or_create` 那一次查找）。这发生在会话总线创建**之前**的普通函数调用里，不是总线消息字段——拒绝的是"内部 session_id 混进总线消息"，不是拒绝外部世界本来就有的线程 ID。

### 5.1 进程启动与会话恢复流程

`start_session(...)` 只回答"给定一个会话该怎么组装"，但**在任何会话存在之前**，进程本身要先跑起来、连上 Discord、并且知道"重启前有哪些会话是活跃的"——这一层是 `DiscordGateway`（Core Service）的职责。

**`Gateway` 通用接口**（放在 `core/gateway.py`，渠道无关，未来新增渠道只需实现它）：
```python
class Gateway(Protocol):
    name: str                           # 必须等于 SessionRow.channel 的取值，如 "discord"
    async def start(self) -> None: ...   # 连接平台、注册命令/监听、恢复历史活跃会话、开始路由；阻塞直到断开
    async def stop(self) -> None: ...    # 优雅断开
```

`DiscordGateway(Gateway)` 实现：
```python
# main.py
storage = StorageService(config.DUCKDB_PATH)
storage.startup()                                    # 打开 DuckDB 连接

plugin_manager = PluginManager(storage, config)       # 此时还没有任何会话，且完全不知道 Discord 的存在

gateways: list[Gateway] = [DiscordGateway(config.DISCORD_BOT_TOKEN, plugin_manager)]
# 未来加 Slack：gateways.append(SlackGateway(config.SLACK_BOT_TOKEN, plugin_manager))
await asyncio.gather(*(gw.start() for gw in gateways))
```
`DiscordGateway.start()` 内部依次做：
1. 登录 discord.py 网关连接（进程唯一一份）
2. 注册 `/agent start`、`/agent stop` 斜杠命令
3. 查 storage 里 `channel="discord"` 且 `status="active"` 的会话，逐个用 discord.py API 找回对应 thread 对象，调用 `plugin_manager.start_session(...)` 重建 `SessionScope`，填进内存路由表 `{thread_id: SessionScope}`——**必须在开始监听消息事件之前完成**，否则重启后旧 thread 会找不到会话
4. 阻塞进入 discord.py 事件循环

之后：
- **收到 `/agent start`**：创建新 thread → `plugin_manager.start_session(channel="discord", native_id=str(thread.id), channel_plugin_factory=lambda: DiscordThreadPlugin(thread))` → 把返回的 `SessionScope` 存入路由表
- **收到普通消息事件**：查路由表，命中则 `scope.bus.emit("user_input", UserInput(text))`（Gateway 对某个会话总线的直接调用——Gateway 不是 Plugin，不需要总线隔离那层保证）；未命中（不是任何已知会话的 thread）则忽略
- **收到 `/agent stop`**：`plugin_manager.stop_session(scope)`、归档 thread、从路由表移除对应 `SessionScope`

这一层设计对未来接入更多渠道天然通用：新增 Slack 只需要一个新的 `SlackGateway`（同样实现 `Gateway` Protocol、同样是 Core Service），调用同一个渠道无关的 `plugin_manager.start_session(...)`/`stop_session(...)`，不需要改动 `PluginManager`、`MessageBus` 或任何现有插件。

## 6. 完整消息生命周期与消息类型总表

```
① 接入
Discord 消息到达 → DiscordGateway 查路由表 {thread_id: SessionScope}，命中对应会话
  → scope.bus.emit("user_input", UserInput(text))

② Loop 被唤醒（本会话总线上唯一订阅者）
ReactLoopPlugin.handle_user_input
  → storage_handle.append_message(user_msg)   # 直接调用，不经总线
  → bus.emit("turn_start", TurnStart())
  → try:
      step_index = 0
      while True:
        ③ bus.emit("step_start", StepStart(step_index))   # StepLimitPlugin 校验，超限 raise AbortTurn
        step_index += 1

        ④ ctx = bus.emit("before_model_call", BeforeModelCall(messages, tools))
           SystemPromptPlugin → TruncatorPlugin → TokenBudgetPlugin（链式，顺序有意义）
           TokenBudgetPlugin 若超预算 → bus.request("summarize", SummarizeRequest(...)) → SummarizerPlugin 应答

        ⑤ response = bus.request("model_request", ModelRequest(ctx.messages, ctx.tools))
           OpenRouterBackendPlugin 唯一应答；异常向上抛出

        ⑥ response = bus.emit("model_response", response)   # 审核/改写钩子（v1 默认无）

        ⑦ 若无 tool_calls：
             out = bus.emit("assistant_message", AssistantMessage(response.text))
             storage_handle.append_message(assistant_msg(out.text))
             break

           若有 tool_calls，逐个：
             call_ctx = bus.emit("before_tool_call", ToolCall(raw_call))   # PermissionPolicyPlugin 校验/可 veto
             payload = to_payload(call_ctx.call)                          # 映射为具体 dataclass
             result = bus.request("tool_call", payload)                   # 对应 ToolPlugin 唯一应答
             result = bus.emit("tool_result", result)                     # 脱敏钩子（v1 默认无）
             storage_handle.append_message(tool_result_msg(...))
        （回到 while 顶部，进入下一个 Step）
    except AbortTurn as e:
        bus.emit("error", Error(e))
        return
  → bus.emit("turn_end", TurnEnd())
```

消息类型总表：

| Topic | Verb | Payload → Reply | 处理者（v1） |
|---|---|---|---|
| `user_input` | emit | `UserInput` | LoopPlugin |
| `turn_start` | emit | `TurnStart` | 无默认订阅者（预留扩展点） |
| `step_start` | emit | `StepStart` | StepLimitPlugin |
| `before_model_call` | emit | `BeforeModelCall` | SystemPromptPlugin → TruncatorPlugin → TokenBudgetPlugin |
| `summarize` | request | `SummarizeRequest` → `SummarizeResult` | SummarizerPlugin |
| `model_request` | request | `ModelRequest` → `ModelResponse` | OpenRouterBackendPlugin |
| `model_response` | emit | `ModelResponse` | 无默认订阅者（预留审核扩展点） |
| `before_tool_call` | emit | `ToolCall` | PermissionPolicyPlugin |
| `tool_call` | request | `BashCall`/`ReadFileCall`/`WriteFileCall`/`EditFileCall` → `ToolCallResult` | 对应 ToolPlugin |
| `tool_result` | emit | `ToolCallResult` | 无默认订阅者（预留脱敏扩展点） |
| `assistant_message` | emit | `AssistantMessage` | DiscordThreadPlugin |
| `error` | emit | `Error` | DiscordThreadPlugin |
| `turn_end` | emit | `TurnEnd` | 无默认订阅者（预留扩展点） |

## 7. 各插件详细设计

### 7.1 LoopPlugin — ReactLoopPlugin
唯一订阅 `user_input` 的编排者，持有本会话的 `storage_handle`，驱动整个 Turn/Step 流程（见第 6 节）。通过 `LOOP=react_loop` 配置选择，架构上允许未来替换为其它编排策略。

### 7.2 BackendPlugin — OpenRouterBackendPlugin
```python
class OpenRouterBackendPlugin:
    def register(self, bus): bus.on_request("model_request", self.complete)
    async def complete(self, msg: ModelRequest) -> ModelResponse:
        # 用 openai SDK, base_url=https://openrouter.ai/api/v1
        ...
```
模型名来自 `OPENROUTER_MODEL` 环境变量。

### 7.3 ToolPlugin（4 个）
每个工具构造时绑定本会话 `workspace_dir`，任何解析后越出该目录的路径直接拒绝（返回 `ToolCallResult(error=...)`，不抛异常）：
- `BashToolPlugin`：子进程执行，超时（默认 60s），stdout/stderr 各截断到固定字节数
- `ReadFileToolPlugin`：支持 offset/limit，默认只读前 N 行
- `WriteFileToolPlugin` / `EditFileToolPlugin`：返回 diff 摘要而非全文回显

每个 `ToolPlugin` 除了 `execute` 方法外，还声明两个静态属性：`llm_name`（发给模型的工具名，如 `"bash"`）和 `schema`（该工具的 JSON Schema，用于 `ModelRequest.tools`）。`plugins/registry.py` 里的工具列表据此构建一张 `{llm_name: payload_cls}` 映射表（`payload_cls` 从各工具 `execute` 的类型注解推断，复用第 4 节的 `_infer_payload_type`），第 6 节流程图里的 `to_payload(call_ctx.call)` 就是查这张表，把模型返回的 `{name, args}` 转成具体的 `BashCall`/`ReadFileCall`/... 实例。这张映射表随插件注册自动生成，不需要额外手工维护。

### 7.4 DiscordGateway（Core Service，实现 `Gateway` Protocol）
进程级、唯一的 discord.py 连接持有者。职责：登录网关、注册 `/agent start`/`/agent stop` 斜杠命令、进程启动时扫描 storage 恢复历史活跃会话、维护 `{thread_id: SessionScope}` 路由表、把收到的 Discord 消息事件桥接为对应会话的 `bus.emit("user_input", ...)`。详见 5.1 节。不参与任何会话总线的消息收发，只做直接函数调用。未来的 `SlackGateway` 等渠道实现同一个 `Gateway` Protocol，`main.py`/`PluginManager` 不需要区分具体是哪个渠道。

### 7.5 ChannelPlugin — DiscordThreadPlugin
每会话一份实例，由 `DiscordGateway` 在 `/agent start`（或重启恢复）时以具体 `discord_thread` 对象构造。只做两件事：订阅本会话总线的 `assistant_message`/`error`，收到后直接调用 `discord_thread.send(...)` 把内容发出去（>2000 字符自动分段）。不负责接收消息、不负责会话查找/恢复——那是 Gateway 的职责。`/agent stop` 由 Gateway 处理（归档 thread、标记会话状态为 ended、从路由表移除），不经过这个 Adapter。

### 7.6 ContextBuilderPlugin 链（`before_model_call`）
- `SystemPromptPlugin`：注入固定 system prompt
- `TruncatorPlugin`：按消息数做滑动窗口截断
- `TokenBudgetPlugin`：统计 token 数，超预算时 `bus.request("summarize", ...)` 触发压缩

### 7.7 SummarizerPlugin（`summarize` 的唯一应答者）
仅在 `TokenBudgetPlugin` 判断超限时才被调用（不是每轮都跑），用模型把旧历史压缩为摘要 + 保留最近 K 条。

### 7.8 PolicyPlugin
- `PermissionPolicyPlugin`：挂在 `before_tool_call`，做权限校验（v1 单团队场景可以先做成"全部放行"的占位实现）
- `StepLimitPlugin`：挂在 `step_start`，超过 `MAX_STEPS_PER_TURN` 时 `raise AbortTurn`

## 8. StorageService（Core Service）

DuckDB schema：
```sql
CREATE TABLE sessions (
    session_key VARCHAR PRIMARY KEY,   -- 内部实现细节，如 "discord:<thread_id>"，从不出现在消息 payload 里
    channel VARCHAR,
    native_id VARCHAR,
    workspace_dir VARCHAR,
    model VARCHAR,
    status VARCHAR,
    created_at TIMESTAMP,
    metadata JSON
);
CREATE TABLE messages (
    session_key VARCHAR,
    seq INTEGER,
    role VARCHAR,
    content JSON,
    created_at TIMESTAMP,
    PRIMARY KEY (session_key, seq)
);
```
接口（直接函数调用，非总线消息）：`get_or_create(channel, native_id) -> SessionRow`、`handle_for(row) -> SessionHandle`，`SessionHandle.append_message(msg)` / `.load_history()` / `.set_status(status)`。

## 9. 配置项

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `DISCORD_BOT_TOKEN` | 无（必填） | Discord bot token |
| `OPENROUTER_API_KEY` | 无（必填） | OpenRouter API key |
| `OPENROUTER_MODEL` | `anthropic/claude-sonnet-4.5` | 默认模型，可配置 |
| `WORKSPACE_ROOT` | `./workspace` | 各会话工作区根目录 |
| `DUCKDB_PATH` | `./data/conic.duckdb` | 持久化文件路径 |
| `MAX_STEPS_PER_TURN` | `25` | 单轮最大 Step 数 |
| `CONTEXT_TOKEN_BUDGET` | `50000` | 触发摘要压缩的 token 阈值 |
| `TRUNCATE_KEEP_LAST_N` | `40` | Truncator 保留的最近消息数 |

## 10. 目录结构

```
src/conic/
  core/
    bus.py          # MessageBus
    manager.py      # PluginManager：渠道无关的 start_session/stop_session、生命周期管理
    gateway.py      # Gateway Protocol（渠道无关契约，具体实现在 plugins/channels/<channel>/gateway.py）
    session.py      # SessionScope、SessionRow（薄数据类）
    messages.py      # 所有消息 dataclass
    errors.py       # AbortTurn、NoResponderError、DuplicateResponderError
  services/
    storage.py       # StorageService（DuckDB）
  plugins/
    registry.py      # 静态注册表
    loops/react_loop.py
    backends/openrouter.py
    tools/{bash.py, read_file.py, write_file.py, edit_file.py}
    channels/
      discord/
        gateway.py     # DiscordGateway（Core Service：连接、斜杠命令、会话恢复、消息路由）
        adapter.py      # DiscordThreadPlugin（Plugin：每会话订阅 assistant_message/error）
    context/{system_prompt.py, truncator.py, token_budget.py, summarizer.py}
    policy/{permission.py, step_limit.py}
  config.py
main.py
```

## 11. 错误处理

- 工具执行失败（越权路径、子进程异常、超时）→ 返回 `ToolCallResult(error=...)`，反馈给模型，不抛异常、不中断进程
- 任意钩子 `raise AbortTurn` → Loop 统一在 while 循环外层捕获 → `bus.emit("error", ...)` → Turn 干净结束，已持久化的状态不受影响（每步成功后才落盘）
- OpenRouter API 异常 → 同样转为 `error` 事件上报到 Discord thread
- Discord 单条消息 2000 字符限制 → `DiscordThreadPlugin` 负责分段发送

## 12. 测试计划

- `MessageBus`：`emit` 链式调用顺序与 mutate 生效；`request` 唯一应答者与重复注册报错；类型推断正确性
- `StorageService`：`get_or_create`/`append_message`/`load_history` round-trip
- 各 `ToolPlugin`：工作区越界拒绝、输出截断
- `ReactLoopPlugin`：用脚本化的 fake `BackendPlugin` 驱动，验证多 Step 结束条件、`AbortTurn` 传播、`MAX_STEPS_PER_TURN` 生效、事件 emit 顺序（测试订阅者断言顺序）
- `TokenBudgetPlugin` + `SummarizerPlugin`：超预算触发摘要、压缩后历史被正确使用
- `DiscordThreadPlugin`：用 mocked discord.py client 做消息路由单元测试；真实 Discord + OpenRouter 端到端为手动验证
- `DiscordGateway`：用 mocked discord.py client 验证启动时的会话恢复逻辑（从 storage 里查出 active 会话、重建路由表）、`/agent start`/`/agent stop` 对路由表的增删、未知 thread 消息被正确忽略

## 13. v1 范围界定

**包含**：4 个核心工具、OpenRouter 单一 backend、Discord 单一 channel、DuckDB 持久化、完整的 ContextBuilder 链（含 Summarizer/TokenBudget）、StepLimit 安全阀。

**不包含（后续可扩展，架构已预留空间）**：
- 多 channel 同时运行（架构已用总线实例隔离支持，只是 v1 只接 Discord 一个）
- 子代理/Task 工具（mini-Kode v3 档位）、Skill 工具（v4 档位）
- 动态插件发现（v1 是静态注册表，`plugins/registry.py` 显式列出）
- `model_response`/`tool_result` 的默认审核/脱敏钩子（v1 只留接入点，不内置实现）

## 附录 A：StorageService 不是 Plugin 的完整论证

见第 3 节引用的三条论据（启动时序矛盾、生命周期矛盾、通信方式不匹配），以及"如果硬塞进总线会怎样"的反证：会导致两套并存的调用接口（bootstrap 阶段直接调用 + 之后总线调用），以及"一个单例资源被伪装成按会话实例化"的抽象泄漏。结论：Storage 归类为 Core Service 是唯一不产生上述矛盾的分类方式。
