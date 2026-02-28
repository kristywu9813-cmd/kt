"""
AI Execution Companion OS v2 — Telegram Bot
=============================================
极简主流程：/today → 自动锁定A → 开始2分钟 → 完成/升级/卡住 → 证据
管理入口：/manage → /goal /phases /tasks /settings
"""

import os
import json
import logging
import asyncio
from datetime import datetime, date

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

from db import database as db
from core import engine
from llm import openai_client as llm

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════
# KEYBOARD HELPERS
# ═══════════════════════════════════════════

def kb(buttons):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row]
        for row in buttons
    ])

# ═══════════════════════════════════════════
# /start
# ═══════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.ensure_user(user_id)
    await update.message.reply_text(
        "🎯 <b>Execution Companion</b>\n\n"
        "今天只做一件事，一步一步走。\n\n"
        "👉 /today — 开始今天的推进\n"
        "⚙️ /manage — 管理目标/任务",
        parse_mode="HTML",
        reply_markup=kb([
            [("▶️ 开始今天", "cmd_today")],
            [("⚙️ 管理", "cmd_manage")],
        ]),
    )

# ═══════════════════════════════════════════
# /today — 主流程核心
# ═══════════════════════════════════════════

async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.callback_query.message
    user_id = update.effective_user.id
    db.ensure_user(user_id)

    if update.callback_query:
        await update.callback_query.answer()

    # ── 1. Check deferred step ──
    deferred = db.get_deferred(user_id)
    if deferred:
        ctx.user_data["current_step_id"] = deferred["deferred_step_id"]
        ctx.user_data["current_mainline_id"] = deferred["mainline_id"]
        db.update_step(deferred["deferred_step_id"], status="ready")
        db.clear_deferred(user_id)

        text = (
            f"📌 <b>继续昨天的推进</b>\n\n"
            f"🔹 {deferred['mainline_title']}\n\n"
            f"{deferred['instruction']}\n\n"
            f"✅ {deferred['acceptance_criteria']}"
        )
        await _send(msg, text, kb([
            [("▶️ 开始 2 分钟", "timer_micro")],
            [("🔄 换一个新任务", "today_fresh")],
        ]))
        return

    # ── 2. Check existing today mainline ──
    existing = db.get_today_mainline(user_id)
    if existing:
        step = db.get_active_step(existing["mainline_id"])
        if step:
            ctx.user_data["current_step_id"] = step["step_id"]
            ctx.user_data["current_mainline_id"] = existing["mainline_id"]
            await _show_step(msg, existing["title"], step)
            return

    # ── 3. Generate candidates ──
    await _generate_and_show_today(msg, ctx, user_id)


async def _generate_and_show_today(msg, ctx, user_id):
    """Generate A/B candidates and auto-present A."""
    goal = db.get_active_goal(user_id)
    phase = None
    phase_id = None

    if goal:
        phase = db.get_active_phase(goal["goal_id"])
        if phase:
            phase_id = phase["phase_id"]

    user = db.get_user(user_id)
    low_energy = bool(user.get("low_energy_mode", 0))

    candidates = engine.choose_candidates(user_id, phase_id, low_energy)
    ctx.user_data["candidates"] = candidates
    ctx.user_data["phase_id"] = phase_id
    ctx.user_data["goal_id"] = goal["goal_id"] if goal else None

    chosen = candidates["B"] if low_energy else candidates["A"]
    ctx.user_data["chosen_candidate"] = chosen

    # Auto-lock A and generate micro step
    mainline_id = db.create_mainline(
        user_id=user_id,
        title=chosen["title"],
        source="auto_from_phase" if phase_id else "manual",
        goal_id=goal["goal_id"] if goal else None,
        phase_id=phase_id,
        task_id_ref=chosen.get("task_id"),
    )
    ctx.user_data["current_mainline_id"] = mainline_id

    # Mark task as in_progress
    if chosen.get("task_id"):
        db.update_task(chosen["task_id"], status="in_progress")

    # Generate micro step via LLM
    task_title = None
    if chosen.get("task_id"):
        # Get task title from candidates
        pass
    micro = llm.generate_micro_step(chosen["title"], task_title)
    ms = micro["micro_step"]

    step_id = db.create_step(
        mainline_id=mainline_id,
        kind="micro",
        duration_min=ms["duration_min"],
        instruction=ms["instruction"],
        acceptance_criteria=ms["acceptance_criteria"],
    )
    ctx.user_data["current_step_id"] = step_id

    # Generate if-then plan (async, save quietly)
    if_then = llm.generate_if_then_plan(chosen["title"])
    if if_then and "plan" in if_then:
        plan = if_then["plan"]
        db.save_if_then(user_id, plan.get("if_trigger", ""), plan.get("then_action", ""), plan.get("reward"))

    # Show to user
    buttons = [[("▶️ 开始 2 分钟", "timer_micro")]]
    if not low_energy:
        buttons.append([("🔄 换一个", "switch_B")])
    buttons.append([("🔋 低能量模式", "low_energy")])

    text = (
        f"📌 <b>今日主线</b>：{chosen['title']}\n\n"
        f"🔹 <b>2 分钟起步</b>\n\n"
        f"{ms['instruction']}\n\n"
        f"✅ {ms['acceptance_criteria']}"
    )
    await _send(msg, text, kb(buttons))


