# Improvement: 与 Pi 的功能对比及改进方向

对比对象:[earendil-works/pi](https://github.com/earendil-works/pi)(TypeScript monorepo,
终端编码 agent 工具包:统一 LLM API + agent 运行时 + TUI + 编码 agent CLI)。
Conic 是 Python 的 Discord-thread agent 引擎,两者形态不同,部分差异属于定位差异而非欠账。

# 功能对比总览

## 模型层

| Pi | Conic 现状 |
|---|---|
| 统一多 provider LLM API(OpenAI / Anthropic / Google / 自定义 provider / OAuth 订阅登录 / llama.cpp 本地模型) | 只有 OpenRouter 一个后端(`plugins/backends/openrouter.py`),靠 OpenRouter 间接多模型 |
| 流式输出(message_start/update/end 事件流) | 完全非流式,单次阻塞 completion |
| 运行时切换模型、thinking level 选择 | 模型仅由环境变量 `OPENROUTER_MODEL` 固定 |

## 工具与扩展

- **工具集**:Pi 支持自定义工具注册(schema + 执行逻辑 + 自定义渲染 + 进度流式更新)、
  覆盖内置工具;Conic 只有 bash / read_file / write_file / edit_file 四个内置工具,
  且插件在 `build_plugin_set()` 中静态硬编码,无动态发现/加载机制。
- **扩展系统**:Pi 的扩展可挂 30+ 生命周期事件(详见下文事件对比);Conic 的 MessageBus
  事件拓扑与之相似(这是两者架构上最接近的地方),但缺 provider 层和 input 层拦截点,
  且没有第三方扩展的加载入口。
- **Skills**:Pi 有完整的 SKILL.md 渐进式加载体系(全局 / 项目 / npm 包多来源发现);Conic 无。
- **Prompt 模板 / 自定义命令**:Pi 支持用户自定义 prompt 模板和斜杠命令;Conic 只有
  `/agent_start`、`/agent_stop` 两个固定 Discord 命令。

## 会话管理(差距最大的一块)

Conic 有 DuckDB 持久化 + 重启恢复 + 摘要压缩;Pi 在此之上还有:

- **会话分支/树**:每条 entry 有 `id`/`parentId`,`/tree` 可视化导航、从任意历史点继续、
  `/fork` / `/clone` 派生新会话;
- **废弃分支自动摘要**(branch summaries);
- **无损压缩**:compaction 是追加的新 entry,原始历史都在;Conic 的 Summarizer 摘要后
  旧消息不再进上下文,不可回退;
- **会话导出/分享**(HTML 导出、gist 上传、HF 数据集分享)、**命名与检索**、
  **ephemeral 模式**(`--no-session`)。

Conic 的历史是线性 append-only:无分支、无导出、无重试/回退某一轮的能力。
(持久化格式详情:Pi 用追加式 JSONL 事件日志,一切状态变化——消息、压缩、模型切换、
扩展状态——都是日志 entry,重放即还原;Conic 用 DuckDB 两张表按 `seq` 线性存消息。)

## 安全与隔离

- Pi 明确"无内置权限系统",但提供三种容器化方案文档(Gondolin micro-VM、Docker、
  OpenShell)+ 项目信任(project trust)机制。
- Conic 的 `PermissionPolicyPlugin` 是空实现(v1 全部放行);文件工具有 workspace
  路径沙箱,但 bash 只设 cwd、可自由逃逸。以 Conic"给 bot 挂真实本地工具"的场景,
  这块比 Pi 更需要补——至少实现权限门控或跑在容器里。

## 接口形态

- Pi 有 SDK 嵌入、RPC 模式(stdin/stdout JSONL)、JSON 事件流模式,以及
  client / server / protocol / session-backends 包(远程会话);Conic 只能作为
  Discord bot 整体启动,无 CLI / HTTP 通道(设计文档 §14 列为预留项)。
- Pi 有 TUI、主题、快捷键绑定——终端形态专属,Conic 不需要。
- Pi 有 telemetry 包和 evals 包;Conic 只有 loguru 日志,无遥测、无评测框架。

## 多模态与其他

- Conic 的 Discord 路径只读 `message.content` 纯文本,不支持图片/附件——Discord
  场景下反而是自然需求;Pi 的统一 API 支持多模态输入。
- 无子 agent / 任务委派(设计文档亦标为 v1 范围外)。

## 建议优先级(结合 Conic 定位:Discord 内小团队编码 agent)

1. **权限门控落地** — 目前是安全空洞,bash 无任何限制;
2. **流式/分段输出** — Discord 下长回复体验差,可借鉴 Pi 的 message_update 模型;
3. **图片/附件输入** — Discord 场景刚需;
4. **多后端抽象兑现** — 至少直连 Anthropic/OpenAI,支持运行时切换模型;
5. **会话分支/重试** — bus 事件模型已支持,存储层给 `messages` 加 `parent_id` 即可起步
   (即 Pi 会话格式 v1→v2 走过的路径)。

