# LoopDesign:三框架 Agent Loop 结构与事件流转详解

hermes-agent(NousResearch,Python)/ openclaw(TypeScript)/ deepseek-harness
(deepseek-ai,TypeScript)三个生产级 agent 框架的 loop 层代码级分析。每个框架
按同一骨架组织:**结构总览(SVG)→ 一次输入的事件流转全过程 → 阶段深挖 →
核心组件实现细节**,末尾附三家横向对照。

- 调研日期 2026-09-16,源码 clone 于 `/tmp/research-hermes-agent`、
  `/tmp/research-openclaw`、`/tmp/research-deepseek-harness`;文中路径均为
  各仓库内相对路径。
- 各 section 开头附总体结构图(内嵌 SVG)。注意 GitHub 的 markdown sanitizer
  会剥离内联 `<svg>`(该处显示为空),请用 VS Code / Typora 等本地预览查看;
  需要 GitHub 显示时可另存为 .svg 文件后用 `![]()` 引用。
- 与 conic 的差距分析和落地方案见 `Improvement-with-pi.md` 的《三框架 Loop 深挖》
  部分;本文是纯参考资料,只描述三家"是什么、怎么运转",不展开 conic 侧方案。

**三家形态速写**

| | hermes-agent | openclaw | deepseek-harness |
|---|---|---|---|
| 定位 | CLI/gateway agent | 多渠道聊天网关(与 conic 场景最像) | 编码 agent CLI(`dsh`) |
| Loop 形状 | 嵌套双循环(step 循环 × API 重试循环),~15 个阶段函数 + verdict 状态机 | 三层:核心 turn 双循环 / Agent 队列 / run attempt 编排 | 外层 turn × 内层 step,事件溯源驱动 |
| 状态载体 | `_LoopState` 内存数据类(~40 字段)+ SQLite | 内存 messages + 双队列,关键片段先落库 | **持久化事件日志,一切内存状态皆为投影(fold)** |
| step 概念 | 有(iteration,一次模型调用) | 无(turn 即step 单位) | 有(显式 `step/start`/`step/end` 事件) |
| step 上限 | `max_iterations` 默认 `sys.maxsize`(子 agent 50) | 无(核心层);Layer C 有 attempt 预算 | 无,任何计数上限都不存在 |
| 设计赌注 | 运行时容错(每个 step 都可能坏,每个 step 都有恢复阶梯) | 交互体验(async 工具、在线 steering、渠道流式) | 可重放性(崩溃/重连/审计免费) |

---

# 一、hermes-agent(Python,嵌套双循环 + verdict 状态机)

## 1.1 Loop 结构总览

<svg xmlns="http://www.w3.org/2000/svg" width="940" height="700" font-family="ui-monospace,'SF Mono',Menlo,Consolas,monospace" font-size="13" fill="#1e293b">
  <defs>
    <marker id="ah1" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" fill="#475569"/>
    </marker>
  </defs>
  <rect x="2" y="2" width="936" height="696" rx="10" fill="#f4f6fa"/>
  <rect x="20" y="12" width="110" height="30" rx="6" fill="#eef2ff" stroke="#6366f1"/>
  <text x="75" y="32" text-anchor="middle">用户消息</text>
  <line x1="130" y1="27" x2="170" y2="27" stroke="#475569" marker-end="url(#ah1)"/>
  <rect x="174" y="12" width="640" height="30" rx="6" fill="#eef2ff" stroke="#6366f1"/>
  <text x="186" y="32">build_turn_context:system prompt 恢复 / 消息组装 / preflight 压缩</text>
  <line x1="470" y1="42" x2="470" y2="64" stroke="#475569" marker-end="url(#ah1)"/>
  <rect x="20" y="68" width="900" height="452" rx="8" fill="#f8fafc" stroke="#334155" stroke-width="1.5"/>
  <text x="34" y="90" font-weight="bold">外层 step 循环  while (api_call_count &lt; max ∧ 预算 &gt; 0) ∨ 宽限调用(grace call)</text>
  <text x="34" y="112">[1] begin_iteration ── redirect 排空(checkpoint+纠正行);中断·预算检查 → break</text>
  <text x="34" y="132">[2] prepare_iteration ── steer 排空(追加到最新 tool result 后);历史修复</text>
  <text x="34" y="152">[3] assemble_api_request ── 组装 per-call decorated 副本,prompt-cache 计划最后做</text>
  <text x="34" y="172">[4] run_preflight_gate ── preflight gate(进展 latch / 熔断 / 溢出强制)</text>
  <rect x="34" y="184" width="872" height="98" rx="6" fill="#fff7ed" stroke="#ea580c"/>
  <text x="46" y="204" font-weight="bold">[5] 内层 API 重试循环  while retry_count &lt; 3</text>
  <text x="46" y="224">build_api_request → perform_api_call(流式,daemon 线程 + stale-stream 监视)</text>
  <text x="46" y="244">check_api_response(空 / 截断 / 内容过滤判定)</text>
  <text x="46" y="264">except → classify(25 值枚举) → one-shot 恢复链 → route → overflow → settle(退避/换凭证/fallback/终态)</text>
  <text x="34" y="304">[6] apply_retry_restarts ── 消费 4 种 restart 信号(redirect / 压缩 / rebuilt / length continuation)</text>
  <text x="34" y="324">[7] normalize_model_response ── 有 tool_calls ↓ 左;无 ↓ 右</text>
  <rect x="34" y="338" width="428" height="128" rx="6" fill="#ecfdf5" stroke="#059669"/>
  <text x="46" y="358" font-weight="bold">[8a] run_tool_round(有 tool_calls)</text>
  <text x="46" y="378">校验/去重 → persist-before-execute(先落库)</text>
  <text x="46" y="398">规划器(batch planner)切段:只读+路径重叠→并行;写重叠→串行</text>
  <text x="46" y="418">gate chain:scope → pre_tool_call → guardrails → 人工审批</text>
  <text x="46" y="438">线程池执行(超时剔除审批等待),按调用序提交</text>
  <rect x="478" y="338" width="428" height="128" rx="6" fill="#eff6ff" stroke="#2563eb"/>
  <text x="490" y="358" font-weight="bold">[8b] finish_text_response(无 tool_calls)</text>
  <text x="490" y="378">空响应阶梯:partial流→复用→nudge→prefill→重试→fallback</text>
  <text x="490" y="398">stop gates(verify / pre_verify / kanban)── 拒收 → 继续循环</text>
  <text x="490" y="418">接受 → break(答案成为 final)</text>
  <text x="34" y="494">[9] handle_outer_loop_error ── 逃逸异常 ≤8 次内重试本迭代</text>
  <line x1="470" y1="520" x2="470" y2="542" stroke="#475569" marker-end="url(#ah1)"/>
  <rect x="20" y="546" width="900" height="30" rx="6" fill="#eef2ff" stroke="#6366f1"/>
  <text x="34" y="566">[10] finalize_turn:trajectory 保存 / 持久化 / 剥离合成 nudge 行 / 后台 review fork</text>
  <rect x="20" y="592" width="900" height="90" rx="8" fill="#fdf2f8" stroke="#db2777"/>
  <text x="34" y="612" font-weight="bold">侧信道(任意时刻可进入)</text>
  <text x="34" y="632">interrupt() → 全线程中断位 + socket abort + 子 agent 递归传播</text>
  <text x="34" y="650">redirect()  → 只取消在途模型请求,同一 turn 退还预算并重建(rebuild)(工具执行期降级为 steer)</text>
  <text x="34" y="668">steer()     → 排队,下一迭代注入</text>
</svg>

代码布局(2026-09 从单体 `run_agent.py` 分解而来,兼容 shim 保留旧导入路径):

- `agent/conversation_loop.py` — `run_conversation()` → `_run_conversation_turn()`,
  驱动一个 turn(一条用户消息到完成);外层 step 循环与 `_LoopState` 在此;
- `agent/turn_*.py` — 每个阶段一个模块(`turn_iteration_prep` / `turn_request_assembly`
  / `turn_preflight_gate` / `turn_api_call` / `turn_api_error` / `turn_response_check`
  / `turn_response_intake` / `turn_tool_round` / `turn_final_response` /
  `turn_stop_gates` / `turn_finalizer` / …);
- `agent/tool_executor.py`、`agent/context_compressor.py`(4,949 行)、
  `agent/interrupt_control.py`、`agent/stream_delivery.py` — 核心组件。

结构要点:

- **嵌套双循环**:外层 step 循环(一圈 = 一次模型调用 + 一轮工具),内层
  `_run_api_retry_loop`(把"一次模型调用"包在重试/降级里);
- **循环体不是平铺代码**,而是 ~15 个阶段函数,经 `_run_phase` 反射调用,
  以 verdict 数据类(`action ∈ {fallthrough, continue, break, return}`)驱动流转;
- **全部循环状态集中在 `_LoopState`**(~40 字段,见 §1.4.1);
- 无硬step 上限(`max_iterations` 默认 `sys.maxsize`),终止靠自然停止 +
  预算(可退款)+ 墙钟 + guardrails。

## 1.2 一次 Turn 的事件流转全过程

按时间顺序(编号对应结构图中的阶段):

1. **入口**:用户消息进 `run_conversation()` → `build_turn_context()`:恢复按
   session 持久化的 system prompt(逐字节还原,保 prompt cache 前缀)、组装
   消息、跑 turn 开始的 preflight 压缩(带宿主进度感知超时,超时抛
   `PreflightCompressionTimedOut` 直接结束 turn)。
2. **step 循环入口检查**(`begin_iteration`):先排空 redirect 队列——若用户在
   上一个 step redirect 过,此刻把半截流式文本落为 assistant checkpoint 行、纠正
   文本落为 user 行,**同一 turn** 继续;然后查用户中断和预算,消耗一个 step 的预算
   (预算耗尽仍允许一次宽限调用 grace call,让模型无工具收尾作答)。
3. **注入插话**(`prepare_iteration`):排空 steer 队列,作为独立 user 行
   **追加在最新 tool result 之后**(cache-safe,绝不重排已缓存前缀);注入
   预算预警(run 墙钟 80% 的收尾提示写进最新 tool result 尾部);修复 role
   交替、消毒工具参数(游标记忆化)。
4. **组装请求**(`assemble_api_request`):在结构化克隆上生成 per-call
   decorated 副本——发送路径的一切改写不触碰持久化 transcript。装配顺序
   承重:build → MoA 上下文 → prefill 插入 → `ContextEngine.select_context`
   钩子 → 消毒 → 过期图片驱逐 → 规范化 → **prompt-cache 计划最后做**;
   token 压力 = 粗估被真实 usage 锚点与 `last_real_prompt_tokens` 托底。
5. **preflight gate**(`run_preflight_gate`):超阈值先压缩再调用;进展不足 latch
   熔断;provider 溢出错误可强制穿透熔断(详见 §1.4.3)。
6. **模型调用**(内层重试循环,§1.3.2):流式调用跑在 daemon 线程,监视
   线程盯用户中断与 stale-stream;delta 经 StreamDeliveryMixin 扇出(display
   + TTS + 插件观察者),带跨 delta 的 `<think>` 剥离和单写者栅栏。异常走
   分类 → one-shot 恢复链 → 路由 → 溢出 → settle 五段管线;成功则折算真实
   usage(可能重新武装压缩预算)。
7. **重启信号消费**(`apply_retry_restarts`):内层循环可能武装 4 种 restart
   信号(redirect / 压缩 / rebuilt-fallback / length continuation),此处按
   优先级消费——大多退还本 step 预算、重建请求、重来同一迭代。
8. **响应归一化**(`normalize_model_response`):transport 归一化、scratchpad
   不完整重试、codex incomplete 续写。
9. **分叉执行**:
   - 有 tool_calls → **Tool round**(§1.3.4):先落库 assistant 行
     (persist-before-execute)→ batch planner 切并行/串行段 → 每调用过 gate chain
     (scope → pre_tool_call 钩子 → guardrails → 人工审批)→ 线程池执行
     (批超时剔除审批等待)→ 结果按原调用序落库 → post-tool 压缩检查 →
     回到 2 进入下一个 step;
   - 无 → **finish_text_response**(§1.3.3):空响应恢复阶梯 → stop gates
     (stop gates,可拒收答案逼模型继续)→ 接受则落库、break。