async def _show_step(msg, mainline_title, step):
    text = (
        f"📌 <b>{mainline_title}</b>\n\n"
        f"🔹 <b>{step['duration_min']} 分钟</b>\n\n"
        f"{step['instruction']}\n\n"
        f"✅ {step['acceptance_criteria']}"
    )
    await _send(msg, text, kb([
        [("▶️ 开始 {0} 分钟".format(step['duration_min']), "timer_micro" if step['kind'] == 'micro' else "timer_upgrade")],
    ]))


# ═══════════════════════════════════════════
# CALLBACK ROUTER
# ═══════════════════════════════════════════

async def callback_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    user_id = update.effective_user.id
    db.ensure_user(user_id)

    # ── Navigation ──
    if data == "cmd_today":
        return await cmd_today(update, ctx)
    if data == "cmd_manage":
        return await _show_manage(q.message, user_id)

    # ── Today: switch to B ──
    if data == "switch_B":
        candidates = ctx.user_data.get("candidates", {})
        b = candidates.get("B")
        if not b:
            await q.edit_message_text("没有备选方案，继续当前主线。")
            return

        # Re-lock with B
        mainline_id = ctx.user_data.get("current_mainline_id")
        if mainline_id:
            # Update mainline title
            conn = db.get_conn()
            conn.execute("UPDATE mainlines SET title=? WHERE mainline_id=?", (b["title"], mainline_id))
            conn.commit()
            conn.close()

        micro = llm.generate_micro_step(b["title"])
        ms = micro["micro_step"]
        step_id = db.create_step(
            mainline_id=mainline_id,
            kind="micro",
            duration_min=ms["duration_min"],
            instruction=ms["instruction"],
            acceptance_criteria=ms["acceptance_criteria"],
        )
        ctx.user_data["current_step_id"] = step_id
        ctx.user_data["chosen_candidate"] = b

        text = (
            f"📌 <b>已切换</b>：{b['title']}\n\n"
            f"🔹 <b>2 分钟起步</b>\n\n"
            f"{ms['instruction']}\n\n"
            f"✅ {ms['acceptance_criteria']}"
        )
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb([
            [("▶️ 开始 2 分钟", "timer_micro")],
        ]))
        return

    # ── Low energy mode ──
    if data == "low_energy":
        db.update_user(user_id, low_energy_mode=1)
        await q.edit_message_text("🔋 低能量模式已开启。重新生成…", parse_mode="HTML")
        await _generate_and_show_today(q.message, ctx, user_id)
        return

    if data == "today_fresh":
        db.clear_deferred(user_id)
        await _generate_and_show_today(q.message, ctx, user_id)
        return

    # ── Timer start (micro: 2min) ──
    if data == "timer_micro":
        step_id = ctx.user_data.get("current_step_id")
        if step_id:
            db.update_step(step_id, status="executing")
        await q.edit_message_text(
            "⏱ <b>2 分钟开始！</b>\n\n做完点「完成」，卡住点「卡住」。",
            parse_mode="HTML",
            reply_markup=kb([
                [("✅ 完成了", "step_done_micro")],
                [("🧱 卡住了", "step_stuck"), ("↩️ 缩小", "step_shrink")],
                [("🚪 退出（明天继续）", "step_exit")],
            ]),
        )
        return

    # ── Timer start (upgrade: 8min) ──
    if data == "timer_upgrade":
        step_id = ctx.user_data.get("current_step_id")
        if step_id:
            db.update_step(step_id, status="executing")
        await q.edit_message_text(
            "⏱ <b>8 分钟继续！</b>\n\n你已经启动了，保持这个势头。",
            parse_mode="HTML",
            reply_markup=kb([
                [("✅ 完成了", "step_done_upgrade")],
                [("🧱 卡住了", "step_stuck"), ("↩️ 缩小", "step_shrink")],
                [("🚪 退出（明天继续）", "step_exit")],
            ]),
        )
        return

    # ── Step done (micro) → offer upgrade ──
    if data == "step_done_micro":
        step_id = ctx.user_data.get("current_step_id")
        if step_id:
            db.update_step(step_id, status="done")

        mainline_id = ctx.user_data.get("current_mainline_id")
        mainline = None
        if mainline_id:
            conn = db.get_conn()
            row = conn.execute("SELECT * FROM mainlines WHERE mainline_id=?", (mainline_id,)).fetchone()
            conn.close()
            mainline = dict(row) if row else None

        # Generate upgrade step
        title = mainline["title"] if mainline else "任务"
        upgrade = llm.generate_upgrade_step(title)
        us = upgrade["step"]

        upgrade_step_id = db.create_step(
            mainline_id=mainline_id,
            kind="upgrade",
            duration_min=us["duration_min"],
            instruction=us["instruction"],
            acceptance_criteria=us["acceptance_criteria"],
            difficulty=us.get("difficulty", 1),
        )
        ctx.user_data["current_step_id"] = upgrade_step_id

        await q.edit_message_text(
            f"✅ <b>2 分钟完成！</b>\n\n"
            f"🔥 继续 8 分钟吗？\n\n"
            f"{us['instruction']}\n\n"
            f"✅ {us['acceptance_criteria']}\n\n"
            f"<i>结束也算赢，你已经推进了一步。</i>",
            parse_mode="HTML",
            reply_markup=kb([
                [("🔥 继续 8 分钟", "timer_upgrade")],
                [("🌙 结束（也算赢）", "review_start")],
            ]),
        )
        return

    # ── Step done (upgrade) → review ──
    if data == "step_done_upgrade":
        step_id = ctx.user_data.get("current_step_id")
        if step_id:
            db.update_step(step_id, status="done")
        await _start_review(q, ctx, user_id)
        return

    # ── Review start ──
    if data == "review_start":
        await _start_review(q, ctx, user_id)
        return

    # ── Step stuck ──
    if data == "step_stuck":
        await q.edit_message_text(
            "先给情绪取个名字：",
            reply_markup=kb([
                [("😤 烦躁", "emo_烦躁"), ("😰 焦虑", "emo_焦虑")],
                [("😩 疲惫", "emo_疲惫"), ("😶 麻木", "emo_麻木")],
                [("😔 沮丧", "emo_沮丧"), ("🤷 不知道", "emo_不知道")],
            ]),
        )
        return

    # ── Emotion selected → stuck type ──
    if data.startswith("emo_"):
        emotion = data.replace("emo_", "")
        ctx.user_data["emotion_label"] = emotion
        await q.edit_message_text(
            f"情绪：<b>{emotion}</b>\n\n什么卡住了你？",
            parse_mode="HTML",
            reply_markup=kb([
                [("✨ 完美主义", "stuck_PERFECTIONISM")],
                [("🏔 目标太大", "stuck_GOAL_TOO_BIG")],
                [("🌀 想太多", "stuck_OVERTHINKING")],
                [("😶‍🌫️ 情绪内耗", "stuck_EMOTIONAL_FRICTION")],
                [("📱 想刷手机", "stuck_REWARD_MISMATCH")],
                [("🔒 觉得不行", "stuck_SELF_LIMITING")],
            ]),
        )
        return

    # ── Stuck type selected → intervention ──
    if data.startswith("stuck_"):
        stuck_type = data.replace("stuck_", "")
        emotion = ctx.user_data.get("emotion_label", "")
        step_id = ctx.user_data.get("current_step_id")
        step = db.get_step(step_id) if step_id else None

        mainline_id = ctx.user_data.get("current_mainline_id")
        mainline = None
        if mainline_id:
            conn = db.get_conn()
            row = conn.execute("SELECT * FROM mainlines WHERE mainline_id=?", (mainline_id,)).fetchone()
            conn.close()
            mainline = dict(row) if row else None

        # Get recent evidence for SELF_LIMITING
        recent_evidence = None
        if stuck_type == "SELF_LIMITING":
            evs = db.list_evidence(user_id, limit=5)
            recent_evidence = [e["counter_evidence"] for e in evs] if evs else None

        # Record stuck event
        if step_id:
            db.create_stuck_event(step_id, stuck_type, emotion)

        # Generate intervention
        iv = llm.generate_intervention(
            stuck_type=stuck_type,
            emotion_label=emotion,
            mainline_title=mainline["title"] if mainline else None,
            step_instruction=step["instruction"] if step else None,
            recent_evidence=recent_evidence,
        )

        # Build message
        lines = [
            f"💬 <b>{iv.get('intervention_text', '')}</b>\n",
            f"🫁 <i>{iv.get('body_reset', '深呼吸3次。')}</i>\n",
        ]
        if iv.get("evidence_quotes"):
            lines.append("📋 <b>你的证据：</b>")
            for eq in iv["evidence_quotes"]:
                lines.append(f"  · {eq}")
            lines.append("")

        rs = iv.get("restart_step", {})
        lines.append(f"🔸 <b>起步动作</b>（{rs.get('duration_min', 2)} 分钟）\n")
        lines.append(f"{rs.get('instruction', '')}\n")
        lines.append(f"✅ {rs.get('acceptance_criteria', '')}\n")
        lines.append(f"\n💬 <i>{iv.get('push_line', '回到计时器 →')}</i>")

        # Save restart step
        if mainline_id:
            restart_step_id = db.create_step(
                mainline_id=mainline_id,
                kind="micro",
                duration_min=rs.get("duration_min", 2),
                instruction=rs.get("instruction", "做一个最小动作"),
                acceptance_criteria=rs.get("acceptance_criteria", "动了就行"),
            )
            ctx.user_data["current_step_id"] = restart_step_id

        await q.edit_message_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=kb([
                [("▶️ 开始 2 分钟", "timer_micro")],
            ]),
        )
        return

    # ── Shrink step ──
    if data == "step_shrink":
        step_id = ctx.user_data.get("current_step_id")
        step = db.get_step(step_id) if step_id else None
        mainline_id = ctx.user_data.get("current_mainline_id")

        instruction = step["instruction"] if step else "做一个最小动作"
        first_action = instruction.split("，")[0] if "，" in instruction else instruction.split("。")[0]

        if mainline_id:
            shrink_id = db.create_step(
                mainline_id=mainline_id,
                kind="micro",
                duration_min=2,
                instruction=f"只做一件事：{first_action}。做完就算赢。",
                acceptance_criteria="完成了这一个动作",
            )
            ctx.user_data["current_step_id"] = shrink_id

        await q.edit_message_text(
            f"↩️ <b>缩小到 2 分钟</b>\n\n"
            f"只做一件事：{first_action}\n\n"
            f"✅ 做完就算赢",
            parse_mode="HTML",
            reply_markup=kb([
                [("▶️ 开始 2 分钟", "timer_micro")],
            ]),
        )
        return

    # ── Exit (defer) ──
    if data == "step_exit":
        step_id = ctx.user_data.get("current_step_id")
        mainline_id = ctx.user_data.get("current_mainline_id")
        if step_id:
            db.update_step(step_id, status="deferred")
            db.create_deferred(user_id, step_id, mainline_id, reason="exit")

        await q.edit_message_text(
            "🌙 <b>没关系，明天继续。</b>\n\n"
            "下次 /today 会自动帮你接上今天的位置。\n"
            "退出不是失败，是暂停。",
            parse_mode="HTML",
            reply_markup=kb([
                [("🎯 回到主菜单", "cmd_start_fresh")],
            ]),
        )
        return

    if data == "cmd_start_fresh":
        await q.edit_message_text(
            "🎯 随时发 /today 继续推进。",
            parse_mode="HTML",
        )
        return

    # ── Review: stuck tag (optional) ──
    if data.startswith("review_tag_"):
        tag = data.replace("review_tag_", "")
        ctx.user_data["review_stuck_tag"] = tag
        await _finish_review(q, ctx, user_id)
        return

    if data == "review_skip_tag":
        await _finish_review(q, ctx, user_id)
        return

    # ── Manage callbacks ──
    if data == "manage_goal":
        return await _show_goal_menu(q.message, user_id, edit=True)
    if data == "manage_phases":
        return await _show_phases_menu(q.message, user_id)
    if data == "manage_tasks":
        return await _show_tasks_menu(q, ctx, user_id)

    # ── Goal creation flow ──
    if data == "goal_create":
        ctx.user_data["awaiting"] = "goal_title"
        await q.edit_message_text("📝 发送你的目标（一句话）：\n\n例如：<i>180天拿到WGU CS学位</i>", parse_mode="HTML")
        return

    # ── Phase creation ──
    if data == "phase_create":
        ctx.user_data["awaiting"] = "phase_title"
        goal = db.get_active_goal(user_id)
        ctx.user_data["target_goal_id"] = goal["goal_id"] if goal else None
        await q.edit_message_text("📝 发送阶段名称：\n\n例如：<i>Sophia先修阶段</i>", parse_mode="HTML")
        return

    if data.startswith("phase_activate_"):
        phase_id = int(data.replace("phase_activate_", ""))
        goal = db.get_active_goal(user_id)
        if goal:
            db.set_active_phase(goal["goal_id"], phase_id)
        await q.edit_message_text("✅ 阶段已激活。", parse_mode="HTML")
        return

    # ── Task management ──
    if data == "tasks_add":
        ctx.user_data["awaiting"] = "task_title"
        await q.edit_message_text("📝 发送任务标题：", parse_mode="HTML")
        return

    if data == "tasks_import":
        ctx.user_data["awaiting"] = "import_paste"
        await q.edit_message_text(
            "📋 <b>粘贴任务清单</b>（一行一个）\n\n"
            "格式：<code>任务名 - 状态 - tags:标签1,标签2</code>\n\n"
            "例如：\n"
            "<code>C960 Discrete Math - in_progress - tags:wgu,math\n"
            "C867 Scripting - not_started - tags:wgu</code>\n\n"
            "状态和标签可选，默认 not_started。",
            parse_mode="HTML",
        )
        return

    if data.startswith("import_confirm_"):
        import_id = int(data.replace("import_confirm_", ""))
        db.confirm_import(import_id)
        draft = db.get_import_draft(import_id)
        count = len(draft["parsed_items"]) if draft else 0
        await q.edit_message_text(f"✅ 已导入 {count} 个任务！\n\n发 /today 开始推进。", parse_mode="HTML")
        return

    if data.startswith("import_discard_"):
        import_id = int(data.replace("import_discard_", ""))
        db.discard_import(import_id)
        await q.edit_message_text("🗑 已丢弃导入。", parse_mode="HTML")
        return

    if data.startswith("task_toggle_"):
        task_id = int(data.replace("task_toggle_", ""))
        conn = db.get_conn()
        row = conn.execute("SELECT status FROM task_items WHERE task_id=?", (task_id,)).fetchone()
        conn.close()
        if row:
            cycle = {"not_started": "in_progress", "in_progress": "completed", "completed": "not_started", "dropped": "not_started"}
            new_status = cycle.get(row["status"], "not_started")
            db.update_task(task_id, status=new_status)
        return await _show_tasks_menu(q, ctx, user_id)

    if data.startswith("task_delete_"):
        task_id = int(data.replace("task_delete_", ""))
        db.delete_task(task_id)
        return await _show_tasks_menu(q, ctx, user_id)

    if data == "tasks_back":
        return await _show_manage(q.message, user_id, edit=True)


