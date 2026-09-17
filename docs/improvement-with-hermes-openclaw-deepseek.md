
# 三框架 Loop 深挖:hermes-agent / openclaw / deepseek-harness

调研日期 2026-09-16,三个仓库的 clone 在 `/tmp/research-hermes-agent`、
`/tmp/research-openclaw`、`/tmp/research-deepseek-harness`(文中引用的文件
路径均为各仓库内相对路径)。三者的形态:

- **hermes-agent**(NousResearch,Python):CLI/gateway agent。嵌套双循环
  (外层步循环 + 内层 API 重试循环),loop 拆成 ~15 个阶段函数,verdict
  数据类驱动状态机。特征是极重的容错投入,代码里全是 issue 编号注释,
  读起来是"每个防御都对应一次生产事故"。
- **openclaw**(TypeScript):多渠道 AI 网关,与 conic 场景最像(Discord/
  Telegram 等聊天平台驱动 agent)。核心 loop 是 vendored 的 pi-agent 后代
  (`packages/agent-core`)——**与本文上半部分对比的 Pi 是同一血统**,可视为
  "Pi 的 loop 在多渠道网关场景下长大后的样子"。
- **deepseek-harness**(deepseek-ai,TypeScript):编码 agent CLI(`dsh`),
  Cordis 插件框架,架构最激进——**事件溯源**:turn/step/工具调用/重试/压缩锁
  全部是持久化 session 事件,发给 LLM 的消息列表是对事件日志的纯推导。

**一个共同的哲学分歧,先记在前面**:三家都没有把"步数上限"当主要终止机制
(deepseek 和 openclaw 的核心 loop 里根本没有 max_steps;hermes 默认
`sys.maxsize`)。终止靠:自然终止(无工具调用)+ 循环检测熔断(§6)+
预算/钩子(deepseek 的 `agent/turn-stopping` 甚至允许插件在停止前注入新工作
续命)。Conic 的 `StepLimitPlugin` 作为安全网保留没问题,但长期主要投资
应放在循环检测上,步数上限可以放宽。

另一个共同验证:三家都把重试/超时/审批/压缩做成 loop 之外的插件/中间件
(deepseek 最彻底),conic"策略不进 loop"的方向是对的,下面各节的方案
也都遵循这一原则——**十个专题没有一个需要推翻 `react_loop.py` 的结构**
(§10 的调度器在 gateway 层、后台执行在工具层、注入复用 §3 的 inbox、
活性盖章全部挂在现有 bus 事件上)。

## §1 模型调用重试/退避

### Conic 现状

完全没有。`OpenRouterModelPlugin.complete()` 裸调 SDK,一个 429/超时/
断流直接异常冒泡到 loop 的 `except Exception`,整个 Turn 以 `error` 事件
终结,用户看到一条错误消息,插话内容作废。这是当前最大的可用性洞。

### 三家做法

| | hermes | openclaw | deepseek |
|---|---|---|---|
| 重试归属 | 内层 API 重试循环(loop 的一部分) | 外层 run 编排层(SDK 自带重试显式关掉 `maxRetries: 0`) | `llm-retry` 插件挂 `agent/request-error` waterfall,loop 本体无重试逻辑 |
| 错误分类 | `classify_api_error` → 四类裁决:`retryable` / `should_compress` / `should_rotate_credential` / `should_fallback` | 瞬时类:`rate_limit \| overloaded \| server_error \| timeout \| output_limit` | provider 自带 `retryableCodes` 声明 |
| 退避 | `min(5·2^(n-1), 120s)` + 50% jitter;解析 `Retry-After`;provider 专用阶梯(ZAI 过载 30/60/90/120s) | base 1s、cap 30s、full jitter,取 `max(jittered, Retry-After)`(连过时的 HTTP-date 格式都解析) | `initialDelayMs·2^n` cap `maxDelayMs` + jitter 比例,尊重 `providerRetryAfterMs` |
| 预算 | `api_max_retries = 3` | 8 次(rate-limit 9 次)且限 90s 墙钟窗口内 | provider 声明 `maxRetries`(或 `always` 无界) |
| 耗尽后 | 重建 transport 一次 → 走 `fallback_providers` 链(fallback 前按其上下文窗口重跑 preflight 压缩)→ 结构化终态失败 | failover:换 auth-profile → 换 fallback 模型;重试单位是整个 run attempt(`agentLoopContinue`) | 抛结构化 `LlmFailure` |
| 特色 | **partial-stream 恢复**:流断了但已收到 delta,用已有文本合成 `finish_reason="length"` 桩,走"续写 nudge"而不是重试(上下文溢出除外,终态) | 退避 sleep 可被中断打断 | **每次重试是持久化事件**:`llm/retry` 在可取消等待前落库、`llm/retry-started` 在之后,重试计数从 projection 重建、`step/start` 时清零 |

关键共识:退避带 jitter、尊重 `Retry-After`、重试预算之外还要有墙钟窗口、
分类决定动作(不是所有错误都值得重试)、重试要可观察。

### Conic 落地方案

短期(推荐,一个下午的量):**重试内建在 backend 里**。`openrouter.py` 的
`complete()` 外面包一层 `_with_retry`:

- 分类:openai SDK 异常映射——`APIConnectionError` / `APITimeoutError` /
  `RateLimitError` / `InternalServerError`(5xx)→ retryable;`BadRequestError`
  等 4xx → fatal 直接抛(其中"上下文超长"类错误留给 §7 的溢出驱动压缩);
- 退避:`min(1s·2^n, 30s)` + full jitter,读 `Retry-After` 取 max;
- 预算:最多 5 次且总耗时 ≤ 90s;
- 可观察:每次重试 emit 一个新 topic `model_retry`(带 attempt 序号、异常、
  等待秒数),Discord 侧可以把状态行改成"⏳ 模型过载,第 2 次重试…"——
  比静默卡住体验好得多;
- 流式已发过 delta 再断:直接整次重调即可,不需要 hermes 式 partial 恢复——
  `MessageUpdateEvent` 本来就是整体替换语义,重试开始时 emit 一次
  "(重试中)"替换掉半截文本,新流的 delta 会重新累积。失败的流拿不到
  usage,不存在重复计费。

长期(记录架构缺口,不急):三家都有"包住一次 provider 调用"的洋葱层
(hermes 的 middleware chain、openclaw 的 extension runner、deepseek 的
`llm/stream` waterfall),conic 的 bus 只有链式变换(emit)和单响应者
(request),**没有 wrap 语义**——重试做不成独立插件正是这个缺口的第一次
显形,和前文"生命周期事件对比 §6 provider 层钩子"是同一件事。若将来
`bus.request` 支持中间件栈,重试再从 backend 里搬出来。

## §2 用户中断进行中的 Turn

### Conic 现状

没有任何通路。更糟的是 `gateway.handle_message` 和 `handle_stop_command`
都 `async with scope.lock`——**Turn 跑飞时连 `/agent_stop` 都要排队等它
自己跑完**(最多 25 步 × 每步一次模型调用 + 工具执行)。`AbortTurn` 是
纯内部机制(policy 插件在事件链里 raise),用户没有扳机。

### 三家做法

- **deepseek**:全链路 `AbortSignal` 融合。`AgentCancelCause = user | parent
  | hook | disposed`;step 循环**每个 await 之间**都 `signal.throwIfAborted()`;
  abort 时流式半截输出保留为 `assistant/message {interrupted: true}` 落库;
  已启动的工具调用**排干并提交真实结果**,未启动的拿合成结果(§4);每个
  Turn 换新 AbortController;abort 期间到达的唤醒被锁存、收敛后重放。
- **openclaw**:`Agent.abort(reason)` → per-run AbortController。
  `stopIfAborted()` 落库一条合成的 aborted assistant 消息(**transcript 永远
  不以悬空 toolUse 结尾**),并追加一条对模型可见的中断提示("工具可能已
  部分执行")——除非 abort 原因带 `turnHandoff: true`(模型切换等干净交接
  跳过提示)。用户侧:~50 个自然语言停止短语(多语言的"stop")+ `/stop`
  → `abortByUser()`,顺带清空排队 followup、取消 subagent。abort 原因区分
  `user_abort | restart | superseded`。
