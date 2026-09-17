# Conic 长任务设计:后台执行、Subagent 与调度

基于 `improvement-with-hermes-openclaw-deepseek.md` §10 的调研结论,综合
hermes-agent / openclaw / deepseek-harness 三家实现,为 conic 设计统一的
长任务系统。机制细节的出处见 `loop-design-hermes-openclaw-deepseek.md`。

## 1. 目标与非目标

**目标**

1. 模型可以启动**后台 shell 任务**(spawn 即返),不再靠 prompt 恳求
   "不要跑长期运行命令";
2. 任务完成后**推送回会话**驱动模型继续(turn 忙则排队,闲则开新 turn),
   模型无需忙轮询;
3. 用户/模型可以创建**定时任务**("每天早上看一眼 CI"、"30 分钟后提醒我");
4. **subagent 作为长任务的一种**接入同一套设施,不建专属基建;
5. 一切任务**可监控**(活性、停滞)、**可控制**(查、杀)、**可审计**
   (provenance 标记 + 落库)。

**非目标**(明确不做,理由见 §10)

- cron 表达式与时区/DST 处理(openclaw 为 DST 写了几百行,证明是深水区;
  `at / after / every` 三种覆盖 conic 场景);
- durable 完成通知重投协议(deepseek 路线:重启后状态可查即可,通知丢了
  靠模型主动 `job_status`);
- 事件溯源存储、进程跨重启收养(进程死即死,标记而非恢复)、isolated
  cron session(conic 的 session=thread,输出天然有归属);
- heartbeat 列为可选尾项,不在本设计核心范围(完成推送已覆盖大半需求)。

## 2. 统一任务模型

**Job = 创建即返句柄 → 进注册表 → 被监控 → 完成推送回会话 → 可控制。**

三种 producer,共享同一条生命周期链:

| kind | 载体 | 监控探针 | 控制动词 |
|---|---|---|---|
| `bash` | 子进程(`asyncio.create_subprocess_shell`) | 输出字节数 + 最近输出时间 | kill(进程组) |
| `subagent` | 嵌套 session(`start_session(channel="subagent")`)驱动的 asyncio task | 子 bus 的 `step_start` 事件时间戳 | kill = 子的 abort 通路(§2 基建) |
| `schedule` | 不产生 job——到点直接注入目标 session(见 §5) | 调度器自身 tick | enable/disable/delete |

**Job 状态机**(取 deepseek 五态,合并 stopping,加 hermes 的 stalled):

```
running ──正常结束──► completed(带 exit_code;非零退出码也是 completed,
   │                   错误详情进输出——deepseek 决策:非零≠失败,模型自判)
   ├──无法启动/内部异常──► failed
   ├──job_kill / session 停止 / 父 abort 传播──► killed
   └──进度冻结超阈值──► stalled(标记 + 通知;进程不自动杀,
                          模型/用户决定是否 kill——hermes 三段式的简化版)
completed/failed/killed/stalled 为终态;终态一经写入不再变更(first-wins)。
```

## 3. 数据模型(DuckDB)

```sql
CREATE TABLE jobs (
  job_id        TEXT PRIMARY KEY,      -- j-{短uuid}
  session_key   TEXT NOT NULL,         -- 归属会话(= Discord thread)
  kind          TEXT NOT NULL,         -- bash | subagent
  status        TEXT NOT NULL,         -- running|completed|failed|killed|stalled
  spec          TEXT NOT NULL,         -- bash: 命令原文;subagent: 任务描述
  child_session_key TEXT,              -- kind=subagent 时的子会话
  exit_code     INTEGER,
  output_path   TEXT,                  -- workspace/.conic/jobs/{job_id}.log
  output_bytes  BIGINT DEFAULT 0,
  last_progress_at TIMESTAMP,          -- 停滞判定依据
  notified      BOOLEAN DEFAULT FALSE, -- 完成通知已投递
  observed      BOOLEAN DEFAULT FALSE, -- 已被 job_status 查看(防双报,学 openclaw)
  created_at    TIMESTAMP, finished_at TIMESTAMP
);

CREATE TABLE schedules (
  schedule_id   TEXT PRIMARY KEY,      -- s-{短uuid}
  session_key   TEXT NOT NULL,         -- 注入目标(= 创建它的 thread)
  kind          TEXT NOT NULL,         -- at | after | every
  next_run_at   TIMESTAMP NOT NULL,
  every_seconds INTEGER,               -- kind=every,≥300
  payload_text  TEXT NOT NULL,         -- 到点注入的 prompt 文本
  enabled       BOOLEAN DEFAULT TRUE,
  created_by    TEXT NOT NULL,         -- user | model(审计)
  last_fired_at TIMESTAMP,             -- 派发落库:先写这里再注入(防重启重发)
  created_at    TIMESTAMP
);
```

进程内运行态(不落库):`JobRegistry` 持有 `{job_id → JobHandle}`,
JobHandle = `{proc | task, output_buffer(截尾), kill(), session_key}`。
**落库的是事实,内存的是句柄**——重启后句柄消失,事实仍可查(§8 恢复)。

## 4. 组件设计

### 4.1 JobRegistry(进程级 service,gateway 持有)