# ═══════════════════════════════════════════
# REVIEW FLOW
# ═══════════════════════════════════════════

async def _start_review(q, ctx, user_id):
    await q.edit_message_text(
        "✅ <b>推进了一步！</b>\n\n"
        "今天卡在哪了？（可选）",
        parse_mode="HTML",
        reply_markup=kb([
            [("✨ 完美主义", "review_tag_PERFECTIONISM"), ("🌀 想太多", "review_tag_OVERTHINKING")],
            [("📱 想刷手机", "review_tag_REWARD_MISMATCH"), ("🔒 觉得不行", "review_tag_SELF_LIMITING")],
            [("跳过", "review_skip_tag")],
        ]),
    )


async def _finish_review(q, ctx, user_id):
    mainline_id = ctx.user_data.get("current_mainline_id")
    mainline = None
    if mainline_id:
        conn = db.get_conn()
        row = conn.execute("SELECT * FROM mainlines WHERE mainline_id=?", (mainline_id,)).fetchone()
        conn.close()
        mainline = dict(row) if row else None

    step_id = ctx.user_data.get("current_step_id")
    step = db.get_step(step_id) if step_id else None

    # Build evidence
    evidence_text = f"完成了：{mainline['title'] if mainline else '任务'}"
    if step:
        evidence_text += f" → {step['instruction'][:40]}…"

    tags = ["small_win"]
    stuck_tag = ctx.user_data.get("review_stuck_tag")
    if stuck_tag:
        tags.append(stuck_tag)

    db.create_evidence(user_id, evidence_text, tags)
    streak = db.update_streak(user_id)

    # Count total evidence
    all_evidence = db.list_evidence(user_id, limit=100)

    await q.edit_message_text(
        f"📋 <b>证据已记录</b>\n\n"
        f"「{evidence_text}」\n\n"
        f"🔥 连续推进 <b>{streak}</b> 天\n"
        f"📋 证据库共 <b>{len(all_evidence)}</b> 条\n\n"
        f"每一步都是证据。",
        parse_mode="HTML",
        reply_markup=kb([
            [("🎯 继续下一步", "cmd_today")],
            [("🌙 今天结束", "session_end")],
        ]),
    )
    return