- **hermes**:三个动词分得最细——`interrupt()`(硬停:跨线程 interrupt 位
  + 递归传播到子 agent + 掐断 HTTP socket + generation CAS 防止与恢复执行
  竞态 + 与压缩提交栅栏协调,防 /stop 打坏进行中的压缩落库);`steer()`
  (不打断,排队注入,§3);`redirect()`(只取消进行中的模型请求,半截
  流式文本存为 checkpoint 行,纠正语句作为 user 行,**同一个 Turn** 重建
  继续;工具执行期间 redirect 降级为 steer)。不配合的工具给 3s 然后放弃,
  结果标记 `[Tool execution cancelled …]`。

### Conic 落地方案

分两层,先协作式后强制:

1. **扳机与状态位**:`SessionScope` 加 `abort_requested: asyncio.Event` 和
   `current_turn: asyncio.Task | None`。新增 `/agent_interrupt` slash 命令
   (以及可选的 `!stop` 文本,走已有的 `input` 拦截事件识别——这正好是
   那个挂载点的第一个真实用户);**不抢锁**,直接 set event。
   `handle_stop_command` 同步修复:先 set abort 再等锁,stop 不再排队。
2. **协作式检查点**(改动 ~10 行):loop 在 (a) 每个 step 开头、(b) 每个
   工具执行前、(c) 每个工具执行后,检查 event → `raise AbortTurn("user
   interrupt")`。配合 §4 的配平(先补齐本批未执行工具的合成结果再抛),
   以及一条写进历史的中断说明("上一轮被用户中断,工具可能部分执行"——
   照抄 openclaw 的语义),下一个 Turn 模型能接得上。
3. **强制层**(后续):模型流的取消(openai SDK 的 stream 支持提前退出,
   在 backend 的 `async for` 里检查 event 并 `break`,退化为 §1 的"半截文本
   + 整体替换"处理)和 bash 子进程 kill(`BASH_TIMEOUT` 的 kill 路径已有,
   复用)。asyncio 单线程模型下不需要 hermes 那套跨线程 interrupt 位,
   `Task.cancel` 是终极手段但先不用——协作式已覆盖绝大多数场景。

## §3 Steering / 队列语义

前文《消息注入(steering / follow-up)》一节的设计(inbox 队列 + step 边界
排空 + turn 末尾 follow-up 检查)**保持不变,依然是正确的落地路径**。本节
补充三家的实现细节,作为该设计的参数手册与后续演进方向。

### 三家做法

- **openclaw**(最完整的队列语义,conic 同场景):四种队列模式
  **`steer`(默认)| `followup` | `collect` | `interrupt`**,解析顺序
  `消息内联指令 ?? session 设置 ?? channel 设置 ?? 全局配置 ?? "steer"`。
  参数:debounce 500ms(连发消息合并)、队列容量 20,超容 drop 策略
  `old | new | summarize`(默认 **summarize——被挤掉的消息摘要成一条**,
  不是静默丢弃)。steering 消费检查点:run 开始、每个串行工具前、并行批
  启动前一次、每个 turn 后、`agent_end` 前最后一次兜底轮询("被接受的
  steer 永远不会被搁浅")。steer 注入时**跳过未启动的工具调用**(已启动的
  跑完;被跳过的拿合成结果 `"Skipped to process an incoming message."`)。
  最激进的特性:**在线 steering**——OpenAI Responses websocket 传输下,
  若新消息对活跃请求的投影只是"追加 user 条目"(前缀不变、设置不变),
  直接推进正在进行的连接,连重新调用都省了。
- **deepseek**:三条输入通道 `followup()`(next-turn + 唤醒)/ `steer()`
  (next-step + 唤醒)/ `inject()`(next-step 不唤醒)。inbox 是**持久化**的
  (`agent/inbox/spliced` 事件,fold 重建),掉电不丢插话。turn 结束条件是
  "本 step 无工具调用 **且** `agent/turn-stopping` 钩子跑完后 next-step
  inbox 仍为空"。
- **hermes**:`steer()` 排队文本,在下一个 iteration 作为独立 user 行追加在
  **最新 tool result 之后**——`cache-safe append`,绝不重排已缓存前缀。

### 对原设计的增补

原设计的改动点全部维持,追加四条(均可后置):

1. **注入位置必须 cache-safe**(hermes 经验,实现时的硬约束而非可选项):
   steering 消息 append 在历史末尾,绝不能插到中间或触发前缀重排,否则
   每次插话打穿一次 prompt cache;
2. **容量与 drop 策略**:conic 用户基数小,先只做 cap(如 20)+ 丢弃最旧
   并在 Discord 回一句提示;openclaw 的 summarize-dropped 留作将来;
3. **跳过未启动工具**的语义依赖 §5 的并行工具,做了并行之后再对齐;
4. **inbox 持久化**(deepseek)可选:DuckDB 一张小表,重启后插话不丢——
   与"崩溃恢复"(§4)是同一批工作,可以捆一起做。

### Steering 输入要不要进 history(每次发给 LLM)?——要

三家全部把 steering 输入持久化为正式的 user 消息、进 history、之后每次
请求都包含。不进 history 的从来不是用户插话,而是系统生成的脚手架。

**三家证据**:

- **hermes**:`steer()` 排空时作为独立 user 行追加在最新 tool result 之后,
  走正常 transcript 持久化(SQLite);
