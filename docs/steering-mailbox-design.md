# Conic Steering & Mailbox 设计文档

> 状态:已定稿(待实现)。本文汇总所有已确认决策,并记录当前不处理的取舍/风险。

## 1. 背景与目标

现状:`gateway` 直接 `chain(UserInputEvent)`,`ReactLoopPlugin` 订阅处理;`scope.lock` 串行;无中途用户干预;无后台任务;工具超时即杀进程。

目标:
- 用户可在 Turn 中途"插入"新要求(high 优先级 steering);
- bash 等长任务转后台,结果完成后回流(低优先级 steering);
- Gateway 与 Loop 改为纯消息响应插件,各自独立协程,通过消息交互(单事件循环,先不做多线程);
- 生命周期全部自退出,PluginManager 负责收尾。

## 2. 核心概念

### 2.1 三种总线原语

| 原语 | 语义 | 方向 |
|---|---|---|
| `on_chain/chain` | 链式管道,每个 handler 可替换 payload | 推 |
| `on_request/request` | 唯一应答者,同步请求-应答 | 推 |
| `create_mailbox/post/drain` | 生产者只投递,消费者主动取走 | 拉 |

**mailbox 与 chain/request 的本质差异**:chain/request 是"推"(生产者触发消费者并同步处理);mailbox 是"拉"(生产者只投递,消费者在任意时刻主动 drain)。mailbox 不需要 handler,类型在声明时绑定。

### 2.2 Mailbox 原语 API

```python
bus.create_mailbox(name: str, payload_type: type)          # 唯一消费者绑定;重复声明抛 DuplicateMailboxError
await bus.post(name: str, payload)                     # 立即返回,不触发任何消费者;类型不符拒绝
await bus.drain(name: str) -> list[T]                  # 消费式:取走全部并清空;空返回 []
await bus.wait_multiply_mailbox(*names)                  # 等待多个 mailbox 任一被 wake
await bus.close()                                      # 置 closed;post 在 closed 后安全 no-op + 告警
```

- `read_last` 已移除(无消费者)。
- `closed` 检查放在 `post` 内部(原子),调用方不做 `if not closed`(避免 TOCTOU)。
- 后台任务完成时若 bus 已 closed → post 静默 no-op,结果丢弃。

### 2.3 Steering 抽象基类

```python
class SteeringItem(ABC):
    source: str            # "user" | "background" | "system"
    @abstractmethod
    def to_history_entries(self) -> list[dict]: ...   # 转换为 storage 兼容的 message 条目
    def is_turn_abort(self) -> bool: ...              # 默认 False
```

实现:

| 类 | mailbox | source | is_turn_abort |
|---|---|---|---|
| `SteeringUserMessage(text)` | `steering.high` | user | False |
| `SteeringBackgroundResult(task_id, exit_code, output)` | `steering.low` | background | False |
| `SteeringStopCommand` | `steering.high` | system | True |

- `<TokenBudget etc>` 订阅 `InputEvent` 拦截逻辑保留(见 §3.1)。
- 后台结果(placeholder + 真实结果)都按"记成用户轮"写入 history。

## 3. 组件设计

### 3.1 Gateway(两层)

- **DiscordGateway(进程单例)**:`discord.Client`、`on_message`、斜杠命令、路由表 `_sessions`。`on_message` 通过路由表把消息 enqueue 到对应会话的队列。
- **SessionGatewayPlugin(per-session 协程)**:
  ```
  while True:
      msg = await q.get(timeout=0.5)          # 轮询,见下
      if scope.closing: return                # 标志退出,不依赖 SessionEnd
      ctx = await bus.chain(meta.InputEvent, Input(text=msg.text))
      if ctx.handled: continue                # 文本命令/拦截插件 hook
      await bus.post("steering.high", SteeringUserMessage(ctx.text))
  ```
- **轮询原因**:async 无抢占,阻塞在 `q.get()` 时 closing 置位唤不醒协程;0.5s 超时醒来查 closing 退出(与 monitor 同构)。
- **SessionStart 前到达的消息直接丢弃**(gateway 入路由表前 return;loop 首轮 drain 时 mailbox 必空)。
- **`/agent_stop`**:构造 `SteeringStopCommand` post 进 high → 置 `scope.closing=True` → pop 路由表。不直接调 `stop_session` 做收尾;存档与 storage 状态变更由 channel 收 `SessionEndEvent` 时执行。
- 路由表在会话非正常结束时残留 → 新消息进入已死会话队列静默丢弃(决策:不处理)。