- `spawn_bash(session_key, command, workspace) -> job_id`:
  `create_subprocess_shell(..., start_new_session=True)`(进程组,kill 干净),
  stdout/stderr 合流写 `output_path`(内存只留尾部 4KB);落库 running 行;
  起一个 reader task 更新 `output_bytes`/`last_progress_at`;
- `spawn_subagent(parent_key, task) -> job_id`:见 §7;
- `status(job_id, wait_seconds=0)`:返回状态 + 输出尾部;`wait>0` 时
  `asyncio.wait` 完成事件(上限 300s);查询即置 `observed=True`;
- `kill(job_id)`:bash → `killpg`;subagent → 子 session 的 abort;
- **容量**:每 session 运行中 job ≤ 3(bash)/ ≤ 2(subagent),满员
  **拒绝不排队**(hermes——排队造成模型无法推理的隐式延迟),工具返回
  明确错误让模型改串行;
- **停滞巡检**:registry 自带 60s tick——`last_progress_at` 冻结超
  10 分钟(bash,看输出)/ 5 分钟(subagent,看 step 事件)→ 置
  `stalled` + 投递通知(不自动杀)。

### 4.2 完成投递通道(依赖 §3 inbox 基建)

job 终态时(completed/failed/killed/stalled):

```
if job.observed: 仅落库,不投递(模型已经看过——openclaw terminalPollObserved)
elif session turn 进行中: 通知文本 → session inbox(deepseek inject:
     下个 step 边界作为 user 消息可见,turn 不能在它头上闭合)
elif 唤醒预算未耗尽: 构造 UserInput(source="job") → 开新 turn(followup)
else: → inbox 静默滞留,下次用户消息时一并带出(deepseek quiet)
```

- **唤醒预算**:每 session 连续非用户唤醒 ≤ 3,真实用户消息被消费时重置
  (deepseek `maxConsecutiveWakes`)——否则一个 watch 循环就能刷屏烧钱;
