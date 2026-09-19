# Rich Interaction:present_* 展示工具族设计

日期:2026-09-18
状态:设计中,未实现

## 1. 动机

模型目前只有两条对用户可见的输出通道:终答 `AssistantMessage` 和流式的
`MessageUpdate`/`MessageDeltaUpdate`,全部是纯文本。两类需求没有着落:

- **结构化交互**:模型被一个只有用户能做的决定阻塞时(方案取舍、权限确认),
  只能在终答里用散文问,用户用散文答,双方都要靠自然语言消歧;
- **结构化展示**:表格、TODO 列表、进度卡片这类内容在 Discord 里用 markdown
  文本渲染效果差(`Improvement-with-pi.md` 的"Discord 显示逻辑改进"和
  "Plan 插件 TODO 列表显示"两节都卡在这里)。

方案:引入一族 **presentation tools**(`present_user_selection`、
`present_user_table`、`present_user_todo`、……),让模型把结构化内容作为
tool call 投递到渠道。

## 2. 核心决策

### 2.1 只做展示,不假设回答(present,不是 ask)

每个 `present_*` tool 的契约是**投递**:立即返回投递凭证
(`{delivered, prompt_id}`),不等待、不返回用户的回应。用户的回应(如果有)
经既有链路(`scope.queue` → `SessionGatewayPlugin` → `InputEvent` 拦截链 →
`steering.high`)作为普通 steering 输入到达,通常开启新 Turn。

对比被否掉的替代方案——tool call 阻塞在 Turn 内等答案(Claude Code 的
`AskUserQuestion` 语义):

| | Turn 内阻塞等待 | 展示后结束 Turn,回应走 steering(本方案) |
|---|---|---|
| 崩溃恢复 | 等待期间进程挂掉,历史留下有 `tool_calls` 无 `tool_result` 的悬空配对(正是 `align_cut` 防的东西),resume 后历史是坏的 | 等待状态不存在于任何协程,就是 loop 的 idle 态,resume 无缝 |
| 用户几小时不回 | 需要发明超时和挂起语义 | 免费,本来就是空闲 |
| `/agent_stop` 穿透 | 阻塞在 tool 里到不了 steering 检查点,要额外监听 | 走正常路径 |
| 模型状态连续性 | Turn 不断,状态全在 | 提问、投递凭证、用户回答全在历史里,新 Turn 首次模型调用能看到完整脉络,损失很小 |

阻塞方案成立的前提(交互式 CLI、用户在跟前、进程生命周期 = 会话生命周期)
在长驻 Discord bot 上全不成立。

### 2.2 自由文本回答永远走终答,不做 present_user_text

模型的 agentic 后训练分布强烈偏向"tool 做事、终答文本对人说话",循环停止
条件(`if not response.tool_calls: break`)本身就是这个约定。把散文回答塞进
tool 参数的实测代价:

- JSON 字符串里的 prose 更短更僵,markdown 保真度下降,转义偶发出错;
- **丢流式**——终答走 `MessageDeltaUpdate` 逐 token 编辑,而 tool call 参数
  的 JSON 片段有意不对用户展示;
- 多一种失败模式(JSON 解析失败 = 回答丢失);
- 停止条件变形(空终答污染历史,或 tool 与终答复读)。

反之,结构化内容放 tool 里由 schema 强制格式、渲染确定,优于从散文里抠。
所以按内容类型分流:

| 内容 | 通道 |
|---|---|
| 自由文本回答 | 终答 `AssistantMessage`(现状不变) |
| 结构化组件(selection/table/todo/confirm) | `present_*` tools |

`present_user_selection` 的 description 里明确要求"先在正文说明情况,再用
本工具展示选项",让两个通道各司其职。

## 3. 事件流

以 `present_user_selection` 为例,一次完整交互:

```
Step N:
  model → tool_call present_user_selection(question, options)
  ReactLoop → bus.request(ToolCallRequestEvent, PresentUserSelectionCall)
  tool → bus.chain(PresentSelectionEvent, PresentSelection(..., native_id=None))
  DiscordThreadPlugin → 渲染成带 Button/SelectMenu 的消息,回填 msg.native_id
  tool → ToolCallResult(output={"delivered": true, "prompt_id": ..., "note": ...})
  model → 看到凭证,收尾终答,结束 Turn
(loop 回到 wait_multiply_mailbox,idle)

用户点击按钮(可能几分钟/几小时后):
  DiscordThreadPlugin → 把点击转成文本 "[selection {prompt_id}] {label}"
  → 走 handle_message 同款入口进 scope.queue
  → SessionGatewayPlugin → InputEvent 链 → steering.high
  → run_loop 开新 Turn,注入历史
  模型在历史里同时看到:当时的 tool_call args(问了什么)+ 带 prompt_id
  标记的用户选择,天然对上号
```

