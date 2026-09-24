import asyncio
import random
import time
from collections.abc import Callable

from loguru import logger

from conic.plugins import meta
from conic.types.messages import (
    AssistantMessage,
    BuildSystemPrompt,
    Error,
    MessageDeltaUpdate,
    MessageUpdate,
    SessionEnd,
    StepStart,
    TurnEnd,
    TurnStart,
)

OUTPUT_REQUIREMENTS = """
Your reply renders on Discord. Follow these rules:

1. *IMPORTANT* Never use tables (no "|" column separators, no "---" divider
   rows). Discord shows them as raw text. Present tabular data
   as a bullet list, one item per row:
   - **API**: 
        - all 30 reviews
   - **web_fetch**
        - blocked by age gate
   For rows with several fields, use nested bullets:
   - **API**
     - Result: all 30 reviews
     - Status: ok

2. Headings: use only #, ##, or ###. For deeper levels, use bold
   text instead.

3. Deliver the reply as ONE message, streamed live: do not split
   it, do not announce progress, do not add meta-commentary.

4. Length: 2000 characters maximum (hard limit). If you are near
   the limit, condense the content instead of splitting it into
   multiple messages.

Pre-send check:
- Any line containing " | " or "---" -> rewrite as a bullet list.
- Any heading of level 4 or deeper -> demote to bold text.
- Total length over 2000 characters -> condense.
"""

DISCORD_MESSAGE_LIMIT = 2000
TYPING_INTERVAL = 8
TYPING_TIMEOUT = 20
STREAM_EDIT_INTERVAL = 1.0
THINKING_TEXTS = (
    "🤔 脑子在转，请稍等…",
    "🧠 神经元触突中…",
    "⚙️ 咔哒咔哒运转中…",
    "🔍 眯眼看代码中…",
    "💭 让我盘一盘…",
    "🛠️ 搬砖中，别催…",
    "📝 打草稿中…",
    "🔄 缓冲区加载中…",
    "🧩 拼图差一块…",
    "🕵️ 蹲坑找 bug 中…",
    "📡 正在联系外星智慧…",
    "🧮 掐指一算…",
    "🗂️ 翻箱倒柜找资料中…",
    "🔧 拧螺丝中…",
    "🌀 大脑正在 loading…",
    "🧵 理线头中，别急…",
    "🎯 瞄准问题中…",
    "📊 画个图表压压惊…",
    "🚀 火箭升空倒计时…",
    "⏳ 泡杯茶的功夫…",
    "😏 已经想到答案了，先晾你一会儿…",
    "🖤 心里有数，嘴上不说…",
    "😈 邪恶计划酝酿中…",
    "🕶️ 一切尽在掌握…",
    "😌 别急，就是想看你等…",
    "🃏 底牌已经摸好…",
    "🐍 悄悄盘算中…",
    "😼 笑而不语，思考中…",
    "🍵 一边喝茶一边看戏…",
    "🎭 演都不演了，思考中…",
    "😏 你猜我在想什么…",
    "🖤 黑化进度加载中…",
    "😈 坏主意正在成型…",
    "🕸️ 布局中，勿扰…",
    "😌 稳，都在计划之中…",
    "🐱 眯眼盯着代码，心里已经笑了…",
    "😏 答案藏好了，等会儿给你个惊喜（惊吓）…",
    "🎯 已经看穿一切，先不说破…",
    "😈 表面淡定，内心疯狂吐槽中…",
    "🖤 心机运转中，勿慌…",
)