# ═══════════════════════════════════════════
# /manage — 管理入口
# ═══════════════════════════════════════════

async def cmd_manage(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.ensure_user(user_id)
    msg = update.message or update.callback_query.message
    if update.callback_query:
        await update.callback_query.answer()
    await _show_manage(msg, user_id)


async def _show_manage(msg, user_id, edit=False):
    goal = db.get_active_goal(user_id)
    phase = None
    task_count = 0

    status_lines = ["⚙️ <b>管理中心</b>\n"]
    if goal:
        status_lines.append(f"🧭 目标：{goal['title']}")
        phase = db.get_active_phase(goal["goal_id"])
        if phase:
            status_lines.append(f"📂 当前阶段：{phase['title']}")
            tasks = db.list_tasks(phase["phase_id"])
            task_count = len(tasks)
            completed = sum(1 for t in tasks if t["status"] == "completed")
            status_lines.append(f"📋 任务：{completed}/{task_count} 完成")
    else:
        status_lines.append("还没有设置目标。")

    user = db.get_user(user_id)
    status_lines.append(f"\n🔥 连续推进：{user['streak_days']} 天")

    text = "\n".join(status_lines)
    markup = kb([
        [("🧭 目标", "manage_goal"), ("📂 阶段", "manage_phases")],
        [("📋 任务", "manage_tasks")],
        [("▶️ 回到 /today", "cmd_today")],
    ])

    if edit:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await msg.reply_text(text, parse_mode="HTML", reply_markup=markup)


# ── Goal menu ──

async def _show_goal_menu(msg, user_id, edit=False):
    goal = db.get_active_goal(user_id)
    if goal:
        text = f"🧭 <b>当前目标</b>：{goal['title']}"
        if goal.get("deadline_date"):
            text += f"\n📅 截止：{goal['deadline_date']}"
        markup = kb([
            [("✏️ 创建新目标", "goal_create")],
            [("← 返回", "cmd_manage")],
        ])
    else:
        text = "🧭 还没有目标。\n\n设一个长线目标（可选），系统会帮你拆解每天的推进点。"
        markup = kb([
            [("➕ 创建目标", "goal_create")],
            [("← 返回", "cmd_manage")],
        ])

    if edit:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await msg.reply_text(text, parse_mode="HTML", reply_markup=markup)


# ── Phases menu ──

async def _show_phases_menu(msg, user_id):
    goal = db.get_active_goal(user_id)
    if not goal:
        await msg.edit_text("先创建一个目标。", reply_markup=kb([[("🧭 创建目标", "goal_create")]]))
        return

    phases = db.list_phases(goal["goal_id"])
    if not phases:
        await msg.edit_text(
            "📂 还没有阶段。\n\n阶段用来分段推进目标（例如：先修阶段 → 核心课程 → 冲刺）",
            parse_mode="HTML",
            reply_markup=kb([
                [("➕ 创建阶段", "phase_create")],
                [("← 返回", "cmd_manage")],
            ]),
        )
        return

    lines = ["📂 <b>阶段列表</b>\n"]
    buttons = []
    for p in phases:
        icon = "🟢" if p["is_active"] else "⚪"
        lines.append(f"{icon} {p['title']}")
        if not p["is_active"]:
            buttons.append([(f"激活「{p['title'][:15]}」", f"phase_activate_{p['phase_id']}")])

    buttons.append([("➕ 新阶段", "phase_create")])
    buttons.append([("← 返回", "cmd_manage")])

    await msg.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb(buttons))