主题、TUI、keybindings、HF 会话分享属于 Pi 终端形态专属,不算 Conic 的真实缺失。

---

# 生命周期事件对比

对比 Pi 扩展系统的生命周期事件(`packages/coding-agent/docs/extensions.md`)
与 Conic 的总线事件(`src/conic/plugins/meta.py`)。

Pi 约有 35 个生命周期事件;Conic 目前有 24 个总线主题(原 15 个 + 本文档第二节
1/2/4/5 项已落地新增的 9 个)。本节记录已对齐的部分、Conic 缺失的事件,以及
不适用于 Conic 形态(Discord bot,非终端 TUI)的事件。

## 一、已对齐(Conic 已有等价物)

| Pi 事件 | Conic 事件 | 说明 |
|---|---|---|
| `context`(LLM 调用前修改 messages) | `before_model_call` | 等价,链式 emit 可改写 messages/tools |
| `tool_call`(可拦截、可改参数) | `before_tool_call` | 等价,handler 可改 payload 或 `raise AbortTurn` |
| `tool_result`(可改写结果) | `tool_result` | 等价,链式 emit 可改写 |
| `turn_start` / `turn_end`(pi 按每次 LLM 响应计) | `step_start` | 粒度对应 Conic 的 Step;但 Conic **没有 `step_end`** |
| `agent_start` / `agent_end`(pi 按一次 agent run 计) | `turn_start` / `turn_end` | 粒度对应 Conic 的 Turn |
| `session_shutdown` | `session_stop` | 部分等价:Conic 只用于让 adapter 停止发送,不带 reason |
| `message_end`(最终消息,可替换) | `assistant_message` | 部分等价:Conic 只覆盖 assistant 文本,不覆盖 user/toolResult 消息 |

## 二、Conic 缺失的事件

### 1. 会话生命周期(优先级:高)✅ 已实现

- **`session_start`**(reason: `"new"` / `"resume"`)— 已实现,由 `DiscordGateway` 在
  `handle_start_command` / `resume_active_sessions` 里紧跟 `PluginManager.start_session()`
  之后 emit,插件可感知"会话开始/恢复"及区分两者。
- **`session_end`**(reason: `"user_stop"`)— 已实现,`handle_stop_command` 在
  `session_stop` 之后、`stop_session()` 之前 emit。范围小于 Pi:目前只覆盖用户主动
  `/agent_stop` 这一种 reason,"线程被删"(resume 时 fetch 失败,session 从未真正建起,
  没有 bus 可 emit)和"进程退出"(`DiscordGateway.stop()` 目前不遍历 `self._sessions` 发
  任何事件)仍是空白——若要补全,后者是可行的后续小改动。

### 2. Step / 工具执行的对称性(优先级:高)部分已实现

- **`step_end`** ✅ 已实现 — 与 `step_start` 成对,由 `ReactLoopPlugin` 在每个 Step 的
  两个退出路径(最终回答 / 工具调用处理完)分别 emit,`step_index` 与该 Step 的
  `step_start` 一致。注意:Step 因 `AbortTurn` 中止时不会补发 `step_end`(与现有
  `turn_start`/`turn_end` 在出错时也不补发 `turn_end`、改用 `error` 事件收尾的既有
  约定一致,视为有意为之)。
- **`tool_execution_start` / `tool_execution_end`** ✅ 已实现 — 括住 `bus.request(tool_call, ...)`
  实际派发,`tool_execution_end` 携带派发得到的原始 `ToolCallResult`(在 `tool_result`
  观察/改写链跑之前)。已知边界:若工具执行本身抛出未捕获异常(内置四个工具都不会,
  只有会异常的第三方工具插件才会触发),会走到外层 `except Exception`,此时
  `tool_execution_end` 不会补发——目前是设计上未覆盖的边界情况,不是本轮范围。
- **`tool_execution_update`(进度流)** — 仍未实现。需要工具执行本身支持非阻塞/可
  流式上报进度,目前 `bash` 等工具是同步 `await` 到底,没有中间点可以 emit。

### 3. 消息流式事件(优先级:中,受阻于非流式后端)

- **`message_start` / `message_update`** — Pi 在 assistant 流式输出期间持续发
  `message_update`。Conic 后端(`backends/openrouter.py`)是非流式单次 completion,
  整个事件层没有 partial message 概念。要支持 Discord 消息渐进式编辑,需要
  先让 backend 支持 streaming,再补这两个事件。

### 4. 上下文压缩(Summarize)前后钩子(优先级:中)✅ 已实现

- **`before_summarize`(可取消/自定义指令)** ✅ 已实现 — 对应 Pi 的
  `session_before_compact`。`TokenBudgetPlugin.apply` 在触发 `summarize` request
  之前先链式 emit `BeforeSummarize(request, cancelled=False)`;钩子可改写
  `request.instructions` 定制摘要提示词,或把 `cancelled` 置 `True` 跳过本次摘要
  (`apply` 直接返回 `None`,历史不变)。