### 3.2 LoopPlugin(主协程 `run_loop`)

`handle_user_input` 移除,改名/重构为 `run_loop`,是会话主协程。

```
async def run_loop():
    while True:
        items = await bus.wait_multiply_mailbox("steering.high", "steering.low")  # 空闲等待
        high, low = await drain("steering.high"), await drain("steering.low")
        if not high and not low: continue          # 防空转,wake 后必 drain
        if any(i.is_turn_abort() for i in high):   # idle abort → 直接关闭,不开 Turn
            await _finalize_session(); return
        try:
            await _run_turn(high + low)             # 合并为单个新 Turn 起点(每 Turn 只开一次)
        except AbortTurn as e:
            if e.reason.ends_session:               # UserAbort:_run_turn 内已完成中止收尾,这里只负责让协程真正退出
                await _finalize_session(); return
            # 否则(如 ModelTimeout):_run_turn 内部已处理 ErrorEvent/TurnEndEvent,回空闲继续 while
```

**Turn 内(检查点规则)**:

1. **Turn 开始**:drain high+low,全部 `to_history_entries()` 注入 history。
2. while 循环 per step:
   - `chain StepStart`
   - `ctx = BeforeModelCall` → `response = await wait_for(bus.request(ModelRequest), model_timeout=120)`
     - **超时**:不进 history,`raise AbortTurn(reason=ModelTimeout)`;`_run_turn` 内 catch 住并处理 `ErrorEvent`(告知用户)→ 补发 `TurnEndEvent`,再重新 raise 同一个 `AbortTurn` 让异常传到 `run_loop`;`reason.ends_session=False` → `run_loop` 捕获后不 return,直接回空闲循环(会话存活,不自动重试)
   - **终答判定(检查点 3)**:模型返回后,**先 drain high**:
     - 含 abort → 走 §3.2 abort 路径
     - high 非空且无 tool_calls → **先 append 助手回答入 history,再注入 high 条目,不 break**(强制再走一步,否则已返回的答案在插入新要求后丢失)→ `continue`
     - high 为空且无 tool_calls → 终答 break
     - high 非空且有 tool_calls → **先把已 drain 出的 high 条目注入 history**,再进入下面的 tool_calls 分支(不 continue,本步继续执行工具;否则这批 high 条目会被静默丢弃)
   - 有 tool_calls:append raw_message → 逐个执行工具
     - 工具 >5s 转后台,见 §3.3;返回 placeholder
     - **每次工具完成后(检查点 2):drain high(仅 high)**:
       - 含 abort → 走 §3.2 abort 路径(丢弃同批其余 high 条目,**中止后续工具执行**,不再继续本 step 剩余的 tool_calls)
       - 非空且无 abort → 注入 history,继续执行下一个工具
   - `chain StepEnd`
3. `chain TurnEndEvent`;**TurnEnd 后 drain low**(可能多条;若同时有 high → 下次唤醒合并为一个新 Turn)。

**abort 语义**

