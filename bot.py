"""
AI Execution Companion OS — Telegram Bot
=========================================
每次 session 完成一个 2-10 分钟的最小行动步。
用微干预解决"认知高执行低"的卡点。

使用方法:
1. pip install python-telegram-bot==20.7 apscheduler
2. 设置环境变量 TELEGRAM_BOT_TOKEN
3. python bot.py
"""

import os
import json
import logging
from datetime import datetime, date
from enum import Enum
from dataclasses import dataclass, field, asdict
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ── Logging ──
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════
# DATA MODELS
# ═══════════════════════════════════════════

class FSM(str, Enum):
    NO_MAINLINE = "NO_MAINLINE"
    CLARIFY = "CLARIFY"
    CANDIDATES = "CANDIDATES"
    MAINLINE_LOCKED = "MAINLINE_LOCKED"
    NEXT_STEP_READY = "NEXT_STEP_READY"
    EXECUTING = "EXECUTING"
    STUCK_PICK = "STUCK_PICK"
    INTERVENTION = "INTERVENTION"
    SESSION_REVIEW = "SESSION_REVIEW"
    SESSION_END = "SESSION_END"


class StuckType(str, Enum):
    PERFECTIONISM = "PERFECTIONISM"
    GOAL_TOO_BIG = "GOAL_TOO_BIG"
    OVERTHINKING = "OVERTHINKING"
    EMOTIONAL_FRICTION = "EMOTIONAL_FRICTION"
    REWARD_MISMATCH = "REWARD_MISMATCH"
    SELF_LIMITING = "SELF_LIMITING"


STUCK_LABELS = {
    StuckType.PERFECTIONISM: ("✨", "完美主义瘫痪"),
    StuckType.GOAL_TOO_BIG: ("🏔", "目标太大了"),
    StuckType.OVERTHINKING: ("🌀", "想太多"),
    StuckType.EMOTIONAL_FRICTION: ("😶‍🌫️", "情绪内耗"),
    StuckType.REWARD_MISMATCH: ("📱", "想刷手机"),
    StuckType.SELF_LIMITING: ("🔒", "觉得自己不行"),
}


@dataclass
class Course:
    name: str
    status: str = "not_started"  # not_started | in_progress | completed


@dataclass
class Step:
    instruction: str
    acceptance_criteria: str
    duration_min: int = 8
    difficulty: int = 1


@dataclass
class Evidence:
    text: str
    timestamp: str = ""
    tags: list = field(default_factory=lambda: ["small_win"])

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()


@dataclass
class UserSession:
    """Per-user state, stored in bot_data or persistence."""
    fsm: str = FSM.NO_MAINLINE
    # Longline
    longline_goal: Optional[str] = None
    longline_deadline: Optional[str] = None
    courses: list = field(default_factory=list)
    # Daily
    mainline_title: Optional[str] = None
    mainline_source: str = "manual"
    time_budget: Optional[str] = None
    definition_of_win: Optional[str] = None
    # Current step
    current_step: Optional[dict] = None
    # History
    stuck_events: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    # Streak
    streak_days: int = 0
    last_progress_date: Optional[str] = None

    def record_progress(self):
        today = date.today().isoformat()
        if self.last_progress_date != today:
            self.streak_days += 1
            self.last_progress_date = today


def get_session(context: ContextTypes.DEFAULT_TYPE) -> UserSession:
    if "session" not in context.user_data:
        context.user_data["session"] = UserSession()
    s = context.user_data["session"]
    # Handle case where it's a dict (from persistence)
    if isinstance(s, dict):
        s = UserSession(**s)
        context.user_data["session"] = s
    return s


def save_session(context: ContextTypes.DEFAULT_TYPE, session: UserSession):
    context.user_data["session"] = session


# ═══════════════════════════════════════════
# AI ENGINE — generates structured responses
# ═══════════════════════════════════════════