- **合并投递**:同 session 有多个待投递终态时合并为一条通知
  (hermes #70300);
- 通知文本模板(单条 ≤ Discord 限制,输出给尾部):

```
[后台任务完成] job {id}(bash)exit {code},耗时 {dur}
输出尾部:
{tail ≤ 1500 字符}
完整输出:{output_path}(可用 read_file 查看)
```

  subagent 完成通知**额外附一句**(openclaw 原文翻译):"子任务完成——这
  不一定意味着用户的原始请求已完成,请对照原始要求核验结果。"

### 4.3 SchedulerService(gateway 级 asyncio task)

- **60s tick**:`SELECT * FROM schedules WHERE enabled AND next_run_at <= now`;
- 每条到期:**先落库**(`last_fired_at = now`,every 则同时重算
  `next_run_at`,at/after 则 `enabled = False`)**再注入**——派发先于执行
  持久化(deepseek 决策),重启不重发;
- 注入 = payload_text 走 4.2 的同一通道(source="schedule";session 忙 →
  inbox,闲 → 开 turn;**同样受唤醒预算约束**);
- **misfire 折叠**(openclaw):重启后 `next_run_at` 已过期的 every 任务
  只补一次并向前重算网格,不按错过次数补;
- 目标 session 已 ended → `enabled=False` + 日志(不删,可查)。

### 4.4 活性监控(ActivityMonitor,依赖 §2 abort 基建)

- `SessionScope` 加 `last_activity_at` + `activity_label`;盖章**全部挂
  现有 bus 事件**(`step_start`、`tool_execution_start/end`、
  `message_delta_update` 节流后),零 loop 改动;
- gateway 巡检 task(60s):活跃 turn 的 `last_activity_at` 超 30 分钟 →
  触发该 session 的 abort(§2 通路)+ Discord 通知"任务因长时间无响应
  被中止"。这是整个设计的地基:**先让卡死可发现,再谈后台任务**。

### 4.5 模型侧工具

| 工具 | 参数 | 说明 |
|---|---|---|
| `bash` 扩展 | `background: bool = false` | true 时 spawn 即返 `"job {id} started"`;(进阶:执行超 10s 自动转后台并返回半截输出,学 openclaw yieldMs——P2 再做) |
| `job_status` | `job_id`, `wait_seconds?` | 状态 + 输出尾部;查询置 observed |
| `job_kill` | `job_id` | 杀进程组 / abort 子会话 |
| `schedule` | `action: create\|list\|delete`, `kind`, `when/every`, `text` | **三护栏**:① 不暴露模型/供应商参数(花费防护,hermes);② `source=="schedule"` 触发的 turn 里本工具从 tool_schemas 剔除(自增殖防护);③ every ≥ 300s |
| `spawn_subagent` | `task`, `background: bool = false` | 见 §7 |

### 4.6 事件与 provenance

- meta 新 topic:`job_start` / `job_end`(带 job 快照,供 channel 插件
  渲染状态行"⏳ 后台任务 ×N")、`subagent_start` / `subagent_stop`、
  `schedule_fired`;
- `Input` / `UserInput` payload 加 `source: str = "user"`
  (`user | job | schedule | subagent | heartbeat`)——**本设计的第一个
  改动就是加这个字段**,后续所有过滤(schedule 工具剔除、唤醒预算重置
  判定)、审计、显示都靠它;
- 注入的通知作为**正式 user 消息落库**(source 记在消息 JSON 里)——
  与 §3 的结论一致:进 history 的是事实,ephemeral 的才走别的通道。

## 5. 关键流程

**后台 bash 一生**:

```
模型: bash(command, background=true)
 → JobRegistry.spawn_bash:进程组启动、jobs 落库(running)、reader task
 → 工具结果即刻返回:"job j-3f2a started (后台运行,完成后会通知;
    可用 job_status 查询)" → 模型继续当前 turn
 → [进程退出] reader 收尾:exit_code / output 落库 → 终态 completed
 → 投递(4.2):忙→inbox / 闲→UserInput(source="job") / observed→静默
 → 模型在新 turn 里读到通知,决定下一步(读日志/重试/汇报用户)
```

**调度触发**:

```
tick 发现到期 → last_fired_at 落库 + next_run_at 重算
 → 注入 payload_text(source="schedule")
 → 该 turn 的 tool_schemas 剔除 schedule 工具(自增殖防护)
 → 模型执行(可以起后台 job——但完成唤醒计入预算)
```

**重启恢复**(§4 配平的同族逻辑):

```
gateway 启动 → jobs 表扫描 status='running' 的行:
   进程/task 已不存在 → 置 killed(detail="进程随重启终止"),notified=False
 → 各 session 首个 turn 前,未 notified 的终态 job 照常走投递通道
 → schedules 表:misfire 折叠(4.3)
```

## 6. 与现有系统的交互

| 依赖 | 关系 |
|---|---|
| §2 中断 | job_kill(subagent)与活性超时杀都走 abort 通路;父 turn abort 时**传播给运行中的子 subagent**(bash job 不随 turn abort 终止——它本来就是为了活过 turn) |
| §3 steering/inbox | 完成通知与调度注入复用 inbox;通知与用户插话在 inbox 内按到达序排队 |
| §4 配平 | 重启恢复扫描与 transcript 配平同批实现,风格一致(合成事实,不伪造成功) |
| §8 工具超时 | `background=false` 的 bash 维持原超时;background 路径不受工具超时约束(受停滞巡检约束) |
| §9 审批门 | `bash(background=true)` 过同一套命令 allowlist;`schedule create` 的 payload 文本过与 cron prompt 同级的检查(hermes 威胁扫描可后补,先靠审批门) |
| §7 压缩 | 通知是普通 user 消息,参与摘要——无特殊处理;超长输出天然在文件里(spill 思路的免费实现) |
| Discord 渲染 | `job_start/end` 事件 → 状态行"⏳ 后台任务 ×2";通知消息走正常发送路径(2000 限制由通知模板自身保证) |

## 7. Subagent(JobRegistry 的第二个 producer)

- **sync 模式(默认,先落地)**:`spawn_subagent(task)` →
  `start_session(channel="subagent", native_id=f"{parent_key}/sub-{n}",
  无 channel 插件, workspace=父 workspace 的 git worktree)` → 向子 bus
  emit `UserInputEvent` 并 await → 子的最终 `AssistantMessage` 文本作为
  工具结果返回 → `stop_session`。父 turn 阻塞期间父的活性由子的事件
  代理盖章(子 `step_start` → 父 `last_activity_at`);
- **background 模式(`background=true`)**:同上构造,但由
  `asyncio.create_task` 驱动,注册 `kind="subagent"` 的 job 后立即返回
  句柄;完成/停滞/控制全部走 §4 的既有链路;
- **约束**:深度 1(子 session 里 `spawn_subagent` 不注册);子用更紧的
  StepLimit(如 15);容量 2 满员拒绝;父 abort 传播给子;子的工具集
  剔除 `schedule`(与 source 防护同理);
- **不做**(相对三家):子→父 mid-run steer(deepseek send_message)、
  父的 sessions_yield 等待、子会话冷恢复复用——都等真实需求出现再说。

## 8. 分阶段落地

| 阶段 | 内容 | 依赖 |
|---|---|---|
| P0 | `source` 字段 + 活性时间戳 + gateway 超时杀 | §2 abort 通路 |
| P1 | JobRegistry + `bash(background)` + `job_status`/`job_kill` + jobs 表 + 停滞巡检 | §8(同在工具层,同批做) |
| P2 | 完成投递通道(inbox/唤醒预算/合并/observed 防双报)+ 重启恢复扫描 + Discord 状态行 | §3 inbox |
| P3 | SchedulerService + schedules 表 + `schedule` 工具(三护栏) | P2 的注入通道 |
| P4 | `spawn_subagent` sync 版 → background 版 | P1–P2;worktree 基建(会话分支一节) |
| P5(可选) | heartbeat(per-session every,空闲才注入,哨兵 token 静默,便宜模型) | P3 |

每一阶段独立可交付;P0+P1 合计约两百行,已消掉"prompt 求模型别跑长命令"
和"卡死无人知晓"两个最疼的问题。

## 9. 遗留决策记录

- **非零退出码 = completed**(deepseek):失败与否由模型看输出判断,
  框架不越权定性;
- **stalled 不自动杀**:标记 + 通知,处置权给模型/用户(hermes 自动打断
  的三段式适合无人值守场景,conic 有人在 thread 里,通知即可);
- **通知不重投**(deepseek):重启后状态可查,重投协议不值得;
- **满员拒绝不排队**(hermes):显式错误可被模型推理,隐式排队不能;
- **调度不做 isolated session**:conic 的 session=thread,注入目标 thread
  即天然隔离与归属。

## 10. 可选功能备注(三家有、核心设计未收,按需启用)

核心设计是三家功能的最小交集;本节是**有意留在门外**的增强项,每项给出
详细解释、出处(框架内文件路径)、conic 的具体实现方案与优先级。优先级
分四档:**A** = 随核心阶段顺手做(成本极低、收益直接);**B** = 第一批
增强(明确信号出现即做,已想清实现);**C** = 需求驱动(等场景压出来);
**D** = 远期/不建议。末尾附总表。

### 10.1 调度增强

#### 10.1.1 事件触发调度(`on-exit` / `stream`)——优先级 C

- **解释**:schedule 不按时间触发,而按外部条件:`on-exit` 在被监视命令
  退出时 fire,`stream` 按被监视命令的 stdout 批次 fire(逐行或正则命中,
  按 `batchMs`/`maxBatchBytes` 攒批)。本质是把调度器扩展成进程监视器。
- **出处**:openclaw `packages/gateway-protocol/src/schema/cron.ts:166-204`
  (wire schema)、`src/cron/schedule.ts`(`on-exit` 的 `computeNextRunAtMs`
  恒返回 `undefined`——永不按时间到期)。
- **conic 实现**:不需要独立机制——`on-exit` = JobRegistry 在 job 终态时
  查 `schedules` 表 `kind='on-exit' AND spec 匹配` 的行并注入;`stream` 与
  10.2.3 的 watch_patterns 是同一实现(reader task 行匹配),二选一即可。
- **信号**:出现"盯着某进程/日志流"的需求;先用 watch 后台 job 顶。

#### 10.1.2 trigger 前置条件脚本——优先级 C

- **解释**:到期 ≠ 必然执行。job 可挂一段前置脚本,到期先跑它,返回
  `{fire: bool, message?} | busy | error` 决定是否真正触发——把"到点但
  无事可做"的空跑成本挡在 LLM 调用之前。
- **出处**:openclaw `src/cron/trigger-script.ts:551`(code-mode 脚本,
  含 busy 语义)。
- **conic 实现**:`schedules` 加 `trigger_command TEXT` 列;tick 时先
  `create_subprocess_shell(trigger_command)`,exit 0 → fire、非 0 → 跳过
  并照常重算 next_run。shell 命令比 openclaw 的脚本引擎简单一个量级。
- **信号**:schedule 数量多、空跑 token 成本可见。

#### 10.1.3 自然语言 schedule 解析——优先级 C

- **解释**:创建接口直接接受 `"every monday 9am"`、`"in 30m"` 等自然语言。
- **出处**:hermes `cron/jobs.py:758-836`(schedule 解析:interval 短语 /
  一次性短语 / ISO / 5 段 cron / weekday+time 自然语言)。
- **conic 实现**:模型侧**天然已覆盖**——用户对模型说人话,模型转成
  `schedule(kind, when, every)` 参数,不需要框架解析;只有做 CLI/配置面
  时才需要(届时用 `dateparser` 库,不手写)。
- **信号**:conic 增加非模型的创建面(CLI/HTTP)时。

#### 10.1.4 cron 表达式 + 时区——优先级 C

- **解释**:5 段 cron 表达式与 per-job 时区,表达"工作日早九"类周期。
- **出处**:hermes `cron/jobs.py:763-832`(croniter,无时区处理);
  openclaw `src/cron/schedule.ts:16-197`(Croner + per-job tz + 折叠小时
  去重 + 偏移转换二分 + Croner 年回卷 workaround——DST 深水区证据)。
- **conic 实现**:`schedules` 加 `kind='cron'` + `expr` + `tz` 列,用
  `croniter` + `zoneinfo` 算 next_run;**引入即接受 DST 边界成本**(至少
  要处理回拨重复触发:fire 前查 `last_fired_at` 距今 < 间隔下限则跳过)。
- **信号**:`every` 表达不了的周期需求真实出现(注意:"每天早九" 可用
  `every 86400 + 锚点`凑合,先别上 cron)。

#### 10.1.5 pacing / stagger(防羊群)——优先级 B

- **解释**:同刻到期的多个任务错峰执行;整点任务默认加随机延迟——防止
  "每小时整点十个 job 齐射"打爆并发与限流。
- **出处**:openclaw `src/cron/stagger.ts:11,64`(整点 cron 默认
  `staggerMs = 5min`)、`pacing.ts`。
- **conic 实现**:tick 发现同刻 due > 1 条时,注入间隔随机 0–60s 排开
  (asyncio.sleep 即可,~10 行);唤醒预算天然是第二道闸。
- **信号**:schedule 数量超过十几条、出现同刻齐射。

#### 10.1.6 wakeMode(立即唤醒 vs 攒批)——优先级 C

- **解释**:per-job 选择到点行为:`now` = 立即唤醒目标会话(带忙碌等待
  预算,openclaw 为 2 分钟);`next-heartbeat` = 事件入队,攒到下次
  heartbeat tick 一起处理——低价值提醒不单独打扰。
- **出处**:openclaw `src/cron/types.ts:41`、
  `service/timer-execution.ts:268-284`(2min busy-deferral budget)。
- **conic 实现**:`schedules` 加 `wake_mode: now|quiet` 列;`quiet` 到点
  只进 inbox 不开 turn(复用 4.2 的静默滞留分支,~5 行)。
- **信号**:做了 P5 heartbeat,或通知打扰成为用户抱怨。

#### 10.1.7 job 链与延续(`context_from` / `continuity`)——优先级 C

- **解释**:`continuity` = 本次运行注入自己上次运行的输出("每日报告要
  对比昨天");`context_from` = 读其他 job 的输出文档(job 流水线)。
- **出处**:hermes `tools/cronjob_tools.py:1001`(CRONJOB_SCHEMA 的
  `context_from`/`continuity` 参数)+ 输出留档
  `~/.hermes/cron/output/{job_id}/{ts}.md`。
- **conic 实现**:`continuity` 在 conic **接近免费**——schedule 注入的是
  同一个 thread,上次运行的对话就在历史里(被摘要压缩前);`context_from`
  等价于"payload 里让模型 read_file 另一个 job 的 output_path",提示词层
  即可,不需要框架支持。
- **信号**:跨 thread 的 job 流水线需求(罕见)。

#### 10.1.8 变化检测门控(`monitor_script` / `monitor_url`)——优先级 B

- **解释**:高频监视类 job 的省钱开关:先跑监视命令/抓 URL,内容与上次
  **哈希相同则不起 agent run**——"没变化就不花 token"。
- **出处**:hermes `cronjob_tools.py`(`monitor` 参数,URL 或脚本路径,
  变化检测门控 agent 运行;`monitor` 与 `no_agent` 互斥)。
- **conic 实现**:10.1.2 trigger_command 的特例:`schedules` 加
  `monitor_command` + `last_monitor_hash` 列,tick 时跑命令、输出 sha256
  与上次比较,相同 → 跳过(不更新 last_fired_at 语义,单独记
  `last_checked_at`)。~30 行。
- **信号**:出现"每 10 分钟看一眼 X 有没有更新"类 schedule。

### 10.2 执行与监控增强

#### 10.2.1 yieldMs 自动转后台——优先级 B(P2 已列为进阶)

- **解释**:前台 exec 超过阈值(默认 10s)自动让渡为后台 job:工具结果
  立即返回"running + job id + 半截输出",模型不被长命令卡住——**模型
  忘记传 `background=true` 时的兜底**。
- **出处**:openclaw `src/agents/bash-tools.schemas.ts:25-77`(`yieldMs`
  默认 10000)、`bash-tools.exec-run.ts:654-723`
  (`Promise.race([run settled, backgrounded])` + `markBackgrounded`)。
- **conic 实现**:bash 插件里 `asyncio.wait_for(proc.wait(), 10)` 超时 →
  `JobRegistry.adopt(proc, 已读输出)` 注册为 job,返回
  `"命令已转后台运行(job {id}),已有输出:\n{半截输出}"`。~25 行,
  依赖 P1 的 registry。
- **信号**:P1 上线后观察——模型是否频繁忘传 background。

#### 10.2.2 活进程让渡(中断时收养而非杀死)——优先级 C

- **解释**:用户中断 turn 时,正在跑的前台命令**不被杀**,活进程被注册表
  收养转为后台 job,半截输出保留——中断"等命令的 turn"不再连坐命令本身。
- **出处**:hermes `tools/process_registry.py`(`adopt_local`)+
  `interrupt_control.py`(redirect 期间 `request_yield(tid)` →
  `yield_to_background_handler`,`_YIELDED_NOTE` 返回模型)。
- **conic 实现**:§2 落地后,bash 插件在 `except asyncio.CancelledError`
  分支里不 kill 而是 `JobRegistry.adopt(proc)`(需 abort 语义区分
  "用户中断 turn" 与 "kill 该工具");~20 行但语义要想清。
- **信号**:用户反馈"中断把我跑一半的构建杀了"。

#### 10.2.3 watch_patterns 输出监视——优先级 B

- **解释**:对后台进程的输出行做子串/正则监视,命中即向会话注入
  `[IMPORTANT:] ...` 通知——"跑着长任务等某行日志出现"的精准推送,比
  轮询/heartbeat 便宜得多。带 strike 限制与寿命上限,超限自动降级为
  只报完成(防刷屏)。
- **出处**:hermes `tools/process_registry.py`(`watch_patterns`,
  strike-limited、lifetime-capped、降级为 notify_on_complete)。
- **conic 实现**:`spawn_bash` 加 `watch: list[str]` 参数;reader task 里
  逐行匹配 → 走 4.2 投递通道(**计入唤醒预算**);每 pattern 命中 ≤3 次
  后停止监视。~40 行。
- **信号**:出现"等编译出 ERROR 就叫我"类用法。

#### 10.2.4 停滞三段式自动处置——优先级 C

- **解释**:核心设计只做"stalled 标记 + 通知";hermes 的完整版是
  stalling 标记 → 主动打断 → 宽限期 → 强制终态,且进度探针分两档阈值
  (空闲 450s / 工具内 1200s——"没动静"和"在认真跑长命令"是两回事)。
- **出处**:hermes `tools/async_delegation.py:775-860`
  (`_STALE_CHECK_INTERVAL=30s`、`_STALE_IDLE_SECONDS=450`、
  `_STALE_IN_TOOL_SECONDS=1200`、`_STALL_GRACE_SECONDS=120`)。
- **conic 实现**:巡检里 stalled 后再冻结 5 分钟 → 自动 `kill(job_id)`
  (配置开关,默认关);subagent 探针分档:子在工具执行中(有
  tool_execution_start 无 end)用长阈值。~20 行。
- **信号**:无人值守场景(conic 有人在 thread,通知已够——§9 已记录
  此取舍,翻案条件即"开始无人值守运行")。

#### 10.2.5 进程 checkpoint 跨重启收养 + 资源帽——优先级 C

- **解释**:后台进程状态落盘,gateway 重启后重新接管(而非标记死亡);
  可选 systemd user scope 包裹加内存上限。
- **出处**:hermes `tools/process_registry.py`(`_write_checkpoint` /
  restore、systemd scope 包装)。
- **conic 实现**:jobs 表已存 `pid` 与 `output_path`,且
  `start_new_session=True` 使进程不随 gateway 死——重启扫描 running 行:
  `os.kill(pid, 0)` 存活 → 重新 tail 日志文件 + pid 轮询(拿不到
  exit_code,进程消失时标记 completed(exit_code=NULL));死 → 标记
  killed。**半功能约 30 行**(收养监视),全功能(资源帽)用
  `systemd-run --user --scope -p MemoryMax=` 前缀。
- **信号**:后台任务普遍长于 gateway 生命周期(如通宵构建)。

#### 10.2.6 poll 退避提示——优先级 A(P1 顺手)

- **解释**:`job_status` 的返回值里附"建议 N 秒后再查"(5→10→30→60
  递增),把轮询频率**教给模型**而不是硬限制——防模型忙轮询烧步数。
- **出处**:openclaw `src/agents/command-poll-backoff.ts:10-11`
  (backoff 表 + `MAX_POLL_WAIT_MS`)。
- **conic 实现**:JobHandle 记 `poll_count`,`job_status` 返回文本尾加
  `"(仍在运行,建议 {backoff[poll_count]}s 后再查,或等完成通知)"`。
  **3 行**。
- **信号**:无需信号,P1 直接带上。

#### 10.2.7 空输出成功静默——优先级 A(P2 顺手)

- **解释**:exit 0 且零输出的后台命令完成不值得一次唤醒。
- **出处**:openclaw `bash-tools.exec-runtime.ts`
  (`notifyOnExitEmptySuccess` 抑制)。
- **conic 实现**:投递前 `if exit_code == 0 and output_bytes == 0:
  observed = True`(走静默分支)。**2 行**。
- **信号**:无需信号,P2 直接带上。

### 10.3 投递与通信增强

#### 10.3.1 durable 投递 claim 协议——优先级 C(大部分已隐式覆盖)

- **解释**:完成事件落投递表,消费者以 claim/release/defer 协议认领,
  防多消费者双投;重启后未送达的重投。
- **出处**:hermes `gateway/run_notifications.py:1653`
  (`claim_event_delivery`、`restore_undelivered_completions`)。
- **conic 实现**:**轻量版已在核心设计里**——`jobs.notified` 标记 +
  §5 重启扫描"未 notified 的终态照常投递"就是单消费者版重投;hermes 的
  增量是多消费者 claim 协议,conic 单进程 asyncio **不需要**。
- **信号**:conic 变成多进程/多 gateway 时(远期)。

#### 10.3.2 跨渠道投递 / webhook / 失败告警——优先级 C

- **解释**:任务结果投到别的渠道(另一个 thread/频道)或 HTTP 端点;
  schedule 连续失败 N 次触发告警(带冷却期防告警风暴)。
- **出处**:openclaw `src/cron/delivery.ts:93-138`(announce/webhook,
  非 bestEffort 时部分投递失败 = 运行失败)、`webhook-url.ts:4`(仅
  HTTP(S)、拒绝 userinfo URL)、`service/failure-alerts.ts`
  (`failureAlert {after, cooldownMs}`)。
- **conic 实现**:`schedules` 加 `deliver_to`(thread id)列,注入后把
  turn 最终回复转发目标 thread;失败告警 = `schedules` 加
  `consecutive_failures` 计数,注入的 turn 以 error 结束时 +1,达 3 →
  发一条告警消息 + 冷却 1h。webhook 暂缓(引入出站 HTTP 的安全面)。
- **信号**:schedule 用于"监控并报警"类场景。

#### 10.3.3 sessions_yield(模型主动让出 turn 等事件)——优先级 B

- **解释**:模型起了后台任务后无事可做时,主动调用 yield 工具干净地
  结束当前 turn("我在等 job X,有结果叫我"),而不是干耗步数或提前
  给出半截答案。resume 时模型看到持久化的等待上下文。
- **出处**:openclaw `src/agents/tools/sessions-yield-tool.ts:35-87`、
  `run/attempt-sessions-yield.ts`(`SESSIONS_YIELD_ABORT_REASON`
  带 `turnHandoff:true`——干净中止不注入"工具可能部分执行"提示;
  `stripSessionsYieldArtifacts` 保证 transcript 以非 assistant 角色结尾)。
- **conic 实现**:`yield_turn(reason)` 工具 → raise `AbortTurn` 的特殊
  子类 `TurnYield` → loop 捕获后:不发 error 事件、正常 `turn_end`、
  历史追加一行 `[等待中: {reason}]`(source="system");有 pending job
  才允许调用(否则报错,学 openclaw 的 `claimYield` 检查)。~30 行,
  依赖 §2 的 abort 语义分类。
- **信号**:观察到模型起后台任务后原地空转步数。

#### 10.3.4 子 → 父 mid-run 汇报——优先级 B

- **解释**:后台 subagent 运行中途向父会话推进度/求决策,而不是只有
  终局一次通信。
- **出处**:deepseek `packages/subagent/subagent/src/continuation.ts`
  (子经 `send_message` 工具 → `sendToParent` → 父忙 steer / 报
  `PARENT_UNAVAILABLE`)。
- **conic 实现**:子 session 的工具集加 `report_progress(text)` →
  文本走 4.2 投递通道(source="subagent",计入唤醒预算);父侧收到
  即普通 user 消息。~15 行,P4 的自然扩展。
- **信号**:后台 subagent 跑 10 分钟以上的任务成为常态。

#### 10.3.5 子向父回复追加附件——优先级 D

- **解释**:子完成后把一段文本附加到父的**最终回复**上呈现(带
  provisional→promoted 生命周期与 2h TTL)——父子结果合并展示。
- **出处**:openclaw `src/agents/subagents/requester-final-attachment.ts`。
- **conic 实现**:Discord 场景下父直接引用子结果转述即可,合并呈现的
  收益小;不建议做。

#### 10.3.6 显式 quiet 投递配置——优先级 A(P2 顺手)

- **解释**:per-job 声明"完成不唤醒,只落 inbox 等下次带出"——模型自己
  知道哪些任务不值得打扰。
- **出处**:deepseek `packages/jobs/tool-jobs`
  (`completionDelivery: wakeup | quiet`)。
- **conic 实现**:`bash`/`spawn_subagent` 加 `notify: "wake"|"quiet"="wake"`
  参数,quiet → 终态直接走静默分支。**~5 行**。

### 10.4 Subagent 增强

#### 10.4.1 continuable 子会话 + 冷恢复——优先级 B

- **解释**:子任务完成后子会话不销毁;父(或用户)之后可继续给子发
  消息续作,进程重启后"发消息即复活"(durable descriptor 冷恢复)。
- **出处**:deepseek `packages/subagent/subagent/src/continuation.ts:102`
  (`startContinuable`)、`:401`(`coldResume`)、父日志里的 catalog 条目。
- **conic 实现**:conic 的 DuckDB session **天然支持一半**——子 session
  的历史/变量本就持久。改动:subagent job 完成后不 `stop_session`(留
  active);`spawn_subagent(resume=job_id)` 查 jobs 表拿 child_session_key
  → `start_session` 走 resume 路径 → emit 新 UserInput。~25 行。
- **信号**:出现"让刚才那个子任务在结果上再改一版"的用法。

#### 10.4.2 进程外 subagent 后端——优先级 C(但有免费 hack)

- **解释**:子 agent 可以是外部 CLI 进程(Claude Code / Codex),借用
  外部 agent 的工具生态与模型。
- **出处**:deepseek `packages/subagent`(provider 接缝:进程内
  fork/spawn + 进程外 CC/Codex + ACP/SDK 后端)。
- **conic 实现**:**免费 hack 已经存在**——P1 之后
  `bash(background=true, command="claude -p '<task>' ...")` 就是进程外
  subagent(输出进日志、完成有通知);正式版(结构化结果、双向通信)
  等 hack 用出真实需求再做。
- **信号**:hack 版用法频繁出现。

#### 10.4.3 refusal 终态——优先级 D

- **解释**:子在第一个 step 前拒绝任务是独立终态,父可区分"拒了"
  (改写重派)与"失败了"(排查)。
- **出处**:deepseek settlement notice 的 stop-reason 词汇表
  (`completed / aborted / max-tokens / refusal / error`)。
- **conic 实现**:完成通知已含子的最终文本,父模型自判足够;不单列终态。

#### 10.4.4 ESTOP 全局暂停——优先级 A(P3 顺手做)

- **解释**:一键暂停所有自动行为(调度 tick、新 job spawn、subagent
  spawn),出事故时"先停血再排查"。
- **出处**:hermes `hermes pause` ESTOP(cron `scheduler.py` tick 闸门)+
  `is_spawn_paused()` 委派暂停开关(TUI `p` / RPC)。
- **conic 实现**:gateway 全局 `paused: bool` + Discord `/agent_pause`
  `/agent_resume` 命令(限 owner):SchedulerService tick 直接 return、
  JobRegistry spawn 返回"系统已暂停"。**~20 行**,自动化系统的保险丝,
  强烈建议 P3 上线前就位。

#### 10.4.5 swarm / kanban 任务板编排——优先级 D

- **解释**:多 agent 协作:任务板、认领、TTL 心跳续租、自动分解、
  独立 worker 进程。
- **出处**:hermes `hermes_cli/kanban_db_dispatch.py`(claim/心跳/回收)、
  openclaw `src/agents/subagents/`(swarm 目录)。
- **conic 实现**:远期;单 subagent 模式吃透之后再议。

### 10.5 安全与运维增强

#### 10.5.1 schedule payload 威胁扫描——优先级 B(对外开放前必做)

- **解释**:无人值守的 prompt 是注入的高价值目标(没有用户盯着)。创建
  时扫描:不可见 Unicode 硬拦(ZWJ emoji 豁免)+ 注入/数据外传正则集。
- **出处**:hermes `tools/cronjob_prompt_scan.py`
  (`_CRON_THREAT_PATTERNS`、`_CRON_EXFIL_COMMAND_PATTERNS`;另有
  `_validate_cron_base_url` 防凭证路由劫持、脚本路径锁死)。
- **conic 实现**:纯函数 `scan_schedule_payload(text) -> reason | None`
  (~50 行正则 + unicodedata 类别检查),`schedule create` 时拒绝并回显
  原因。
- **信号**:conic 的 Discord server 有非管理员成员时(= 对外开放即做)。

#### 10.5.2 任务输出留档——优先级 A(已基本覆盖)

- **解释**:每次运行的输出持久留档可回溯。
- **出处**:hermes `~/.hermes/cron/output/{job_id}/{ts}.md`。
- **conic 实现**:bash job 的 `output_path` 已覆盖;schedule 触发的 turn
  本身在 thread 里,天然留档。唯一补充:`output_path` 文件**不自动清理**,
  给 `agent_stop` 加一步归档/清理即可。
- **信号**:无需信号,清理逻辑随 P1 带上。

#### 10.5.3 运行回执双状态记账——优先级 A(P3 顺手)

- **解释**:"跑没跑"(Run 状态)与"送没送到"(Delivery 状态)分开
  持久化——事后能回答"为什么我没收到提醒":是没触发,还是触发了没送到。
- **出处**:openclaw `src/cron/store/run-receipt-store.ts`、
  `completion-status.ts`(Run/Delivery/Completion 三值分离)。
- **conic 实现**:`schedule_runs` 子表
  `(schedule_id, fired_at, injected BOOLEAN, turn_error TEXT)`——tick
  注入时写一行,注入失败也写。**~15 行**,排查提醒问题的第一手证据。

#### 10.5.4 后台 review fork——优先级 D(正交)

- **解释**:每 turn 结束后台起一个廉价 fork 重放 transcript,审视"有无
  技能/记忆值得保存",复用父凭证打同一前缀 cache,受聚合 token 预算约束。
- **出处**:hermes `agent/background_review.py` + `run_agent.py:740-838`
  (defer-to-idle、结构化克隆、工具白名单、预算)。
- **conic 实现**:与长任务正交,属于将来的记忆系统;届时它会是
  JobRegistry 的第三种 producer(kind="review"),基建已就绪。

### 10.6 优先级总表

| 档 | 项 | 触发条件 |
|---|---|---|
| **A(随核心阶段顺手)** | 10.2.6 poll 退避提示(P1)· 10.5.2 输出清理(P1)· 10.2.7 空输出静默(P2)· 10.3.6 quiet 参数(P2)· 10.4.4 **ESTOP**(P3 前)· 10.5.3 运行回执(P3) | 无需信号,合计 <50 行 |
| **B(信号即做)** | 10.2.1 自动转后台 · 10.2.3 watch_patterns · 10.3.3 sessions_yield · 10.3.4 子→父汇报 · 10.4.1 continuable 冷恢复 · 10.5.1 威胁扫描 · 10.1.5 stagger · 10.1.8 变化检测门控 | 各自条目内注明的信号 |
| **C(需求驱动)** | 10.1.1 事件触发 · 10.1.2 trigger 脚本 · 10.1.3 NL 解析 · 10.1.4 cron+时区 · 10.1.6 wakeMode · 10.1.7 job 链 · 10.2.2 活进程让渡 · 10.2.4 停滞自动处置 · 10.2.5 进程收养 · 10.3.1 claim 协议(多进程才需要)· 10.3.2 跨渠道/告警 · 10.4.2 进程外后端(先用 hack) | 等场景压出来,按条目取用 |
| **D(远期/不建议)** | 10.3.5 回复附件(不建议)· 10.4.3 refusal 终态(不建议)· 10.4.5 swarm/kanban(远期)· 10.5.4 review fork(正交,归记忆系统) | — |

一条元备注:A 档六项合计不足五十行,却补齐了"防忙轮询、防打扰、能急停、
能审计"四个运维基本面——它们不是增强,是核心设计漏掉的收尾,应视为
P1–P3 验收标准的一部分。其余 24 项保持"场景压出需求再取用"的原则不变。