# ── Tasks menu ──

async def _show_tasks_menu(q_or_msg, ctx, user_id):
    goal = db.get_active_goal(user_id)
    phase = None
    if goal:
        phase = db.get_active_phase(goal["goal_id"])

    if not phase:
        msg = q_or_msg.message if hasattr(q_or_msg, 'message') else q_or_msg
        text = "📋 先创建目标和阶段才能管理任务。"
        try:
            await msg.edit_text(text, reply_markup=kb([[("🧭 创建目标", "goal_create"), ("← 返回", "cmd_manage")]]))
        except:
            await msg.reply_text(text, reply_markup=kb([[("🧭 创建目标", "goal_create"), ("← 返回", "cmd_manage")]]))
        return

    tasks = db.list_tasks(phase["phase_id"])
    status_icons = {"not_started": "⬜", "in_progress": "🟡", "completed": "✅", "dropped": "🗑"}

    lines = [f"📋 <b>任务列表</b>（{phase['title']}）\n"]
    lines.append("点击切换状态：⬜→🟡→✅\n")

    buttons = []
    for t in tasks[:20]:
        icon = status_icons.get(t["status"], "⬜")
        buttons.append([(f"{icon} {t['title'][:30]}", f"task_toggle_{t['task_id']}")])

    buttons.append([("➕ 添加任务", "tasks_add"), ("📋 批量导入", "tasks_import")])
    buttons.append([("← 返回", "tasks_back")])

    if not tasks:
        lines.append("还没有任务。添加任务或批量导入。")

    msg = q_or_msg.message if hasattr(q_or_msg, 'message') else q_or_msg
    try:
        await msg.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb(buttons))
    except:
        await msg.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=kb(buttons))