- 批次中**任一条目 is_turn_abort → 丢弃同批其余 high 条目**,直接走中止路径。
- 该检测适用于**每一处 drain high 的位置**(检查点 2、检查点 3),不只是终答判定那一刻;检查点 2 若漏查会导致 `/agent_stop` 在工具执行期间被静默吞掉(注入成一条普通 history 消息)而不真正生效。
- Turn 内 abort:检测点(检查点 2/3)`raise AbortTurn(reason=UserAbort)`;`_run_turn` 内 catch 住,干净中止当前 Turn(**不 chain ErrorEvent**)→ 补发 `TurnEndEvent`,再重新 raise 让异常传到 `run_loop`。
- **协程退出的落点在 `run_loop`,不在 `_run_turn` 内部**:`run_loop` catch 到 `AbortTurn` 后,按 `reason.ends_session` 分流 ——`UserAbort` → 调 `_finalize_session()`(内部 `chain SessionEndEvent`)并 `return`,真正终止 `run_loop` 协程;`ModelTimeout` 等非终止性 reason → 不 return,直接回空闲循环等待下一批 steering。`_run_turn` 本身**不**调用 `_finalize_session()`,也不直接 return 到外层 —— 必须靠异常传播,否则 `run_loop` 会在处理完 abort 后又绕回 `wait_multiply_mailbox` 卡死(此时 gateway 早已 pop 路由表,不会再有新消息唤醒它;而 `bus.close()` 又被要求在 `gather()` 完成之后才能调用,`gather()` 反过来要等 `run_loop` 先 return —— 若退出信号没有正确从 `_run_turn` 传播到 `run_loop`,这里会构成死锁,会话永远卡在未 `ended` 状态)。
- idle abort:不启动 Turn,直接在 `run_loop` 里调 `_finalize_session()` → `SessionEndEvent` → `return`,与 Turn 内 abort 共用同一个 `_finalize_session()`,但触发点不同(idle 在外层循环直接命中,不经过异常传播)。
- `AbortTurn` 带 `Reason` 枚举(`ModelTimeout` / `UserAbort` / ...),`Reason` 需要暴露 `ends_session: bool`,由 `run_loop` 据此决定是否 `return`。

**终答不处理 low**:low 永不在 mid-turn 注入,只在 Turn 边界(Turn 开始 / TurnEnd 后 / idle 唤醒)消费。

### 3.3 后台任务管理器(monitor)

- **每会话单协程**,`poll_interval=0.5s` 轮询;内部维护任务状态表 `task_id → (proc, waiting_flag, start_time)`。
- **进程所有权**:proc 自出生即归 monitor,**无交接**;管道 stdout/stderr 自始归 monitor,不跨协程传递。

**调用流(bash → monitor)**:

```
bash.execute():
    resp = await bus.request(StartBackgroundTask, TaskStartRequest(command))   # 同步请求-应答
    if resp.error: return ToolCallResult(error=...)     # spawn 失败同步返回,不进 5s 等待
    try:
        result = await wait_for(completion_event[task_id], timeout=bash_fast_window)
        return result                                    # 5s 内完成 → 正常 ToolCallResult
    except TimeoutError:
        return ToolCallResult(output="background task running, id=...")  # 5s 放弃,返回 placeholder
```

- bash 开始时 `waiting_flag=True`(**登记即 True**,快任务 0.1s 完成也不会漏);bash 5s 放弃时才翻 `False`。
- **spawn 失败**:由 `bus.request(StartBackgroundTask, ...)` 同步返回 `resp.error`,bash 在进入 `wait_for(completion_event...)` 之前就直接 return,完全不接触 `completion_event`。monitor 侧对应地**不把该 task_id 记入任务状态表**(即不存在 proc/waiting_flag 条目),因此轮询循环的"任务完成"/"超时"分支不会再对它做任何处理 → 天然无双投,不依赖预置 completion_event 之类的第二套机制。

**monitor 每轮 poll(优先级从高到低)**:

1. `scope.closing` → **静默 kill 全部 proc** → 退出;**不 resolve 任何等待者**(等待者由 bash 自身 5s 超时拿 placeholder)——结果丢弃。
2. 收到新任务请求时也先查 closing → 置位则拒绝,返回 error。
3. 任务完成(`returncode != None`):waiting=True → resolve 完成事件(走 bash 正常路径);waiting=False → `post("steering.low", SteeringBackgroundResult(...))`。
4. `bash_task_timeout=60s` 总时限(从 spawn 起算):超时 → kill proc → `post low 超时 steering`(60s > 5s,必然走 low 路径)。

- 完成/超时两种结果都由 monitor 自己 post;结果在进程退出后一次性 `communicate()` 拿全(不做增量流式输出)。
- 快任务 0.5s 轮询延迟可接受;4.9~5.4s 边界竞态语义定死:waiting 翻转后一律走 low 路径,有界延迟(决策:不处理)。

### 3.4 PluginManager(生命周期装配 + 收尾)