class AI:

    BIG_GOAL_KEYWORDS = ["天", "学位", "毕业", "全部", "所有", "完成整个", "master", "degree", "finish all", "月内"]

    @staticmethod
    def is_big_goal(text: str) -> bool:
        t = text.lower()
        return any(kw in t for kw in AI.BIG_GOAL_KEYWORDS)

    @staticmethod
    def generate_candidates(courses: list[Course]) -> dict:
        in_progress = [c for c in courses if c.status == "in_progress"]
        not_started = [c for c in courses if c.status == "not_started"]
        primary = (in_progress or not_started or [None])[0]
        secondary = (in_progress[1:] or not_started[1:] or [primary])[0] if primary else None

        a_title = f"推进「{primary.name}」— 完成下一个学习单元" if primary else "推进当前最紧急的任务"
        a_reason = f"{primary.name} 正在进行中，保持势头" if primary and primary.status == "in_progress" else "优先启动第一个任务"

        if secondary and secondary != primary:
            b_title = f"轻量推进「{secondary.name}」— 阅读/整理笔记"
            b_reason = "低能量也能做，不浪费今天"
        else:
            b_title = "整理学习计划 / 预约辅导 / 复习旧笔记"
            b_reason = "低能量也能推进一步"

        return {
            "A": {"title": a_title, "reason": a_reason},
            "B": {"title": b_title, "reason": b_reason},
        }

    @staticmethod
    def generate_step(mainline_title: str, time_budget: str = None) -> Step:
        duration = 5 if time_budget and "15" in time_budget else 10 if time_budget and "90" in time_budget else 8
        # Extract core subject from mainline title
        core = mainline_title.replace("推进", "").replace("轻量", "").strip("「」—— ")
        return Step(
            instruction=f"打开「{core[:20]}」相关材料，找到你上次停下的位置，阅读接下来的 1 个小节（不超过 2 页）。",
            acceptance_criteria="能用 1 句话说出这个小节讲了什么",
            duration_min=duration,
            difficulty=1,
        )

    @staticmethod
    def shrink_step(step: dict) -> Step:
        original = step.get("instruction", "") if step else ""
        first_action = original.split("，")[0] if "，" in original else "打开需要的文件或页面"
        return Step(
            instruction=f"只做一件事：{first_action}。做完就算赢。",
            acceptance_criteria="完成了这一个动作（不管质量）",
            duration_min=2,
            difficulty=1,
        )

    @staticmethod
    def generate_intervention(stuck_type: StuckType) -> dict:
        data = {
            StuckType.PERFECTIONISM: {
                "intervention_text": (
                    "完美是个陷阱 — 它假装在帮你，其实在拦你。\n\n"
                    "现在做一个深呼吸。\n"
                    "我们的目标不是「做好」，是「做了」。\n"
                    "写一个烂版本，比空白强一万倍。"
                ),
                "restart_step": Step(
                    instruction="用最丑、最烂的方式，写下关于这个任务你知道的 3 个词。不准修改。",
                    acceptance_criteria="屏幕上出现了 3 个词（质量零要求）",
                    duration_min=2,
                ),
                "push_line": "烂版本已经比空白好了。计时器开始 →",
            },
            StuckType.GOAL_TOO_BIG: {
                "intervention_text": (
                    "大象怎么吃？一口一口。\n\n"
                    "你不需要看到终点，只需要看到下一步。\n"
                    "现在这一步只有 2 分钟。"
                ),
                "restart_step": Step(
                    instruction="只做一件事：打开你需要的那个页面/文件/工具。打开就行，不用做别的。",
                    acceptance_criteria="目标页面/文件已经打开在屏幕上",
                    duration_min=2,
                ),
                "push_line": "打开了？你已经开始了。继续 →",
            },
            StuckType.OVERTHINKING: {
                "intervention_text": (
                    "你的大脑在转圈，不是在前进。\n\n"
                    "现在停下来，双手握拳 3 秒，松开。\n"
                    "不需要想清楚才开始，开始了才会想清楚。"
                ),
                "restart_step": Step(
                    instruction="不做选择 — 直接做第一个动作：打开/点击/写第一个字。",
                    acceptance_criteria="已经动手做了第一个物理动作",
                    duration_min=2,
                ),
                "push_line": "动了就对了。计时器走起 →",
            },
            StuckType.EMOTIONAL_FRICTION: {
                "intervention_text": (
                    "先给这个情绪取个名字（烦躁？焦虑？疲惫？）\n\n"
                    "说出来：「我现在感到 ____。」\n"
                    "然后把双脚踩实地面，感受脚底的压力。\n"
                    "情绪不需要消失，我们带着它做 2 分钟。"
                ),
                "restart_step": Step(
                    instruction="带着这个情绪，只做一件最小的事：写下今天任务的标题。",
                    acceptance_criteria="写下了标题",
                    duration_min=2,
                ),
                "push_line": "情绪还在？没关系，我们已经在动了 →",
            },
            StuckType.REWARD_MISMATCH: {
                "intervention_text": (
                    "手机的奖励是即时的，但也是空的。\n\n"
                    "试试这个：先做 2 分钟，做完了你再刷 — \n"
                    "带着「我完成了一步」的感觉刷，味道完全不一样。"
                ),
                "restart_step": Step(
                    instruction="把手机翻面朝下放在伸手够不到的地方，然后打开任务材料。",
                    acceptance_criteria="手机已远离 + 任务材料已打开",
                    duration_min=2,
                ),
                "push_line": "2 分钟后你自由了。开始 →",
            },
            StuckType.SELF_LIMITING: {
                "intervention_text": (
                    "「我不行」是一个想法，不是事实。\n\n"
                    "看看你之前的证据 — 你也觉得不行过，但你做到了。\n"
                    "现在不需要「行」，只需要「试 2 分钟」。"
                ),
                "restart_step": Step(
                    instruction="写下这句话：「我不确定我行，但我可以试 2 分钟。」然后开始做。",
                    acceptance_criteria="写下了这句话并开始了第一个动作",
                    duration_min=2,
                ),
                "push_line": "试了就是证据。走 →",
            },
        }
        return data[stuck_type]


