"""
Core Rule Engine — candidate selection, import parsing, big-goal interception.
This is the "rules layer" that decides WHAT to do; LLM decides HOW to say it.
"""

import re
import json
from db import database as db


# ── Candidate Selection (§9) ──

def choose_candidates(user_id: int, phase_id: int = None, low_energy: bool = False) -> dict:
    """
    Returns {"A": {...}, "B": {...}} candidates.
    A = shortest path (in_progress first, then easiest not_started)
    B = low energy (lighter task or shrunk version of A)
    """
    if not phase_id:
        goal = db.get_active_goal(user_id)
        if goal:
            phase = db.get_active_phase(goal["goal_id"])
            if phase:
                phase_id = phase["phase_id"]

    if not phase_id:
        return {
            "A": {
                "title": "建立任务池 — 写出5个待办任务标题",
                "reason": "还没有任务，先列出来",
                "task_id": None,
            },
            "B": {
                "title": "建立任务池 — 写出3个关键任务标题",
                "reason": "轻量版，只写3个最重要的",
                "task_id": None,
            },
        }

    in_progress = db.list_tasks(phase_id, status_filter="in_progress")
    not_started = db.list_tasks(phase_id, status_filter="not_started")
    all_tasks = in_progress + not_started

    if not all_tasks:
        return {
            "A": {"title": "建立任务池 — 写出5个待办任务标题", "reason": "任务池为空", "task_id": None},
            "B": {"title": "建立任务池 — 写出3个关键任务标题", "reason": "轻量版", "task_id": None},
        }

    # A: first in_progress, or easiest not_started
    primary = in_progress[0] if in_progress else not_started[0]
    a_title = f"推进「{primary['title']}」"

    # B: different task or lighter version
    secondary = None
    for t in all_tasks:
        if t["task_id"] != primary["task_id"]:
            secondary = t
            break

    if secondary:
        b_title = f"轻量推进「{secondary['title']}」— 阅读/整理/预习"
    else:
        b_title = f"「{primary['title']}」— 只做最小起步动作"

    return {
        "A": {
            "title": a_title,
            "reason": f"{'继续进行中' if primary['status'] == 'in_progress' else '优先启动'}",
            "task_id": primary["task_id"],
        },
        "B": {
            "title": b_title,
            "reason": "低能量也能推进",
            "task_id": secondary["task_id"] if secondary else primary["task_id"],
        },
    }


# ── Big Goal Interception (R6) ──

BIG_GOAL_PATTERNS = [
    r"\d+天", r"\d+个月", r"学位", r"毕业", r"全部", r"所有",
    r"完成整个", r"master", r"degree", r"finish all", r"月内",
    r"半年", r"一年", r"拿到.*证",
]


def is_big_goal(text: str) -> bool:
    t = text.lower()
    return any(re.search(p, t) for p in BIG_GOAL_PATTERNS)


# ── Import Parsing (§8) ──

def parse_import_text(raw_text: str) -> list:
    """
    Parse pasted task list.
    Format: "title - status - tags:a,b,c"
    Separator: "-" or "—"
    """
    items = []
    for line in raw_text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue

        parts = re.split(r"\s*[-—]\s*", line)
        title = parts[0].strip()
        if not title:
            continue

        status = "not_started"
        tags = []
        type_ = "misc"

        for part in parts[1:]:
            p = part.strip().lower()
            if p in ("not_started", "in_progress", "completed", "dropped"):
                status = p
            elif p.startswith("tags:"):
                tags = [t.strip() for t in p.replace("tags:", "").split(",") if t.strip()]
            elif p.startswith("type:"):
                type_ = p.replace("type:", "").strip()

        # Auto-detect type from keywords
        if not type_ or type_ == "misc":
            lower_title = title.lower()
            if any(kw in lower_title for kw in ["exam", "考试", "测验"]):
                type_ = "exam"
            elif any(kw in lower_title for kw in ["chapter", "章", "节", "单元"]):
                type_ = "chapter"
            elif any(kw in lower_title for kw in ["video", "视频"]):
                type_ = "video"
            elif any(kw in lower_title for kw in ["course", "课程"]):
                type_ = "course"

        items.append({
            "title": title,
            "type": type_,
            "status": status,
            "tags": tags,
            "difficulty_self_rating": None,
        })

    return items


# ── Phase Review ──

def get_phase_summary(phase_id: int) -> dict:
    tasks = db.list_tasks(phase_id)
    total = len(tasks)
    completed = sum(1 for t in tasks if t["status"] == "completed")
    in_progress = sum(1 for t in tasks if t["status"] == "in_progress")
    not_started = sum(1 for t in tasks if t["status"] == "not_started")
    return {
        "total": total,
        "completed": completed,
        "in_progress": in_progress,
        "not_started": not_started,
        "tasks": tasks,
    }