- **`summarize_done` / `summarize_failed`** ✅ 已实现 — 对应 Pi 的
  `session_compact` / `session_compact_failed`。成功时携带 `SummarizeResult` emit
  `summarize_done`;失败时先 emit `summarize_failed`(带异常)再重新抛出——
  "摘要失败中止本轮 Turn"的既有行为不变,只是现在失败前多了一次可观测的通知。

### 5. 输入拦截(优先级:中)✅ 已实现

- **`input`(continue / transform / handled)** ✅ 已实现 — `DiscordGateway.handle_message`
  在把文本交给 `UserInputEvent` 之前,先链式 emit `Input(text, handled=False)`。
  钩子可返回改写过 `text` 的 `Input`(继续走 ReactLoop,但用改写后的文本),或把
  `handled` 置 `True` 完全拦下(`UserInputEvent` 不会发出)。目前还没有任何插件
  真正挂在这个事件上——`!status` 之类的文本命令插件仍待实现,这里只是补齐了
  挂载点本身。

### 6. Provider / 模型层钩子(优先级:低)

- **`before_provider_request` / `before_provider_headers` / `after_provider_response`** —
  Conic 的 `model_request` 是 message 级抽象,没有 HTTP 层(headers、原始 payload、
  响应状态码)的拦截点。想做请求改写、自定义鉴权头、用量统计需要它们。
- **`model_select` / `thinking_level_select`** — Conic 无运行时切换模型能力
  (模型由环境变量固定),自然也没有此事件。若将来支持 `/model` 类命令则需要。

### 7. 错误重试语义(优先级:低)

- **`agent_settled`** — Pi 区分"一次 run 结束(可能自动重试)"与"确定不再继续"。
  Conic 的 `turn_end`/`error` 是终态,没有重试概念;若将来加自动重试,需要区分。

## 三、不适用(Pi 的终端/TUI 形态专属,Conic 不需要)

- `ui_prompt_start` / `ui_prompt_end` — 终端阻塞式 UI 提示(Discord 场景的对应物是
  interaction/按钮,如需要应设计成 Discord 专属事件)。
- `user_bash` — 终端 `!` 直跑 shell 命令的拦截。
- `project_trust` — 终端"是否信任该项目目录"的交互。
- `resources_discover`(skills/prompts/themes 路径发现)— Conic 尚无 skills/themes 体系;
  若引入动态 prompt/skill 加载可参考。
- `session_before_switch` / `session_before_fork` / `session_before_tree` / `session_tree` /
  `session_info_changed` — 依赖 Pi 的会话分支/树/命名功能,Conic 历史为线性结构,
  先有分支功能才谈得上这些事件。

## 四、建议落地顺序

1. ✅ `session_start`(带 reason:new/resume)+ `session_end`(带 reason)— 已实现(`session_end` 目前只有 `user_stop` 一种 reason)。
2. ✅ `step_end` + `tool_execution_start/end` — 已实现。
3. ✅ `before_summarize` / `summarize_done` / `summarize_failed` — 已实现。
4. ✅ `input` 拦截事件 — 已实现(挂载点已就绪,尚无插件使用它)。
5. streaming(`message_update`)与 provider 层钩子 — 仍待做,依赖后端改造,放到最后。

---

# 会话分支

两种分支形态,共享同一套框架基础:

- **reply 线程内分支**:用户在线程内 reply 某条历史消息 = 从那个点分支继续
  对话(对应 Pi 的 `/tree` 从任意历史点继续);
- **`/agent_fork` 新线程分支**:用新 Discord thread 承载新分支(对应 Pi 的
  `/fork`),指令设计见本节末尾。

## 框架基础

没有原理性阻碍,需要以下三层基础设施。

### 持久层

- **消息 chunk 的 message_id 映射**:分支的前提是能把"用户 reply 的那条 Discord
  消息"映射回历史条目,但现状是零基础——
  - `DiscordThreadPlugin._send` 发完即丢,不记录 Discord 返回的 message id;
  - 超过 2000 字符切块发送,一条逻辑 assistant 消息对应 N 条 Discord 消息,
    reply 可能指向任意一块;
  - `gateway.on_message` 只取 `message.content`,`message.reference` 被丢弃。

  需要:发送时收集所有 chunk 的 message id 持久化(`discord_message_ids` 列或
  独立映射表),接收时把 `message.reference.message_id` 解析回历史条目。
  改动涉及 gateway、channel plugin、storage 三处。