# ═══════════════════════════════════════════
# KEYBOARD BUILDERS
# ═══════════════════════════════════════════

def kb(buttons: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    """Shorthand: buttons = [[("text","callback_data"), ...], ...]"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row]
        for row in buttons
    ])


def main_menu_kb():
    return kb([
        [("🎯 直接填写今日主线", "mode_manual")],
        [("🧭 设置长线目标 + 课程", "mode_longline")],
    ])


def time_budget_kb():
    return kb([
        [("15 分钟", "time_15"), ("30 分钟", "time_30")],
        [("60 分钟", "time_60"), ("90+ 分钟", "time_90")],
    ])


def win_definition_kb():
    return kb([
        [("推进一点点", "win_push")],
        [("完成一次练习/测验", "win_quiz")],
        [("提交/预约一件事", "win_submit")],
    ])


def candidate_kb():
    return kb([
        [("🅰️ 锁定 A", "lock_A")],
        [("🅱️ 锁定 B", "lock_B")],
        [("✏️ 手动填写", "mode_manual")],
    ])


def step_ready_kb(duration: int):
    return kb([
        [("⏱ 开始计时（{} 分钟）".format(duration), "timer_start")],
    ])


def executing_kb():
    return kb([
        [("✅ 完成了", "step_done")],
        [("🧱 卡住了", "step_stuck"), ("↩️ 太难了，缩小", "step_shrink")],
    ])


def stuck_type_kb():
    rows = []
    items = list(STUCK_LABELS.items())
    for i in range(0, len(items), 2):
        row = []
        for st, (icon, label) in items[i:i + 2]:
            row.append((f"{icon} {label}", f"stuck_{st.value}"))
        rows.append(row)
    rows.append([("↩️ 返回", "stuck_cancel")])
    return kb(rows)


def review_kb():
    return kb([
        [("🔄 继续下一步", "review_continue")],
        [("🌙 今天到此为止", "review_end")],
    ])


def stuck_tag_kb():
    """For optional review tagging."""
    rows = []
    items = list(STUCK_LABELS.items())
    for i in range(0, len(items), 2):
        row = []
        for st, (icon, label) in items[i:i + 2]:
            row.append((f"{icon} {label}", f"tag_{st.value}"))
        rows.append(row)
    rows.append([("跳过", "tag_skip")])
    return kb(rows)


def restart_kb():
    return kb([
        [("🎯 开始新 Session", "restart")],
    ])


# ═══════════════════════════════════════════
# MESSAGE FORMATTERS
# ═══════════════════════════════════════════

def fmt_step_card(step: dict, mainline: str = None) -> str:
    lines = []
    if mainline:
        lines.append(f"📌 *今日主线*：{_esc(mainline)}\n")
    lines.append(f"🔹 *下一步*（{step['duration_min']} 分钟）\n")
    lines.append(f"{_esc(step['instruction'])}\n")
    lines.append(f"✅ 验收：{_esc(step['acceptance_criteria'])}")
    return "\n".join(lines)


def fmt_intervention(stuck_type: StuckType, data: dict) -> str:
    icon, label = STUCK_LABELS[stuck_type]
    lines = [
        f"{icon} *{_esc(label)}*\n",
        f"{_esc(data['intervention_text'])}\n",
        "─────────────\n",
        f"🔸 *起步动作*（{data['restart_step'].duration_min} 分钟）\n",
        f"{_esc(data['restart_step'].instruction)}\n",
        f"✅ {_esc(data['restart_step'].acceptance_criteria)}\n",
        f"\n💬 _{_esc(data['push_line'])}_",
    ]
    return "\n".join(lines)


def fmt_review(session: UserSession) -> str:
    evidence = f"我今天完成了：{session.mainline_title}"
    if session.current_step:
        evidence += f" → {session.current_step.get('instruction', '')[:40]}…"
    lines = [
        "✅ *推进了 1 步*\n",
        f"📋 {_esc(evidence)}\n",
        f"🔥 连续推进 *{session.streak_days}* 天\n",
        "─────────────\n",
        "今天卡在哪了？（可选，帮助识别模式）",
    ]
    return "\n".join(lines)


def fmt_session_end(session: UserSession) -> str:
    lines = [
        "🌙 *今天的推进完成了*\n",
        f"🔥 连续推进 *{session.streak_days}* 天",
    ]
    if session.evidence:
        lines.append("\n─────────────")
        lines.append(f"📋 *证据库*（共 {len(session.evidence)} 条）\n")
        for ev in session.evidence[-5:]:
            lines.append(f"  • {_esc(ev['text'][:50])}")
    lines.append("\n\n每一步都是证据。明天见。")
    return "\n".join(lines)


def _esc(text: str) -> str:
    """Escape markdown v2 special chars (simplified for MarkdownV1)."""
    # Using Markdown V1 which needs less escaping
    return text.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`") if text else ""


# Actually let's use HTML parse mode to avoid markdown escaping pain
def fmt_step_card_html(step: dict, mainline: str = None) -> str:
    lines = []
    if mainline:
        lines.append(f"📌 <b>今日主线</b>：{mainline}\n")
    lines.append(f"🔹 <b>下一步</b>（{step['duration_min']} 分钟）\n")
    lines.append(f"{step['instruction']}\n")
    lines.append(f"✅ 验收：<i>{step['acceptance_criteria']}</i>")
    return "\n".join(lines)


def fmt_intervention_html(stuck_type: StuckType, data: dict) -> str:
    icon, label = STUCK_LABELS[stuck_type]
    step = data["restart_step"]
    return (
        f"{icon} <b>{label}</b>\n\n"
        f"{data['intervention_text']}\n\n"
        f"─────────────\n\n"
        f"🔸 <b>起步动作</b>（{step.duration_min} 分钟）\n\n"
        f"{step.instruction}\n\n"
        f"✅ {step.acceptance_criteria}\n\n"
        f"💬 <i>{data['push_line']}</i>"
    )


def fmt_review_html(session: UserSession) -> str:
    evidence = f"我今天完成了：{session.mainline_title}"
    if session.current_step:
        evidence += f" → {session.current_step.get('instruction', '')[:40]}…"
    return (
        f"✅ <b>推进了 1 步</b>\n\n"
        f"📋 {evidence}\n\n"
        f"🔥 连续推进 <b>{session.streak_days}</b> 天\n\n"
        f"─────────────\n\n"
        f"今天卡在哪了？（可选，帮助识别模式）"
    )


def fmt_session_end_html(session: UserSession) -> str:
    lines = [
        "🌙 <b>今天的推进完成了</b>\n",
        f"🔥 连续推进 <b>{session.streak_days}</b> 天",
    ]
    if session.evidence:
        lines.append("\n─────────────")
        lines.append(f"📋 <b>证据库</b>（共 {len(session.evidence)} 条）\n")
        for ev in session.evidence[-5:]:
            lines.append(f"  · {ev['text'][:60]}")
    lines.append("\n\n每一步都是证据。明天见。")
    return "\n".join(lines)


# ═══════════════════════════════════════════
# HANDLERS
# ═══════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point: /start"""
    session = UserSession()
    save_session(context, session)
    await update.message.reply_text(
        "🎯 <b>Execution Companion</b>\n\n"
        "今天只做一件事。\n"
        "锁定你的推进点，然后我们一步一步走。",
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/reset — restart session keeping evidence & streak"""
    session = get_session(context)
    session.fsm = FSM.NO_MAINLINE
    session.mainline_title = None
    session.current_step = None
    session.stuck_events = []
    session.time_budget = None
    session.definition_of_win = None
    save_session(context, session)
    await update.message.reply_text(
        "🔄 Session 已重置。\n\n今天只做一件事。",
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )


async def cmd_evidence(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/evidence — show evidence history"""
    session = get_session(context)
    if not session.evidence:
        await update.message.reply_text("📋 证据库还是空的。完成你的第一步，收集第一条证据。")
        return
    lines = [f"📋 <b>证据库</b>（{len(session.evidence)} 条）\n"]
    for ev in session.evidence[-10:]:
        lines.append(f"  · {ev['text'][:60]}")
    lines.append(f"\n🔥 连续推进 <b>{session.streak_days}</b> 天")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/status — show current state"""
    session = get_session(context)
    lines = [f"📊 <b>当前状态</b>：{session.fsm}\n"]
    if session.longline_goal:
        lines.append(f"🧭 长线目标：{session.longline_goal}")
    if session.courses:
        lines.append(f"📚 课程数：{len(session.courses)}")
    if session.mainline_title:
        lines.append(f"📌 今日主线：{session.mainline_title}")
    lines.append(f"🔥 连续推进：{session.streak_days} 天")
    lines.append(f"📋 证据数：{len(session.evidence)}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ── Callback Query Router ──

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Routes all inline keyboard callbacks."""
    query = update.callback_query
    await query.answer()
    data = query.data
    session = get_session(context)

    # ── Mode selection ──
    if data == "mode_manual":
        session.fsm = FSM.NO_MAINLINE
        save_session(context, session)
        await query.edit_message_text(
            "✏️ 今天做到哪算赢？\n\n直接发一条消息告诉我，例如：\n"
            "<i>完成 C 语言第 3 章练习题</i>",
            parse_mode="HTML",
        )
        return

    if data == "mode_longline":
        session.fsm = "LONGLINE_SETUP"
        save_session(context, session)
        await query.edit_message_text(
            "🧭 <b>设置长线目标</b>\n\n"
            "发一条消息告诉我你的大目标，例如：\n"
            "<i>180 天拿到 WGU CS 学位</i>",
            parse_mode="HTML",
        )
        return

    # ── Time budget (clarify) ──
    if data.startswith("time_"):
        t = data.replace("time_", "")
        session.time_budget = f"{t} 分钟"
        save_session(context, session)
        await query.edit_message_text(
            f"⏱ 时间预算：<b>{t} 分钟</b>\n\n做到哪算赢？",
            parse_mode="HTML",
            reply_markup=win_definition_kb(),
        )
        return

    # ── Win definition (clarify) ──
    if data.startswith("win_"):
        win_map = {"win_push": "推进一点点", "win_quiz": "完成一次练习/测验", "win_submit": "提交/预约一件事"}
        session.definition_of_win = win_map.get(data, "推进一点点")

        # Rewrite intercepted big goal into actionable mainline
        if session.fsm == FSM.CLARIFY and session.mainline_title:
            rewritten = f"{session.mainline_title[:15]}… → 今天 {session.time_budget or '30 分钟'}内{session.definition_of_win}"
            session.mainline_title = rewritten

        session.fsm = FSM.MAINLINE_LOCKED
        save_session(context, session)
        await _generate_and_show_step(query, context, session)
        return

    # ── Lock candidates ──
    if data in ("lock_A", "lock_B"):
        key = data.split("_")[1]
        candidates = context.user_data.get("candidates", {})
        chosen = candidates.get(key, {})
        session.mainline_title = chosen.get("title", "推进任务")
        session.mainline_source = "auto_from_longline"
        session.fsm = FSM.MAINLINE_LOCKED
        save_session(context, session)
        await _generate_and_show_step(query, context, session)
        return

    # ── Timer start ──
    if data == "timer_start":
        session.fsm = FSM.EXECUTING
        save_session(context, session)
        duration = session.current_step.get("duration_min", 8) if session.current_step else 8
        await query.edit_message_text(
            f"⏱ <b>计时开始！</b>（{duration} 分钟）\n\n"
            f"{session.current_step.get('instruction', '') if session.current_step else ''}\n\n"
            f"做完了点「完成」，卡住了点「卡住」。",
            parse_mode="HTML",
            reply_markup=executing_kb(),
        )
        return

    # ── Step done ──
    if data == "step_done":
        session.fsm = FSM.SESSION_REVIEW
        session.record_progress()
        evidence = Evidence(
            text=f"完成了：{session.mainline_title} → {session.current_step.get('instruction', '')[:40] if session.current_step else ''}…"
        )
        session.evidence.append({"text": evidence.text, "timestamp": evidence.timestamp, "tags": evidence.tags})
        save_session(context, session)
        await query.edit_message_text(
            fmt_review_html(session),
            parse_mode="HTML",
            reply_markup=stuck_tag_kb(),
        )
        return

    # ── Stuck ──
    if data == "step_stuck":
        session.fsm = FSM.STUCK_PICK
        save_session(context, session)
        await query.edit_message_text(
            "什么卡住了你？",
            reply_markup=stuck_type_kb(),
        )
        return

    if data.startswith("stuck_") and data != "stuck_cancel":
        st_value = data.replace("stuck_", "")
        try:
            stuck_type = StuckType(st_value)
        except ValueError:
            await query.edit_message_text("未知类型，请重试。", reply_markup=stuck_type_kb())
            return

        session.fsm = FSM.INTERVENTION
        session.stuck_events.append({"type": st_value, "timestamp": datetime.now().isoformat()})
        save_session(context, session)

        intervention = AI.generate_intervention(stuck_type)
        # Save restart step for use after user clicks start
        context.user_data["pending_intervention_step"] = {
            "instruction": intervention["restart_step"].instruction,
            "acceptance_criteria": intervention["restart_step"].acceptance_criteria,
            "duration_min": intervention["restart_step"].duration_min,
        }

        await query.edit_message_text(
            fmt_intervention_html(stuck_type, intervention),
            parse_mode="HTML",
            reply_markup=kb([[("⏱ 开始 2 分钟 →", "intervention_go")]]),
        )
        return

    if data == "stuck_cancel":
        session.fsm = FSM.EXECUTING
        save_session(context, session)
        step = session.current_step or {}
        await query.edit_message_text(
            fmt_step_card_html(step, session.mainline_title) + "\n\n⏱ 继续执行中…",
            parse_mode="HTML",
            reply_markup=executing_kb(),
        )
        return

    # ── Intervention go ──
    if data == "intervention_go":
        pending = context.user_data.get("pending_intervention_step")
        if pending:
            session.current_step = pending
        session.fsm = FSM.EXECUTING
        save_session(context, session)
        step = session.current_step or {}
        await query.edit_message_text(
            f"⏱ <b>2 分钟起步！</b>\n\n"
            f"{step.get('instruction', '')}\n\n"
            f"✅ {step.get('acceptance_criteria', '')}",
            parse_mode="HTML",
            reply_markup=executing_kb(),
        )
        return

    # ── Shrink step ──
    if data == "step_shrink":
        micro = AI.shrink_step(session.current_step)
        session.current_step = {
            "instruction": micro.instruction,
            "acceptance_criteria": micro.acceptance_criteria,
            "duration_min": micro.duration_min,
        }
        session.fsm = FSM.EXECUTING
        save_session(context, session)
        await query.edit_message_text(
            f"↩️ <b>缩小到 2 分钟</b>\n\n"
            f"{micro.instruction}\n\n"
            f"✅ {micro.acceptance_criteria}",
            parse_mode="HTML",
            reply_markup=executing_kb(),
        )
        return

    # ── Review tags ──
    if data.startswith("tag_"):
        tag_value = data.replace("tag_", "")
        if tag_value != "skip" and session.evidence:
            session.evidence[-1]["tags"].append(tag_value)
        save_session(context, session)
        await query.edit_message_text(
            fmt_review_html(session).replace("今天卡在哪了？（可选，帮助识别模式）", ""),
            parse_mode="HTML",
            reply_markup=review_kb(),
        )
        return

    # ── Review actions ──
    if data == "review_continue":
        session.fsm = FSM.MAINLINE_LOCKED
        save_session(context, session)
        await _generate_and_show_step(query, context, session)
        return

    if data == "review_end":
        session.fsm = FSM.SESSION_END
        save_session(context, session)
        await query.edit_message_text(
            fmt_session_end_html(session),
            parse_mode="HTML",
            reply_markup=restart_kb(),
        )
        return

    # ── Restart ──
    if data == "restart":
        session.fsm = FSM.NO_MAINLINE
        session.mainline_title = None
        session.current_step = None
        session.stuck_events = []
        save_session(context, session)
        await query.edit_message_text(
            "🎯 <b>新 Session</b>\n\n今天只做一件事。",
            parse_mode="HTML",
            reply_markup=main_menu_kb(),
        )
        return

    # ── Course status toggles ──
    if data.startswith("course_toggle_"):
        idx = int(data.replace("course_toggle_", ""))
        if idx < len(session.courses):
            c = session.courses[idx]
            cycle = {"not_started": "in_progress", "in_progress": "completed", "completed": "not_started"}
            c["status"] = cycle.get(c["status"], "not_started")
            save_session(context, session)
        await _show_courses_editor(query, context, session)
        return

    if data == "courses_done":
        # Generate candidates
        courses = [Course(**c) for c in session.courses]
        candidates = AI.generate_candidates(courses)
        context.user_data["candidates"] = candidates
        session.fsm = FSM.CANDIDATES
        save_session(context, session)

        text = (
            f"🧭 <b>{session.longline_goal}</b>\n\n"
            f"🅰️ <b>{candidates['A']['title']}</b>\n"
            f"   <i>{candidates['A']['reason']}</i>\n\n"
            f"🅱️ <b>{candidates['B']['title']}</b>\n"
            f"   <i>{candidates['B']['reason']}</i>"
        )
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=candidate_kb())
        return

    if data == "course_add":
        await query.edit_message_text(
            "📚 发送课程名称（一行一个）来添加更多课程：",
            parse_mode="HTML",
        )
        session.fsm = "ADDING_COURSES"
        save_session(context, session)
        return


# ── Helper: generate step and show ──

async def _generate_and_show_step(query, context, session):
    step = AI.generate_step(session.mainline_title or "任务", session.time_budget)
    session.current_step = {
        "instruction": step.instruction,
        "acceptance_criteria": step.acceptance_criteria,
        "duration_min": step.duration_min,
    }
    session.fsm = FSM.NEXT_STEP_READY
    save_session(context, session)

    await query.edit_message_text(
        fmt_step_card_html(session.current_step, session.mainline_title),
        parse_mode="HTML",
        reply_markup=step_ready_kb(step.duration_min),
    )


async def _show_courses_editor(query, context, session):
    status_icons = {"not_started": "⬜", "in_progress": "🟡", "completed": "✅"}
    rows = []
    for i, c in enumerate(session.courses):
        icon = status_icons.get(c.get("status", "not_started"), "⬜")
        rows.append([(f"{icon} {c['name']}", f"course_toggle_{i}")])
    rows.append([("➕ 添加课程", "course_add"), ("✅ 完成设置", "courses_done")])
    await query.edit_message_text(
        "📚 <b>课程清单</b>（点击切换状态）\n"
        "⬜ 未开始 → 🟡 进行中 → ✅ 已完成",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(text=t, callback_data=d) for t, d in row]
            for row in rows
        ]),
    )