```
start_session():
    装配插件(register)
    await bus.chain(SessionStartEvent, reason=...)        # 同步在 create_task 之前,上下文插件先就绪
    tasks = {loop: create_task(loop_plugin.run_loop()),
             gateway: create_task(gateway_plugin.run()),
             monitor: create_task(monitor_plugin.run())}
    scope.tasks = tasks
    # 随后:await gather(*tasks) → 见下 stop/收尾

stop_session / 收尾(由 PluginManager 自身 join,不设独立 supervisor):
    await gather(*scope.tasks.values())                  # join 三协程,全部自退出
    if not session_end_emitted: chain SessionEndEvent     # 兜底:异常退出时补发
    storage.handle_for(row).set_status("ended")
    await bus.close()                                    # 唤醒仍阻塞 wait 的协程;最后一步
```

- 三协程**全部自退出,零 cancel**:loop 收 abort 退出(见 §3.2 "abort 语义"里 `AbortTurn` 从 `_run_turn` 传播到 `run_loop` 才真正 `return` 的机制);gateway 轮询见 closing 退出;monitor 轮询见 closing 杀进程退出。三者的退出**都不依赖 `bus.close()`**,全部由自身逻辑(abort 分支 / closing 轮询)触发。
- 协程异常纪律:此处指**非预期异常**(bug、第三方库抛错等),内部 `try/except`,除 `CancelledError` 外**一律不得导致协程退出**;捕获异常 → 发 `ErrorEvent` 告知用户 → 记录日志继续跑。`AbortTurn` 是预期内的控制流信号,不受此纪律约束 —— 它必须被显式 catch 并按 `reason.ends_session` 处理,不能被这条"兜底不退出"规则连带吞掉。
- `bus.close()` 职责仅唤醒阻塞 `wait` 的协程,不是取消手段;必须在 join 完成之后调用(先 close 会破坏还在跑的 Turn)。**注意**:按上面"三协程全部自退出"的不变量,`gather()` 完成时三者必然都已自行 return,届时不会再有协程阻塞在 `wait_multiply_mailbox` 上——`bus.close()` 在本设计的正常路径里实际不承担真正的"唤醒"作用,只是防御性收尾(以及让 `post` 在此后安全 no-op)。如果 `run_loop` 的 abort 传播机制实现有误(异常没有正确从 `_run_turn` 冒泡到 `run_loop`),`run_loop` 会卡死在 `wait_multiply_mailbox` 里,而 `bus.close()` 因为排在 `gather()` 之后永远不会被调用——即没有安全网,务必保证 §3.2 的异常传播路径实现正确。

### 3.5 模型超时(120s)

- `wait_for(bus.request(ModelRequest), 120)` 包在 loop 内。
- 超时 → `AbortTurn(reason=ModelTimeout)`,不进 history,不自动重试;发 `ErrorEvent` 告知用户;本 Turn 结束,会话继续等新消息。
- 取消会传播到模型客户端;流式(`stream_updates=True`)下需显式关闭响应连接,避免连接泄漏污染连接池(实现硬约束)。
- 语义边界:120s 为**单次请求超时,不含流式返回时长**;当前总是流式,流式请求的悬挂风险评估见 §7。

## 4. 关键时序

### 4.1 中途介入(steering.high)
用户中途发消息 → gateway plugin post high → loop 当前模型返回(检查点 3)→ drain high 非空 → 先 append 助手已答答案 → 注入 high → `continue` 强制再走一步。

### 4.2 后台任务回流
bash 5s 放弃返回 placeholder → 进程完成 → monitor post low → 场景 A:loop 空闲被 low 唤醒 → 开新 Turn;场景 B:TurnEnd 后 drain low 命中 → 下一 Turn 起点。等待唤醒依赖 `wait_multiply_mailbox(high, low)`(只等 high 或只等 low 都会漏)。

### 4.3 agent_stop
`/agent_stop` → gateway post `SteeringStopCommand` + 置 closing + pop 路由 → loop 下一检查点 drain 到 → 干净中止 → `TurnEndEvent` → `SessionEndEvent` → 退出;gateway 协程 0.5s 内见 closing 退出;monitor 静默 kill 全部 proc 退出 → PluginManager join 完成 → storage ended + `bus.close()`。

### 4.4 模型超时
120s 超时 → `AbortTurn(ModelTimeout)` → `ErrorEvent` → `TurnEndEvent` → loop 回空闲(会话存活,后台任务不受影响)。

## 5. 配置