# ═══════════════════════════════════════════
# TEXT MESSAGE HANDLER
# ═══════════════════════════════════════════

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    awaiting = ctx.user_data.get("awaiting")
    db.ensure_user(user_id)

    # ── Goal creation ──
    if awaiting == "goal_title":
        ctx.user_data["awaiting"] = None
        goal_id = db.create_goal(user_id, text)
        # Auto-create default phase
        phase_id = db.create_phase(goal_id, "默认阶段", is_active=1)
        ctx.user_data["target_phase_id"] = phase_id

        await update.message.reply_text(
            f"✅ 目标已创建：<b>{text}</b>\n\n"
            f"已自动创建「默认阶段」。\n"
            f"现在可以添加任务，或发 /today 开始推进。",
            parse_mode="HTML",
            reply_markup=kb([
                [("📋 添加任务", "manage_tasks")],
                [("▶️ 直接开始 /today", "cmd_today")],
            ]),
        )
        return

    # ── Phase creation ──
    if awaiting == "phase_title":
        ctx.user_data["awaiting"] = None
        goal_id = ctx.user_data.get("target_goal_id")
        if not goal_id:
            goal = db.get_active_goal(user_id)
            goal_id = goal["goal_id"] if goal else None
        if goal_id:
            db.create_phase(goal_id, text, is_active=1)
            await update.message.reply_text(f"✅ 阶段「{text}」已创建并激活。", parse_mode="HTML",
                reply_markup=kb([[("📋 管理任务", "manage_tasks"), ("← 返回", "cmd_manage")]]))
        return

    # ── Task creation ──
    if awaiting == "task_title":
        ctx.user_data["awaiting"] = None
        goal = db.get_active_goal(user_id)
        phase = db.get_active_phase(goal["goal_id"]) if goal else None
        if phase:
            db.create_task(phase["phase_id"], text)
            await update.message.reply_text(f"✅ 任务已添加：{text}", parse_mode="HTML",
                reply_markup=kb([[("➕ 继续添加", "tasks_add"), ("📋 查看任务", "manage_tasks")]]))
        else:
            await update.message.reply_text("先创建目标和阶段。", reply_markup=kb([[("🧭 创建目标", "goal_create")]]))
        return

    # ── Import paste ──
    if awaiting == "import_paste":
        ctx.user_data["awaiting"] = None
        goal = db.get_active_goal(user_id)
        phase = db.get_active_phase(goal["goal_id"]) if goal else None
        if not phase:
            await update.message.reply_text("先创建目标和阶段。")
            return

        parsed = engine.parse_import_text(text)
        if not parsed:
            await update.message.reply_text("没有解析到任务。请检查格式后重试。")
            return

        import_id = db.create_import_draft(user_id, phase["phase_id"], text, parsed, "paste")

        # Show preview
        status_icons = {"not_started": "⬜", "in_progress": "🟡", "completed": "✅"}
        lines = [f"📋 <b>导入预览</b>（{len(parsed)} 个任务）\n"]
        for i, item in enumerate(parsed[:20]):
            icon = status_icons.get(item.get("status", "not_started"), "⬜")
            tags = ", ".join(item.get("tags", []))
            tag_str = f" [{tags}]" if tags else ""
            lines.append(f"{i+1}. {icon} {item['title']}{tag_str}")

        lines.append("\n确认导入？")

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=kb([
                [("✅ 确认导入", f"import_confirm_{import_id}")],
                [("🗑 丢弃", f"import_discard_{import_id}")],
            ]),
        )
        return

    # ── Default: treat as manual mainline (no goal/phase scenario) ──
    if engine.is_big_goal(text):
        await update.message.reply_text(
            f"⚡ 「{text[:20]}…」太大了。\n\n"
            "建议先用 /manage → 创建目标 → 添加任务，\n"
            "然后 /today 会自动帮你拆成每天的小步。",
            parse_mode="HTML",
            reply_markup=kb([
                [("⚙️ 去管理", "cmd_manage")],
                [("▶️ 直接开始", "cmd_today")],
            ]),
        )
        return

    # Quick manual mainline
    mainline_id = db.create_mainline(user_id, text)
    micro = llm.generate_micro_step(text)
    ms = micro["micro_step"]
    step_id = db.create_step(mainline_id, "micro", ms["duration_min"], ms["instruction"], ms["acceptance_criteria"])
    ctx.user_data["current_mainline_id"] = mainline_id
    ctx.user_data["current_step_id"] = step_id

    await update.message.reply_text(
        f"🔒 <b>已锁定</b>：{text}\n\n"
        f"🔹 <b>2 分钟起步</b>\n\n"
        f"{ms['instruction']}\n\n"
        f"✅ {ms['acceptance_criteria']}",
        parse_mode="HTML",
        reply_markup=kb([[("▶️ 开始 2 分钟", "timer_micro")]]),
    )