# ── Text Message Handler (context-dependent) ──

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle free text input based on current FSM state."""
    session = get_session(context)
    text = update.message.text.strip()

    # ── Longline setup: receiving goal title ──
    if session.fsm == "LONGLINE_SETUP":
        session.longline_goal = text
        session.courses = []
        session.fsm = "LONGLINE_COURSES"
        save_session(context, session)
        await update.message.reply_text(
            f"🧭 目标已设置：<b>{text}</b>\n\n"
            "现在发送你的课程清单（一行一个），例如：\n"
            "<i>C779 Web Development\n"
            "C867 Scripting and Programming\n"
            "D322 Cloud Computing</i>",
            parse_mode="HTML",
        )
        return

    # ── Receiving courses list ──
    if session.fsm in ("LONGLINE_COURSES", "ADDING_COURSES"):
        new_courses = [line.strip() for line in text.split("\n") if line.strip()]
        for name in new_courses:
            session.courses.append({"name": name, "status": "not_started"})
        save_session(context, session)
        # Show course editor
        status_icons = {"not_started": "⬜", "in_progress": "🟡", "completed": "✅"}
        rows = []
        for i, c in enumerate(session.courses):
            icon = status_icons.get(c.get("status", "not_started"), "⬜")
            rows.append([(f"{icon} {c['name']}", f"course_toggle_{i}")])
        rows.append([("➕ 添加课程", "course_add"), ("✅ 完成设置", "courses_done")])
        await update.message.reply_text(
            f"📚 已添加 {len(new_courses)} 门课程（点击切换状态）\n"
            "⬜ 未开始 → 🟡 进行中 → ✅ 已完成",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(text=t, callback_data=d) for t, d in row]
                for row in rows
            ]),
        )
        return

    # ── Manual mainline input ──
    if session.fsm == FSM.NO_MAINLINE:
        # Check for big goal interception (R6)
        if AI.is_big_goal(text):
            session.mainline_title = text
            session.fsm = FSM.CLARIFY
            save_session(context, session)
            await update.message.reply_text(
                f"⚡ 「{text[:20]}…」太大了，我们缩小到今天能验收的一步。\n\n"
                "先告诉我：今天能投入多久？",
                parse_mode="HTML",
                reply_markup=time_budget_kb(),
            )
            return

        # Normal mainline
        session.mainline_title = text
        session.fsm = FSM.MAINLINE_LOCKED
        save_session(context, session)

        step = AI.generate_step(text, session.time_budget)
        session.current_step = {
            "instruction": step.instruction,
            "acceptance_criteria": step.acceptance_criteria,
            "duration_min": step.duration_min,
        }
        session.fsm = FSM.NEXT_STEP_READY
        save_session(context, session)

        await update.message.reply_text(
            f"🔒 <b>已锁定</b>：{text}\n\n" + fmt_step_card_html(session.current_step),
            parse_mode="HTML",
            reply_markup=step_ready_kb(step.duration_min),
        )
        return

    # ── Fallback: during execution, treat text as note ──
    if session.fsm in (FSM.EXECUTING, FSM.NEXT_STEP_READY):
        await update.message.reply_text(
            "📝 已记录。继续执行当前步骤 →",
            reply_markup=executing_kb(),
        )
        return

    # ── Default ──
    await update.message.reply_text(
        "发送 /start 开始新的 session，或 /reset 重置当前 session。",
    )


# ═══════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("=" * 50)
        print("ERROR: 请设置环境变量 TELEGRAM_BOT_TOKEN")
        print()
        print("步骤：")
        print("1. 在 Telegram 找 @BotFather")
        print("2. 发送 /newbot 创建机器人")
        print("3. 复制 token")
        print("4. 运行：")
        print("   export TELEGRAM_BOT_TOKEN='你的token'")
        print("   python bot.py")
        print("=" * 50)
        return

    app = Application.builder().token(token).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("evidence", cmd_evidence))
    app.add_handler(CommandHandler("status", cmd_status))

    # Callback queries (inline keyboard)
    app.add_handler(CallbackQueryHandler(callback_router))

    # Text messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("🚀 Execution Companion Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
