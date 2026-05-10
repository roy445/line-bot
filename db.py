import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


DB_PATH = Path(__file__).with_name("bot.db")


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chat_modes (
                source_id TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pending_reminders (
                source_id TEXT PRIMARY KEY,
                target_id TEXT,
                title TEXT NOT NULL,
                remind_at TEXT NOT NULL,
                recurrence TEXT,
                raw_text TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reminder_drafts (
                source_id TEXT PRIMARY KEY,
                target_id TEXT NOT NULL,
                title TEXT NOT NULL,
                recurrence TEXT,
                raw_text TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT NOT NULL,
                target_id TEXT,
                title TEXT NOT NULL,
                remind_at TEXT NOT NULL,
                recurrence TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                sent_at TEXT
            );
            """
        )
        ensure_column(conn, "pending_reminders", "target_id", "TEXT")
        ensure_column(conn, "pending_reminders", "recurrence", "TEXT")
        ensure_column(conn, "reminders", "target_id", "TEXT")
        ensure_column(conn, "reminders", "recurrence", "TEXT")
        conn.execute("UPDATE reminders SET target_id = source_id WHERE target_id IS NULL")


def ensure_column(conn, table, column, definition):
    columns = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def set_chat_mode(source_id, enabled):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO chat_modes(source_id, enabled, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                enabled = excluded.enabled,
                updated_at = excluded.updated_at
            """,
            (source_id, 1 if enabled else 0, now_iso()),
        )


def is_chat_mode(source_id):
    with connect() as conn:
        row = conn.execute(
            "SELECT enabled FROM chat_modes WHERE source_id = ?",
            (source_id,),
        ).fetchone()
    return bool(row and row["enabled"])


def add_message(source_id, role, content):
    with connect() as conn:
        conn.execute(
            "INSERT INTO messages(source_id, role, content, created_at) VALUES(?, ?, ?, ?)",
            (source_id, role, content, now_iso()),
        )


def recent_messages(source_id, limit=12):
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT role, content FROM messages
            WHERE source_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (source_id, limit),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def add_memory(source_id, kind, content):
    content = content.strip()
    if not content:
        return
    with connect() as conn:
        conn.execute(
            "INSERT INTO memories(source_id, kind, content, created_at) VALUES(?, ?, ?, ?)",
            (source_id, kind, content, now_iso()),
        )


def get_memories(source_id, limit=8):
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, kind, content FROM memories
            WHERE source_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (source_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def delete_memories_like(source_id, keyword):
    keyword = keyword.strip()
    if not keyword:
        return 0
    with connect() as conn:
        cursor = conn.execute(
            "DELETE FROM memories WHERE source_id = ? AND content LIKE ?",
            (source_id, f"%{keyword}%"),
        )
    return cursor.rowcount


def clear_memories(source_id):
    with connect() as conn:
        cursor = conn.execute("DELETE FROM memories WHERE source_id = ?", (source_id,))
    return cursor.rowcount


def save_pending_reminder(source_id, target_id, title, remind_at, raw_text, recurrence=None):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO pending_reminders(source_id, target_id, title, remind_at, recurrence, raw_text, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                target_id = excluded.target_id,
                title = excluded.title,
                remind_at = excluded.remind_at,
                recurrence = excluded.recurrence,
                raw_text = excluded.raw_text,
                created_at = excluded.created_at
            """,
            (source_id, target_id, title, remind_at, recurrence, raw_text, now_iso()),
        )


def get_pending_reminder(source_id):
    with connect() as conn:
        row = conn.execute(
            """
            SELECT title, remind_at, raw_text, target_id, recurrence
            FROM pending_reminders
            WHERE source_id = ?
            """,
            (source_id,),
        ).fetchone()
    return dict(row) if row else None


def clear_pending_reminder(source_id):
    with connect() as conn:
        conn.execute("DELETE FROM pending_reminders WHERE source_id = ?", (source_id,))


def confirm_pending_reminder(source_id):
    pending = get_pending_reminder(source_id)
    if not pending:
        return None

    add_reminder(
        source_id,
        pending["target_id"] or source_id,
        pending["title"],
        pending["remind_at"],
        pending.get("recurrence"),
    )
    clear_pending_reminder(source_id)
    return pending


def save_reminder_draft(source_id, target_id, title, raw_text, recurrence=None):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO reminder_drafts(source_id, target_id, title, recurrence, raw_text, created_at)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                target_id = excluded.target_id,
                title = excluded.title,
                recurrence = excluded.recurrence,
                raw_text = excluded.raw_text,
                created_at = excluded.created_at
            """,
            (source_id, target_id, title, recurrence, raw_text, now_iso()),
        )


def get_reminder_draft(source_id):
    with connect() as conn:
        row = conn.execute(
            "SELECT target_id, title, recurrence, raw_text FROM reminder_drafts WHERE source_id = ?",
            (source_id,),
        ).fetchone()
    return dict(row) if row else None


def clear_reminder_draft(source_id):
    with connect() as conn:
        conn.execute("DELETE FROM reminder_drafts WHERE source_id = ?", (source_id,))


def add_reminder(source_id, target_id, title, remind_at, recurrence=None):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO reminders(source_id, target_id, title, remind_at, recurrence, created_at)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (source_id, target_id, title, remind_at, recurrence, now_iso()),
        )


def list_pending_reminders(source_id, limit=30):
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, title, remind_at, recurrence FROM reminders
            WHERE source_id = ? AND status = 'pending'
            ORDER BY remind_at ASC
            LIMIT ?
            """,
            (source_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def delete_pending_reminders(source_id, reminder_ids):
    reminder_ids = sorted({int(reminder_id) for reminder_id in reminder_ids})
    if not reminder_ids:
        return 0

    placeholders = ",".join("?" for _ in reminder_ids)
    with connect() as conn:
        cursor = conn.execute(
            f"""
            UPDATE reminders
            SET status = 'cancelled'
            WHERE source_id = ?
              AND status = 'pending'
              AND id IN ({placeholders})
            """,
            [source_id, *reminder_ids],
        )
    return cursor.rowcount


def delete_pending_reminder_range(source_id, start_id, end_id):
    start_id = int(start_id)
    end_id = int(end_id)
    if start_id > end_id:
        start_id, end_id = end_id, start_id

    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE reminders
            SET status = 'cancelled'
            WHERE source_id = ?
              AND status = 'pending'
              AND id BETWEEN ? AND ?
            """,
            (source_id, start_id, end_id),
        )
    return cursor.rowcount