要点:

- **`native_id` 回填**利用 chain 允许 handler 原地改 payload 的既有语义,
  tool 保持渠道无关——渲染细节全在渠道插件里,换渠道只换渲染。
- **无渠道渲染**(chain 跑完 `native_id` 仍为 None)返回
  `ToolCallResult(error=...)`,模型退回纯文本提问。
- 用户不点按钮、直接打字回复也完全成立——那就是一条普通 steering 消息,
  模型靠历史里的提问上下文理解它。

## 4. Tool 定义示例:present_user_selection

按既有 tool 约定(payload dataclass + `llm_name` + `schema` + `on_request`)。
description 是本设计**最关键的部分**,它的结构参考 Claude Code 的
`AskUserQuestion`(经实测有效的同类工具),借用它四个手法:

1. **第一句是否定门槛**("only when blocked on a decision that is genuinely
   the user's to make"),先说什么时候不该用;
2. **自答来源逐一点名**(conversation / repository / sensible defaults),
   模型问之前有清单可查;
3. **功能性判据**("答案会不会改变你下一步做什么"——模型生成时真的能自测);
4. **替代动作写死**("选显然的默认项、在正文说明选了什么和为什么、继续干"),
   被拦住时有明确出路,不会卡在"不能问又不敢定";
5. 字段级 description 的密度也照抄:每个字段都带格式约束 + 例子,与 schema
   的 min/max **双写**(provider 不一定强制 schema 约束,自然语言那份是给模型看的)。

`AskUserQuestion` 是阻塞式工具(result 直接带回答案),所以它没有"不要等"
的语言可抄——**present 语义段是我们自己的**:模型的先验是"tool result 带回
答案",必须用 description + result note 双重压制"调用后原地等答案"的冲动,
并把异步代价(一次多余提问 = 会话死等到用户回来)写进去抬高提问门槛。

```python
MAX_QUESTION_CHARS = 200


@dataclass
class PresentUserSelectionCall:
    question: str
    options: list  # [{"label": str, "description": str}, ...]
    multi_select: bool = False


class PresentUserSelectionToolPlugin:
    llm_name = "present_user_selection"
    schema = {
        "type": "function",
        "function": {
            "name": "present_user_selection",
            "description": (
                "Display a selection prompt (clickable options) to the user. "
                "PRESENTATION ONLY: this tool does NOT wait for or return the user's "
                "choice. It returns immediately with a delivery receipt; if the user "
                "picks an option, their choice arrives later as a regular user message "
                "referencing this prompt — possibly hours later, in a future turn. "
                "Use it only when you are blocked on a decision that is genuinely the "
                "user's to make: one you cannot resolve from the conversation, the "
                "repository, or sensible defaults — and only when the answer changes "
                "what you do next. For choices with an obvious default, pick it, state "
                "the choice and why in your reply, and proceed; asking here stalls the "
                "whole session until the user returns. "
                "State the necessary context in your normal reply text BEFORE calling "
                "this tool (the prompt itself must stay short), then finish your reply "
                "and end the turn. Do not call this tool again for the same decision, "
                "and do not call other tools to wait for the answer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The decision being asked, one sentence, ends with a question mark.",
                    },
                    "options": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 5,
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {
                                    "type": "string",
                                    "description": "Short button text, 1-5 words.",
                                },
                                "description": {
                                    "type": "string",
                                    "description": "One line on what choosing this implies (trade-offs, consequences).",
                                },
                            },
                            "required": ["label"],
                        },
                        "description": (
                            "Mutually exclusive choices (unless multi_select). Put your "
                            "recommended option first and mark it '(recommended)'. Do not "
                            "add an 'other' option; the user can always reply in free text."
                        ),
                    },
                    "multi_select": {
                        "type": "boolean",
                        "description": "Allow picking several options. Default false.",
                    },
                },
                "required": ["question", "options"],
            },
        },
    }

    def __init__(self, workspace_dir: str):
        self._bus = None

    def register(self, bus) -> None:
        self._bus = bus
        bus.on_request(meta.ToolCallRequestEvent, self.execute)

    async def execute(self, call: PresentUserSelectionCall) -> ToolCallResult:
        if len(call.question) > MAX_QUESTION_CHARS:
            return ToolCallResult(error=(
                f"question is too long ({len(call.question)} chars, max {MAX_QUESTION_CHARS}); "
                "put the context in your reply text and keep the prompt to one sentence"
            ))
        msg = await self._bus.chain(
            meta.PresentSelectionEvent,
            PresentSelection(
                question=call.question, options=call.options, multi_select=call.multi_select,
            ),
        )
        if msg.native_id is None:
            return ToolCallResult(error="no channel rendered the selection prompt")
        return ToolCallResult(output=json.dumps({
            "delivered": True,
            "prompt_id": msg.native_id,
            "note": "The user's choice (if any) will arrive as a future user message.",
        }))
```

result 里的 `note` 是故意的:tool result 是模型"决定下一步"时刻真正读的
东西,把"答案在未来才来"再讲一遍,比只靠 description 更能压住重复调用。

`execute()` 开头的长度校验同样是故意的:schema 里的 `maxLength` 各家
provider 不一定强制执行,tool 层自己检查并把纠偏指令作为 error 喂回模型
("把上下文放正文、prompt 保持一句话"),是**运行时**对抗"搬运终答"
(见 6.2)唯一可靠的硬约束。

`__init__` 收 `workspace_dir` 但不用,匹配 `PluginManager` 对 `tool_classes`
统一 `tool_cls(workspace_dir=...)` 的实例化方式(与 `ConfiguredBashToolPlugin`
同一手法);若嫌别扭可借机给 `tool_classes` 引入通用工厂协议,属另一个改动。

## 5. 改动清单

| 位置 | 改动 |
|---|---|
| `types/messages.py` | `PresentSelection(question, options, multi_select, native_id: str \| None = None)` |
| `plugins/meta.py` | `PresentSelectionEvent = "present_selection"` 常量,注释写明发出者与"渠道插件应回填 native_id"约定 |
| `plugins/tools/present_selection.py` | 上述 tool plugin |
| `plugins/channels/discord.py` | `on_chain(PresentSelectionEvent)`:渲染 Button/SelectMenu、回填 `native_id`;点击回调 → `"[selection {prompt_id}] {label}"` 走 `handle_message` 入口 |
| `plugins/registry.py` | `tool_classes` 加入新 tool,policy 插件加入 `policy_plugins` |
| `plugins/policy/present_limit.py` | hook `ToolCallEvent`:同一 Turn 内第二次 `present_*` 调用直接拒绝(error 返回 "already presented; end your turn")。一个插件同时拦"重复投递"(6.1)和"碎片化连问"(6.3) |
| `prompts/execution.md` | 行动偏置补一句:"倾向带着声明的假设继续推进,而不是停下来问"(见 6.3);description 已自带使用规则,系统提示词不需要专门 section |

后续家族成员(`present_user_table`、`present_user_todo`、confirm 类)复用
同一骨架:payload dataclass + 专属 chain event + 渠道插件渲染 + 立即返回
凭证。fire-and-forget 成员(table/todo)连"回应走 steering"都不需要,更简单。

## 6. 风险与对策

三类风险的共同根源是模型的训练先验与 present 语义的错配,严重度**高度依赖
具体模型**:强模型读懂 description 基本不犯,OpenRouter 上换较弱模型时概率
明显上升。落地前应拿实际在用的模型跑一组"提问后无回应"的用例,再决定
policy 拦截等硬防线要做到什么程度。

### 6.1 空等

模型拿到 `{delivered: true}` 凭证后**不结束 Turn,留在 Turn 里想办法"等到"
答案**。根源:训练分布里 tool result 就是要的信息("我调了获取用户输入的
工具 → 下一步我手里应该有用户输入"),凭证里没有答案时,一部分模型会试图
自己把答案"取"回来,而系统里没有取答案的通道。四种形态:

1. **轮询**(最典型):`bash sleep 30` → 看历史没答案 → 再 sleep……每圈烧
   一个 Step 和一次模型调用,Discord 刷一串工具状态行,直到
   `MAX_STEPS_PER_TURN` 被 `StepLimitPlugin` 打断,Turn 以 error 收尾。
   有个时序细节让它显得"仿佛有道理":loop 在每个工具批次后会 drain
   `steering.high`,用户回得够快时答案确实可能中途注入——但这是碰运气,
   契约必须是"结束 Turn,答案下个 Turn 来"。
2. **重复投递**:觉得第一次"没成功",换措辞再调一次,用户看到重复卡片。
3. **幻觉答案**(最危险,无声):不等了,直接脑补"用户选择了 A,继续……",
   带着没人做过的决定往下跑。空等只是浪费,这个是错误决策。
4. **废话收尾**(温和):每个 Step 补一句"正在等待您的回复……"再结束。

防线三层:

- **description + result note** 在模型两个决策时刻(选工具时、读结果时)
  各声明一遍"答案在未来,结束 Turn";present 命名本身降低先验错配;
- **policy 限频**(`present_limit.py`)硬拦重复投递和一部分轮询;
- **`StepLimitPlugin` 兜底**,保证最坏情况有界。

幻觉答案没有运行时防线:真实答案在历史里有 `[selection {prompt_id}]` 的
可辨认形状,"声称收到答案但历史无标记"只能进 eval 用例检出。

### 6.2 搬运终答

模型把本该写在终答文本里的内容装进 tool 参数。已砍掉 `present_user_text`,
堵死最坏形态(整个回答走 tool),剩余变体:

1. **字段膨胀**:不在正文铺垫,把几段分析塞进 `question` 或选项
   `description`——"JSON 里写散文"的全部毛病(文风僵、markdown 失真、
   转义出错)换个地方复发,卡片臃肿、正文空洞。
2. **用提问代替回答**:用户问"A 和 B 哪个好",模型不给分析和推荐,甩回一张
   "A 还是 B?"的卡——形式上用了工具,实质把回答的责任还给用户。
3. **空壳终答**:调完 tool 用空串或"请选择"收尾;loop 会把终答
   `append_message` 进历史,空壳 assistant 消息永久污染 transcript。

对策:字段膨胀有**运行时硬约束**——`execute()` 的长度校验把纠偏指令作为
error 喂回(见第 4 节),这比 description 软约束可靠;后两种只能靠
description("先在正文说明、给出你的推荐")+ eval(present 调用的 Turn 里
终答长度分布、是否含自身推荐)。

### 6.3 滥用提问

该自己决定的事拿去问用户。根源是 RLHF 顺从倾向——"问"永远显得安全。形态:

1. **决策甩锅**:答案能从仓库、对话历史或常规默认值推出(repo 里明明是
   pytest,还问"用哪个测试框架?"),问一遍规避出错责任;
2. **碎片化连问**:一个决定拆成三四个小问题,每问断一次 Turn;
3. **仪式性确认**:"我可以开始了吗?"式开工许可。

**本设计特有的代价放大**:交互式 CLI 里一次多余提问浪费几秒;在"结束 Turn
等 steering"模型下,一次多余提问 = 整个会话死等到用户下次上线(可能几小时)。
提问门槛必须比同步产品高得多,description 里"asking here stalls the whole
session until the user returns"就是为此。

对策:

- description 的否定门槛 + 自答来源清单 + 功能性判据 + **替代动作**
  ("选显然默认项、正文声明选了什么和为什么、继续")——只说"别问"不够,
  要给出路(见第 4 节,借自 `AskUserQuestion`);
- `execution.md` 行动偏置补"倾向带着声明的假设推进"(见第 5 节);
- policy 限频(同一 Turn 最多一次 present)顺带压碎片化连问——一个 Turn
  多张卡本身就是坏 UX(答了第一张,其余全过期);
- schema 强制推荐项前置,降低用户回答成本,减轻"问"的伤害。

**校准提醒:不能压过头。** 压过头就退回原始失败模式——模型带着错误假设
闷头跑偏,那正是做这个功能的动机。目标是消灭"可自答之问",不是减少提问
总量;界限靠 eval 定(采样提问,判"这问题它本可以自己定吗"),不靠 prompt
措辞一次写死。

### 6.4 其他

- **点击与提问的关联**:`prompt_id` 标记注入;历史里 tool_call args 本身就
  记录了问题全文,模型无需额外查询。
- **会话已结束后的迟到点击**:走 `handle_message` 入口,路由表里没有该
  thread 时自然丢弃,与迟到普通消息行为一致。