- **openclaw**:`commitPendingMessages()` 正式提交进 transcript,且有两条
  "不许丢"规则——run 被 stop 时**先把 pending steering 落进 transcript 再
  退出**;在线 steering 的 `reserve` 语义("一旦可能已过线,本地不得声称
  已撤回")保证推上活跃连接的插话最终也落 transcript;
- **deepseek**:`steer()` **进 inbox 时就已是持久化事件**
  (`agent/inbox/spliced`),被 preStep 认领后成为 turn 的 user 消息落日志
  ——连"还没被消费的插话"都不怕进程崩溃。

**为什么必须持久化**(而不是当次注入用完即弃):

1. **行为可解释性**:steering 改变模型后续所有行为,从 history 消失则
   之后的 transcript 解释不了模型为什么转向——调试、审计、resume 后
   模型自己都会困惑;
2. **多 step 一致性**:插话通常是改目标;conic 每 step `load_history()`
   全量重发的结构下这是硬性的——**不落库 = 下一个 step 就失忆**;
3. **崩溃/恢复**:落库的插话 resume 后仍然生效;
4. **用户预期**:用户在 thread 里打的字理应是对话的一部分。

**界线——什么不进 history**(三家都严格区分):

| 进 history(durable user 消息) | 不进 history(ephemeral) |
|---|---|
| steering / follow-up 用户文本 | hermes 的预算预警——写进最新 tool result 的**未持久化尾部**(per-call 装饰) |
| | conic 已有的 `ExtraPromptPlugin` 动态状态(拼进最后一条消息的发送副本) |
| | hermes 的合成 nudge 行——带标记落库但 finalize 时从返回历史剥离 |
| | openclaw 的中断引导——custom 消息,`convertToLlm` 时才投影成 user 角色 |

**conic 执行细节**(在原设计"逐条 `append_message({"role":"user",...})`"
之上补三条):

1. **在排空点落库,不在到达点落库**:消息到达时进 inbox,step 边界排空时
   才 append——保证它排在本批 tool result **之后**(OpenAI 协议不允许
   user 消息插在 assistant.tool_calls 与 tool 结果之间,落库时机错了
   历史就非法);
2. **追加位置 cache-safe**:只在末尾 append,绝不回改前缀(同上文增补
   第 1 条,这里是落库视角的同一约束);
3. **带 source 标记**(衔接 §10):`append_message` 的 JSON 里加
   `source: "steering"`——LLM 看到的内容不变,但渠道回显、审计、将来
   按类剥离都靠它。

一句话:**用户说的话永远是 history 的一等公民;每 step 都变、只为当次
调用服务的内容才走 ephemeral 通道**。conic 现有的两条通道(storage 落库
vs `ExtraPromptPlugin` 尾部拼接)正好对应这两类,steering 走前者。

## §4 崩溃/中断后的 transcript 配平

### Conic 现状:两条路径会产生不配平历史,其中一条是现存 bug

`react_loop.py` 的顺序是:先落库 `response.raw_message`(含 N 个
tool_calls),再逐个执行工具、逐个 append `role:"tool"` 结果行。于是:

1. **现存 bug(墙上的枪)**:批次中第 k 个工具的 `before_tool_call` 链上
   有 handler `raise AbortTurn`(`react_loop.py` 对 AbortTurn 是直接
   `raise`,不像普通异常那样转成 Error 结果),则前 k-1 个结果已落库、
   第 k..N 个没有——历史里的 assistant.tool_calls 悬空。**下一个 Turn 的
   模型调用会被 OpenAI 协议直接 4xx 拒绝,session 永久卡死**(每个新
   Turn 都加载同一份坏历史)。目前没炸只是因为内置插件都不会在批次中途
   raise(Permission 是空壳、StepLimit 挂在 step_start)——§2 的用户中断和
   §9 的审批门一旦实现,这把枪就会响。
2. **进程崩溃**:工具执行中途进程被杀,重启 resume 后同样是悬空
   tool_calls,同样永久卡死。

顺便指出一个 conic 已经做对的点:**persist-before-execute**(assistant
tool-call 行先落库、工具副作用后发生)正是 hermes 明确表述的不变式
("a failed append ends the turn rather than running tools from
process-only state"),conic 天然满足 ✅。

### 三家做法

- **deepseek**:两端都管。运行时:abort 后已启动的调用排干、提交真实结果,
  **每个未启动的调用拿一条按模型顺序合成的 `tool/result`**("tool call
  aborted before dispatch",错误码 `TOOL_ABORTED_BEFORE_DISPATCH`),
  call/result 恒配平。恢复时:`interruptedTurnClosers(persisted)` 在 resume
  时给被杀进程的日志**追加合成闭合事件**(缺失的工具错误结果、`step/end`、
  `turn/end`),使日志重放合法。
- **openclaw**:abort 路径落库合成的 aborted assistant 消息;async 工具的
  assistant fragment 在任何副作用之前就带持久 turnId 落库("every executed
  call has a durable owner");混合有效/无效调用的批次,**所有** call 都保留
  在 assistant 行上(每个 tool_call 必须有配对结果),只 dispatch 有效的。
- **hermes**:persist-before-execute 不变式(见上);混合批次同样全员保留、
  无效的直接给错误结果。

### Conic 落地方案(建议排最先做——正确性 bug,改动最小)

两个修复点,互补:

1. **运行时配平**(`react_loop.py`,~10 行):把工具批次循环包进
   try/finally 或在 `except AbortTurn` 里,**先给本批未产生结果的每个
   tool_call 补一条** `{"role": "tool", "tool_call_id": …, "content":
   "Error: turn aborted before this tool executed"}` **再抛**。对齐
   deepseek 的合成结果语义。
2. **加载时修复**(`storage.py` 的 `load_history` 或 loop 的 turn 开头,
   ~15 行):扫描历史尾部,发现 assistant 消息的 tool_calls 中存在没有
   配对 `tool` 行的 id → 就地补合成错误结果行并落库("interrupted before
   completion",一次性修复,幂等)。这条覆盖进程崩溃,是 deepseek
   `interruptedTurnClosers` 的 DuckDB 版。

两个修复都不改变正常路径的任何行为,可以先行合入。§2(中断)上线前
必须有 1;resume 功能既然已存在,2 现在就该有。

## §5 工具并行执行

### Conic 现状

严格串行(`for call in response.tool_calls` 逐个 await)。工具少时无感,
但模型一旦习惯批量发 read_file(强模型的常见行为),延迟线性叠加。

### 三家做法

| | hermes | openclaw | deepseek |
|---|---|---|---|
| 默认 | 并行(带规划器) | 并行 | 按声明分组 |
| 安全判定 | `_plan_tool_batch_segments`:只读工具、opt-in 的 MCP 工具、**文件路径重叠准入**(读读重叠→并行;任何写重叠→串行 barrier);`delegate_task` 强制串行 | 每工具 `executionMode` 声明;批内**任一** sequential 工具 → 整批串行 | 每工具 `isConcurrencySafe` 分类器,未知/抛错一律 `exclusive`;连续 parallel 组进滚动池(池宽运行时可改),exclusive 单独跑作 barrier;**每组开始前重新分类** |
| 结果顺序 | 线程池 + start-order gate(审批提示按调用顺序出现) | `tool_execution_end` 按完成序 emit,但结果**消息**按 assistant 源序重排 | dispatch 重叠,但 `tool/result` 事件、结果上下文、post-execute 策略**按模型顺序提交**(`commitReady` 只沿连续槽位推进) |
| 特色 | 批超时剔除审批等待时间(§8) | **async 工具**:模型还在流式输出时,已解析完的工具调用就开始执行(assistant fragment 先落库再执行) | abort 时未启动调用拿合成结果(§4) |

共识:**执行可以乱序,提交必须按模型顺序**;安全性宁可保守(未知 =
串行);审批与并行的交互要专门处理。

### Conic 落地方案

- 工具插件类加类属性 `concurrency_safe: bool`(`read_file` True;`bash` /
  `write_file` / `edit_file` False——将来想放宽 write 再做 hermes 式路径
  重叠分析,第一版不要);第三方工具插件缺省 False(deepseek 的"未知即
  exclusive");
- loop 里把 `response.tool_calls` 按 safe 标志切成连续段:safe 段
  `asyncio.gather`(逐个包 try/except,单个失败不连坐),unsafe 段维持
  逐个 await;
- **结果按模型原顺序 append 落库**(gather 返回序即入参序,天然满足);
  `tool_execution_start/end` 事件各自即时 emit(观察真实并发),
  `tool_result` 变换链和落库按序跑;
- `MessageUpdateEvent` 的工具状态行改为合并显示("🔧 read_file(a.py) +2
  more");
- 与 §9 审批的交互,第一版直接简化:**批内有需要 ask 的调用 → 整批降级
  串行**(避免 hermes start-order gate 的复杂度)。

排序建议靠后:收益是延迟不是正确性,且依赖超时(§8)、审批(§9)的
语义先定型。

## §6 工具循环/失控检测

### Conic 现状

只有 `StepLimitPlugin` 一个钝器:25 步硬停,对"第 3 步就开始原地打转"
无感知,对"第 26 步就要成功"误伤。

### 三家做法

- **openclaw**(最成体系,`src/agents/tool-loop-detection.ts`):滑动窗口
  最近 30 次调用,检测器组:`generic_repeat`(同名同参重复)/
  `argument_churn`(同名、参数抖动)/ `unknown_tool_repeat`(阈值 10)/
  `known_poll_no_progress`(轮询无进展)/ `ping_pong` /
  `global_circuit_breaker`(30)。critical 阈值 20。**两级响应**:第一次
  critical → 整批返回 blocked 结果 + 恢复引导文本("Do not repeat this
  exact tool action. Reassess…"),给模型自救机会;同一 run 第二次
  critical → `terminateRun` 硬停。另有两个专项:压缩后复发同一循环 →
  直接 abort(post-compaction loop guard);跨 attempt 存活的
  idle-timeout 成本失控熔断。
- **hermes**:`ToolGuardrails.before_call`(循环/滥用守卫,可硬停 Turn);
  iteration budget 可退款(纯 `execute_code` 步不计数);预算 90% 时往最新
  tool result 里注入一次性预警。
- **deepseek**:核心 loop 无内建检测,交给插件层(guard 包)——终止纯靠
  "无工具调用 + inbox 空"。

### Conic 落地方案

新 policy 插件 `ToolLoopDetectorPlugin`,纯事件挂载,不碰 loop:

- 挂 `before_tool_call` 记录 `(name, canonical_args_hash)` 进 per-session
  滑动窗口(30);
- 同签名重复达到阈值(如 5)→ **第一次**:挂 `tool_result` 链把该次结果
  替换成引导文本("你在重复完全相同的工具调用,结果不会变化。请换一种
  方式或说明你卡在哪里")——利用 conic 已有的结果可变换链,零新机制;
- 引导后仍再犯 → `raise AbortTurn("tool loop detected")`,Discord 侧收到
  明确的"检测到循环已停止"而不是干烧 25 步;
- 参数抖动/ping-pong 检测器后置(openclaw 的经验是 generic_repeat 拦住
  大部分案例);
- 落地后把 `StepLimitPlugin` 默认值放宽(25 → 50 或更高),让它回归
  "安全网"角色——对齐三家的终止哲学。

## §7 摘要/压缩鲁棒性

前文《上下文压缩对比(Pi compaction vs Conic Summarizer)》的问题清单与
改进优先级 1–4 **全部维持**。本节从三家再补三个 conic 真实存在的风险点
和对应工事。

### 三家的增量信息

- **hermes**(`context_compressor.py`,4,949 行,防御最重):
  - **防抖熔断**:压缩冷却期 + "结构性无操作退避"(这次压缩没减掉东西就
    退避)+ **连续 2 次无效压缩 → 熔断**,只有 provider 回报的真实
    prompt_tokens 证明压缩生效才重新武装。对应场景:保留区本身就超预算时
    "每步都压、每步无效"的死循环;
  - **真实用量托底**:`_pressure_with_real_floor`——chars 估算被最近一次
    provider 真实 prompt_tokens 托底,专防非 ASCII(中文!)2× 低估的
    死亡螺旋。这正是前文改进优先级第 4 条"把真实 usage 接回预算判断"的
    具体实现形态;
  - 压缩触发点有三处(turn 开始 preflight / 每次 API 前 / 工具结果回填后),
    且 **provider 413/上下文溢出错误也路由进压缩**(`turn_overflow.py`);
  - prompt cache 字节级保护:system prompt 按 session 持久化、逐字节还原,
    tools[] 顺序冻结,易变内容只进未持久化尾部。
- **openclaw**:
  - 触发公式 `contextTokens > 窗口 − reserve(16384)`,保留最近 20000
    **token**;切点回退到 turn 边界、绝不拆 tool call/result 对;
  - **溢出恢复链**:溢出错误 → 压缩 → **重试本次 attempt**(≤3 次)→
    降级为 tool 结果截断 → 最终报错并给 "/reset" 指引。注意这依赖 §1 的
    重试基建——"压缩后重试"没有重试机制就无从谈起;
  - tool 结果预算独立成体系:单条按窗口分级 16k/32k/64k 字符、单条 ≤30%
    窗口、合计 ≤50% 窗口;截断是**非破坏投影**(发送前变换,不改库),
    保留 spill 文件指针和错误尾部;5 分钟 cache-TTL 之外的旧 tool 结果
    可置换为占位符;
  - 摘要硬上限 `MAX_COMPACTION_SUMMARY_CHARS = 16000`,文件操作元数据和
    最近未解决的用户请求(≤800 字符)强制幸存。
- **deepseek**:压缩引擎是能力接缝(`ctx.compaction`),**压缩加持久化锁**
  (`compaction/start` 事件,防中断打坏);provider 确认的溢出永远符合
  压缩资格;**spill 包**——超长工具输出落到 spill 存储,上下文里只留
  定位符 + 取回提示。

### 对 conic 改进优先级的追加(接原列表 1–4)

5. **防抖熔断**(hermes)——conic 现状有真实死循环风险:`keep_recent=5`
   按条数保留,一条超长 bash 输出就能让保留区超预算,于是每个 step 都
   触发一次全量摘要、每次都无效。最小工事:摘要后重估 token,**没降到
   预算内就本 Turn 不再触发**(session 变量记一个 flag),并 emit
   `summarize_failed` 让人看见;
6. **溢出错误驱动压缩**(openclaw/hermes/deepseek 三家一致)——§1 的错误
   分类里把"上下文超长"单列,不重试、路由到 summarize 后重调本次请求。
   依赖 §1 先落地;
7. **工具输出 spill**(deepseek/openclaw)——conic 有 workspace 和
   read_file,天然成立:超长 bash/read 输出(如 >8k 字符)写进
   `workspace/.conic/spill/<id>.txt`,tool 结果替换为"输出过长已存至 …,
   头尾各 N 行如下,需要时用 read_file 取回"。比单纯截断保住了可取回性,
   实现是 tool_result 链上一个小插件。

## §8 工具超时

### Conic 现状

`bash` 自带 `BASH_TIMEOUT`(60s,超时 kill 返回 error)✅;**其他工具和
将来的第三方/MCP 工具没有任何超时**——一个挂住的工具 = 挂住整个 Turn
(在 §2 落地前,连中断都没有,等于挂死 session)。

### 三家做法

- **deepseek**:超时不在 loop 里——`guard/timeout-policy` 插件,协作式
  执行工具自声明的 `timeoutMs`,超时转成**普通错误 tool 结果**;
- **hermes**:批级 deadline(`timeouts.tools.*` 配置),且 deadline
  **剔除人工审批等待时间**(在等待源头计量,不算门内滞留——修过的坑:
  楔死的插件不该冻结整批计时);超时的槽位合成 `Error executing tool 'X':
  timed out after Ns` 结果,楔死线程直接放弃不 join;
- **openclaw**:核心 loop 无 per-tool 超时,工具拿 run 的 AbortSignal
  自管(exec 工具自己有超时);app 层有 lane 超时兜底。plugin **hooks**
  倒是每类都有文档化的超时 + 失败策略(fail-closed vs 放行)。

### Conic 落地方案

bus.request 没有 wrap 语义(§1 同一缺口),所以放 loop 里最直接:

- `react_loop.py` 的 `bus.request(ToolCallRequestEvent, payload)` 改为
  `asyncio.wait_for(..., timeout)`,超时捕获 `TimeoutError` → 结果 =
  `"Error: tool timed out after Ns"`,与现有异常处理路径汇合(反馈给模型,
  Turn 继续);
- 超时值来源:工具类可选类属性 `default_timeout_s`(缺省全局值,如 120s,
  环境变量可配);bash 保留自己更精细的内部超时(先到先杀);
- 已知债务记录在案:asyncio 的 wait_for 取消是协作式,真正楔死的同步
  工具(在 executor 线程里跑的)取消不掉——与 hermes"放弃不 join"同一
  现实,暂接受;
- 将来 §9 的 ask 审批实现时,**等待用户点按钮的时间要从工具计时里剔除**
  (hermes 的教训直接抄:在等待源头计量)。

## §9 审批门(Permission)

### Conic 现状

`PermissionPolicyPlugin` 是空实现,v1 全放行。前文"安全与隔离"一节已把
它列为改进优先级第 1,本节给出借鉴三家后的具体设计。

### 三家做法

- **deepseek**(失效方向设计得最严谨):`tools/pre-execute` waterfall 返回
  `allow | deny | ask | cancel`;`ask` 经可选的 approval 服务解析,
  **没有 approval 服务时 ask 降级为 deny(fail-closed)**;单调性守卫——
  监听器链"不能把 deny 翻回 allow";未知工具也走完整策略管线(策略层
  看得到每个名字);拒绝对模型可见(`Error: <reason>` 结果);
- **hermes**(层次最全):作用域封锁 → `pre_tool_call` 插件钩子(可拦可
  改参)→ guardrails(可硬停 Turn)→ 人工审批系统:pattern allowlist、
  session 级/永久级批准记忆、YOLO 模式、gateway 审批往返、**连续拒绝
  熔断器**、审批前后观察者钩子;
- **openclaw**:`beforeToolCall`/`beforeToolBatch` 策略管线 + exec 专项
  审批 `deny | allowlist | ask | auto | full`,`ask` 推送到伴生 app;多来源
  策略取**更严格者**合并。

### Conic 落地方案(三步走)

1. **静态规则**(先做,纯本地):`PermissionPolicyPlugin` 实现 per-tool
   allow/deny + bash 命令的正则 allowlist/denylist(配置文件)。**deny 不
   raise AbortTurn**——转成 tool 错误结果反馈模型让它换路(三家一致做法),
   只在连续拒绝达到阈值时才 abort(hermes 的拒绝熔断器);
2. **ask = Discord 按钮**:检查需要 ask 的调用时,channel 插件在 thread 里
   发一条带 ✅/❌ button 的审批消息(`discord.ui.View`),插件 await 一个
   `asyncio.Future` 等 gateway 回调;**超时(如 120s)默认 deny**;批准时
   提供"本次 / 本会话记住"两档(hermes 的 session approvals)。等待时间
   从 §8 的工具计时中剔除;
3. **失效方向两条铁律**(写进代码注释级别的约定):(a) ask 无人应答/无
   审批通道 = deny(deepseek);(b) 事件链单调性——conic 的 emit 链做不了
   强制,退而求其次:决策字段设计为只能收紧(payload 上 `decision` 一旦为
   deny,后续 handler 写回 allow 视为 bug,loop 侧忽略)。

依赖关系:2 需要"工具执行中途与 Discord 交互"的通路,和 §2 的中断基建
有共享部分(都要求 loop 在等待外部输入时保持响应),建议排在 §2 之后。

## §10 长任务:后台执行、Subagent 与调度

本节把"subagent / 任务委派"与"后台任务 / cron / 监控"两个专题合并:三家
的演化方向证明 **subagent 正在收敛为"长任务的一种",而不是独立机制**——
deepseek 的 one-shot 后台 subagent 字面上就走 `ctx.jobs` 注册表,与后台
shell 命令是同一个抽象的两个 producer;hermes 的委派完成事件与后台进程
完成事件汇入同一条 completion_queue、由同一个 watcher 投递。统一的任务
抽象是:**创建即返句柄 → 进注册表 → 被监控 → 完成推送回会话 → 可控制
(查 / 杀 / steer)**;后台进程、subagent、调度触发的 turn 只是这条生命
周期链上的三种 producer,差异仅在执行载体与监控探针。本节按生命周期组织。

### Conic 现状

全链条零设施。没有调度器、没有 heartbeat、没有后台工具执行、没有任何
监控信号:`bash` 同步 await 到底(靠 `BASH_TIMEOUT` 兜底),prompt 里让
模型"不要跑长期运行命令"——把长任务问题外包给模型自觉;turn 卡死无人
知晓,直到用户放弃。两个结构性缺口:**turn 只能由 `UserInputEvent` 驱动
(没有非用户来源的输入通道,cron 事件和完成通知无处注入)**;**没有活性
信号(gateway 无从判断一个 session 是在干活还是已挂死)**。

Subagent 同样为零(设计文档标为 v1 范围外),但 conic 的架构离它意外地
近——`PluginManager.start_session` 已经就是"组装一个带独立 bus/storage/
workspace 的会话",subagent 本质上是没有 channel 插件的嵌套 session。

### 三家做法

**监控哲学先记一笔**:hermes = **看门狗轰炸**(每类任务配专职守护线程);
openclaw = **事件流 + 熔断器**(监控信号并入统一唤醒/事件总线);
deepseek = **日志即监控**(一切状态是持久事件,监控 = 读日志/投影)。

#### (a) 任务从哪来:创建面与护栏

**任务创建路径与护栏**——创建面谱系:deepseek 单一面 → openclaw 三路 →
hermes 五路;三家都开放"模型给自己建任务",且都为这条路设了专门护栏:

- **deepseek(单一面)**:只有模型工具 `schedule_create/list/delete`;创建
  动作本身即 `schedule/change` 持久事件(审计免费);调度表 agent 私有。
  护栏:`every ≥ 300s` 硬下限;
- **openclaw(三路)**:① 客户端协议(gateway `CronJobWire` create/patch);
  ② 模型自建(automations 工具,heartbeat prompt 明确把周期任务往这引;
  "提醒我" = `at` + `deleteAfterRun`);③ **系统声明式投影**——heartbeat
  等系统 job 由配置对账产生(`declarationKey` 标识,配置变更时 reconcile),
  且 `payload:heartbeat` 被客户端 create/patch **拒绝**(系统专有种类);
  job 带 `owner {agentId, sessionKey, accountId}` 归属;
- **hermes(五路,护栏最重)**:① CLI `hermes cron create`(schedule 输入
  最宽:`"30m"` / `"in 30m"` / `"every monday 9am"` 自然语言 / ISO / 5 段
  cron);② Gateway HTTP API(`POST /api/jobs`,三重守卫);③ **模型工具
  `cronjob_manage`**(create/update/pause/resume/remove/run/resnap);
  ④ 编程 chokepoint `create_job_with_scheduler_registration`(三面共用);
  ⑤ 手编 jobs.json(可用但非受管)。**模型自建的护栏教科书级**:
  - **花费防护**:`model/provider/base_url/reasoning_effort` 从模型工具
    schema 里**刻意删除**——"无人值守的花费不能被 agent 指向别的模型";
    CLI/dashboard 保留;
  - **自增殖防护**:**cron 生成的 agent 默认禁用 cronjob 工具集**(防
    "job 建 job"循环增殖,需显式 `cron.allow_agent_scheduling: true`);
    cron agent 恒失去 messaging/clarify 工具集;
  - **注入防护**:prompt 威胁扫描(不可见 Unicode 硬拦 + 注入/外传正则);
    `base_url` 校验防凭证路由到攻击端点;脚本路径锁死 `~/.hermes/scripts/`;
  - **可用性诚实**:无活 ticker 时创建即警告"存了但永不触发"。

共同点:三家都**不在创建时限制 job 数量**——并发约束全部放在触发时
(tick 锁 / 全局并发 8 / maintenance 互斥)。

**Subagent 的创建面**(同属任务创建,单列):三家都是模型工具——hermes
`delegate_task`(顶层调用恒后台、嵌套恒同步,schema 上的 `background` 参数
**刻意忽略**——顶层后台保主会话响应,嵌套同步防 fan-out 失控;另有
list/steer/stop 控制动作与操作员暂停开关);openclaw `spawn_agent`
(task/label/model/超时/沙箱/cleanup 等 20+ 参数);deepseek 委派工具带
`run_in_background` 参数(默认前台,`backgroundMode: one-shot |
continuable`)。调度器触发的任务则不经工具——cron 到点直接产生
agentTurn/command(见 (b))。

#### (b) 定时调度(cron)与 heartbeat

**Schedule 种类:一条表达力谱系**——deepseek 最小(3 种,"提醒"定位)、
hermes 居中(3 种 + 重复计数)、openclaw 最大(5 种,把调度扩展成了监视)。

- **deepseek(刻意最小)**:`after`(相对延迟秒数)/ `at`(绝对 ISO 时间)
  / `every`(固定间隔,`MIN_EVERY_INTERVAL_SECONDS = 300`——**5 分钟硬
  下限**直接封死"每秒轮询"式滥用)。没有 cron 表达式是设计决定,包自述
  定位是 "durable one-shot and fixed-rate **reminders**";
- **hermes**:`{"kind":"cron","expr":<5 段>}`(懒加载 croniter,无时区
  处理)/ `{"kind":"interval","minutes":N}` / `{"kind":"once","run_at":ISO}`,
  外加重复计数 `forever | once | N`;每次触发后重算 `next_run_at`;
- **openclaw(把"时间到了"扩展到"外部条件成立了")**:

  | kind | 到期语义 |
  |---|---|
  | `at` | 一次性:epoch-ms/ISO 归一化为 UTC;`at > now` 才到期,否则返回 `undefined`(= 已烧过) |
  | `every` | **锚点网格制**:`next = anchor + (floor((now−anchor)/everyMs)+1)·everyMs`——不是"跑完再等 N",而是对齐确定性网格,宕机多久回来都落在同一相位 |
  | `cron` | Croner 库算下一次出现,解析器 LRU 缓存 512;时区 per-job,缺省**宿主本地时区**而非 UTC |
  | `on-exit` | **事件驱动**:被监视命令退出时触发;永远不"按时间到期"(`computeNextRunAtMs` 恒 `undefined`) |
  | `stream` | 按被监视命令的 stdout 批次触发(逐行或正则命中,按 `batchMs`/`maxBatchBytes` 攒批) |

  另有 job 级 `trigger` 前置条件脚本(到期 ≠ 必然执行,可再跑一段
  code-mode 脚本判断"现在该不该真的触发",返回 `{fire, message?} |
  busy | error`)。**DST 是重灾区**:折叠小时去重、偏移转换点二分查找、
  Croner 年回卷 workaround——为此写了几百行(schedule.ts:52–197),这是
  conic 方案里"别做 cron 表达式起步"的直接证据。

**处理管线(一个 job 的一生)**:

openclaw(SQLite 持久化,含 queued/running 运行态标记 + run receipts):

```
创建(配置 / 模型经 automations 工具) → SQLite 落库 → 算 nextRunAtMs
→ 定时器到期 → 可运行性检查:enabled? · queuedAt/runningAt 已置?
    (重叠→抑制,不排第二次) · trigger 脚本 · 全局并发 ≤8(超出候补)
→ 按 sessionTarget 路由:
    main      → payload 进系统事件队列;wakeMode:"now" → 立即 heartbeat
                唤醒并等完成(忙碌延迟预算 2 分钟,只有 busy/guard 延迟
                消耗预算);"next-heartbeat" → 等下一次 heartbeat tick 排空
    isolated  → 每次运行铸造全新 session(cron:<jobId>,空 transcript)
    session:x → 指定持久 session
→ payload 执行:systemEvent(注入文本)/ agentTurn(message 为 prompt 跑
    turn,可指定模型/超时)/ command(argv 子进程)/ script(headless
    脚本,默认 300s + 工具预算 50)/ heartbeat(系统专用)
→ 投递:none | announce(渠道消息;非 bestEffort 时部分投递失败 = 运行
    失败)| webhook(仅 HTTP(S));Run 与 Delivery 状态分开记账;连续
    失败达 failureAlert.after → 告警(带冷却)
→ 重排 nextRunAtMs;deleteAfterRun → 退役
```

hermes(jobs.json + 文件锁;特色:**每个 job 都是一次性的完整 agent**——
不注入现有会话,跑完即弃,绝对隔离但每次冷启动、无会话上下文):

```
60s ticker(daemon 线程,死了自动重启;心跳文件供 cron status 查 staleness)
→ tick() 拿跨进程文件锁(gateway ticker / 独立 daemon / 手动 tick 绝不重叠)
→ 前置闸门:ESTOP 暂停 · 排水闸 · 检出代码比自己新则让位
→ 到期 job:claim_job_for_fire 认领精确 occurrence 身份(带时钟偏移窗口)
    ——跨进程防双发的关键
→ run_job():构造全新 AIAgent(session cron_{job}_{ts}),存储的 prompt
    当一个 turn 跑;请求注册进 abort 通道可跨线程打断
→ 投递到配置 surface;失败进重试队列;执行/事故分别记账;
    agent teardown 推迟到投递之后(#58720)
```

deepseek(**调度表没有独立存储**——它是对 `schedule/change` 事件
(create/delete/dispatch)fold 出来的投影,重启 = 重放日志即恢复):

```
模型调 schedule_create/list/delete → 每次变更落 'schedule/change' 事件
→ ScheduleRuntime(每根 agent 一个;变更提交时、每次 status→idle 时重新驱动)
→ driveOnce():flush 持久化(durability preflight——先保证日志落盘再决策)
    → fold 出调度表 → dueDecision:最早到期的一次性 / 整批到期的 every /
      否则 armed 一段有界 setTimeout(超长等待切分多段)
→ 派发 = runMaintenance(在 idle Phase 里认领;认领之下重新 fold 重新判定):
    createUserMessage(source: plugin:schedule) → agent.followup()
    → append 'schedule/change' {operation:'dispatch'}   ← 派发本身落库
→ agent 忙(runMaintenance 同步抛)→ waitForIdle() 重试
→ every 批次按 occurrence 记 acceptedAt;损坏的调度日志使 runtime 受控
    故障,不炸 agent
```

deepseek 的三个精妙点:**派发经 maintenance Phase**(注入提醒时保证没有
turn 正在从日志推导请求,与压缩共用同一互斥);**dispatch 也是持久事件**
("这条提醒发过了"本身可重放,重启不重发);**durability preflight**
(决策前先 flush,宁可晚发不可发了没记住)。

**设计取舍对比**:

| 设计问题 | hermes | openclaw | deepseek |
|---|---|---|---|
| 表达力 vs 复杂度 | 中(cron 表达式但无时区处理) | 最大(5 种 + 时区/DST + 条件脚本),代价几百行深水区 | 最小(3 种 + 5min 下限),"提醒"定位 |
| 执行上下文 | **全新 agent**(绝对隔离,无上下文) | **可选**(main 注入 / isolated 新会话 / 指定会话) | **恒注入本 agent 会话**(提醒天然有上下文) |
| 防双发 | occurrence 认领 + 跨进程文件锁 | queuedAt/runningAt 标记 + durable 预准入回执 | dispatch 事件落库 + maintenance 互斥 |
| 错过处理(misfire) | 重算 next_run,occurrence 身份防补发风暴 | 每 job **折叠成一次**补跑,每次重启 ≤5,超出 stagger;整点 cron 默认 5min stagger 防羊群;可配 skipMissedJobs | 重放日志后 dueDecision 自然补最早到期的一次 |
| 忙碌语义 | 不适用(独立 agent) | main 会话:`wakeMode:"now"` 有 2min 忙碌预算 / `next-heartbeat` 等 tick | waitForIdle 重试,绝不并发 |
| 谁能建 | 配置/CLI | 配置 + **模型自建**(automations 工具,"提醒我" = `at` 一次性 + `deleteAfterRun`) | **模型自建**(`schedule_*` 三工具) |

**Heartbeat(周期性自主唤醒)**——只有 openclaw 有,且把它做成了**统一
唤醒总线**:heartbeat 本身是系统拥有的 cron 监视 job(`every`,默认 30m),
prompt:"看 monitor scratch 和待处理事件,无事回 `SILENT_REPLY_TOKEN`";
成本护栏:scratch 空则跳过(但有到期任务时绝不因空 scratch 跳过)、
session 忙则**丢弃不排队**、用户 turn 可抢占执行中的 heartbeat、可配便宜
模型 + `lightContext` + 静默可见性;总线角色:exec 完成、hook、cron 事件、
subagent 状态都经 `requestHeartbeat(source, intent)` 唤醒,一次唤醒 turn
排空系统事件队列,转写 prompt 标注来源(`[OpenClaw exec completion]` /
`[OpenClaw cron wake]`)。一切内部 turn 带 `InputProvenance
{kind:"internal_system", sourceTool: exec|cron|heartbeat}`,插件可按
trigger 过滤。hermes 无模型侧 heartbeat(只有基础设施活性心跳);
deepseek 无 heartbeat/守护进程。

#### (c) 任务怎么跑:执行载体(后台进程 / subagent / 调度 turn)

三家殊途同归到同一模式:**spawn 即返 + 注册表管辖 + 轮询工具仅兜底**:

- **openclaw**:exec 超过 `yieldMs`(默认 **10s**)自动转后台
  (`Promise.race`),工具结果返回 "running + sessionId + 用 process 工具
  跟进";`process` 工具 poll/log/write/kill;另有 `sessions_yield` 工具
  让模型主动让出 turn 等事件(turnHandoff 干净中止,resume 时看到持久化
  的 yield 上下文);
- **hermes**:`terminal(background=true)` 显式后台 + **redirect 时活进程
  让渡**(前台命令的 `Popen` 被 ProcessRegistry `adopt_local` 收养,半截
  输出立即返回模型);注册表:每进程一个 reader 线程 + 200KB 滚动缓冲、
  磁盘 checkpoint 跨重启恢复、`process_manage` 工具
  (list/poll/log/wait/kill/write/handoff);
- **deepseek**:`ctx.jobs` 注册表——后台 shell 即返 job id;
  `job_output(wait ≤10min)` / `job_list` / `job_kill`;非零退出码是
  completed 带详情而非 failed;system prompt 明示模型"别忙轮询,会话内
  会收到通知"。

**Subagent 作为执行载体**——与后台进程同构(spawn 即返句柄),差异在
载体与语义:

| | hermes | openclaw | deepseek |
|---|---|---|---|
| 前后台默认 | **顶层恒后台、嵌套恒同步** | 恒为独立 session/run,父 turn 继续 | one-shot 默认**前台**,`run_in_background` opt-in;continuable 天然后台 |
| 句柄语义 | `{"dispatched", delegation_id}`——句柄非结果 | `accepted + childSessionKey/runId` | `{childId, messageId}`,**子 inbox 接受时即 resolve,不等完成**——父的工具调用永不长阻塞 |
| 容量与约束 | **满员拒绝、绝不排队**(默认 3 槽)+ 深度限制 + 子独立 step 预算(默认 50) | `runTimeoutSeconds` 死线 + sweep-kill | 深度限制 + 每子唯一 Activation |
| 载体与隔离 | fork 的 `AIAgent` + git worktree 隔离 + HMAC 生命周期契约 | 独立 session/run + 沙箱选项 + swarm(多 agent) | provider 接缝:进程内 fork/spawn + **进程外 Claude Code / Codex** + ACP/SDK 后端;durable descriptor 支持跨重启冷恢复 |

调度触发的 turn 的载体已在 (b) 的处理管线中:hermes 每 job 全新 AIAgent、
openclaw 按 sessionTarget 路由、deepseek 注入本会话。

#### (d) 任务跑得如何:运行监控

**运行活性(turn/run 还活着吗)**:

- **hermes**(最重):`_touch_activity` 在每个长等待点盖时间戳 + 标签——
  API 调用开始、错误恢复、退避睡眠每 30s、串行工具 30s 心跳、并发批
  ~30s 心跳(标签精确到 "concurrent tools running (Ns, k remaining)");
  **gateway 不活动监视器**(默认 1800s)杀掉时间戳过期的 session;另有
  turn 活性 generation 计数器(与中断的 CAS 共用)和跨进程 turn-lease
  续租;
- **openclaw**:不做时间戳级监控,靠 attempt 结局分类兜底——Layer C 的
  空闲超时熔断器(连续 5 次 idle timeout 且无进展 → 停,"计费的部分
  token ≠ 进展");每 session 一个活跃 run + 全局 lane 防失控并发;
- **deepseek**:核心 loop 只有 `signal.throwIfAborted()` 和 `agent/status`
  事件——**没有墙钟看门狗**,卡死处置全部下放插件层。

**流式健康(模型响应还在动吗)**:hermes 专职监视线程盯 delta——
stale-stream 超时按 provider 定(本地 900s、云端按上下文规模缩放、
reasoning 模型有下限)+ **跨 turn 的 "stale streak" 熔断器**;openclaw 用
session 设置 `firstEventTimeoutMs`/`timeoutMs`,超时归瞬时错误走重试;
deepseek 适配器内 SSE 空闲看门狗,且流帧 `attemptId/revision/index` 三元组
让流本身可监控/可重连。

**后台任务的停滞检测**(三家差异最大):

- **hermes 停滞看门狗**:daemon 线程每 30s 扫全部后台委派,采样
  `progress_fn() → (token, in_tool)`——进度 token 冻结超 450s(空闲)/
  1200s(工具内)→ 标记 stalling 并主动打断,再 120s 宽限仍不返回 →
  强制终态 `stalled`;进程注册表的 `watch_patterns` 对输出行做子串监视,
  命中即注入 `[IMPORTANT:]`(strike 限制 + 寿命上限,超限自动降级为只报
  完成);kanban worker 的心跳是**模型驱动的**(worker 主动调
  `kanban_heartbeat` 工具续租 claim TTL,派发器回收无心跳任务但**活 PID
  绝不回收**);基础设施自监控:ticker 心跳文件、ticker 线程死掉自动重启、
  incident/投递重试计数表;
- **openclaw**:`process` poll 结果自带 `retryInMs` 退避提示(5→10→30→60s
  ——把监控频率教给模型而非硬限制);cron 有 run receipts、Run/Delivery
  双状态、failureAlert(连续失败告警 + 冷却);subagent 注册表 sweep-kill
  + `runTimeoutSeconds` 死线;
- **deepseek**:jobs 五状态(`running|stopping|completed|killed|failed`),
  settlement first-wins、按 session id 做所有权栅栏、注册项寿命长于生产者
  fiber;subagent 靠事件驱动的 Activation 注册表 watch 子 agent;父侧
  控制工具(list/interrupt/send)做主动检查。

**行为监控(任务在空转吗)**:openclaw 最成体系——滑动窗口 + **结局
哈希**驱动 6 检测器(详见 §6),压缩后守卫继续监控三重签名;hermes 走
"预警先行"——step 预算 90% 预警、run 墙钟 80% 收尾通知、guardrails 可
硬停;deepseek 核心无内建,guard 包插件化。关键经验:**判无进展要看
结果是否相同,不能只看参数**(openclaw 的结局哈希剥离易变字段)。

#### (e) 任务怎么结束:完成投递、控制与双向通信

三家一致:**完成通知是推不是拉**——注入回会话驱动新 turn(或 steer 进
活 turn),轮询工具只是兜底,模型忙轮询被 prompt 明令禁止。

- **deepseek**(投递语义最精确):**owner 空闲 → `followup()`(开新
  turn,受 `maxConsecutiveWakes=3` 唤醒预算约束,防"被唤醒的 turn 又起
  job、完成又唤醒"的自激链,用户真实消息被认领时预算重置);owner 忙 →
  `inject()`(进 next-step inbox,turn 不能在它头上闭合)**;
- **hermes**:完成事件 → 全局 `completion_queue` → gateway **2s watcher**
  协程 → 注入源会话开新 turn;durable 投递 claim 协议防双投、同会话多
  完成合并为一次注入、目标不可达重排队;
- **openclaw**:完成 → systemEvent + heartbeat 唤醒;**已被 poll 观察过
  的完成不再唤醒**(`terminalPollObserved`,防双报);
- **后台 subagent 完成回投**:hermes 顶层委派恒后台、容量
  3 **拒绝不排队**;openclaw 父忙 → **steer 进活 turn**、空闲 → 排队唤醒;
  deepseek settlement notice 同样 idle→followup / 忙→steer,子会话跨进程
  重启可冷恢复。

**Subagent 特有的完成与控制语义**:

- **完成 ≠ 请求完成**:openclaw 在完成通知里注入指令——"This completion
  ends one child run, not necessarily the original user request. Compare
  the result with the requested outcome…"——防模型把子任务完成当用户请求
  完成直接收尾,零成本提示词护栏;deepseek 的 stop-reason 词汇表还含
  `refusal`(子在 pre-step 就拒了任务);
- **双向通信**:hermes 父控子(`delegate_task(action="steer"/"stop")`),
  子的后台进程可 `handoff` 给父;deepseek 子经 `send_message` 工具
  **steer 父**(父不在线报 `PARENT_UNAVAILABLE`),父经控制工具
  list/interrupt/send;openclaw 子可向父的最终回复追加附件
  (provisional→promoted,TTL 2h);
- **父的等待模式**:被动(完成自动重入)为主;openclaw 另有
  `sessions_yield` 工具让父主动让出 turn 等子完成(无 pending 子时调用
  报错);deepseek 可 `job_output(wait)` 主动等;
- **崩溃语义分两半**:子的**状态**可恢复(会话持久 → deepseek 冷恢复:
  发消息即复活)与子的**通知**是否重投是独立决策——hermes 重投
  (durable 投递表 + restore_undelivered_completions),deepseek 刻意
  不重投(防重启风暴,父用控制工具主动发现)。

#### (f) 用户看什么:进度可见与事后审计

**进度可见**:hermes 有 spinner、每 iteration 的 `step_callback`(gateway
`agent:step` 事件)、并发批心跳标签直接可读;openclaw 的
progress-draft-compositor 渲染含工具行的滚动状态卡、`run_status
phase:"retrying"` 让用户看见重试、heartbeat 可见性三开关
(showOk/showAlerts/useIndicator);deepseek 一切皆事件,任何前端订阅即得
完整进度,无需专门通道。

**事后审计**:hermes 有 trajectory 保存、SQLite WAL/修复模块、cron
executions/incidents 表、中间件 trace;openclaw 有 run receipts、投递状态
三值化(delivered/not-delivered/unknown)、**turn-taint 溯源**(网络来源
内容的污点传播——监控的是提示注入风险);deepseek 的事件日志本身即完整
审计(每次重试/工具调用/压缩锁/inbox 变更可回放)+ 不变式检查器。

#### (g) 共同模式提炼(三家一致,可视为行业结论)

1. **完成通知是推不是拉**——注入回会话驱动新 turn,轮询工具只是兜底;
2. **忙碌语义必须显式定义**——空闲开新 turn / 忙则进队列(deepseek
   inject、openclaw 排队投递)/ heartbeat 类低价值唤醒忙时直接丢弃;
3. **防自激与防风暴是一等设计**——deepseek 唤醒预算、openclaw misfire
   折叠 + stagger + 全局并发 8、hermes 容量拒绝 + 停滞看门狗;
4. **调度与任务状态必须持久化**(SQLite / JSON+锁 / 事件日志),重启
   不丢不重;
5. **每个后台任务必须有活性判据与停滞处置**——进度 token / 结局哈希 /
   心跳续租,冻结即打断,绝不无声挂死;
6. **非用户输入全部打 provenance 标记**(openclaw `InputProvenance`、
   deepseek `source.kind`、hermes 完成事件类型),供过滤、审计、显示。

7. **Subagent 即任务**:创建即返句柄 → 注册表 → 监控 → 完成推送四环节
   与后台进程完全同构(deepseek 直接复用 jobs 注册表,hermes 汇入同一
   completion_queue);subagent 特有的只剩执行载体(嵌套 agent)、监控
   探针(进度 token/step 数而非输出字节)和控制动词(steer/stop 而非
   kill/write)。

### Conic 落地方案(七步,依赖递进)

(本节方案已展开为完整设计文档:`long-task-design.md`——统一任务模型、
DuckDB 数据模型、组件设计、关键流程与分阶段落地;下文保留纲要。)

conic 的结构基础:session = Discord thread(输出天然有归属,不需要
openclaw 的 isolated session 概念)、bus 事件驱动、DuckDB 持久化、
`Input` 拦截链已就位。

1. **活性时间戳 + gateway 级超时杀**(与 §2 中断同批,共用 SessionScope
   改造与 abort 通路,几十行):`SessionScope` 加 `last_activity_at` +
   标签,活性盖章全部挂在现有 bus 事件上(step_start、tool_execution_*、
   model 调用前后),零 loop 改动;gateway 一个巡检 asyncio task,超时
   (如 30min)的活跃 turn 走 §2 的 abort 通路——先让"卡死可发现",
   再谈后台任务;
2. **后台 bash + JobRegistry**(与 §8 工具超时同批,同在工具层):
   `BashToolPlugin` 加 `background: bool` 参数(进阶:学 openclaw 超过
   10s 自动转后台):spawn 后立即返回 `"job {id} started"`;session 级
   JobRegistry(内存 dict + DuckDB `jobs` 表存状态与输出尾部,跨重启
   可查);新工具 `job_status(id, wait_seconds?)` 供模型兜底轮询;
   **内置停滞判定**:哪怕只做"输出字节数 N 分钟不变 → 标记 stalled 并
   通知",也远好于无声挂死(hermes 的"冻结 → 打断 → 宽限 → 强制终态"
   三段式可后补);
3. **完成通知回注入**(依赖 §3 的 inbox/steering 基建):job 完成时——
   turn 进行中 → 投 session inbox(= deepseek 的 inject,下个 step 边界
   可见);空闲 → 直接构造 `UserInput` 开新 turn(= followup),文本形如
   `"[后台任务完成] job {id} exit {code}\n<输出尾部>"`。**必须带唤醒
   预算**(抄 deepseek:连续非用户唤醒 ≤3,真实用户消息重置),否则
   Discord 场景一个 watch 循环就能刷屏烧钱。同时给 `Input`/`UserInput`
   payload 加 `source` 字段(`user | job | schedule | heartbeat`)——
   provenance 现在加成本最低,将来过滤/审计/显示都靠它;
4. **Cron/提醒**(依赖 3 的注入通道):gateway 级 `SchedulerService`
   (asyncio task,60s tick)。**学 deepseek 起步**:只做 `at` / `after`
   / `every(≥5min)` 三种,躲开 cron 表达式依赖与时区/DST 深水区
   (openclaw 为 DST 写了几百行,证明那是真沼泽);DuckDB `schedules`
   表(id / kind / next_run_at / payload_text / session_key / enabled),
   重启后重算 next_run(错过的折叠成一次,学 openclaw);**派发落库**(fire 时更新 `last_fired_at` 再注入,学 deepseek 的 dispatch 事件——重启不重发;conic 单进程 asyncio 下防双发免费,防重发要靠这条);到点把 payload
   文本经注入通道投目标 session(忙则排队,不并发);模型侧一个
   `schedule` 工具(create/list/delete)——用户在 thread 里说"每天早上
   看一眼 CI"即可,这是 Discord 助手场景的杀手级小功能。**模型自建的
   三类护栏一并做**(hermes 经验):schedule 工具不暴露模型/供应商
   参数(花费防护);由 schedule 触发的 turn 里禁用 schedule 工具
   (自增殖防护,靠 第 3 步的 source 字段判定);every 下限 5min;
5. **进度可见增强**(事件消费侧小改动):`MessageUpdateEvent` 状态行
   已有雏形,补"第 N 次重试中"(依赖 §1 的 `model_retry` 事件)和
   "后台任务 N 个运行中"(依赖 2 的 JobRegistry);
6. **Heartbeat(可选,最后)**:openclaw 模式按需裁剪——per-session
   `every` 配置(默认关);到点且 session 空闲 → 注入 `[heartbeat]`
   提示词,模型无事回哨兵 token 则不发 Discord 消息;忙则跳过不排队;
   必须同时提供便宜模型选项,否则纯烧钱。注意步骤 3 的完成推送已覆盖
   大半需求,heartbeat 只补"没有明确完成事件的持续监视",优先级最低。

7. **Subagent = JobRegistry 的第二个 producer**(将来时):
   `spawn_subagent` 工具经 `PluginManager.start_session(channel="subagent",
   native_id=f"{parent_key}/sub-{n}", 无 channel 插件, workspace 用父
   workspace 的 git worktree)` 组装嵌套 session——**默认 sync**(await 子
   turn,最终 assistant 文本作为工具结果;实现简单一个量级,可先行落地)、
   `background: true` opt-in(asyncio task 驱动子 turn,注册
   `kind="subagent"` 的 job 后立即返回句柄,之后**完全复用本节链路**:
   完成投递走第 3 步、停滞监控探针 = 子 bus 的 step_start 事件(零侵入)、
   `job_status`/`job_kill` 统一生效——kill 即触发子的 §2 abort 通路);
   约束:深度 1、容量 2 满员**拒绝不排队**(hermes)、子用更紧的
   StepLimit、父 abort 传播给子、完成通知附"子任务完成 ≠ 用户请求完成"
   提示(openclaw)、**通知不重投**(deepseek——重启后子会话在 DuckDB
   完好、job 表状态可查,模型用 job_status 主动发现,比 durable 投递
   协议便宜得多);事件:`subagent_start/stop` topic,
   `source="subagent"` 进 provenance。**架构红利:subagent 不再需要任何
   专属基建**——注册表/投递/监控/控制四件套全部复用第 2/3 步,subagent
   特有的只剩"spawn 一个嵌套 session",而这正是 `start_session` 已经会
   做的;前置依赖随之收敛为"第 2/3 步就位"(其自身已依赖 §2/§4/§8)。

明确不做:**deepseek 的"日志即监控"不追**——前提是事件溯源存储,
conic 的 DuckDB 消息表做不到,改造成本远超收益;审计需求靠 source 标记
+ loguru 结构化即可。

## 各专题的建议落地顺序

按"正确性 > 可用性 > 体验 > 能力"排,并标注依赖:

| # | 专题 | 理由与依赖 |
|---|---|---|
| 1 | §4 配平修复 | 现存 bug(AbortTurn 中途批次)+ 崩溃恢复缺口;改动最小,先行合入 |
| 2 | §1 模型重试/退避 | 最大可用性洞;backend 内建,无依赖 |
| 3 | §2 用户中断 | 依赖 1(中断必须配平);顺带修 /agent_stop 排队问题;**§10 的活性时间戳 + 超时杀与此同批**(共用 SessionScope 改造与 abort 通路) |
| 4 | §3 steering/follow-up | 原节方案已就绪,依赖 3 的部分基建(inbox 与 abort 共享 SessionScope 改造) |
| 5 | §8 工具超时 | 10 行改动;在 3 之前工具挂死 = session 挂死,做完 3 后仍值得 |
| 6 | §10 后台 bash + 完成通知 | 后台 bash 与 5 同批(同在工具层);完成通知依赖 4 的 inbox;顺带给 Input/UserInput 加 source 字段(含停滞判定) |
| 7 | §6 循环检测 | 纯新增插件;落地后放宽 StepLimit |
| 8 | §7 压缩加固 | 原节优先级 1–4 + 新增 5(防抖熔断)/6(溢出驱动,依赖 2)/7(spill) |
| 9 | §9 审批门 | 安全优先级最高但实现依赖 2/3 的基建;静态规则(第 1 步)可提前 |
| 10 | §10 cron/提醒 | 依赖 6 的注入通道;`schedule` 工具是 Discord 场景高价值小功能 |
| 11 | §5 工具并行 | 纯性能;等 9 的审批语义定型后做,避免返工 |
| 12 | §10 subagent(后台模式)+ heartbeat | 将来时;subagent 复用 6 的 JobRegistry(sync 版可先行),heartbeat 依赖 10 |

其中 1、2、5 三项加起来不到一百行改动,却消掉了"session 永久卡死"的
全部三种成因(坏历史、模型调用失败、工具挂死),建议作为第一批。