def clear_pending_reminders(source_id):
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE reminders
            SET status = 'cancelled'
            WHERE source_id = ? AND status = 'pending'
            """,
            (source_id,),
        )
    return cursor.rowcount


def due_reminders():
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, source_id, target_id, title, remind_at, recurrence FROM reminders
            WHERE status = 'pending'
            ORDER BY remind_at ASC
            """
        ).fetchall()

    now = datetime.now().astimezone()
    due = []
    for row in rows:
        reminder = dict(row)
        remind_at = parse_dt(reminder["remind_at"])
        if remind_at and remind_at <= now:
            due.append(reminder)
    return due


def mark_reminder_done(reminder_id, remind_at, recurrence=None):
    next_time = next_occurrence(remind_at, recurrence)
    with connect() as conn:
        if next_time:
            conn.execute(
                "UPDATE reminders SET remind_at = ?, sent_at = ? WHERE id = ?",
                (next_time.isoformat(timespec="seconds"), now_iso(), reminder_id),
            )
        else:
            conn.execute(
                "UPDATE reminders SET status = 'sent', sent_at = ? WHERE id = ?",
                (now_iso(), reminder_id),
            )


def parse_dt(value):
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        return dt.astimezone()
    return dt


def next_occurrence(remind_at, recurrence):
    if not recurrence:
        return None

    current = parse_dt(remind_at)
    if not current:
        return None

    now = datetime.now(current.tzinfo).replace(microsecond=0)
    next_time = current

    if recurrence == "daily":
        while next_time <= now:
            next_time += timedelta(days=1)
        return next_time

    if recurrence.startswith("weekly:"):
        while next_time <= now:
            next_time += timedelta(days=7)
        return next_time

    if recurrence.startswith("monthly:"):
        day = int(recurrence.split(":", 1)[1])
        next_time = add_month(current, day)
        while next_time <= now:
            next_time = add_month(next_time, day)
        return next_time

    return None


def add_month(dt, day):
    year = dt.year + (dt.month // 12)
    month = (dt.month % 12) + 1
    max_day = days_in_month(year, month)
    return dt.replace(year=year, month=month, day=min(day, max_day))


def days_in_month(year, month):
    if month == 12:
        next_month = datetime(year + 1, 1, 1)
    else:
        next_month = datetime(year, month + 1, 1)
    this_month = datetime(year, month, 1)
    return (next_month - this_month).days


def dumps(data):
    return json.dumps(data, ensure_ascii=False)