- **parent 语义**:`messages` 表目前主键 `(session_key, seq)`,`load_history`
  即 `ORDER BY seq`,纯线性。需要:
  - 每条消息加 `id` / `parent_id` 列,历史成树;
  - `sessions` 行上加"当前 leaf"指针;
  - `load_history` 改为从 leaf 沿 `parent_id` 走到根(DuckDB 递归 CTE);
  - `react_loop.py` 中 4 处 `append_message` 调用带上 parent 语义。

  这正是 Pi 会话格式 v1(线性)→ v2(`id`/`parentId` 树)的演进路径。

  分支切点需与工具调用组对齐:用户 reply 的是"可见的 assistant 文本",内部
  历史在可见消息之间还有 tool_call assistant 消息与 tool 回复,切点不能把
  它们拆开——`core/messagealign.align_cut` 的不变式可直接复用。

  (好消息:`SummarizerPlugin` 只改写当次 `BeforeModelCall` 上下文、从不回写
  DB,原始历史完整,分支到摘要覆盖前的任意点都可行。)

### workspace 管理

- **git checkpoint(决定:per-turn commit)**:历史可以回退,磁盘不会——分支
  回到 N 轮前,workspace 已被"未来"的 bash/write_file 改过,模型上下文与文件
  状态不一致(Pi 面对本地目录同样不解决,只分支对话)。方案:
  - workspace 初始化为 git 仓库;
  - **每个 Turn 结束时自动 `git add -A && git commit`**,workspace 无变更则跳过
    (纯问答轮不产生 commit,噪音接近零、成本毫秒级);
  - **commit sha 记录到持久层**:Turn 对应的历史条目(或 turn 级记录)上存
    关联的 commit sha,使"历史树节点 ↔ workspace 状态"可互相定位;
  - 分支时 checkout 分支点条目所记的 sha,恢复该时刻的文件状态,保证模型
    上下文与磁盘一致;
  - 切换 leaf 时兜底 commit 一次,防止离开分支时丢失未快照的改动。

  曾考虑"只在分支创建时 commit"的轻量方案:它只能防分支间交叉污染
  (保护被离开的 leaf 的当前状态),无法恢复分支点的历史文件状态——
  "回到改坏之前重来"这一 coding agent 分支的核心场景不成立,故不采用。
  完整方案(每分支克隆 workspace / worktree)成本高,先不做。

### 架构层面

- **分支目标传递**:`UserInput` 目前只有 `text`,ReactLoop 每步直接
  `self._storage.load_history()`,"从哪个 leaf 继续"这一信息没有通道。需要:
  - `UserInput` 携带分支目标(reply 解析出的历史条目 id,无 reply 则为当前 leaf);
  - 从 gateway 经 `UserInputEvent` 一路传到存储读取处,`SessionHandle` 的
    读写接口按 leaf 参数化;
  - 交互约定:reply = 从该点分支,不 reply = 继续当前 leaf;bot 回复时也
    reply 到触发消息,让 reply-chain 提供分支的视觉线索(Discord 线程是线性
    流,没有 `/tree` 式可视化,分支交错显示只能靠约定缓解)。

## 分支指令:`/agent_fork`(新 Thread 承载分支)

在"reply = 线程内分支"之外的另一种(也可能是主要的)分支形态:**用新 Discord
thread 承载新分支**,源线程保持线性不受干扰——直接绕开了上文提到的
"分支交错显示在同一线程里"的 UI 难题。对应 Pi 的 `/fork`(从某点派生独立
会话文件,header 记 `parentSession`)。

### 指令形态

Discord 平台约束:slash command 交互**无法 reply 到某条消息**,所以
"从 Reply 节点分支"需要另外两种入口承载:

1. **`/agent_fork [title]`**(slash command,在源会话线程内执行)——
   从当前 leaf 分支:复制"根 → 当前 leaf"的完整路径。
2. **消息右键菜单命令 "Fork from this message"**(`app_commands.ContextMenu`,
   message 类型)——分支点 = 被选中的那条消息对应的历史节点。这是
   "指定节点分支"的正规入口。
3. **(可选)reply 触发文本**:对旧消息 reply 一条 `!fork [title]`,由
   `input` 拦截事件识别——依赖消息注入章节的拦截层,作为 2 的低成本替代。

### 执行流程

1. **解析分支点**:
   - 有目标消息:Discord message id → `discord_message_ids` 映射 → 历史条目
     (chunk 命中任意一块都归并到其逻辑消息);再用 `align_cut` 不变式把切点
     对齐到合法边界(不拆开 tool_call assistant 消息与其 tool 回复)。
   - 无目标消息:源会话当前 leaf。
   - 分支点必须是已落库的条目;源会话 Turn 进行中时,取最近已持久化的合法
     边界(不等待、不打断源 Turn)。
2. **创建新 thread**:在源线程的父 channel 下建公开线程,名字取 `title`,
   缺省 `fork-of-<源线程名>@<分支点短id>`。
3. **建新会话(持久层)**:
   - 新 `session_key = "discord:{new_thread_id}"`;
   - `sessions` 表增列:`parent_session_key`、`fork_entry_id`(对应 Pi 的
     `parentSession` header);
   - **历史采用复制**:把"根 → 分支点"路径上的条目复制进新会话(条目 id 在
     新会话命名空间内可保留原值)。不采用跨会话共享 parent 指针——两个
     thread 会并发运行,共享历史让 load_history、压缩、并发控制全部复杂化;
     DuckDB 里复制几十条消息成本可忽略。