| 配置项 | 默认 | 归属 |
|---|---|---|
| `bash_fast_window` | 5s | bash 等待完成事件的窗口 |
| `bash_task_timeout` | 60s | monitor 总时限(从 spawn 起算) |
| `model_timeout` | 120s | loop 单次模型请求 |
| `poll_interval` | 0.5s | monitor / gateway 轮询间隔 |

- 约束:`bash_fast_window <= bash_task_timeout`(否则 5s 放弃但 60s 未到,任务永不投递)。
- `BashToolPlugin` 的旧 `timeout` 参数删除;`ConfiguredBashToolPlugin(timeout=config.bash_timeout)` 一并移除。

## 6. 已移除 / 改名清单

- `UserInputEvent` topic:删除(gateway 改为 post mailbox;loop 不再订阅)。
- `SessionStopEvent` topic:删除(冗余;gateway pop 路由表 = 硬静音,channel 收 `SessionEndEvent` 归档)。
- `handle_user_input` / `handle_input`:改为 `run_loop`(mailbox 驱动)。
- `scope.lock`:失去意义(gateway 不再排队触发 UserInputEvent),移除。
- `read_last`:移除。
- `BashToolPlugin.timeout`:移除(时限移交 monitor)。

## 7. 已知取舍 / 风险(已决策:当前不处理)

1. **路由表泄漏**:会话非正常结束时 `_sessions` 残留,新消息静默丢弃(不处理)。
2. **迟到窗口**:TurnEnd 后完成的后台任务靠 idle `wait_multiply_mailbox` 中 low 感知开新 Turn(已由 `wait_multiply_mailbox` 覆盖)。
3. **4.9~5.4s 边界竞态**:有界延迟,语义定死走 low(不处理)。
4. **流式模型超时**:120s 为单次请求、不含流式返回;当前总是 `stream_updates=True`,流式请求实际无超时上限,存在"loop 等不到检查点"的悬挂风险——需在实现期评估:要么流式也加总时长,要么为流式加 idle 探测。
5. **单事件循环**:先不做多线程;若未来 gateway 真开线程,需 `loop.call_soon_threadsafe` + 锁(`asyncio.Event`/`Lock` 非线程安全)。
6. **idle abort 丢弃同批 low**:`run_loop` 外层循环在 idle 唤醒时若同一批里既有 abort 又有 low(如后台任务结果恰好和 `/agent_stop` 同时到达),`low` 条目不会被传给 `_run_turn`,直接随 `_finalize_session()` 一起丢弃,用户看不到这次后台任务的输出(不处理:会话本就要结束)。
7. **检查点 2 abort 会留下配不上的 tool_calls**:检查点 3 判定"有 tool_calls"时会先把整条含 N 个 tool_calls 的助手消息 `append` 进 history;若中间某次工具完成后(检查点 2)drain high 命中 abort,会"中止后续工具执行",导致后面 N-k 个 tool_calls 永远不会有对应 tool_result 写入 history——history 在会话结束时停在一个 tool_calls/tool_result 配对不完整的状态。只要以后没有路径会重新读取/续用这份 history(展示历史、恢复会话、喂给下一个 session 当上下文等),就不影响功能;暂不处理,但如果将来出现"读取历史会话"类需求,需要重新评估(方案:给被跳过的 tool_calls 补一条 cancelled 占位 tool_result,或在读取时做容错)。

## 8. 落地顺序

1. `bus`:mailbox 三原语 + `wait_multiply_mailbox` + `closed`(含 post 内原子检查)。
2. `SteeringItem` 抽象基类 + `SteeringUserMessage` / `SteeringBackgroundResult` / `SteeringStopCommand`。
3. Gateway 双层改造(单例路由 + per-session 协程,轮询退出),`InputEvent` 链保留。
4. Loop 重写 `run_loop` + 三检查点 + `AbortTurn(Reason)` + 120s 模型超时。
5. monitor(轮询、task 状态表、closing 优先、60s 总时限)+ bash 改造(request 建任务、5s 等待窗口、placeholder)。
6. PluginManager 装配 `create_task`、join 三协程、兜底补发 SessionEnd、storage ended、`bus.close()`。
7. 配置迁移(`bash_fast_window` / `bash_task_timeout` / `model_timeout`),移除旧 `bash_timeout`。
8. 测试迁移:所有订阅 `UserInputEvent` 的调用点/测试改为 mailbox 语义。