class DiscordThreadPlugin:
    def __init__(self, thread, clock: Callable[[], float] = time.monotonic):
        self._thread = thread
        self._clock = clock
        self._typing_task: asyncio.Task | None = None
        self._stopped = False
        self._status_message = None
        self._buffer = ""
        self._thinking_text = THINKING_TEXTS[0]
        self._awaiting_first_delta = False
        self._last_edit_time = float("-inf")

    def register(self, bus) -> None:
        bus.on_chain(meta.SessionEndEvent, self.on_session_end)
        bus.on_chain(meta.TurnStartEvent, self.on_turn_start)
        bus.on_chain(meta.StepStartEvent, self.on_step_start)
        bus.on_chain(meta.MessageUpdateEvent, self.on_message_update)
        bus.on_chain(meta.MessageDeltaUpdateEvent, self.on_message_delta_update)
        bus.on_chain(meta.TurnEndEvent, self.on_turn_end)
        bus.on_chain(meta.ErrorEvent, self.on_error)
        bus.on_chain(meta.AssistantMessageEvent, self.on_assistant_message)
        bus.on_chain(meta.BuildSystemPromptEvent, self.contribute_output_requirements)

    async def contribute_output_requirements(self, msg: BuildSystemPrompt) -> BuildSystemPrompt:
        msg.sections["output"] = OUTPUT_REQUIREMENTS
        return msg

    async def on_session_end(self, _msg: SessionEnd) -> None:
        self._stopped = True
        self._stop_typing()

    async def on_turn_start(self, _msg: TurnStart) -> None:
        self._ensure_typing()
        self._thinking_text = random.choice(THINKING_TEXTS)
        self._buffer = self._thinking_text
        self._awaiting_first_delta = True
        self._last_edit_time = float("-inf")
        self._status_message = await self._thread.send(self._buffer)

    async def on_step_start(self, _msg: StepStart) -> None:
        self._ensure_typing()

    def _ensure_typing(self) -> None:
        if self._typing_task is not None and not self._typing_task.done():
            return
        self._typing_task = asyncio.create_task(self._keep_typing())

    async def on_message_update(self, msg: MessageUpdate) -> None:
        self._buffer = msg.text
        self._awaiting_first_delta = True
        await self._apply_edit(force=True)

    async def on_message_delta_update(self, msg: MessageDeltaUpdate) -> None:
        force = self._awaiting_first_delta
        if self._awaiting_first_delta:
            self._buffer = ""
            self._awaiting_first_delta = False
        self._buffer += msg.text_delta
        await self._apply_edit(force=force)

    async def _apply_edit(self, force: bool) -> None:
        if self._status_message is None or self._stopped:
            return
        now = self._clock()
        if not force and (now - self._last_edit_time) < STREAM_EDIT_INTERVAL:
            return
        self._last_edit_time = now
        content = self._buffer if self._buffer.strip() else self._thinking_text
        if len(content) > DISCORD_MESSAGE_LIMIT:
            content = "…" + content[-(DISCORD_MESSAGE_LIMIT - 1):]
        try:
            await self._status_message.edit(content=content)
        except Exception as exc:
            logger.warning("failed to live-update responsive message: {}", exc)

    async def on_turn_end(self, _msg: TurnEnd) -> None:
        self._stop_typing()

    async def on_error(self, msg: Error) -> None:
        self._stop_typing()
        await self._finalize(f"⚠️ {msg.exc}")

    async def on_assistant_message(self, msg: AssistantMessage) -> None:
        await self._finalize(msg.text)

    async def _finalize(self, text: str) -> None:
        if self._stopped:
            return
        text = text or "(empty response)"
        if self._status_message is not None:
            try:
                await self._status_message.edit(content=text[:DISCORD_MESSAGE_LIMIT])
            except Exception as exc:
                logger.warning(
                    "failed to edit responsive message into final text, sending a new message: {}", exc
                )
                await self._send(text[:DISCORD_MESSAGE_LIMIT])
            await self._send(text[DISCORD_MESSAGE_LIMIT:])
        else:
            await self._send(text)
        self._status_message = None

    def _stop_typing(self) -> None:
        if self._typing_task is not None and not self._typing_task.done():
            self._typing_task.cancel()
            self._typing_task = None

    async def _keep_typing(self) -> None:
        try:
            async with asyncio.timeout(TYPING_TIMEOUT):
                while True:
                    async with self._thread.typing():
                        await asyncio.sleep(TYPING_INTERVAL)
        except (asyncio.CancelledError, TimeoutError):
            pass

    async def _send(self, text: str) -> None:
        if self._stopped:
            return
        for i in range(0, len(text), DISCORD_MESSAGE_LIMIT):
            await self._thread.send(text[i: i + DISCORD_MESSAGE_LIMIT])