4. **workspace 隔离(必须,不是可选)**:两个 thread 可并发执行工具,不能
   共享 workspace。用分支点条目记录的 git commit sha,从源 workspace
   `git worktree add`(或 clone)出新 workspace,checkout 到该 sha——
   这正是 per-turn commit + sha 落库的直接收益:新分支的文件状态与其
   上下文精确一致。
5. **启动会话**:走 `PluginManager.start_session` 正常路径,`session_start`
   事件带 `reason="fork"` + 父会话信息(衔接前文缺失事件清单)。
6. **新 thread 首条消息**:bot 发一条 header——链接回源线程与分支点消息
   (message link),说明"本分支继承至 <分支点> 的上下文";之后等用户输入,
   不自动跑 Turn。
7. **响应原指令**:ephemeral 回复新 thread 链接。

### 边界情况

- 在非会话线程 / 已 ended 的会话里执行 → ephemeral 报错。
  (对 ended 会话可考虑放开:fork 是"复活"已归档会话的自然方式。)
- 右键菜单选中的消息不在任何会话映射里(如 bot 的 header 消息、系统消息)
  → 报错并提示可选范围。
- 分支点是 user 消息:复制路径**含**该消息,fork 后不自动执行——用户可以
  在新 thread 里直接补充/改写后再触发(等价于 Pi tree 里"选中 user 消息
  放回编辑器"的语义,Discord 下退化为手动重发)。
- 源 workspace 不存在或 sha 缺失(旧数据):降级为空 workspace + header 里
  注明文件状态未继承。
- 配额:`git worktree` 数量与磁盘占用随 fork 增长,`agent_stop` 时应
  `git worktree remove` 回收;必要时限制每源会话的活跃 fork 数。

### 与线程内 reply 分支的关系

两者共享全部框架基础(chunk message_id 映射、parent 语义、sha 落库、
分支目标传递),只是分支的**呈现载体**不同:reply 分支留在原线程(轻量、
适合"换个问法重试一句"),`/agent_fork` 开新线程(重量、适合"从这里开始
走另一条实现路线"且需要并发)。建议实现顺序:先做 `/agent_fork`——它不
需要解决"同一线程内当前 leaf 是哪条"的交互歧义,存储改造相同但 UX 更简单,
可作为分支能力的第一个落地形态。

---

# 消息注入(steering / follow-up)

借鉴 Pi `agent-loop.ts` 的双层循环:内层每轮 LLM 调用之间注入 steering 消息
(用户在 agent 运行中途插话,下一步生效),外层在 agent 本该停止时检查
follow-up 队列,非空则自动继续。

## Conic 现状的问题

用户消息到达时 `gateway.handle_message` 直接 `async with scope.lock` 阻塞:
Turn 运行中收到的新消息挂在锁上排队,等整个 Turn 结束后才作为独立的下一个
Turn 依次执行。消息不丢,但**运行中无法插话**——agent 跑偏了只能等它跑完
25 步;多条排队消息也各起一个完整 Turn,而不是合并进上下文。

## 目标行为

1. **Turn 进行中接收 UserInput(steering)**:新消息不再阻塞在锁上,而是进入
   会话的 inbox 队列;ReactLoop 在每个 Step 边界(下一次模型调用前)排空
   inbox,把消息以 user role 追加进历史——本 Turn 的下一步就能看到插话。
2. **Turn 结束后有新 Input 则自动执行下一个 Turn(follow-up)**:Turn 收尾时
   检查 inbox,非空则不结束、直接以队列消息开启下一个 Turn(等价于 Pi 的
   外层 while + `getFollowUpMessages`)。

## 改动点

- **Session 增加 inbox 队列**(`SessionScope` 加 `asyncio.Queue` 或 list+lock):
  `handle_message` 从"抢锁 emit"改为"入队;若无 Turn 在跑则启动 Turn"。
  现有 per-session lock 保留,仅用于保证同一会话同时只有一个 Turn 在执行。
- **ReactLoop 两处消费 inbox**:
  - 每次 `StepStartEvent` 之后、`before_model_call` 之前排空 inbox,逐条
    `append_message({"role": "user", ...})` 并 emit(供 channel 插件回显确认);
  - Turn 主循环退出前(最终 assistant 消息落库后)再查一次 inbox,非空则
    `continue` 进入新 Turn(发 `turn_end` + 新 `turn_start`,保持事件语义)。
- **竞态收口**:消息"入队"与"Turn 是否在跑"的判断必须原子(在会话锁内判断),
  避免 Turn 恰好收尾时新消息既没被 follow-up 消费、也没人再启动 Turn。
- **事件层配合**:steering 注入点正好是前文"缺失事件"里的 `input` 拦截事件
  的天然挂点——注入前先过可拦截的链式 emit,一并实现。
- **与分支的交互**:带 reply 的消息(分支意图)不适合作为 steering 注入当前
  Turn——它要切换 leaf。约定:reply 消息始终走 follow-up 路径(等当前 Turn
  结束后作为新 Turn 在目标分支上执行),普通消息才作 steering 注入。

## 顺带可抄的防御(同源自 Pi loop)

- 模型响应 `finish_reason == "length"`(输出被 token 上限截断)时,消息里的
  tool call 参数可能残缺,应全部判失败而不执行(Conic 目前会照常解析执行)。

---

# 上下文压缩对比(Pi compaction vs Conic Summarizer)

对比来源:Pi `packages/coding-agent/docs/compaction.md` vs Conic 的
`TokenBudgetPlugin` + `SummarizerPlugin` + `TruncatorPlugin`。

## 逐环节对比

| 环节 | Pi | Conic |
|---|---|---|
| 触发条件 | `contextTokens > 窗口 − reserve(16384)`,按模型窗口动态计算,可按模型覆盖 | 固定阈值 `CONTEXT_TOKEN_BUDGET=50000`,与实际模型窗口无关 |
| 检查位置 | 三处:tool 结果回填后、请求前、新输入前 | 一处:`before_model_call` |
| token 计数 | 用 provider 返回的真实用量校准 | `chars//4` 粗估(对中文严重低估) |
| 保留策略 | 最近 `keepRecentTokens=20000` **token** | 最近 `keep_recent=5` **条消息**——一条超长 bash 输出就能让保留部分超预算 |
| 切点对齐 | 不切开 tool call/result 组;单 turn 超预算时劈开生成双摘要再合并 | `align_cut` 同样不拆 tool 组(已对齐);无劈 turn 处理 |
| 摘要提示词 | 结构化模板:Goal / Constraints / Progress / Key Decisions / Next Steps / Critical Context + `<read-files>` `<modified-files>` | 一句 "Summarize the following conversation history concisely" |
| 输入序列化 | 转成 `[User]:` / `[Tool result]:` 标签纯文本防串戏;tool 结果截 2000 字符 | `f"{role}: {content}"` 直接拼接(raw_message 为 dict 时拼出 Python repr);tool 输出不截断全量喂入 |
| 迭代性 | 上一次摘要作为下一次的输入,理解跨周期累积 | 每次对全部旧历史从头总结 |
| 持久化 | `CompactionEntry` 落库,压缩一次生效,后续复用 | **不落库**:摘要只存在于当次 `BeforeModelCall`,超预算后**每个 Step 都重新总结一遍**(每步多一次全量模型调用) |
| 无损性 | 原始条目全在,可回退/分支到压缩前 | DB 原文同样都在,只是没有"压缩记录"概念 |
| 手动控制 | `/compact [聚焦指令]`,`session_before_compact` 钩子可接管 | 无手动入口、无事件钩子(见缺失事件清单第 4 条) |
| 文件追踪 | 从 tool call 抽取读/改文件清单,跨压缩累积在 `details` | 无——压缩后"改过哪些文件"全靠摘要模型自觉 |

## Conic 特有隐患:Truncator 与 Summarizer 各自为政

`TruncatorPlugin`(保留最近 40 条)和 `TokenBudgetPlugin` 都挂在
`before_model_call`,按注册顺序链式执行:

- Truncator 先跑:Summarizer 看到的旧历史已被硬截断,被扔掉的消息
  **既没进摘要也没进上下文,静默丢失**;
- TokenBudget 先跑:Truncator 可能把刚生成的摘要连同保留消息再截一刀。

Pi 没有这个问题——压缩是单一管线。建议把两者合并:Truncator 仅作为
摘要失败时的兜底降级,不再作为独立的常规裁剪。

## 改进优先级(按性价比)

1. **摘要落库/缓存** — 消除"超预算后每步重摘要"的双倍开销,顺便成为
   CompactionEntry 式无损压缩记录的雏形(存成一条特殊消息行即可);
2. **结构化摘要模板 + 序列化防串戏 + tool 输出截断** — 纯提示词/字符串
   处理改动,成本极低;
3. **keep 按 token 而非按条数**,并理顺 Truncator 与 Summarizer 的关系
   (合并为单一压缩管线);
4. **token 估算校准** — 用 OpenRouter 响应里的真实 usage 反馈校准,
   替代 chars//4。

---

# Discord 显示逻辑改进(表格与代码块)

## 现状问题

- `DiscordThreadPlugin._send` 是纯字符切片:每 2000 字符硬切,可能切在
  代码块中间——前一条消息围栏未闭合、后一条的孤儿内容和闭合围栏都以
  纯文本渲染,两条全碎;
- Discord **不渲染 markdown 表格**:模型输出的表格原样显示竖线文本,
  比例字体下完全不对齐。

## 设计(已确认方案)

新模块 `src/conic/plugins/channels/discord_format.py`(纯函数,便于单测):

1. **`render_tables(text) -> str`** — 表格转等宽代码块:
   - 扫描非代码块区域的 markdown 表格,重排为对齐的等宽文本并包进
     ``` 围栏(保留表格结构,Discord 内等宽显示);
   - 对齐交给 **`tabulate` + `wcwidth`**(两个纯 Python 轻依赖):
     wcwidth 原生处理东亚全角宽度/组合字符,比手写
     `unicodedata.east_asian_width` 健壮;自己只写"识别 markdown
     表格 → 解析出行列"的部分;
   - 已在代码块内的伪表格文本不动;其余 markdown(粗体、行内码、围栏、
     列表)Discord 原生支持,原样保留。

2. **`chunk_message(text, limit=2000) -> list[str]`** — markdown 感知分块:
   - 切点优先段落边界(`\n\n`),其次行边界(`\n`),仅超长单行才字符硬切;
   - 全程追踪代码围栏状态:切点落在围栏内时,当前块尾补 ```` ``` ````
     闭合、下一块头补 ```` ```lang ```` 续开(保留语言标签);
   - 实际预算按 `limit − 围栏开销` 计,保证任何块不超 2000。
   - 超长代码块采用"围栏闭合续开"跨多条消息呈现;曾考虑"超阈值转
     文件附件"方案,因多出上传路径和错误处理,暂不采用。

3. **其他 Discord 不支持的 markdown 转换**(与 `render_tables` 同一遍处理,
   纯正则替换,按模型输出频率排):
   - 水平分隔线 `---` / `***`(模型非常爱用,原样显示)→ 空行或装饰行;
   - `####` 及以下标题(Discord 只支持 `#`–`###`)→ 降级为 `###` 或 `**粗体**`;
   - 任务列表 `- [ ]` / `- [x]`(方括号原样保留)→ ☐ / ☑ 字符;
   - 内嵌图片 `![alt](url)`(不渲染)→ 提取裸 URL,让 Discord 自动生成预览。

   低频不处理:脚注、引用式链接、缩进式代码块、HTML、LaTeX(原样显示,
   可接受)。masked link `[text](url)` bot 消息原生支持,无需处理。

`discord.py._send` 改为 `render_tables` → `chunk_message` → 逐条发送
(约 3 行);错误消息路径自然复用;事件流、typing、`_stopped` 逻辑不动。

## 依赖选型(调研结论:无一站式库)

没有现成库同时覆盖"表格转换 + 围栏感知分块";现有拆分工具(如
Discord-long-text-splitter)明确声明不处理代码块。可用积木:

| 积木 | 用途 | 结论 |
|---|---|---|
| `tabulate` + `wcwidth` | 等宽表格对齐(含 CJK 宽度) | **采用**,替代手写对齐 |
| `discord.ext.commands.Paginator`(discord.py 内置) | 按行切分 ≤2000 页 | 仅参考:它假设整体包在一个代码块里,不追踪正文中任意围栏 |
| `table2ascii` | Discord 向 ASCII 表格 | 与 tabulate 重叠且生态小,不用 |
| `markdown-it-py` / `mistune` | 可靠定位表格/围栏 | 先用正则(~20 行),撑不住再上 |

围栏感知分块(`chunk_message`)没有现成实现,自写 ~50 行。

## 测试要点

新增 `tests/plugins/channels/test_discord_format.py`:表格重排(含中文
宽度对齐)、代码块内伪表格不误判、行/段落边界分块、围栏跨块闭合续开
(带语言标签)、超长单行硬切、恰好 2000 边界、表格+代码混排;更新
`test_discord.py` 中针对 2000 硬切的断言。

改动面:1 个新模块 + `discord.py` 3 行 + 测试;不碰 bus、storage、loop。

---

# Plan-and-Execute 融合

## 意图判断:模型自主为主,不建前置路由器

四种方案对比后的结论:

| 方案 | 做法 | 评估 |
|---|---|---|
| A. 前置路由器 | 每条输入先过轻量 LLM/分类器,分流 chat / simple / complex | 不采用:多一跳延迟;分类器看不到"打开代码才知道深浅"的复杂度;误判代价不对称 |
| B. 模型自主决定 | system prompt 行为准则 + plan 工具,模型在第一个 Step 内自行决定规划或直答 | **主方案**(Claude Code / Pi 的做法):判断与执行共享上下文,零额外延迟 |
| C. 执行中升级 | 默认 ReAct 直跑,复杂度信号出现时升级(step 数超阈值、反复报错) | 兜底,B 的补充 |
| D. 用户显式声明 | `/agent_plan` 命令强制先出计划等确认 | 覆盖手段,零误判但把负担给用户 |

落地(全部走现有 bus 机制,不改 loop 结构):

1. 新增 plan 工具插件(见下节);
2. `prompts/execution.md` 加行为准则——判据必须是**可观察特征**
   (≥3 个文件的改动、有依赖顺序的步骤、"实现/重构/迁移"类动词 → 先规划;
   单文件小改、问答 → 直接做),不能写"复杂时请规划"这种空话,否则模型
   要么从不规划、要么事事规划;
3. `StepLimitPlugin` 改造:第 15 步注入提示"步数过半,若任务复杂请先用
   plan 工具整理剩余工作",25 步仍硬停;
4. 可选 `/agent_plan` 命令:UserInput 带 flag,`BuildSystemPromptEvent`
   注入"必须先产出计划并等用户确认"。

明确不做:经典 Planner/Executor 双循环(独立 planner 产出完整计划、
executor 逐步执行、replan 循环)——适合长时程可并行子任务配子 agent 的
场景,对 conic 定位是过度设计;"plan 作为工具 + 提示词准则"拿到 80% 收益。

## Plan 插件职责

本质:把计划变成**上下文里持久存在、可更新的锚点**,对抗长任务目标漂移
(模型 20 步后早忘了第 3 步,计划每步重新出现在眼前)。四件事:

1. **工具接口**(模型侧,仅两个):
   - `write_plan(steps)` — 首次制定 / 整体重写(replan);
   - `update_step(index, status, note?)` — pending → in_progress → done / blocked。
   - 不需要 `read_plan`:计划直接注入上下文,模型永远看得见。
2. **状态持久化与注入**(核心职责):
   - 计划存为会话级状态(DuckDB 单独记录或 sessions 列),**不是**对话
     消息——否则会被 Truncator/Summarizer 裁掉;
   - 挂 `BuildSystemPromptEvent`,每个 Step 渲染成 prompt section 注入。
     计划不占对话历史、不被压缩、每步必现——正好绕开摘要系统的弱点。
3. **进度对外可见**(Discord 侧,见下节)。
4. **给策略层提供挂点**(后期可选):StepLimit 升级提示带计划上下文;
   Turn 结束计划未完成且 inbox 空 → 自动续跑下一 Turn(衔接 follow-up,
   走向长任务自治的最小一步)。

边界(不做什么):不做规划本身(那是主模型的事)、不驱动执行(没有独立
executor 循环)、不校验计划质量(提示词准则的事)。

体量:一个工具插件(两个 payload)+ 一个 prompt section 挂钩 + Discord
一条状态消息,全部现有 bus 机制,不碰 loop。

## Discord TODO 列表显示

Discord 不渲染 `- [ ]`,但 bot 可编辑自己的消息——用"一条自更新的计划
消息"呈现,纯文本 markdown:

```
📋 **Plan**  (2/5 完成)

✅ ~~调研现有 storage 接口~~
✅ ~~给 messages 表加 parent_id 列~~
🔄 **改造 load_history 为树遍历**
⬜ 更新 react_loop 的 append 调用
⬜ 跑通全部测试
```

状态 emoji:✅ 完成 / 🔄 进行中 / ⬜ 待办 / ⛔ 阻塞;完成项删除线,
进行中加粗;首行进度计数。超长时已完成项折叠成一行 `✅ 已完成 N 项`。

**更新策略(底部则编辑、被顶则重发)**:

```
计划状态变化(debounce 合并 1–2 秒内连续变更)
  └─ 计划消息 id == thread.last_message_id ?
       ├─ 是 → 原地 edit(一次 API 调用,id 不变)
       └─ 否 → 删除旧计划消息,在线程底部重发
```

- agent 静默连续执行时是纯 edit;被 assistant 回复/用户插话顶上去后
  才花两次调用挪回底部;
- 线程里任何时刻至多一条计划消息(重发时删旧,不留过期快照);
- "是否最新"直接比较 discord.py 缓存的 `thread.last_message_id`,
  天然覆盖所有发送方;竞态可容忍(误编辑一次,下次更新自动纠正);
- 删除 404 静默跳过;`_stopped` 后不再发;消息 id 随计划状态持久化
  (重启后可续编辑/删除)。

**上下文跳过——conic 架构下天然成立**:历史由 ReactLoop 主动
`append_message` 写入,Plan 插件直接 `thread.send` 的 info 消息不经过
存储层,`load_history` 天然看不到。模型对计划的感知走 prompt section,
两条通道各司其职:

```
计划状态(持久化,会话级)
  ├─→ prompt section    → 模型每步看到(进 LLM 上下文)
  └─→ Discord info 消息 → 用户看到(不进上下文,不进历史)
```

对应 Pi 的 `CustomEntry`(不进 LLM 上下文)/ `CustomMessageEntry`
(进上下文)之分,conic 用"进不进 messages 表"实现同样语义。

与分支设计的冲突消解:info 消息不在历史里,本来就不是合法分支目标;
reply 计划消息落入已有边界情况("不在会话映射里 → 报错提示")。

不采用:Embed(embed 内不渲染标题语法,视觉割裂,列为可选升级)、
交互按钮勾选(计划由模型维护,用户手动改状态造成双写;用户干预走
steering 插话)。

**通用化**:"底部则编辑、被顶则重发"是可复用原语——工具执行进度提示
(`tool_execution_update`)、流式输出渐进编辑都能用。建议做成 channel
层小组件 `RepositionableMessage`,Plan 插件是第一个用户。