10. **收尾**(`finalize_turn`):trajectory 保存、剥离合成 nudge 行、导出未
    消费 steer、后台 review fork(守护线程重放 transcript 问"有无技能/记忆
    可存",复用父凭证打同一前缀 cache,受聚合输入 token 预算约束)。

中断事件随时切入:`interrupt()` 在 2/6/9 的检查点生效;`redirect()` 在 6
生效(掐 socket、同 turn 重建),在 9(工具执行期)降级为 steer;`steer()`
恒排队、在 3 消费。三动词的完整语义见 §1.4.5。

## 1.3 阶段深挖

### 1.3.1 外层 step 循环:10 个阶段的迁移表

循环条件:`(api_call_count < max_iterations AND iteration_budget.remaining > 0)
OR _budget_grace_call`。

| # | 阶段(文件) | 职责与非 fallthrough 迁移 |
|---|---|---|
| 1 | `begin_iteration`(turn_iteration_prep.py:300) | 排空 redirect 队列(应用为 checkpoint 行 + 纠正 user 行);step 计数 +1、消耗预算。**break**:用户中断(`"interrupted_by_user"`)/ review 预算耗尽 / step 预算耗尽且无宽限 |
| 2 | `prepare_iteration`(同上:93) | 恒 fallthrough。排空 steer → 追加为最新 tool result 之后的独立 user 行(cache-safe);注入 run 预算 80% 收尾预警与迭代预算预警;工具参数消毒;剥离中断脚手架幽灵行;role 交替修复后重锚定 `current_turn_user_idx` |
| 3 | `assemble_api_request`(turn_request_assembly.py:106) | 恒 fallthrough。产出 `api_messages`(decorated 的 per-call 副本)。**顺序承重**:build → MoA 上下文 → prefill 插入(结构化克隆) → `ContextEngine.select_context` 钩子 → 消毒 → 过期出站图片驱逐 → 空白规范化 + 工具调用规范化 → 代理对剥离 → **`build_prompt_cache_plan` 最后做** → 压力估算 = native 估算 \| 粗估,被 usage 锚点覆盖,再被 `last_real_prompt_tokens` 托底 |
| 4 | `run_preflight_gate`(turn_preflight_gate.py) | preflight gate,详见 §1.4.3 |
| 5 | `announce_api_call` | 恒 fallthrough(spinner/verbose) |
| — | `_run_api_retry_loop` | 内层重试环(§1.3.2),可 `return` 终态结果直接结束 turn |
| 6 | `apply_retry_restarts` | 按优先级消费 restart 信号:redirect 重启(`restart_count` 超限 → break,纠正文本经 `steer()` 重排队)→ 中断 break → 压缩重启(**计入 retry 数**)→ rebuilt 重启(fallback 激活;顺带清 `_preflight_compression_blocked`——新 provider 得到一次干净 preflight)→ length continuation 重启(输出上限 `max_tokens × 2^n` 升配)。`response is None` 且无信号 → break(`"all_retries_exhausted_no_response"`) |
| 7 | `normalize_model_response`(turn_response_intake.py:115) | transport 归一化;dict/list 内容强转 str;`post_api_request` 钩子。**continue**:`<REASONING_SCRATCHPAD>` 不完整(≤2 次)/ codex `incomplete` 续写;**return**:scratchpad 3 次失败的部分结果 |
| 8a | `run_tool_round`(有 tool_calls) | §1.3.4 |
| 8b | `finish_text_response`(无) | 8 步判定链,§1.3.3 |
| 9 | `handle_outer_loop_error`(turn_loop_errors.py:35) | 7/8 抛异常时兜底。按 traceback 模块集合分类"本地处理错误 vs API 错误";先给未应答的 tool_call id 填错误结果。**break**:解释器关闭 / 本地错误 / `api_call_count ≥ max-1` / `_outer_error_count ≥ min(8, max_iterations)`。否则 fallthrough 重试本迭代 |
| 10 | `finalize_turn`(turn_finalizer.py) | 唯一出口:预算汇总、trajectory 保存、持久化、从返回历史剥离合成 nudge 行、导出未消费 steer(`result["pending_steer"]`)、后台 memory/skill review fork、结果 dict |

### 1.3.2 内层 API 重试循环

每 step:`retry_count=0`,`max_retries=3`(配置 `api_max_retries`,min 1),
fresh `TurnRetryState`(§1.4.2)。循环 `while retry_count < max_retries`。

**相内阶段**:`nous_rate_limit_guard`(跨会话 Nous 限流:有 fallback →
rebuilt 重启 break,计数器归零;无 → return 终态限流结果)→
`build_api_request`(重置流跟踪;**对当前 provider** 重放 reasoning 回显 +
cache decoration——fallback 安全;OpenRouter 空响应重试加 cache-bypass 头;
LLM 请求中间件;`pre_api_request` 钩子)→ `perform_api_call`(在 redirect
锁下置 `_model_request_active`;响应与 redirect 交叉 → 响应作废,
`clear_interrupt(preserve_redirect=True)` 成功 → 武装 redirect 重启 break,
失败 → `interrupted=True` break)→ `check_api_response`(形状非法 →
急切 fallback break / 终态 return / 抖动退避 continue;`content_filter` →
拒答结果或 fallback;`length` → 截断恢复子状态机,见下;成功 → 折算真实
usage——**可能重新武装压缩预算**——break)。

**异常路径 `handle_api_error`**,管线顺序承重:

```
分类前恢复(每项 continue 立即重试):
  UnicodeEncodeError → ASCII 消毒(≤2 遍) | 4xx 图片拒收 → 剥图转纯文本
  | Bedrock 事件序错 → 切 converse 模式(一次)
→ classify_api_error → ClassifiedError{reason, status_code, retryable,
    should_compress, should_rotate_credential, should_fallback, billing_unverified}
  (api_request_error 钩子先跑;插件可经 transform_api_error_classification 覆盖)
→ 分类后 one-shot 恢复链(每项一个 one-shot guard,命中即 continue):
  免费档换模型 → Nous 计费凭证刷新 → 凭证池轮换 → 图片缩小 →
  工具消息剥图 → reasoning 强制 latch → 401 按 provider 刷新凭证 →
  thinking 签名/加密内容/native-compaction/语法 payload 剥离
→ retry_count += 1 → 中断检查(redirect → break;interrupt → return)
→ route_classified_error:
  溢出类 ∧ 压缩关闭 ∧ 非输出上限 → return 终态(compaction_disabled,不可重试)
  long_context_tier → 压缩器封顶 200k → 压缩 → 武装压缩重启+溢出 latch,break
  rate_limit/billing/upstream(急切) 或 timeout/overloaded(≥2 次后),有 fallback,
    池救不了 → _try_activate_fallback → break(计数器归零)
    [例外:relay 包装的输出上限 429(可解析预算)豁免]
  Z.AI 过载 → max_retries 提到阶梯上限(30/60/90/120s 长 phase)
  auth 刷新后仍 401,auth_failover 未用,有 fallback → fallback break
  Nous 真实账户 429 → 记入跨会话熔断器,retry_count=max-1,continue(顶部守卫跑一次)
→ recover_from_overflow(turn_overflow.py):
  413 → 压缩,进展按**序列化字节**打分(<95% 或行数减少) → 压缩重启 break;
    否则剥保留视觉 payload continue;否则终态(次数封顶)
  context_overflow → 可解析 available_tokens → 输出上限钳制
    min(avail, local)−64 + 压缩 break;不可解析的输出上限 → 终态 fail-fast;
    否则采纳 provider 报告的窗口(持久化) + 压缩按 token 打分 →
    压缩重启 + 溢出 latch,break;缩不动 → 终态 return
→ settle_unrecovered_error:
  客户端错误(本地 ValueError/TypeError 等;或不可重试 ∧ ¬should_compress ∧
    reason ∉ 瞬时集合) → Copilot 凭证 400 自愈一次(retry_count=0 continue)
    → should_fallback 则 fallback break → return 不可重试结果(billing 结构化块)
  retry_count ≥ max → 重建主 transport 一次(retry_count 归零,重开 fallback 态,
    continue) → fallback break → return 耗尽结果
  否则 → 退避:Retry-After 头/体优先(封顶 600s,0 视为缺失) else
    jittered_backoff(base=2, max=60);429/Z.AI 自适应阶梯;
    退避睡眠可中断(200ms 切片,~30s 活性心跳):
    中断 return / redirect break / 正常 fallthrough 重试
```

**截断恢复子状态机**(`finish_reason="length"`):思考耗尽/重复主导 → 带部分
结果结束;工具调用被截断 → ≤4 次输出上限倍增 continue,第 4 次终态
return(工具尾闭合);文本 → 追加片段行 + 续写 nudge(≤4 次且 prompt 没占
满窗口;思考截断附带一次性关 reasoning),武装 length continuation 重启;
到顶 → 丢弃碎片尾迹,保留已缝合部分,终态。流被内容过滤终止且有 fallback
→ 回滚到最近干净 turn,武装 rebuilt 重启。

### 1.3.3 finish_text_response:判定链、stop gates 与空响应阶梯

**8 步判定链**(turn_final_response.py:46):

1. reasoning-only 干净停止 → 把 reasoning 提升为正文;
2. `<think>` 剥离后无可见内容 → 进入空响应恢复阶梯(§1.4.4);
3. ack/stall 续写:尾部"我继续"类意图或 codex 中间 ack ∧ 有工具 ∧
   `codex_ack_continuations < 2` → 追加中间 assistant + nudge user 行,continue;
4. 长度碎片缝合(`truncated_response_parts` + 本次);
5. 掉工具调用:`finish_reason=="tool_calls"` 但 `tool_calls` 为空,≤3 次 →
   追加 ephemeral nudge 对,continue;
6. 弹出尾部 ephemeral 脚手架行(`_thinking_prefill` 等 4 类标记);
7. **stop gates** 有门命中 continue → `final_response=None`,继续循环;
8. 接受:追加最终 assistant 行、flush DB(失败容忍,finalize 重试),break。

**stop gates(turn_stop_gates.py)**:只对已接受的文本答案运行,
固定顺序,首个命中的门获胜。共享拒收机制:答案作为中间行落库 flush →
追加合成 **user 角色** nudge 行(标记 `_verification_stop_synthetic` /
`_pre_verify_synthetic` / `_kanban_stop_synthetic`)→ `continue_turn=True`、
`final_response=None` → 答案存入 `_pending_verification_response`
(+是否已流式预览),预算耗尽时兜底。

| # | 门 | 触发条件 | finish_reason 标记 |
|---|---|---|---|
| 1 | verify-on-stop | 开关开启 ∧ 基于本 turn 文件改动路径构造的 nudge 非空(内部次数有界) | `verification_required` |
| 2 | `pre_verify` 插件钩子 | 本 turn 编辑过文件 ∧ 钩子返回 continue(兼容 Claude Code Stop-hook 协议 `{"decision":"block","reason"}`)∧ 次数未超 | `verify_hook_continue` |
| 3 | kanban 终态工具守卫 | worker 未调 `kanban_complete/block` 就想结束 | `kanban_terminal_required` |

无门命中 → 答案成为 final。

### 1.3.4 Tool round 子状态机(turn_tool_round.py + tool_executor.py)

**批次序列**:

1. `validate_tool_calls`:id 去重 → 幻觉工具名自动修复 → 无效名:混合批
   标记后继续(仅派发有效项);全无效 → 1..2 次追加 per-call 错误结果
   (`Tool 'X' does not exist. Available: ...`;空名给反回声文本)continue,
   第 3 次终态 return。JSON 参数:非 `}`/`]` 结尾 = 流中截断 → 立即终态;
   否则 1..2 次裸重试,第 3 次注入 per-call JSON 错误结果;
2. 去重 + `delegate_task` 限量;
3. `stage_tool_call_message`:丢弃裸 marker 内容、分类 housekeeping 轮、
   弹出 `_thinking_prefill` 行、重武装 nudge 预算;
4. **persist-before-execute**:先 flush 到 SQLite;失败 → break
   (`"session_persistence_failed"`,failed)——**绝不从纯进程态执行工具**;
5. 发中间评论、flush 流式框;
6. `_execute_tool_calls`(run_agent.py:1274):单调用直接串行;多调用 →
   `_plan_tool_batch_segments` 切成有序 (parallel|sequential) 段——并行安全
   = 只读目录 + 文件路径重叠准入(读读重叠可并行,写重叠 = 串行 barrier)
   + MCP opt-in;`_NEVER_PARALLEL_TOOLS`、参数不可解析、`delegate_task`、
   写重叠均为 barrier;
7. 执行后:持久化失败 break;guardrail 硬停 break(`"guardrail_halt"`);
   纯 `execute_code` 轮退还 step 预算;`compress_after_tool_results`(可因
   不可行动交接 break);continue。

**每调用终态结果**(全部按原调用序经 `_commit_tool_result` 变成
`role:"tool"`):

| 结局 | 结果内容 | 来源 |
|---|---|---|
| success | 工具结果串(可被 `transform_tool_result` 变换) | worker |
| error | `Error executing tool 'X': {exc}` | worker 异常 / 工具自报 |
| blocked | 作用域/插件/guardrail 文本(`blocked=True`) | gate chain |
| timeout | `timed out after Ns`(真结果在 deadline 快照与装配之间到达则**真结果赢过伪造超时**) | deadline |
| cancelled | `[Tool execution cancelled — X was skipped due to user interrupt]` | 中断 |
| invalid-args | 解析错误文本预填槽位,从不派发 | `_parse_tool_call` |
| invalid-name | 有效工具目录提示(混合批)/ 反回声文本(空名) | 校验 |
| thread-missing-result | worker 消失 | executor |
| abandoned-at-gate | worker 返回 None,主线程已写槽(不双报) | `_BatchAbandoned` |
| not-started | 解释器关闭,工具未启动 | `submit_all` |

## 1.4 核心组件实现细节

### 1.4.1 `_LoopState` + `_run_phase`:verdict 驱动引擎

**`_LoopState`**(conversation_loop.py:1273),~40 字段,三类:

- **turn 级固定**(从 TurnContext seed):`user_message`、`system_message`、
  `moa_config`、`original_user_message`(redirect 纠正会追加进来)、
  `conversation_history`、`turn_id`、插件上下文/预取缓存等;
- **turn 级可变**(被阶段函数重绑):`messages`(压缩/修复可整体替换)、
  `active_system_prompt`(failover 后重同步)、`current_turn_user_idx`
  (列表重写后重锚定)、`api_call_count`、`final_response`、`interrupted`、
  `failed`、`restart_count`(封顶 `max_retries`)、`_outer_error_count`
  (封顶 8)、`compression_attempts`(共享压缩预算,默认 3)、
  `_preflight_compression_blocked`(进展不足 latch)、
  `_provider_overflow_recovery_pending`(merge-only-True latch)、
  `codex_ack_continuations`(≤2)、`length_continue_retries`(≤4)、
  `truncated_tool_call_retries`(≤4)、`truncated_response_parts`、
  `_pending_verification_response`(stop gates 扣押的答案)、
  `_turn_exit_reason`(诊断标签)等;
- **每 step 重置**:`retry_count`、`max_retries`、`response`、`finish_reason`、
  `api_request_id`(`{turn_id}:api:{n}`)、fresh 的 `TurnRetryState`。

**verdict 概念**:2026-09 的大拆分(PR #102117)把单体 `run_agent.AIAgent`
里的内联循环体切成 turn_*.py 阶段函数,但**刻意保持原有控制流不变**——
原来写在循环体里的 `continue`/`break`/`return` 和局部变量重绑,拆出去之后
没有地方放,于是被**物化成返回值**:每个阶段返回一个 dataclass,
`action` 字段携带控制流意图,其余字段携带"这个阶段改写了哪些循环局部
变量"。verdict 不是事件、不是命令,它就是**被搬进返回值的局部变量重绑 +
goto**。

**verdict 类型体系(两层)**:

- **Tier 1(经 `_run_phase` 派发,字段按名拷回 `_LoopState`)**:16 个
  独立的普通 `@dataclass`(`IterationStart`、`AssembledRequest`、
  `PreflightGateVerdict`、`ApiCallVerdict`、`ApiErrorVerdict`、
  `ToolRoundVerdict`、`FinalResponseVerdict`、`OuterErrorVerdict` 等)——
  **没有共享基类**,唯一的"模式"是命名约定:`action: str` +
  `result: Optional[Dict] = None` + 其余字段与 `_LoopState` 字段**同名**
  (含前导下划线)。所有字段无类型标注、除 result 外无默认值——每个阶段
  必须显式把每个字段穿引出来;
- **Tier 2(由父阶段手工消费,从不进 `_run_phase`)**:约 12 个子
  verdict(`ToolValidationVerdict`、`StopGateVerdict`、`TruncationVerdict`、
  `ClassifiedErrorVerdict`、`OverflowVerdict`、`UnrecoveredErrorVerdict`
  等)。命名**不**带下划线(父阶段手工映射回下划线局部变量),有的干脆
  不用 action 而用布尔(`StopGateVerdict.continue_turn`、
  `PostToolCompressionVerdict.end_turn`)。唯一的继承出现在这层:
  "verdict 即工作状态"模式——`_Recovery(OverflowVerdict)` /
  `_Trunc(TruncationVerdict)` 把只读调用上下文(agent、api_messages…)
  也放进子类,sub-handler 就地改字段,`done(action)` 盖章返回。这类
  working-state 子类**绝不能**进 `_run_phase`(`fields()` 会把上下文字段
  也拷上 state)。

**action 语义**:标准集 `fallthrough | continue | break | return`,含义是
**loop 相对的**——外层循环里 continue/break 对应 Python 的
continue/break;重试循环里对应重试 while(实现为 `return None`);
`return` 恒为"这个 dict 就是整个 turn 的结果,立即返回"。**解释不是统一
switch,而是每个调用点只检查该阶段可能发出的 action**、按序 if 链。已知
重载:`check_api_response` 的 `break` 同时表示"成功拿到响应"和"restart
信号已武装",由下游 `apply_retry_restarts` + `response is None` 消歧。四个
恒 fallthrough 的阶段(`prepare_iteration` 等)action 仅为契约统一性存在,
调用点直接丢弃。`result` 载荷恒为 turn 结果 dict(`final_response,
messages, api_calls, completed, failed, failure_reason, ...`),tier-2 的
result 原样穿过父阶段冒泡。

**`_run_phase` 拷回机制**(conversation_loop.py:1356-1387,逐字):

```python
_PHASE_PARAMS: Dict[Any, tuple] = {}   # 按函数对象缓存参数名(去掉 "agent")
_LATCHED_VERDICT_FIELDS = {"handle_api_error": frozenset({"_provider_overflow_recovery_pending"})}

def _run_phase(fn, agent, state: _LoopState, **extra):
    params = _PHASE_PARAMS.get(fn)
    if params is None:
        params = _PHASE_PARAMS[fn] = tuple(p for p in inspect.signature(fn).parameters if p != "agent")
    verdict = fn(agent, **{n: extra[n] if n in extra else getattr(state, n) for n in params})
    latched = _LATCHED_VERDICT_FIELDS.get(getattr(fn, "__name__", ""), ())
    for f in fields(verdict):
        if f.name in ("action", "result"):
            continue
        value = getattr(verdict, f.name)
        if f.name not in latched:
            setattr(state, f.name, value)
        elif value:
            setattr(state, f.name, True)
    return verdict
```

- 参数绑定:按**参数名**从 state `getattr`,`extra` 覆盖同名 state 字段
  (只有两个阶段收 extra:`handle_api_error` 的 `api_error`、
  `handle_outer_loop_error` 的 `e`);参数名在 state/extra 都不存在 →
  dispatch 时 `AttributeError`(这是契约:"新的 helper 输入/输出只需要在
  `_LoopState` 加一个字段,别的什么都不用");
- latch:全表只有一条——`handle_api_error` 的
  `_provider_overflow_recovery_pending` merge-only-True(该阶段按次报告,
  但溢出武装的生命周期长于单次调用,falsy 值被忽略不清除)。按
  `fn.__name__` 查表,改函数名会静默丢掉 latch 语义;
- **静默漂移风险**:`_LoopState` 非 slots,verdict 里 typo/改名的字段会
  `setattr` 出一个没人读的影子属性,不炸不警告;没有任何检查器校验
  verdict↔state 字段对齐。

**构造惯例**:每个阶段定义局部 `_verdict(action, result=None)` 闭包,
快照当前全部局部变量,使每个退出点一行搞定、字段永远全量穿引
(`handle_api_error` 在每个 sub-verdict 之后重新 rebind ~8 个局部再重建
闭包);tier-2 working-state 子类用 `self.done()` / `fail_turn()` /
`end_turn()` 方法盖章。

**verdict 之外的第二控制通道**:verdict 只重绑引用;阶段可自由改
`agent` 属性(`_empty_content_retries` 等)、原地改 `messages` 列表,
`TurnRetryState`(`s._retry`)的 restart 标志位是与 action 并行的控制
信道——重试循环的 `break` 不看 restart 位就没有意义。

**可测性**:测试直接以显式 kwargs 调阶段函数 + `SimpleNamespace` 桩
agent,断言返回 verdict 的 action/字段 + 桩上记录的副作用;`tests/` 里
零处引用 `_run_phase` 或 `_LoopState`。阶段 helper 在 conversation_loop
**import 时绑定**("源码树替换不能在 turn 中途加载歪掉的阶段"),所以
patch 原模块函数拦不住 loop——loop 内部的次级 helper 反而刻意在阶段内
延迟 import,保证 `patch("agent.conversation_loop.X")` 生效。

### 1.4.2 `TurnRetryState` 与错误分类器

**`TurnRetryState`**(每个 API 调用块一份):20+ 个 `*_attempted` one-shot
布尔位——各 provider 的 401 凭证刷新、凭证池轮换 `has_retried_429`、图片
缩小、工具消息剥图、thinking 签名/加密内容/语法 payload 剥离、reasoning
强制、主 transport 重建 `primary_recovery_attempted`、`auth_failover_attempted`
等,每种恢复手段每块只许用一次;外加 4 个 restart 信号位:
`restart_with_compressed_messages` / `restart_with_length_continuation` /
`restart_with_rebuilt_messages` / `restart_with_redirected_messages`。

**`classify_api_error`** → `ClassifiedError{reason, status_code, retryable,
should_compress, should_rotate_credential, should_fallback, billing_unverified}`;
插件可经 `transform_api_error_classification` 钩子覆盖分类器。

**`FailoverReason` 枚举(25 值,error_classifier.py:33)**:`auth,
auth_permanent, billing, rate_limit, upstream_rate_limit, overloaded,
server_error, timeout, ssl_cert_verification, context_overflow,
payload_too_large, image_too_large, image_corrupt, model_not_found,
provider_policy_blocked, content_policy_blocked, format_error,
invalid_encrypted_content, multimodal_tool_content_unsupported,
reasoning_mandatory, thinking_signature, long_context_tier,
oauth_long_context_beta_forbidden, llama_cpp_grammar_pattern, unknown`。

### 1.4.3 上下文压缩器(context_compressor.py,4,949 行)

- **触发公式**:`threshold_tokens = (context_length − max_tokens) ×
  threshold_percent`,floor `MINIMUM_CONTEXT_LENGTH`,小窗口 cap 85%;
- **三个触发点**:turn 开始 preflight(带超时)、每次 API 前的
  `run_preflight_gate`、tool 结果回填后的 `compress_after_tool_results`;
  provider 413/溢出错误也路由进压缩(`turn_overflow.py`);
- **preflight 门的完整条件**(合取):压缩启用 ∧ 消息>1 ∧
  `compression_attempts < max(3)` ∧ 非 review-fork 首请求 ∧(未被进展
  latch 阻塞 ∨ provider 溢出强制)∧(不 defer 给真实 usage ∨ 强制)∧
  非失败冷却 ∧ `should_compress(pressure)`。溢出强制但冷却中 → return
  deferred;强制且耗尽或其他门拦 → **return 溢出耗尽终态(fail closed
  ——llama.cpp 会静默截断,宁失败不静默)**;
- **防抖熔断**:压缩冷却期 + 结构性无操作退避(没减掉东西就退避)+
  连续 2 次无效压缩 → 熔断,只有 provider 报告的真实 prompt_tokens 证明
  压缩生效才重新武装;
- **真实用量托底**:`_pressure_with_real_floor`——chars 估算被最近一次
  真实 prompt_tokens 托底,防非 ASCII 2× 低估死亡螺旋;
- **摘要方式**:廉价辅助模型总结中段,保护头部 + token 预算的尾部;tool
  输出先剪;另有 micro-compaction(逐轮折叠,默认关——破坏 prompt cache)
  与 native 服务端 compaction(OpenAI Responses,本地压缩器保持武装兜底);
- **prompt cache 字节级保护**:system prompt 按 session 持久化、逐字节还原;
  tools[] 顺序冻结;易变内容只进未持久化尾部;cache 断点(Anthropic
  `cache_control`)按 provider 重铺,failover 后重新 decoration。

### 1.4.4 空响应恢复阶梯(turn_empty_response.py,顺序承重)

进入条件:`<think>` 剥离后无可见内容。

1. **partial-stream 恢复**:流里已有可见文本 → 直接当 final,break;
2. **复用上轮内容**:上轮有内容 ∧ 那轮工具全是 housekeeping
   ({memory, todo_list, skill_manage, session_search})→ 复用,break;
3. **post-tool nudge(一次)**:最近 5 条有 tool 行 ∧ 未用过 → 追加
   `"(empty)"` 合成 assistant 行 + nudge user 行,continue;
4. **thinking 前缀续写(×2)**:有 reasoning → 把 reasoning 本身作为
   assistant 行(`_thinking_prefill` 标记)放回,让模型"看着自己的思考写正文";
5. **预算内空重试**:空响应签名去重(完全相同的空响应短路),预算按请求
   成本折减,5–60s 抖动退避可中断,continue;
6. **fallback provider**:激活后**回到外层 continue**(preflight 对新窗口重跑);
7. **终态 `"(empty)"` 哨兵**落库(留在 transcript 防续写重放);有 reasoning
   则给用户 500 字符预览,break。

### 1.4.5 InterruptControlMixin:中断控制(interrupt_control.py)

**原语**:`_interrupt_requested` + 消息/原因、per-thread 中断位(执行线程
+ 每个工具 worker tid;未绑定则挂起待发)、`_hard_interrupt_requested`
事件、`_pending_redirect`/`_pending_steer` 各带锁、`_model_request_active`
事件(redirect 锁下围住 provider 调用置/清)、socket 级 abort、压缩提交
栅栏(硬取消两相:认领**前**等完进行中的提交、认领**后**取消未决提交)、
活性 generation CAS(`require_generation`:abort 与恢复执行竞态时 CAS
失败即放弃)、`_active_children`(递归传播)。

**`interrupt(message, hard_cancel, require_generation)`**:generation 认领
→ redirect 锁下:栅栏等待 → 发布中断态(CAS 消费)→ 栅栏取消 →
**清空 `_pending_redirect`** → codex 原生 interrupt → 掐 socket → 执行线程
+ 全部 worker tid 置位 → 递归传播给子 agent。
`clear_interrupt(preserve_redirect=True)` 是 loop 的"是转向还是停止"测试:
仅当有 pending redirect 时才清位返回 True。

**`steer(text)`**:锁下追加/拼接,不打断。排空点:(a) tool round 后,(b)
`prepare_iteration` → 最新 tool result 后的独立 user 行;turn 结束仍剩 →
`result["pending_steer"]` 导出。

**`redirect(text)`**:codex → 原生 `turn/steer`。工具执行期(`_executing_tools`)
→ **降级为 steer + 向 worker 发 yield 请求**(前台终端命令把活进程交给
后台注册表)。否则 redirect 锁下:要求 `_model_request_active` 已置
(响应已完成则返回 False,由上层排新 turn);追加 pending_redirect
(`[Additional user correction]` 拼接)、置中断位——**只中断执行线程 +
掐请求 socket,不 fan-out worker/子 agent**。

**Phase × 动词矩阵**:

| Phase \ 动词 | `interrupt()` | `redirect()` | `steer()` |
|---|---|---|---|
| API 调用中 | 监视线程抛 `InterruptedError`;已流出的部分文本保留为 final,turn 结束 | 掐 socket;检测 pending redirect → 武装 redirect 重启 → 同一迭代退款重建(checkpoint 行 + 纠正行);响应与 redirect 交叉 → 响应作废 | 排队,下个 `prepare_iteration` 送达 |
| 退避睡眠中 | return 中止结果 | 武装 redirect 重启 break | 排队 |
| tool round 中 | worker 全体置位;未启动 future 取消;3s 宽限后合成 cancelled 结果 | 降级 steer + yield 请求 | 排队,tool round 后送达 |
| 迭代之间 | `begin_iteration` break | redirect 队列在中断检查**之前**排空(redirect 优先) | `prepare_iteration` 排空 |
| 错误处理中 | return 中止结果 | preserve-redirect → redirect 重启 break | — |

### 1.4.6 流式交付(StreamDeliveryMixin,stream_delivery.py)

- **默认开流式**(`_should_stream`)即使没有显示消费者——stale-stream 健康
  检查依赖 delta;ACP、无消费者的 MoA、mock 关闭;
- `interruptible_streaming_api_call`(chat_completion_helpers.py:3502):
  HTTP 请求跑在 **daemon worker 线程**,监视循环盯中断与 stale-stream
  (per-provider 超时:本地默认 900s、云端按上下文规模缩放、reasoning
  模型有 floor;跨 turn "stale streak" 熔断器);
- delta 经 `_fire_stream_delta` 扇出到 `stream_delta_callback`(display)+
  `_stream_callback`(TTS),带**跨 delta 的 `<think>` 状态化剥离器**、
  单写者栅栏(被取代的重试不能交错吐 token)、`on_stream_delta` 插件钩子
  (排队,不在 token 路径上);
- **partial-stream 恢复**:错误前已交付 delta → 用已流出文本合成
  `finish_reason="length"` 桩,走续写 nudge 而不是重试(上下文溢出除外,
  终态)。

### 1.4.7 工具执行器:planner、gate chain 与超时

- **每调用 gate chain**(`_dispatch_authorized_once`):作用域封锁 →
  `pre_tool_call` 插件钩子(经并发授权门串行化,有界锁等待;可拦可改参)
  → `ToolGuardrails.before_call`(循环/滥用守卫,可硬停 turn)→ 人工审批
  (pattern allowlist、session/永久批准记忆、YOLO 模式、gateway 审批往返、
  连续拒绝熔断器;**等待时间从批 deadline 剔除,在等待源头计量**)→
  start-order gate(审批提示按提交顺序出现;有界等待,过期允许乱序)→
  带活性心跳执行;
- **超时**:并发批 deadline = `timeouts.tools.*` / 环境变量(+审批剔除);
  5s 切片轮询、~30s 心跳;到期/中断:取消未启动 future、gate 放弃、worker
  tid 置中断位(中断给 3s 宽限),executor 放弃不 join。串行:每调用
  deadline,200ms 级中断轮询切片;
- **参数规范化缓存**:`_CANON_ARGS_CACHE`(32 MB 字节预算,记忆化),
  发送路径改写在结构化克隆上 copy-on-write,从不触碰持久 transcript;
- **文件系统 checkpoint**(tools/checkpoint_manager.py):文件变更工具执行
  前做 shadow-git 快照(共享 bare store,每目录每 turn 一次);
- **子 agent**:`delegate_task` fork `AIAgent`(顶层后台 + 句柄,嵌套同步),
  独立 step 预算(默认 50)、硬中断递归传播、worktree 隔离、HMAC 签名生命
  周期契约。

---

# 二、openclaw(TypeScript,三层洋葱)

## 2.1 Loop 结构总览

<svg xmlns="http://www.w3.org/2000/svg" width="940" height="666" font-family="ui-monospace,'SF Mono',Menlo,Consolas,monospace" font-size="13" fill="#1e293b">
  <defs>
    <marker id="ah2" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" fill="#475569"/>
    </marker>
  </defs>
  <rect x="2" y="2" width="936" height="662" rx="10" fill="#f4f6fa"/>
  <rect x="20" y="12" width="900" height="28" rx="6" fill="#eef2ff" stroke="#6366f1"/>
  <text x="34" y="31">Discord / Telegram / … 渠道消息</text>
  <line x1="470" y1="40" x2="470" y2="60" stroke="#475569" marker-end="url(#ah2)"/>
  <rect x="20" y="64" width="900" height="64" rx="6" fill="#f0fdf4" stroke="#16a34a"/>
  <text x="34" y="84" font-weight="bold">App 层:FollowupQueue(每 session)</text>
  <text x="34" y="104">模式 steer(默认)/followup/collect/interrupt · debounce 500ms · cap 20 · 超容 drop=summarize(折叠成摘要行)</text>
  <line x1="470" y1="128" x2="470" y2="148" stroke="#475569" marker-end="url(#ah2)"/>
  <rect x="20" y="152" width="900" height="420" rx="8" fill="#f8fafc" stroke="#334155" stroke-width="1.5"/>
  <text x="34" y="172" font-weight="bold">Layer C:run attempt 编排  while(true) —— maxAttempts = clamp(24 + 8×auth-profile 数, 32, 160)</text>
  <text x="34" y="192">瞬时错误:退避重试(8 次 / 90s 窗口,从当前 transcript 续跑,绝不重发原 prompt)</text>
  <text x="34" y="212">溢出:压缩后重跑(≤3) · auth:轮换 auth-profile · 耗尽:failover 换模型 · 空响应/reasoning-only 小额度重试</text>
  <rect x="36" y="228" width="868" height="328" rx="8" fill="#fff7ed" stroke="#ea580c"/>
  <text x="50" y="248" font-weight="bold">Layer B:Agent —— steeringQueue / followUpQueue(submitted 栅栏) · activeRun + AbortController</text>
  <rect x="52" y="264" width="836" height="276" rx="8" fill="#eff6ff" stroke="#2563eb"/>
  <text x="66" y="284" font-weight="bold">Layer A:核心 loop(agent-loop.ts,turn 粒度,无step 上限)</text>
  <text x="66" y="306">外层 while(true):  ◄── follow-up 续命(getFollowUpMessages 非空则再来一圈)</text>
  <text x="66" y="326">  内层 while(hasMoreToolCalls ∨ pending):</text>
  <text x="66" y="346">    abort 检查 → 提交 steering 消息</text>
  <text x="66" y="366">    streamAgentResponse:流事件 → message_update(partial 占位槽实时编辑)</text>
  <text x="66" y="386">      async 工具:toolcall_end 即片段落库(先有持久 owner(turnId))→ 即刻执行(模型还在输出)</text>
  <text x="66" y="406">      在线 steering:投影 append-only 则推上活跃连接</text>
  <text x="66" y="426">    终端工具批(并行/串行;beforeToolBatch = 工具循环检测拦截点)</text>
  <text x="66" y="446">    hasMoreToolCalls 三元判定 → turn_end</text>
  <text x="66" y="466">    shouldStopAfterTurn 钩子(steering 优先于停止)→ steering 轮询</text>
  <text x="66" y="486">  队列空 → agent_end;pending 非空 → 下一 turn</text>
  <rect x="20" y="588" width="900" height="64" rx="8" fill="#fdf2f8" stroke="#db2777"/>
  <text x="34" y="608" font-weight="bold">失控防护(tool-loop detection):6 检测器(窗口 30 / 警告 10 / critical 20 / 全局熔断 30)</text>
  <text x="34" y="628">警告附在结果尾 → 第一次 critical 整批 blocked + 引导 → 第二次 terminateRun;另有 post-compaction 守卫与 idle-timeout 成本熔断</text>
</svg>

**pi 血统**:核心 loop 是 pi-agent 的 vendored 后代(`packages/agent-core`,
不再是外部依赖)——与本仓 `Improvement-with-pi.md` 对比的 Pi 同源,可视为
"Pi 的 loop 在多渠道网关场景下长大后的样子"。

三层,越外层管越粗粒度的容错:

- **Layer A 核心 loop**(`packages/agent-core/src/agent-loop.ts`,1698 行):
  turn 粒度双 while;入口 `agentLoop(prompts, context, config, signal,
  streamFn)` / `agentLoopContinue`(重试/续跑,断言末条非 assistant),
  返回 `EventStream<AgentEvent>`。**事件词汇表**:`agent_start → (turn_start
  → message_start/update/end + tool_execution_start/update/end → turn_end)*
  → agent_end`。无独立 step 概念——turn 即step 单位;
- **Layer B Agent 类**(agent.ts):持有 `messages/tools/model`、
  steering + follow-up 双队列、`activeRun {promise, abortController}`;
  `prompt()` 在 run 活跃时抛错("Use steer() or followUp()");
- **Layer C run 编排**(`src/agents/embedded-agent-runner/run-loop.ts`):
  `while(true)` 的 attempt 重试环——模型 failover、溢出压缩重跑、空响应
  重试、成本失控熔断,全在这层。

## 2.2 一次消息的事件流转全过程

1. **渠道消息进入 app 层**(`src/auto-reply/reply/queue/*`):解析队列模式
   `内联指令 ?? session 条目 ?? cfg.byChannel[ch] ?? 全局 ?? "steer"`;
   debounce 500ms 合并连发。活跃 run 存在时:`steer`(默认)→ steer 候选
   park 进 steerPending 子状态机,预约(reserve)成功注入 agent-core
   steering 队列,失败回落 followup 队列;`followup` → 排队等下个 run;
   `collect` → 排空时合并成单批;`interrupt` → 清 command lane + 打断当前
   run 后立即跑。心跳被可见 user turn 抢占,自身从不 steer。
2. **Layer C 起 attempt**:预算检查(`maxAttempts = clamp(24 + 8×auth-profile
   数, 32, 160)`)→ `prepareAndDispatchEmbeddedRunAttempt` 驱动 AgentSession
   (包着 Layer B 的 Agent)。
3. **runLoop 逐 turn 推进**(判定表见 §2.3.1):abort 检查 →
   `commitPendingMessages`(取消状态在 message_start 前后各复查一次,防
   被取消的批变成空 provider 续写)→ `streamAgentResponse`。
4. **流式期间**:`message_update` 事件被 embedded-agent 订阅者消费,按
   渠道 `streaming.mode = off|partial|block|progress` 分流——partial/progress
   原地编辑一条消息(progress-draft-compositor 渲染含工具行的滚动状态卡);
   block 模式喂 EmbeddedBlockChunker 切块逐条发(§2.4.8)。**async 工具**
   在 `toolcall_end` 即片段落库并开始执行(模型还在输出);**在线 steering**
   检查新消息能否投影为 append-only 推上活跃连接(§2.4.3)。
5. **终端工具批**:未被流内执行的调用 → `executeToolCalls`:
   `beforeToolBatch`(= tool-loop detection 拦截点)→ `beforeToolCall`
   (策略管线/exec 审批)→ 执行 → `afterToolCall`/`afterToolOutcome` →
   `tool_execution_end` 按完成序 emit、toolResult 消息按 assistant 源序落。
6. **turn 收尾**:`turn_end` → `prepareNextTurn`(可换 context/model/
   thinking)→ `shouldStopAfterTurn` 钩子(steering 已排出则推迟——steering
   优先于停止)→ turn 后 steering 轮询;内层退出后排 follow-up,空则最后
   一次 steering 兜底轮询,仍空才 `agent_end`。
7. **attempt 结束回 Layer C 分类**:瞬时错误 → 退避重试(8 次/90s 窗口,
   **从当前 transcript 续跑,绝不重发原 prompt**);溢出 → 压缩后重跑(≤3);
   auth → 轮换 auth-profile;耗尽 → failover 换模型;终态隐形重试
   (reasoning-only ≤2、缺 assistant ≤1、空响应 ≤1、finalize 修订 ≤3)。
8. **渠道输出 final**:block coalesce(idle 1000ms)+ humanDelay(首条后
   随机 800–2500ms,模拟打字节奏)。

## 2.3 阶段深挖

### 2.3.1 runLoop 判定表(agent-loop.ts L235–540)

**循环级状态变量**:

| 变量 | 初值 | 变更 |
|---|---|---|
| `config` | initialConfig | `prepareNextTurn` 返回覆盖时整体替换(换 model/thinkingLevel/reasoning) |
| `firstTurn` | true | 首个内迭代后 false(首个 `turn_start` 由外壳发) |
| `turnOpen` | true | 追踪未配对 turn_start;abort 路径若 `!turnOpen` 先补发 |
| `turnTainted` | 从 messages 反向扫描(网络来源工具结果/已标记 assistant) | **user** steering 提交时清零;`\|\|=` 工具结果污点;盖到每条 assistant 上(提示注入溯源) |
| `criticalToolLoopSeen` | false | 任何批 intervention → true;run 内粘性;`Agent.prompt()` 重置 |
| `pendingMessages` | 初始 steering 轮询 | = 批的 steeringMessages / turn 后轮询 / follow-up 排空 / 最终轮询 |
| `hasMoreToolCalls` | 每外层进入置 true | 每 turn 重算(见 #8) |

**每内迭代精确顺序**:

| # | 条件 | 结局 |
|---|---|---|
| 1 | 顶部 `signal.aborted` | `stopIfAborted()`:落库 `stopReason:"aborted"` 的失败 assistant(带污点)+ 事件括号 + `<turn_aborted>` 引导(turnHandoff 豁免)→ `agent_end`,**return** |
| 2 | pending 非空 → `commitPendingMessages()`;全部被取消且无工具续写 | `continue`(不产生空 provider 续写) |
| 3 | 提交后 abort | 同 #1 |
| 4 | `streamAgentResponse(...)`(§2.3.2) | — |
| 5 | `providerFailed = stopReason ∈ {error, aborted}` | 剩余工具清空 |
| 6 | 否则 `remainingToolCalls` = 未被流内执行的调用 → 终端批 `executeToolCalls` | 结果推入 context+newMessages |
| 7 | 合并批:`terminate = every`,`terminateRun = some`,`fatal = first` | — |
| 8 | `hasMoreToolCalls = streamed.continuationRequired \|\| (stopReason==="stop" && endTurn===false && !batch.terminate) \|\| (batch && !batch.terminate)` | — |
| 9 | emit `turn_end`;`batch.fatal` | **throw** → `pushLoopFailure`(失败 assistant + turn_end + 中断消息 + agent_end;队列 restore) |
| 10 | `stopReason==="aborted"` | 中断消息(非 handoff)→ **return** |
| 12 | `batch.terminateRun` | 合成 `TOOL_LOOP_RECOVERY_TERMINATED_MESSAGE` assistant + 完整事件括号 → **return** |
| 13 | `providerFailed` | `agent_end` → **return**(重试交给 Layer C) |
| 14 | `prepareNextTurn` | 可换 `state.context`/model/thinking |
| 16 | pending 空 ∧ `shouldStopAfterTurn()` | **return**(steering 已排出时推迟) |
| 17 | pending 空 → turn 后 steering 轮询 | — |
| 19 | stop 快照 | **先把 pending steering 落进 transcript** 再 return(旧主关闭,后继重新接纳) |
| 20 | 内层 `while (hasMoreToolCalls \|\| pending 非空)` 复查 | 下一 turn |

**外层再入**(L527–536):内层退出 → `getFollowUpMessages()`;空 → **最后
一次 steering 轮询**("agent_end 不能搁浅已接受的 steer");仍空 → break
→ `agent_end`。**此层无任何 turn 计数上限**。

### 2.3.2 streamAgentResponse 状态机(agent-stream-response.ts L110–410)

状态:`partialMessage`、`partialIndex`(占位在 `context.messages` 的槽)、
`committedContentCount`(已提交内容游标)、`streamedTurnId`、
`executedIds:Set`、`admissions`/`executions` 双 promise 链、
`executionFailure`、`executionAbort:AbortController`。

| 流事件 | 效果 |
|---|---|
| `start` | 部分消息插入 messages 尾槽,emit `message_start` |
| `text_*` / `thinking_*` / `toolcall_start/delta` | 更新槽位;`contentIndex < committedContentCount` 的跳过;emit 的 `message_update` 中 contentIndex 按已提交游标**重基** |
| `toolcall_end` ∧ 该调用 `async:true` ∧ 未执行过 ∧ 之前未提交内容全为非工具或已 async | **片段提交**:切出 `[committed..idx]` 前缀,强制 `stopReason:"toolUse"`,usage 清零("usage 属于终端片段"),`turnId=uuidv7()` → `commitFragment`(**awaited,先于任何副作用**)→ 游标推进 → `enqueueTools(prefix)`,**模型继续输出** |
| `done` / `error` | `finalizeAssistantMessage` |
| 流无终态即结束 | `result()` 大声拒绝(契约违例) |

**`enqueueTools` 准入链**:

```ts
const execution = previousAdmission.then(async () => {
  const batch = await executeAsyncTools(message, calls, executionSignal, emit, {
    waitForPrevious: () => previousExecutions,   // sequential 批等完前批
    onParallelStarted: releaseAdmission,          // 首个 body 启动即放行下一批准入
    hasUnobservedAsyncToolResults });
  batches.push(batch);
  if (batch.fatal || batch.terminateRun) executionAbort.abort(...);
}).catch(e => { executionFailure ??= {error:e}; executionAbort.abort(e); })
  .finally(releaseAdmission);
executions = Promise.all([previousExecutions, execution]).then(() => {});
```

批 N+1 的**准入**等批 N 首个 body 启动或完成——"流式批串行准入,body
重叠执行";`executedIds` 在入队时(执行前)标记供终端扫描去重。

**`finalizeAssistantMessage` 收尾**:失败终态先 `executionAbort` 栅栏掉
已排队未启动的工具 → 输出上限特例(先 `await executions` 再改写
stopReason/errorCode)→ 非 toolUse 结束时剥掉不可执行的非 async 工具调用
→ `commitFragment(final)` → 终端残余 async 调用入队 → `await executions`,
rethrow `executionFailure` → **`continuationRequired = await
steering.finish()`**——仅当在线 steering 被接受进活跃响应 ∧ 传输层
`needsContinuation()` 为 true(`hasMoreToolCalls` 的第一析取项)。

### 2.3.3 工具批生命周期

```
pending → validate ─解析/未知工具/校验失败→ IMMEDIATE(error)
        → beforeToolBatch(整批一次,openclaw 接 admitToolCallBatch)
            ─intervention→ 全批 BLOCKED(tool-loop)[二次 critical 则 terminate/terminateRun]
        → tool_execution_start → beforeToolCall ─{block:true}→ IMMEDIATE("blocked",未启动)
        → 任一预检点 signal.aborted → IMMEDIATE("Operation aborted")
        → READY{execute(onImplementationStart), dispose}
READY ─启动前 steering 轮询非空→ SKIPPED("Skipped to process an incoming message.",
        {status:"skipped", deniedReason:"steering"})
      ─启动:onStart → lifecycle.commitReadyCalls(可 throw → REJECTED → 批 fatal;
        兄弟未启动 → SKIPPED("Tool execution was blocked before launch.",
        deniedReason:"tool-admission"))
      ─executing:onUpdate → tool_execution_update;body 可在 start 前 resolve
executing ─resolve→ finalize:afterToolCall(仅已启动;可改写 content/details/
              terminate/isError;throw→error) → afterToolOutcome(**所有**结局含
              blocked/skipped;同等改写权;throw→error,terminate 保留)
              → 循环警告追加 → tool_execution_end(完成序) → toolResult 消息(源序)
          ─reject→ error 结果;callerCancelled ⇔ 已启动 ∧ aborted ∧ error===signal.reason
break 后未启动尾部 → completeUnstartedToolCall(admission|steering|"Operation aborted")
```

并行判定:`config.toolExecution==="sequential"` 或批内任一工具
`executionMode==="sequential"` → 整批串行。并行启动**串行化**
(`launchParallelToolCalls`):下一个 launch 只能从前一调用的 `onStart`
微任务(或 pre-start 完成)发起——guard→commit→implementation 三步相邻。
批 terminate ⇔ 所有已定结果 `terminate===true`。

### 2.3.4 Steering 的 7 个检查点

| # | 位置 | 说明 |
|---|---|---|
| 1 | run 开始 | `Agent.continue()` 已排空时置 `skipInitialSteeringPoll` 防双排 |
| 2 | **活跃响应内**(在线 steering) | 入队即转发;设置 `continuationRequired` |
| 3 | 串行组每次调用前 | 非空 → 释放剩余为 skipped,break;运行中的调用不受影响 |
| 4 | 两模式启动前一次 | 并行批的**唯一**一次轮询 |
| 5 | 串行组末次调用完成后 | "最后一个调用期间接受的 steer 必须优先于停止钩子" |
| 6 | turn 后(批没产生 steering 时) | 成为下一 turn 的 pendingMessages |
| 7 | `agent_end` 前、follow-up 排空为空之后 | 防异步排空期间接受的 steer 被搁浅 |

契约(types.ts:310):一次轮询返回的批**原样**带进下一 turn,不再重轮询
(保持队列排空顺序);流式批期间轮询按响应记忆化。

### 2.3.5 Layer C:attempt 管线(run-loop.ts L290–706)

管线:预算检查 → dispatch → 归一化 → continueAfterAttempt → 权限重启 →
恢复 → assistant 失败 → 已定 turn 收尾 → 终端超时 → 终态。每级返回
`complete | retry | proceed`。

**预算与常量**:`maxAttempts = clamp(24 + 8×auth-profile 数, 32, 160)`;
`progress_continuation` 类重试**退还**预算;瞬时:`MAX_TRANSIENT_RETRIES=8`
(限流 `MAX_RATE_LIMIT_ATTEMPTS=10` 即 9 次重试),窗口 90s(限流/输出上限
豁免窗口),退避 `min(30s, 1s·2^(n−1)) × (0.5+rand)` 与 Retry-After 取
max(连过时 HTTP-date 都解析);溢出压缩 ≤3;空闲超时熔断:连续 5 次
(计费的部分 token ≠ 进展);隐形终态重试:reasoning-only ≤2、缺
assistant ≤1、空响应 ≤1、压缩续写 ≤1、finalize 修订 ≤3、未分类空错误 ≤3。

**分类 → 动作**(节选):重试预算耗尽 → 可升级则 `fallback_model` 否则
返回错误载荷(complete);`before_agent_run` 钩子拦截 → complete(blocked);
溢出预检命中(`fits|compact_only|truncate_tool_results_only|compact_then_truncate`
四路)→ retry;瞬时且重放安全 → 退避后 retry;attempt 后溢出 → 压缩
retry(≤3;压缩后压力仍在 → 不再压缩直接 retry;耗尽 → 浮出);不支持的
thinking level → 降级重试;auth 刷新成功 → retry;过载轮换超限且配了
fallback → 升级换模型;终态各类 → 带内部指令 prompt 的小额度 retry,否则
`completeEmbeddedRun`。

FailoverReason 码(gateway-protocol):`auth, auth_permanent, format,
rate_limit, overloaded, billing, server_error, timeout, tls_certificate,
context_overflow, model_not_found, session_expired, empty_response,
no_error_details, unclassified, unknown`。

## 2.4 核心组件实现细节

### 2.4.1 FollowupQueue(app 层队列,src/auto-reply/reply/queue/*)

**`FollowupQueueState`**(进程级注册表按 session key):`items`、`inFlight`
(送达期间保留在 items 但不计容量/深度)、`draining/drainOwner`、
`steerAcceptanceTail`、`summaryLines/summarySources`(index 对齐;源保持强
引用使取消能追踪已摘要内容)、`droppedCount/evictedSummaryCount`、`lastRun`。

默认:debounce 500ms、cap 20、drop=`summarize`(被挤掉的消息**折叠成摘要行**
而非静默丢弃)。模式规范化:`interrupt|interrupts|abort → interrupt` 等。

**steer 候选子状态机**:

```
enqueued(steerCandidate) → steerPending{phase:"waiting", predecessor, settle}
  admit(): await predecessor(与 abort 竞速) →
    "cancelled"(已中止/不再被 park 持有)
    "fallback"(前驱拒绝或 steerPending 被替换)→ 留队作 followup
    "steer" → phase:"injecting"(注入方判定重放安全)
  accepted/fallback → settle;consume() → FIFO 保序移除、清 steerAnchor/
    protectFromQueueOverflow、完成生命周期、空队 GC
```

drop 处置:`queue-cap | queue-cap-old | queue-cap-new` 经 `onQueueDisposition`
上报;`steerAnchor/protectFromQueueOverflow` 保护 steer 候选不被驱逐;
延迟溢出在 consume 时重放。

### 2.4.2 PendingMessageQueue(核心层队列,agent.ts:164)

`QueueMode = "all" | "one-at-a-time"`。每消息:`messages[] → inFlight[]`
(排空后、`message_end` 提交前持有)→ committed;**`submitted:Set`** =
被在线 steering 保留——后续排空**停在第一个未提交边界**(排队输入不能改写
在途续写);`cancelled:WeakSet` 由 `consumeQueuedMessageCancellation` 消费。
run 结束 `restore()`:`messages = [...inFlight, ...messages]`、清 submitted
("准入栅栏属于已关闭的 run")。`peek()` 在 inFlight 非空时返回 inFlight
(工具检查点把下一注入批固定在响应存活期间)。

### 2.4.3 stream-steering(在线 steering,stream-steering.ts)

每响应至多一次提交。流程:传输层给 `onActiveResponse(control)` → 订阅
队列观察者 → 入队时 `peek()` → `reserve(messages)`("一旦提交可能已过线,
本地取消不得声称已撤回")→ 全上下文投影对比(`JSON.stringify` 前缀比较;
任何前缀改写 → 释放并放弃,回退普通排队)→ 全 user 检查 →
`control.steer(userMessages)` 推上活跃连接(OpenAI Responses websocket
传输支持)。`finish()`:停止、等转发链、抛存储的失败、返回
`needsContinuation?.() === true`。

### 2.4.4 Tool-loop detection(tool-loop-detection.ts / tool-loop-admission.ts)

共享态:per-session 滑动窗口 `TOOL_CALL_HISTORY_SIZE=30`,按 runId 划界;
条目 `{toolName, argsHash, toolCallId, outcomeKind?, resultHash?}`。
"无进展" = 结局哈希相同(剥离 exec/send 的易变字段;`tool-loop-veto`
结局单独标记)。阈值:警告 10(固定,调参已在 #111382 退役)、
`UNKNOWN_TOOL_THRESHOLD=10`、`CRITICAL_THRESHOLD=20`、全局熔断 30。

| 检测器(优先级序) | 命中定义 | 警告 | critical |
|---|---|---|---|
| `unknown_tool_repeat` | 未知工具错误连击 | — | ≥10 |
| `global_circuit_breaker` | 任意工具无进展连击 | — | ≥30 |
| `known_poll_no_progress` | 已知轮询工具,同参 + 同结局连击 | ≥10("加大等待或报告失败") | ≥20 |
| `ping_pong` | A/B 调用签名交替 | ≥10 | ≥20 **且**有无进展证据 |
| `generic_repeat` | 非轮询:警告看同参计数;critical 必须结局证明无进展 | ≥10 | 无进展连击 ≥20 |
| `argument_churn` | 在稳定参数模式集合内打转 | ≥10(仅警告,带 livenessSignal) | — |

**拦截与升级**:拦截点 = agent-core `beforeToolBatch`(`admitToolCallBatch`
原子批准入,兄弟作为合成记录投影进历史副本,防无关兄弟驱逐真实历史)。
警告 → 按 `runId:detector:tool:hash` 去重,附在工具结果文本尾部,调用照常
执行。第一次 critical → `ToolLoopIntervention`,**整批 blocked**——触发者
得 `reason + "Do not repeat this exact tool action. Reassess…"`,兄弟得
"另一调用导致未执行";仅同 `actionKey` 的调用记为 veto 证据;
`criticalToolLoopSeen=true`(run 内粘性)。第二次 critical →
`terminal:true` → "This run is stopping now" + `terminateRun`。
**post-compaction 守卫**:每次压缩成功即武装;窗口 3 个 attempt;基线 =
压缩前尾部的 `toolName\0argsHash` 签名;窗口内 ≥3 次 tool+args+**result**
三重相同("相同结果证明压缩没有改变循环")→
`PostCompactionLoopPersistedError` 中止 lane。

### 2.4.5 Compaction 与 tool 结果预算

- **压缩**(packages/agent-core/src/harness/compaction/compaction.ts):
  触发 `contextTokens > contextWindow − reserveTokens`(默认 reserve 16384、
  `keepRecentTokens` 20000);切点回退到 turn 边界、绝不拆 tool call/result
  对;结构化摘要模板(Goal / Constraints / Progress / Key Decisions /
  Next Steps / Critical Context),劈开的 turn 有单独前缀摘要 + 迭代合并;
  摘要硬上限 `MAX_COMPACTION_SUMMARY_CHARS = 16000`;文件操作元数据与最近
  未解决的用户请求(≤800 字符)强制幸存;
- **溢出恢复**(run/overflow-context-recovery.ts):溢出错误 → 压缩 →
  **重试 attempt**(≤3)→ 降级 tool 结果截断 → 报错并给 "/reset" 指引;
  预检(run/preemptive-compaction.ts)每次 provider 调用前估压力,四路
  `fits|compact_only|truncate_tool_results_only|compact_then_truncate`;
  reserve 有效值 cap 窗口 25%;
- **tool 结果预算**(tool-result-truncation.ts + tool-result-limits.ts):
  单条按窗口分级 16k/32k/64k 字符、单条 ≤30% 窗口、合计 ≤50% 窗口;截断
  是**非破坏投影**(经 `transformContext` 发送前变换,不改库),保留
  spill 文件指针与错误尾部(head + `…omitted…` + tail);cache-TTL 剪枝:
  5 分钟外的旧 tool 结果可置换占位符(软剪 0.3 上下文占比、硬清 0.5);
- **Anthropic 服务端 compaction** 亦支持(`anthropicServerCompaction`,
  compaction-replay 传输重放 provider 拥有的压缩块);**context engine**
  (src/context-engine/)是可插拔装配/压缩/召回槽位,legacy 引擎默认。

### 2.4.6 Abort 与 turn-interruption

| 原因 | 生产者 | turnHandoff |
|---|---|---|
| `"user_abort"` | 停止短语(~50 个多语言)/`/stop` → `abortByUser()` | 否 |
| `"restart"` | 配置/权限重启 | 否 |
| `"superseded"` | 新 run 接管 session | 否 |
| `{code:"sessions_yield", turnHandoff:true}` | yield 类工具 | **是** |
| `PostCompactionLoopPersistedError` | post-compaction 守卫 | 否 |
| "Model response closed" / 批 fatal | 仅内部 `executionAbort`,从不是 run 信号 | n/a |

**每路径的 transcript 终态**:loop 检查点 abort → 合成
`{content:[{text:""}], stopReason:"aborted", errorMessage}` assistant +
`customType:"openclaw:turn-aborted"` 引导消息(`convertToLlm` 时投影为
user 角色;handoff 豁免——"干净、刻意的停止之后,下一 turn 不该被告知工具
可能部分执行");流中 abort → 半截内容经 `commitFragment` 提交为
`stopReason:"aborted"` 的最终 assistant;loop 抛出 → `pushLoopFailure`
(失败 assistant + turn_end + 中断消息 + agent_end,队列 restore);被
abort 的工具 → `"Operation aborted"` 错误结果,`callerCancelled` 抑制网络
污点。**不变式:transcript 绝不以悬空 toolUse 结尾**。

### 2.4.7 Provider 层与重试归属

SDK 自带重试显式关闭(`maxRetries: 0`);重试全部归 Layer C 的
failover-retry-controller。provider 抽象:`packages/ai` 的 `ApiRegistry`
(anthropic-messages / openai-completions / openai-responses / google /
vertex / mistral / bedrock …),模型是数据(llm-core 目录 + alias/override
解析);扩展可注册 provider。`before_provider_request` /
`after_provider_response` 扩展事件包住 provider 调用。

### 2.4.8 渠道渲染:EmbeddedBlockChunker 与 progress 卡片

- **block 模式**(embedded-agent-block-chunker.ts):Markdown 感知的状态化
  分块器——`minChars/maxChars` 默认 800/1200,切点偏好 段落 → 换行 → 句子;
  **拒绝在代码围栏内切**,被迫切时合成闭合/重开围栏行(保留语言标签);
  下游 coalesce(idle 1000ms)+ humanDelay(首条后 800–2500ms 随机,模拟
  打字);
- **partial/progress 模式**:`emitAssistantStreamData({text, delta,
  replace})` 原地编辑一条消息;progress-draft-compositor 渲染含工具状态行
  的滚动卡片;
- **turn-taint 溯源**:网络来源的工具结果标记 turn 污点
  (`__openclaw.resultContentSource === "network"`),经
  `withAssistantTurnTaint` 传播到 assistant 消息——prompt 注入的来源追踪;
- 另有 cron/heartbeat(心跳 run 活跃时被丢弃而非排队)、多渠道 session
  路由(reply-run registry,每 session 一个活跃操作 + 全局 lane)、CLI
  后端 dispatch(可把 run 派给 Claude Code CLI / Codex app-server,与
  embedded 路径互为 failover)。

---

# 三、deepseek-harness(TypeScript,事件溯源)

## 3.1 Loop 结构总览

<svg xmlns="http://www.w3.org/2000/svg" width="940" height="756" font-family="ui-monospace,'SF Mono',Menlo,Consolas,monospace" font-size="13" fill="#1e293b">
  <defs>
    <marker id="ah3" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" fill="#475569"/>
    </marker>
  </defs>
  <rect x="2" y="2" width="936" height="752" rx="10" fill="#f4f6fa"/>
  <rect x="20" y="12" width="280" height="28" rx="6" fill="#eef2ff" stroke="#6366f1"/>
  <text x="32" y="31">followup() → next-turn(+唤醒)</text>
  <rect x="330" y="12" width="280" height="28" rx="6" fill="#eef2ff" stroke="#6366f1"/>
  <text x="342" y="31">steer() → next-step(+唤醒)</text>
  <rect x="640" y="12" width="280" height="28" rx="6" fill="#eef2ff" stroke="#6366f1"/>
  <text x="652" y="31">inject() → next-step(不唤醒)</text>
  <line x1="160" y1="40" x2="380" y2="82" stroke="#475569" marker-end="url(#ah3)"/>
  <line x1="470" y1="40" x2="470" y2="82" stroke="#475569" marker-end="url(#ah3)"/>
  <line x1="780" y1="40" x2="560" y2="82" stroke="#475569" marker-end="url(#ah3)"/>
  <rect x="20" y="86" width="900" height="84" rx="8" fill="#fefce8" stroke="#ca8a04"/>
  <text x="34" y="106" font-weight="bold">持久化 session 事件日志(JSONL,append-only)—— 唯一事实源(source of truth);输入落为 agent/inbox/spliced 事件</text>
  <text x="34" y="126">turn/start · step/start · user/message · assistant/message|attempt · tool/call · tool/result</text>
  <text x="34" y="146">llm/retry · compaction/start|end · agent/inbox/spliced · request/header …</text>
  <line x1="470" y1="170" x2="470" y2="192" stroke="#475569" marker-end="url(#ah3)"/>
  <text x="486" y="186">投影 fold(一切状态皆为推导,无内存事实源)</text>
  <rect x="20" y="196" width="900" height="46" rx="6" fill="#f5f3ff" stroke="#7c3aed"/>
  <text x="34" y="216">inbox / llmRetry / turnBoundary … 各投影</text>
  <text x="34" y="234">deriveMessages() → LLM 请求消息列表</text>
  <rect x="20" y="258" width="900" height="100" rx="8" fill="#f0fdf4" stroke="#16a34a"/>
  <text x="34" y="278" font-weight="bold">Phase 状态机</text>
  <text x="34" y="298">idle ── wakeDriver(followup/steer) ──► running{abort, turn, step, wakeRequested}</text>
  <text x="34" y="318">running ── kick() 结束 ──► idle(锁存(latched)的唤醒此时重放);idle ⇄ maintenance(互斥)</text>
  <text x="34" y="338">cancel(cause) → 驱动经 throwIfAborted unwind,经 kick finally 落回 idle</text>
  <line x1="470" y1="358" x2="470" y2="386" stroke="#475569" marker-end="url(#ah3)"/>
  <text x="486" y="378">kick(): while (await turn()) { }</text>
  <rect x="20" y="390" width="900" height="250" rx="8" fill="#eff6ff" stroke="#2563eb" stroke-width="1.5"/>
  <text x="34" y="410" font-weight="bold">turn():while(true)</text>
  <text x="34" y="432">preStep:inbox.claim(首个 step:next-step 全部 + next-turn 一条)+ prompt 组装 + agent/pre-step ── reject → blocked</text>
  <text x="34" y="452">step/start 落库</text>
  <text x="34" y="472">step():agent/request waterfall(可换模型)→ llm.stream()(仅流式)</text>
  <text x="34" y="492">  错误终态 → assistant/attempt 落库 → agent/request-error waterfall(llm-retry 应答,重试先落库)→ 同 step 重试</text>
  <text x="34" y="512">  工具调度:isConcurrencySafe 分组(未知 = exclusive barrier);dispatch 重叠,commit 游标按模型序提交</text>
  <text x="34" y="532">  abort:已启动排干(drain)提交真结果;未启动合成 TOOL_ABORTED_BEFORE_DISPATCH 配对</text>
  <text x="34" y="552">step/end 落库</text>
  <text x="34" y="572">终止:completed / max-tokens(sticky)∧ next-step 空 → agent/turn-stopping 钩子(最后注入机会)→ 仍空才 break</text>
  <text x="34" y="592">turn/end(reason) 落库 —— finally,任何路径都闭 turn</text>
  <text x="34" y="616">return inbox.hasPending → kick 决定是否再开一 turn(换新 AbortController)</text>
  <rect x="20" y="656" width="900" height="84" rx="8" fill="#fdf2f8" stroke="#db2777"/>
  <text x="34" y="676" font-weight="bold">崩溃恢复(crash repair)与 compaction 锁</text>
  <text x="34" y="696">resume:interruptedTurnClosers 合成闭合事件 —— TOOL_OUTCOME_UNKNOWN(已启动,慎重试)/ TOOL_NOT_STARTED(可重试)</text>
  <text x="34" y="716">开着的 step/turn 补 step/end + turn/end{interrupted};孤儿 compaction/start 由 session/end-seed 边界证明失效</text>
</svg>

**根本区别:没有"内存中的当前对话状态"。** 一切(turn/step 边界、用户
消息、assistant 输出、工具调用与结果、每次重试、压缩锁、inbox 变更)都是
append-only 的**持久化 session 事件**;发给 LLM 的消息列表是
`session.deriveMessages()` 对日志的**纯推导**;运行时状态(重试计数、
inbox、turn 边界)一律用投影(对日志的 fold)重建。

代码布局(`packages/core/agent-loop/src/`,Cordis 插件框架,"everything
is a plugin"):

- `index.ts` — `AgentLoop` service:创建/恢复/发布 agent、有序 teardown、
  注册 turnBoundary 投影;
- `agent.ts` — `ReactLoopAgent`:实际驱动("每个请求都从 session 日志
  推导");Phase 状态机在此;
- `tool-calls.ts` — 每 step 的工具调度器;`inbox.ts` — 双通道持久 inbox;
- `assistant-stream.ts` — `AssistantStreamAttempt`:每 attempt 的流帧 +
  持久累积;`runtime-context.ts` — 运行时上下文投影。

扩展点是两类类型化事件:**waterfall**(带 `next()` 的中间件链——
`agent/pre-step`、`agent/request`、`agent/request-error`、`llm/stream`、
`tools/pre-execute`、`tools/post-execute`)与 **emit/serial**
(`agent/turn-stopping`、`agent/status`、`agent/error`、流帧)。重试/超时/
审批/压缩全部是挂在这些事件上的插件,loop 本体不含这些逻辑。

## 3.2 一次输入的事件流转全过程

1. **输入落库**:`followup()`(next-turn+唤醒)/ `steer()`(next-step+唤醒)
   / `inject()`(next-step 不唤醒)→ `send()`:先计算 `wakeAfterAbort`
   (**在 inbox 插入之前**——splice 观察者里的重入 cancel 不能重新归类它;
   该情形下消息还从 next-step 重定向 next-turn)→ `inbox.splice` 落
   `agent/inbox/spliced` 事件 → `wakeDriver()` 或锁存(latch)。
2. **Phase 迁移**:idle → running(新 AbortController,step=0),
   `kick(): while (await turn()) {}` 启动。
3. **turn 开始**:`turn/start` 落库;`preStep`:`inbox.claim`(首个 step认领
   next-step 全部 + next-turn 恰一条;后续 step 只认领 next-step)→
   `systemPrompt.assemble()`(每 step 重组;投影按适配器的 systemPromptUpdate
   模式决定重发为 system 消息还是 in-history 更新)→ runtime-context 投影
   作为额外消息 → `agent/pre-step` waterfall(压缩插件在此看压力,可
   reject → turn 以 blocked 结束)。
4. **step 执行**:`step/start` 落库;请求配置过 `agent/request` waterfall
   (插件可中途换模型,header 变更落 `request/header` 事件,reason
   `initial|resume|change|series`);`llm.stream()`(**只有流式**,适配器
   异常统一归一为终态 finish chunk,不在流中抛);chunk →
   AssistantStreamAttempt 帧(start/chunk/end)+ 持久累积;正常 finish →
   `assistant/message` 落库(含 stream 记录与 replayState 推理签名)。
5. **错误重试**:终态 error/aborted → `assistant/attempt` 落库 →
   `agent/request-error` waterfall:llm-retry 插件读投影计数,**先落
   `llm/retry` 事件再进入可取消等待**,`llm/retry-started` 后重试同 step
   (`firstAttempt=false`,用户消息不重复追加);无人应答 → 抛 `LlmError`。
6. **工具执行**:每 call 先落 `tool/call` 事件 → 调度器按
   `isConcurrencySafe` 分组执行(pre-execute waterfall → 审批 → guard →
   dispatch → post-execute)→ **commit 游标按模型序**落 `tool/result`
   (`sourceEventSeqs` 回指 call);`additionalContexts` 经 splice 无唤醒
   注入 next-step inbox;`concludesTurn` 可终结 turn。
7. **step/turn 收尾**:`step/end` 落库;终止判定 = `completed`/`max-tokens`
   (sticky)∧ next-step 空 ∧ `agent/turn-stopping` 串行钩子跑完后仍空;
   `turn/end(reason)` 在 finally 恒落——**任何路径都闭 turn**。
8. **续 turn 或休眠**:`return inbox.hasPending` → kick 再开一 turn(换新
   AbortController、作废过期锁存)或落回 idle(此时重放锁存的唤醒)。
9. **崩溃恢复**(resume):`handle.read(0)` → `interruptedTurnClosers`
   合成闭合事件(§3.4.8)→ session 以 `[...persisted, ...closers]` seed;
   压缩孤儿锁由 `session/end-seed` 边界判定失效(§3.4.7)。

## 3.3 阶段深挖

### 3.3.1 Phase 状态机(agent.ts:41)

```ts
type Phase =
  | { kind: 'idle';        lastTurn: number }
  | { kind: 'maintenance'; abort: AbortController; lastTurn: number; wakeRequested: boolean }
  | { kind: 'running';     abort: AbortController; turn: number; step: number; wakeRequested: boolean }
```

| 从 | 触发 | 到 / 效果 |
|---|---|---|
| idle | `wakeDriver()`(followup/steer 的 send 带唤醒) | running(新 AbortController,turn=lastTurn,step=0);启动 `kick()` |
| idle | `runMaintenance(job)` | maintenance;非 idle 同步抛 "already has active work" |
| maintenance | job 结束(finally) | idle;非 disposed ∧ `wakeRequested && inbox.hasPending` → 重放唤醒 |
| running | `kick()` finally(`while(await turn())` 退出) | idle{lastTurn:turn};同上重放锁存唤醒 |
| running | `turn()` 返回 true(turn 结束时 inbox 有货) | 留在 running:**换新 AbortController**、`wakeRequested=false`(过期锁存作废)、step=0 |
| running/maintenance | `cancel(cause, {keepInbox?})` | `abort.abort(cause)`;`!keepInbox` → `inbox.clear()` 且清锁存。Phase 不直接变——驱动经 `throwIfAborted` unwind,由 kick finally 落回 idle |
| any | dispose | `cancel({kind:'disposed'})` → `whenIdle()`(循环等 activityDone 恒定,覆盖链式唤醒)→ scope 销毁;**disposed 从不锁存** |

**唤醒锁存**:`wakeAfterAbort` 在 inbox 插入**之前**计算;锁存仅当
`非 disposed ∧ (maintenance ∨ wakeAfterAbort)`——**活着的(未 abort 的)
running 驱动从不锁存**(它自己在下个边界认领)。取消原因:
`{kind:'user'} | {kind:'parent'} | {kind:'hook'; reason} |
{kind:'disposed'}`(持久化形态加 `legacy`)。

### 3.3.2 turn()/step() decision 状态机

```ts
type PreStepDecision = { kind:'reject' } | { kind:'enter'; messages: UserMessage[]; startsRequestSeries?: true }
// step() 返回 {kind:'completed'} | {kind:'max-tokens'} | null;错误/中止是抛出
type RequestErrorAction = { kind:'retry' } | undefined
// TurnEndReason: completed | aborted{reason} | blocked | error{LlmFailure} | max-tokens | interrupted
//   (interrupted 仅崩溃修复/冷读合成产生;插件可扩展 TurnEndReasonMap)
```

`step()` 返回语义:`completed` = 无工具调用 **或**某工具结果携带
`concludesTurn`;`max-tokens` = finish 为 max-tokens(工具未执行);
`null` = 工具跑了未终结,turn 继续。

**turn 内判定表**(agent.ts:287–322;`turnEnds` 初 null):

| 事件 | 条件 | 动作 |
|---|---|---|
| preStep → reject | — | `turnEnds={blocked}`;**return false**(已认领消息"既不丢弃也不重发"——文档化取舍) |
| preStep → enter,空消息 | turnEnds 已置 | break(正常闭 turn) |
| preStep → enter,空消息 | step===0(turn 首个 step) | `turnEnds={completed}`;return false——turn 边界开了但没花模型调用 |
| step 返回 stepEnd | turnEnds 为 null 或非 max-tokens | `turnEnds = stepEnd`——**max-tokens sticky**:后续 completed step 不能降级 |
| step 后 | turnEnds 真值 ∧ next-step 空 | 跑 `agent/turn-stopping` 串行钩子 → 复查:仍空 → break;钩子 steer 了 → 继续 |
| step 后 | turnEnds 为 null 或 next-step 非空 | 继续循环(target='next-step') |
| 抛出 ∧ signal.aborted | — | `turnEnds={aborted, reason:signal.reason}`;rethrow(kick 收容) |
| 抛出非 abort | — | `turnEnds={error}`;emit `agent/error`;rethrow |
| finally | 恒 | `append('turn/end', {turn, reason:turnEnds!})` |

**不存在 max-step/max-turn 计数器。**

### 3.3.3 Tool-call scheduler 状态机(tool-calls.ts)

**每调用生命周期**:`planned`(`parseArguments`:坏 JSON 保留原文,空 →
`{}`)→ `started`(`tool/call` 事件落库,seq 记入 `callSeqs[i]`)→
prepare 三种产物:`dispatch`(body 进 in-flight)/ `post-result`(立即
填槽,仍跑 post 链)/ `final-result`(填槽,跳过 post 链)→ `committed`
/ `skipped`(合成对)。

**分组**:读首调用模式——`parallel` ⇒ 组 = 其余全部(滚动池),
`exclusive` ⇒ 组 = `[first]`(barrier)。模式源:工具的 `isConcurrencySafe`
分类器;未知/隐藏/未声明/非法/**抛出** ⇒ exclusive。**重分类两处**:组
形成时;`fillPool` 每次启动前(变卦 → break 留给下个 barrier 组)。

**commit 游标**:`committed` 只在 `slots[committed] !== undefined` 时推进
——dispatch 任意重叠、**提交严格按模型顺序**。每次提交:post 链(如需)→
`tool/result` 落库 → `additionalContexts` 进 next-step inbox →
`concluded ||= result.concludesTurn`。池宽 `maxParallelToolCalls`(默认
10,运行时可改,每组开始时读取)。

**失败/边界矩阵**:

| 情形 | 行为 |
|---|---|
| pre-execute → `deny{reason}` | `post-result`:`{content:[Error: reason], isError:true}`——模型可见,管线继续 |
| `ask` 且**无** approval 服务 | 降级 deny:`tool "X" requires approval (not yet supported)` |
| `ask` → 审批 cancel / 调用方已 abort | aborted-before-dispatch 结果 |
| guard 升级 | allow 之后复查 `guardReason(exec)`——单调:"监听器顺序不能把拒绝翻回许可" |
| abort,已启动 | 池停止补充;in-flight 排干(drain)、**按序提交真实结果** |
| abort,组内未启动 | 每个合成 `tool/call`+`tool/result` 对:`TOOL_ABORTED_BEFORE_DISPATCH` |
| abort,后续组 | `planned.slice(next)` 同样补合成对 |
| 调度器内部失败 | `schedulerFailure` 记录;排干 in-flight;rethrow **不伪造结果**(已落库的 `tool/call` 留空,交给 resume 崩溃修复) |
| 非 abort 组结束 | 断言 `committed === started`,否则抛不变式违例 |

## 3.4 核心组件实现细节

### 3.4.1 session 事件日志与投影

- JSONL 持久化,版本化格式迁移(v0→v3);`session-checkpoint-policy` 在
  模型请求、工具 dispatch、完成的 step 处加语义持久性检查点;
- **投影(session-projection)**:版本化、schema 校验的 fold
  (turnBoundary、inbox、llmRetry、subagent 状态…)——统一的状态重建机制;
- 事件词汇:`turn/start · step/start · user/message · assistant/message |
  assistant/attempt · tool/call · tool/result · llm/retry ·
  compaction/start|end · agent/inbox/spliced · request/header ·
  session/end-seed …`。

### 3.4.2 Inbox 状态机(inbox.ts,纯 fold)

`InboxState = { 'next-turn': UserMessage[]; 'next-step': UserMessage[] }`:

```ts
'agent/inbox/spliced': {
  target: 'next-turn' | 'next-step'
  start: number; removedCount?: number      // 0 时省略
  inserted: UserMessage[]
  outcome?: 'canceled'                       // discardRemoved 且有移除时
}
```

fold 校验 start/removedCount 边界、应用 `toSpliced`,并**拒绝跨两通道的
重复消息 id**("message X is already pending"——抛出即毒化损坏日志的重放,
宁炸不静默)。`claim(target, turn)`:恒排空全部 next-step;
`target==='next-turn'` 时另从队头恰取一条;每条 emit
`agent/inbox/claimed`。`clear()`(来自 cancel)持久清空并带
`outcome:'canceled'`。工具的 `additionalContexts` 经 splice 无唤醒插入
next-step(step 环已活着)。

### 3.4.3 AssistantStreamAttempt 状态机(assistant-stream.ts)

状态:`constructed → started(start 帧) → streaming(chunk*) → terminal`
(settled-committed | abandoned)。身份:`attemptId = sessionId:递增计数`
(同 step 重试拿新 attempt);`revision` = **全 attempt 共享**的单调计数;
`index` = attempt 内 chunk 序号。帧:`start{attemptId, revision, turn,
step}` / `chunk{index, time, chunk}` / `end{outcome}`。

`settle(eventType, append)` **同步持久 append 先行、帧后发**。持久化结局
矩阵:

| 情形 | 持久事件 |
|---|---|
| 正常结束 | `assistant/message{message(blocks + source{provider, model, replayState?}), usage?, stream}` |
| 适配器归一化的 error/aborted 终态 chunk | `assistant/attempt`(→ request-error waterfall) |
| 流中抛出 ∧ aborted ∧ 有可见块 | `assistant/message{interrupted:true, stream}`——半截可见前缀保留 |
| 流中抛出无可见块 / started 后非 abort 抛出 | `assistant/attempt` |
| `live.start()` 前失败 | 无持久、无帧 |
| settle 的 append 抛出 | `AggregateError([流错误, 落库错误])`;end 帧 abandoned |

`stream` 字段把 chunk 压缩记录持久化——重放/重连可复现流(基准测试里有
active-stream-reconnect / long-session-browser);`replayState`(原生推理
签名)随消息 source 保存,跨请求正确重放 thinking。

### 3.4.4 工具管线与审批(tools/src/index.ts)

每调用管线:scheduler `prepare` → `tools/pre-execute` waterfall(返回
`allow | deny | ask | cancel`)→ `ask` 经可选的 `approval` 服务解析
(**无服务 ⇒ ask 降级 deny,fail-closed**)→ 单调 guard(`guardReason`;
deny 不可被后续监听器翻回)→ `dispatch`(工具体)→ `tools/post-execute`
waterfall → finalize。无效 JSON 参数保留原文;未知工具走完整策略管线
(策略层看得到每个名字)拿 `UNKNOWN_TOOL` 结果;一切失败对模型可见
(`isError: true`)。

**PTC(programmatic tool calling)**:激活时直接工具调用折叠为单一
`run_code` 工具(受限 Node 程序 + host 绑定 + 输出台账);被折叠的直接
调用在策略管线**之前**确定性拒绝("pre-execute 监听器、审批、guard 绝不能
观察——更不能批准——一个只会失败的调用")。**沙箱**:`SandboxProvider.confine()`
返回强制 argv;Linux 内核沙箱 / macOS sandbox-exec / Windows ACL;shell
工具分 bash-local / bash-sandbox 变体。**超时**:不在 loop——
`guard/timeout-policy` 插件协作式执行工具自声明的 `timeoutMs`,超时转
普通错误 tool 结果。

### 3.4.5 llm 服务与适配器

- `packages/llm/llm`:抽象 llm service = 适配器注册表 +
  `stream(options): AsyncIterable<StreamChunk>`,包在可拦截的 `llm/stream`
  waterfall 里(重试、重放、路由中间件可包住适配器流或自产 chunk);
  适配器抛错归一为终态 finish chunk(`{kind:'aborted'|'error', failure}`),
  不在流中间抛异常;
- `prepareCall(provider, model)` 把模型元数据(窗口、模态、
  systemPromptUpdate 模式、适配器默认值)绑定到**一个适配器代际**——
  prepare 与 dispatch 之间的设置变更不会歪斜;
- 两个适配器族:`llm-deepseek`(原生,同时讲 Anthropic-Messages 风格协议
  与 OpenAI chat-completions)与 `llm-pi-ai`(库背书多 provider);
- **DeepSeek 细节**:chat-completions 翻译器处理 `delta.reasoning_content`
  → harness reasoning 块 / reasoning-delta chunk;usage 映射懂 DeepSeek
  context-caching 字段 `prompt_cache_hit_tokens / prompt_cache_miss_tokens`
  (与 OpenAI 兼容的 `cached_tokens`)→ `cacheRead`;Messages 协议在
  assistant 消息上保留 `ReplayEnvelope`(reasoning **签名**),原生
  thinking 跨请求正确重放。

### 3.4.6 Retry 状态机(packages/llm/llm-retry)

```ts
type ResolvedRetryPolicy =
  | { mode:'normal'; maxRetries:number; retryableCodes:readonly string[];
      initialDelayMs:number; maxDelayMs:number; jitterRatio:number }
      // 默认: 5, [EMPTY_RESPONSE, RATE_LIMIT, SERVER, TIMEOUT, TRANSPORT], 500, 10_000, 0.1
  | { mode:'always'; initialDelayMs:number; maxDelayMs:number; jitterRatio:number }
```

持久事件:`llm/retry{retryId, turn, step, provider, mode, policyKey, retry,
(maxRetries), delayMs, failure}` 在**可取消等待之前**落库,
`llm/retry-started` 在延迟之后——**重试先于执行持久化**。计数投影
`llmRetry` 按 `(provider, policyKey)` 键控,`step/start` 与 `turn/end`
清零——计数**跨进程重启存活**,step 中途换模型各算各的。

waterfall 语义:监听器可不调 `next()` 直接答 `{kind:'retry'}`(接管恢复)
或委托;`always` 模式**先委托下游**再自己退避(除非融合信号已 abort,
永远重试);`normal` 模式码不在 retryableCodes 或次数耗尽 → 委托(别的
监听器仍可救)。延迟:`providerRetryAfterMs` 在 `(0, maxDelayMs]` 内则
采纳,超出 → normal 放弃、always 回退本地;本地 =
`min(initial·2^(retry−1), max) × (1−jitter+2·jitter·rand)`。

### 3.4.7 Compaction 引擎、锁与 spill

- **能力接缝**:`ctx.compaction`(CompactionEngine 子类把历史区间替换为
  摘要节点);`compaction-basic` 挂 `agent/pre-step` 看**压力**
  (tokenMeter vs `thresholdRatio × contextWindow`,保留 `retainRatio`)、
  挂 `agent/request-error` 做**溢出恢复**(provider 确认的溢出永远符合
  资格);另有 tool-result 剪枝器、图片卸载、`/compact` 命令;
- **锁** = 持久 `compaction/start{compactionId, turn: number|null}` 事件,
  仅被匹配 id 的 `compaction/end` 释放;事务:校验 → **start 与校验同步
  相邻追加**(摘要 yield 前锁已持有)→ LLM 摘要 → 稳定性复查 → 提交体
  (`compaction/summary` + 替换范围的 checkpoint `user/message`)→ end。
  每条失败路径恰好尝试一次 `compaction/end{error}`;"失败的关闭刻意留下
  可检测的未配对 start";
- **进程死在压缩中途**:从不合成 end;失效由 **seed 边界**证明——resume
  时追加 `session/end-seed`,`latestEndSeedSeq > 孤儿 start.seq` 视为已
  释放(上个生命周期的孤儿不再阻塞,本生命周期的仍阻塞);
- **spill**(packages/spill):超长工具输出落 spill 存储,上下文里只留
  定位符 + 取回提示(spill-policy 做替换、spill-local 是文件系统后端)。

### 3.4.8 Crash-repair(session/src/repair.ts `interruptedTurnClosers`)

单次前向扫描,维护 `openTurn / openStep / pendingCalls:Map<callId,
{step, callSeq?}>`。输出(日志已配平则为空):合成事件 seq 从
`last.seq+1` 续,**全部复用最后一条真实事件的时间戳**(确定性,绝不
"来自未来"):

1. 每个未配对调用一条 `tool/result{isError:true}`,**区分两种悬空**:
   - 已记录 `tool/call` 无结果 → 码 `TOOL_OUTCOME_UNKNOWN`:"结局未知;
     仅当操作只读或幂等才重试;可能有副作用则先核实外部状态或询问用户,
     勿盲目重试。"
   - assistant 请求了但从未记录启动 → 码 `TOOL_NOT_STARTED`:"仍需要就
     重试。"
2. openStep 非空 → 合成 `step/end`("开着 step 的 turn/end 是不变式违例");
3. 合成 `turn/end{reason:{kind:'interrupted'}}`——`interrupted` 的唯一
   运行外生产者。

调用点:`resumeWith` 在 `handle.read(0)` 后计算 closers,经同一持久化
句柄按普通批追加,session 以 `[...persisted, ...closers]` seed。**不触碰
compaction 锁**(由 §3.4.7 的 seed 边界机制处理)。

### 3.4.9 外围:hooks 桥、subagent

- **外部 hook 桥**(packages/hooks):`hooks-claude-code` 直接运行未修改的
  Claude Code 命令钩子(SessionStart、Pre/PostToolUse、Stop、subagent
  start/stop),`hooks-codex` 跑 Codex 钩子,共享 hook-protocol(matcher、
  超时、限制性结果合并);
- **subagent**(packages/subagent):provider 接缝——进程内 fork/spawn +
  进程外 Claude Code / Codex subagent + ACP/SDK 后端;可续接后台任务
  (`startContinuable`)、深度限制、模型选择、控制工具。

---

# 四、逐阶段横向对照

| 阶段 | hermes | openclaw | deepseek |
|---|---|---|---|
| **输入排队** | steer/redirect 两个内存队列 + 锁;redirect 在中断检查前排空 | app 层 FollowupQueue(4 模式、debounce 500ms、cap 20、summarize-drop)+ 核心层 PendingMessageQueue(submitted 栅栏) | **持久化 inbox**(splice 事件 fold),三通道;claim 精确到"首个 step多取一条 next-turn" |
| **请求组装** | 每 step 全量重建 decorated 副本(结构化克隆防污染),cache 计划最后做,真实 usage 锚点托底估算 | `convertToLlm` 投影 + `transformContext` 非破坏截断投影 | `deriveMessages()` 纯推导;system prompt 每 step 重组,按适配器模式决定重发形态 |
| **模型调用与重试** | 重试在 loop 内:25 值分类枚举 + one-shot 恢复链 + 4 种 restart 信号;退避 `min(5·2^n,120)+jitter` | 重试在最外层:重试整个 attempt(从当前 transcript 续跑,绝不重发原 prompt);8 次/90s 窗;SDK 自带重试显式关闭 | 重试是插件(waterfall 应答);**每次重试先落库**;计数投影跨重启存活 |
| **流处理** | daemon 线程 + 监视线程(stale-stream 检测,按 provider/上下文规模定超时);断流合成 length 桩续写 | 事件流 + 片段提交游标 + **async 工具边流边执行** + 在线 steering(投影 append-only 检查) | attempt/revision frame 状态机;同步落库先于帧发布;半截可见前缀持久化为 `interrupted:true` 消息 |
| **工具执行** | batch planner 切段(只读 + 文件路径重叠准入)+ 线程池 + start-order gate(审批按序);审批等待剔除计时 | 批准入链(admissions 串行、body 重叠);批内任一 sequential ⇒ 整批串行 | `isConcurrencySafe` 分组(未知即 exclusive)+ commit 游标(连续槽位)+ 两处重分类 |
| **结果提交顺序** | 原调用序(真结果赢过伪造超时) | assistant 源序重排(事件按完成序) | 严格模型序(游标推进) |
| **终止判定** | 文本 + **stop gates 可拒收答案**;step/预算/墙钟多重上限 + 宽限调用(grace call) | 无上限;`hasMoreToolCalls` 三元判定 + `shouldStopAfterTurn` 钩子;steering 优先于停止 | 无上限;completed/max-tokens sticky + `agent/turn-stopping` 钩子复查 inbox |
| **中断后 transcript** | 半截文本保留为 final;工具合成 cancelled 结果;redirect 同 turn 重建 | 合成 aborted assistant + 引导消息(handoff 豁免);**绝无悬空 toolUse** | 已启动排干(drain)提交真结果、未启动合成配对;崩溃由 resume 修复(区分 OUTCOME_UNKNOWN / NOT_STARTED) |
| **失控防护** | guardrails 可硬停 + 预算退款(纯 execute_code 轮)+ 90% 预警注入 | 6 检测器两级响应(警告→引导→终止)+ post-compaction 守卫 + 成本熔断 | 核心无内建,插件层职责(guard 包) |
| **崩溃恢复** | SQLite + persist-before-execute;无自动配平修复 | 关键片段(turnId)先落库;run 级恢复在 Layer C | **一等能力**:closers 合成 + 重试/inbox/锁全部从日志重建 |

## 三个值得单独记住的设计判断

1. **"重试放哪"分出三个层次**:hermes 放 loop 里(恢复最快、粒度最细,
   代价是 loop 最复杂);openclaw 放最外层(loop 干净,粒度粗——重跑整个
   attempt);deepseek 放插件(loop 最干净,代价是需要 waterfall 基础设施
   + 重试事件持久化)。
2. **"半截输出怎么办"三家全部选择保留而非丢弃**:hermes 直接当 final、
   openclaw 提交为 aborted assistant、deepseek 落成 `interrupted:true`
   消息。用户已经看到的东西必须进历史,否则下一轮模型与用户认知分叉。
3. **配平的两种时机**:openclaw/deepseek 运行时就保证不悬空(abort 路径
   同步合成结果),deepseek 另有 resume 修复兜底崩溃;且 deepseek 修复时
   **区分"启动了、结局未知"(幂等才可重试)与"从未启动"(放心重试)**
   两种悬空,给模型的指引完全不同。
