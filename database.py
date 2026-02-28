"""
Database layer — SQLite schema + CRUD.
All tables per spec §7.
"""

import sqlite3
import os
import json
from datetime import datetime, date
from typing import Optional

DB_PATH = os.environ.get("ECOS_DB_PATH", "ecos.db")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        timezone TEXT DEFAULT 'Asia/Shanghai',
        default_step_minutes INTEGER DEFAULT 8,
        tone TEXT DEFAULT 'firm_kind',
        low_energy_mode INTEGER DEFAULT 0,
        weekly_summary_enabled INTEGER DEFAULT 0,
        streak_days INTEGER DEFAULT 0,
        last_progress_date TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS goals (
        goal_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        deadline_date TEXT,
        track TEXT,
        is_active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (user_id) REFERENCES users(user_id)
    );

    CREATE TABLE IF NOT EXISTS phases (
        phase_id INTEGER PRIMARY KEY AUTOINCREMENT,
        goal_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        start_date TEXT,
        deadline_date TEXT,
        is_active INTEGER DEFAULT 0,
        focus_tags TEXT DEFAULT '[]',
        max_daily_mainline INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (goal_id) REFERENCES goals(goal_id)
    );

    CREATE TABLE IF NOT EXISTS task_items (
        task_id INTEGER PRIMARY KEY AUTOINCREMENT,
        phase_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        type TEXT DEFAULT 'misc',
        status TEXT DEFAULT 'not_started',
        tags TEXT DEFAULT '[]',
        difficulty_self_rating INTEGER,
        source TEXT DEFAULT 'manual',
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (phase_id) REFERENCES phases(phase_id)
    );

    CREATE TABLE IF NOT EXISTS mainlines (
        mainline_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        goal_id INTEGER,
        phase_id INTEGER,
        date TEXT NOT NULL,
        title TEXT NOT NULL,
        source TEXT DEFAULT 'manual',
        locked INTEGER DEFAULT 1,
        time_budget_default INTEGER DEFAULT 60,
        energy_label_default TEXT DEFAULT '中',
        task_id_ref INTEGER,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (user_id) REFERENCES users(user_id)
    );

    CREATE TABLE IF NOT EXISTS steps (
        step_id INTEGER PRIMARY KEY AUTOINCREMENT,
        mainline_id INTEGER NOT NULL,
        kind TEXT DEFAULT 'micro',
        version INTEGER DEFAULT 1,
        duration_min INTEGER DEFAULT 2,
        instruction TEXT NOT NULL,
        acceptance_criteria TEXT NOT NULL,
        difficulty INTEGER DEFAULT 1,
        status TEXT DEFAULT 'ready',
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (mainline_id) REFERENCES mainlines(mainline_id)
    );

    CREATE TABLE IF NOT EXISTS deferred_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        deferred_step_id INTEGER NOT NULL,
        mainline_id INTEGER,
        reason TEXT DEFAULT 'exit',
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (user_id) REFERENCES users(user_id),
        FOREIGN KEY (deferred_step_id) REFERENCES steps(step_id)
    );

    CREATE TABLE IF NOT EXISTS stuck_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        step_id INTEGER NOT NULL,
        stuck_type TEXT NOT NULL,
        emotion_label TEXT,
        user_note TEXT,
        timestamp TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (step_id) REFERENCES steps(step_id)
    );

    CREATE TABLE IF NOT EXISTS evidence (
        evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        counter_evidence TEXT NOT NULL,
        tags TEXT DEFAULT '["small_win"]',
        timestamp TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (user_id) REFERENCES users(user_id)
    );

    CREATE TABLE IF NOT EXISTS if_then_plans (
        plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        date TEXT NOT NULL,
        if_trigger TEXT NOT NULL,
        then_action TEXT NOT NULL,
        reward TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (user_id) REFERENCES users(user_id)
    );

    CREATE TABLE IF NOT EXISTS import_drafts (
        import_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        phase_id INTEGER NOT NULL,
        source TEXT DEFAULT 'paste',
        raw_text TEXT,
        parsed_items TEXT DEFAULT '[]',
        state TEXT DEFAULT 'draft',
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (user_id) REFERENCES users(user_id),
        FOREIGN KEY (phase_id) REFERENCES phases(phase_id)
    );
    """)
    conn.commit()
    conn.close()


# ── User ──

def ensure_user(user_id: int) -> dict:
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        conn.execute("INSERT INTO users (user_id) VALUES (?)", (user_id,))
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return dict(row)


def get_user(user_id: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_user(user_id: int, **kwargs):
    conn = get_conn()
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [user_id]
    conn.execute(f"UPDATE users SET {sets} WHERE user_id=?", vals)
    conn.commit()
    conn.close()


def update_streak(user_id: int):
    user = get_user(user_id)
    today = date.today().isoformat()
    if user["last_progress_date"] != today:
        new_streak = user["streak_days"] + 1
        update_user(user_id, streak_days=new_streak, last_progress_date=today)
        return new_streak
    return user["streak_days"]


# ── Goal ──

def create_goal(user_id: int, title: str, deadline_date=None, track=None) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO goals (user_id, title, deadline_date, track) VALUES (?,?,?,?)",
        (user_id, title, deadline_date, track))
    gid = cur.lastrowid
    conn.commit()
    conn.close()
    return gid


def list_goals(user_id: int) -> list:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM goals WHERE user_id=? AND is_active=1 ORDER BY created_at DESC", (user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_active_goal(user_id: int) -> Optional[dict]:
    goals = list_goals(user_id)
    return goals[0] if goals else None


# ── Phase ──

def create_phase(goal_id: int, title: str, is_active: int = 1) -> int:
    conn = get_conn()
    if is_active:
        conn.execute("UPDATE phases SET is_active=0 WHERE goal_id=?", (goal_id,))
    cur = conn.execute(
        "INSERT INTO phases (goal_id, title, is_active) VALUES (?,?,?)",
        (goal_id, title, is_active))
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    return pid


def list_phases(goal_id: int) -> list:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM phases WHERE goal_id=? ORDER BY created_at", (goal_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_active_phase(goal_id: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM phases WHERE goal_id=? AND is_active=1", (goal_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def set_active_phase(goal_id: int, phase_id: int):
    conn = get_conn()
    conn.execute("UPDATE phases SET is_active=0 WHERE goal_id=?", (goal_id,))
    conn.execute("UPDATE phases SET is_active=1 WHERE phase_id=?", (phase_id,))
    conn.commit()
    conn.close()


# ── TaskItem ──

def create_task(phase_id: int, title: str, type_="misc", status="not_started", tags=None, difficulty=None, source="manual") -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO task_items (phase_id, title, type, status, tags, difficulty_self_rating, source) VALUES (?,?,?,?,?,?,?)",
        (phase_id, title, type_, status, json.dumps(tags or []), difficulty, source))
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


def list_tasks(phase_id: int, status_filter=None) -> list:
    conn = get_conn()
    q = "SELECT * FROM task_items WHERE phase_id=?"
    params = [phase_id]
    if status_filter:
        q += " AND status=?"
        params.append(status_filter)
    q += " ORDER BY created_at"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_task(task_id: int, **kwargs):
    conn = get_conn()
    kwargs["updated_at"] = datetime.now().isoformat()
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [task_id]
    conn.execute(f"UPDATE task_items SET {sets} WHERE task_id=?", vals)
    conn.commit()
    conn.close()


def delete_task(task_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM task_items WHERE task_id=?", (task_id,))
    conn.commit()
    conn.close()


# ── Mainline ──

def create_mainline(user_id: int, title: str, source="manual", goal_id=None, phase_id=None, task_id_ref=None) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO mainlines (user_id, goal_id, phase_id, date, title, source, task_id_ref) VALUES (?,?,?,?,?,?,?)",
        (user_id, goal_id, phase_id, date.today().isoformat(), title, source, task_id_ref))
    mid = cur.lastrowid
    conn.commit()
    conn.close()
    return mid


def get_today_mainline(user_id: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM mainlines WHERE user_id=? AND date=? ORDER BY created_at DESC LIMIT 1",
        (user_id, date.today().isoformat())).fetchone()
    conn.close()
    return dict(row) if row else None


# ── Step ──

def create_step(mainline_id: int, kind: str, duration_min: int, instruction: str, acceptance_criteria: str, difficulty: int = 1) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO steps (mainline_id, kind, duration_min, instruction, acceptance_criteria, difficulty) VALUES (?,?,?,?,?,?)",
        (mainline_id, kind, duration_min, instruction, acceptance_criteria, difficulty))
    sid = cur.lastrowid
    conn.commit()
    conn.close()
    return sid


def get_step(step_id: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM steps WHERE step_id=?", (step_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_step(step_id: int, **kwargs):
    conn = get_conn()
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [step_id]
    conn.execute(f"UPDATE steps SET {sets} WHERE step_id=?", vals)
    conn.commit()
    conn.close()


def get_active_step(mainline_id: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM steps WHERE mainline_id=? AND status IN ('ready','executing') ORDER BY created_at DESC LIMIT 1",
        (mainline_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ── Deferred ──

def create_deferred(user_id: int, step_id: int, mainline_id: int, reason: str = "exit"):
    conn = get_conn()
    conn.execute(
        "INSERT INTO deferred_links (user_id, deferred_step_id, mainline_id, reason) VALUES (?,?,?,?)",
        (user_id, step_id, mainline_id, reason))
    conn.commit()
    conn.close()


def get_deferred(user_id: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        "SELECT dl.*, s.instruction, s.acceptance_criteria, s.duration_min, s.mainline_id, m.title as mainline_title "
        "FROM deferred_links dl "
        "JOIN steps s ON dl.deferred_step_id = s.step_id "
        "JOIN mainlines m ON dl.mainline_id = m.mainline_id "
        "WHERE dl.user_id=? ORDER BY dl.created_at DESC LIMIT 1",
        (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def clear_deferred(user_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM deferred_links WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


# ── StuckEvent ──

def create_stuck_event(step_id: int, stuck_type: str, emotion_label: str = None, user_note: str = None):
    conn = get_conn()
    conn.execute(
        "INSERT INTO stuck_events (step_id, stuck_type, emotion_label, user_note) VALUES (?,?,?,?)",
        (step_id, stuck_type, emotion_label, user_note))
    conn.commit()
    conn.close()


# ── Evidence ──

def create_evidence(user_id: int, text: str, tags=None) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO evidence (user_id, counter_evidence, tags) VALUES (?,?,?)",
        (user_id, text, json.dumps(tags or ["small_win"])))
    eid = cur.lastrowid
    conn.commit()
    conn.close()
    return eid


def list_evidence(user_id: int, limit: int = 10) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM evidence WHERE user_id=? ORDER BY timestamp DESC LIMIT ?",
        (user_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── IfThenPlan ──

def save_if_then(user_id: int, if_trigger: str, then_action: str, reward: str = None):
    conn = get_conn()
    conn.execute(
        "INSERT INTO if_then_plans (user_id, date, if_trigger, then_action, reward) VALUES (?,?,?,?,?)",
        (user_id, date.today().isoformat(), if_trigger, then_action, reward))
    conn.commit()
    conn.close()


# ── ImportDraft ──

def create_import_draft(user_id: int, phase_id: int, raw_text: str, parsed_items: list, source: str = "paste") -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO import_drafts (user_id, phase_id, source, raw_text, parsed_items) VALUES (?,?,?,?,?)",
        (user_id, phase_id, source, raw_text, json.dumps(parsed_items, ensure_ascii=False)))
    iid = cur.lastrowid
    conn.commit()
    conn.close()
    return iid


def get_import_draft(import_id: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM import_drafts WHERE import_id=?", (import_id,)).fetchone()
    conn.close()
    if row:
        d = dict(row)
        d["parsed_items"] = json.loads(d["parsed_items"])
        return d
    return None


def confirm_import(import_id: int):
    draft = get_import_draft(import_id)
    if not draft:
        return
    for item in draft["parsed_items"]:
        create_task(
            phase_id=draft["phase_id"],
            title=item.get("title", ""),
            type_=item.get("type", "misc"),
            status=item.get("status", "not_started"),
            tags=item.get("tags", []),
            difficulty=item.get("difficulty_self_rating"),
            source=draft["source"])
    conn = get_conn()
    conn.execute("UPDATE import_drafts SET state='confirmed' WHERE import_id=?", (import_id,))
    conn.commit()
    conn.close()


def discard_import(import_id: int):
    conn = get_conn()
    conn.execute("UPDATE import_drafts SET state='discarded' WHERE import_id=?", (import_id,))
    conn.commit()
    conn.close()