# ═══════════════════════════════════════════
# COMMANDS: /evidence /status
# ═══════════════════════════════════════════

async def cmd_evidence(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    evs = db.list_evidence(user_id, limit=10)
    if not evs:
        await update.message.reply_text("📋 证据库还是空的。发 /today 完成第一步。")
        return
    user = db.get_user(user_id)
    lines = [f"📋 <b>证据库</b>（共 {len(evs)} 条）\n"]
    for ev in evs:
        lines.append(f"  · {ev['counter_evidence'][:60]}")
    lines.append(f"\n🔥 连续推进 <b>{user['streak_days']}</b> 天")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = db.get_user(user_id)
    goal = db.get_active_goal(user_id)
    lines = ["📊 <b>状态</b>\n"]
    if goal:
        lines.append(f"🧭 目标：{goal['title']}")
        phase = db.get_active_phase(goal["goal_id"])
        if phase:
            lines.append(f"📂 阶段：{phase['title']}")
            tasks = db.list_tasks(phase["phase_id"])
            c = sum(1 for t in tasks if t["status"] == "completed")
            lines.append(f"📋 任务：{c}/{len(tasks)}")
    lines.append(f"🔥 连续：{user['streak_days']} 天")
    deferred = db.get_deferred(user_id)
    if deferred:
        lines.append(f"⏸ 有未完成步骤待继续")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ═══════════════════════════════════════════
# HELPER
# ═══════════════════════════════════════════

async def _send(msg, text, markup):
    """Send or edit message."""
    try:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=markup)
    except:
        await msg.reply_text(text, parse_mode="HTML", reply_markup=markup)


# ═══════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("=" * 50)
        print("ERROR: 请设置环境变量 TELEGRAM_BOT_TOKEN")
        print("=" * 50)
        return

    # Init database
    db.init_db()
    logger.info("Database initialized")

    # Check OpenAI key
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if openai_key:
        logger.info("OpenAI API key found, LLM features enabled")
    else:
        logger.warning("OPENAI_API_KEY not set, using fallback responses")

    app = Application.builder().token(token).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("manage", cmd_manage))
    app.add_handler(CommandHandler("evidence", cmd_evidence))
    app.add_handler(CommandHandler("status", cmd_status))

    # Callbacks
    app.add_handler(CallbackQueryHandler(callback_router))

    # Text
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("🚀 Execution Companion Bot v2 is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
