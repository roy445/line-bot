import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    inspect,
    insert,
    select,
    text,
    update,
)


def database_url():
    url = os.getenv("DATABASE_URL")
    if url:
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+psycopg2://", 1)
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
        return url
    return "sqlite:///" + str(Path(__file__).with_name("bot.db"))


engine = create_engine(
    database_url(),
    future=True,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if database_url().startswith("sqlite") else {},
)
metadata = MetaData()


chat_modes = Table(
    "chat_modes",
    metadata,
    Column("source_id", String, primary_key=True),
    Column("enabled", Integer, nullable=False, default=0),
    Column("updated_at", String, nullable=False),
)

memories = Table(
    "memories",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("source_id", String, nullable=False),
    Column("kind", String, nullable=False),
    Column("content", Text, nullable=False),
    Column("created_at", String, nullable=False),
)

messages = Table(
    "messages",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("source_id", String, nullable=False),
    Column("role", String, nullable=False),
    Column("content", Text, nullable=False),
    Column("created_at", String, nullable=False),
)

pending_reminders = Table(
    "pending_reminders",
    metadata,
    Column("source_id", String, primary_key=True),
    Column("target_id", String),
    Column("title", Text, nullable=False),
    Column("remind_at", String, nullable=False),
    Column("recurrence", String),
    Column("category", String),
    Column("raw_text", Text, nullable=False),
    Column("created_at", String, nullable=False),
)

reminder_drafts = Table(
    "reminder_drafts",
    metadata,
    Column("source_id", String, primary_key=True),
    Column("target_id", String, nullable=False),
    Column("title", Text, nullable=False),
    Column("recurrence", String),
    Column("raw_text", Text, nullable=False),
    Column("created_at", String, nullable=False),
)

reminders = Table(
    "reminders",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("source_id", String, nullable=False),
    Column("target_id", String),
    Column("title", Text, nullable=False),
    Column("remind_at", String, nullable=False),
    Column("recurrence", String),
    Column("category", String),
    Column("status", String, nullable=False, default="pending"),
    Column("created_at", String, nullable=False),
    Column("sent_at", String),
)

repeat_questions = Table(
    "repeat_questions",
    metadata,
    Column("source_id", String, primary_key=True),
    Column("reminder_id", Integer, nullable=False),
    Column("created_at", String, nullable=False),
)

tasks = Table(
    "tasks",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("source_id", String, nullable=False),
    Column("title", Text, nullable=False),
    Column("category", String),
    Column("status", String, nullable=False, default="open"),
    Column("created_at", String, nullable=False),
    Column("completed_at", String),
)

user_settings = Table(
    "user_settings",
    metadata,
    Column("source_id", String, primary_key=True),
    Column("city", String),
    Column("tone", Text),
    Column("daily_summary_time", String),
    Column("updated_at", String, nullable=False),
)

daily_summary_logs = Table(
    "daily_summary_logs",
    metadata,
    Column("source_id", String, primary_key=True),
    Column("summary_date", String, primary_key=True),
    Column("sent_at", String, nullable=False),
)


def init_db():
    metadata.create_all(engine)
    ensure_existing_columns()


def ensure_existing_columns():
    expected = {
        "pending_reminders": {
            "target_id": "TEXT",
            "recurrence": "TEXT",
            "category": "TEXT",
        },
        "reminders": {
            "target_id": "TEXT",
            "recurrence": "TEXT",
            "category": "TEXT",
        },
        "user_settings": {
            "city": "TEXT",
            "tone": "TEXT",
            "daily_summary_time": "TEXT",
        },
    }
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table_name, columns in expected.items():
            if not inspector.has_table(table_name):
                continue
            existing = {col["name"] for col in inspector.get_columns(table_name)}
            for column, definition in columns.items():
                if column not in existing:
                    conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column} {definition}"))


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def row_to_dict(row):
    return dict(row._mapping) if row else None


def rows_to_dicts(rows):
    return [dict(row._mapping) for row in rows]


def set_chat_mode(source_id, enabled):
    with engine.begin() as conn:
        result = conn.execute(
            update(chat_modes)
            .where(chat_modes.c.source_id == source_id)
            .values(enabled=1 if enabled else 0, updated_at=now_iso())
        )
        if result.rowcount == 0:
            conn.execute(
                insert(chat_modes).values(
                    source_id=source_id,
                    enabled=1 if enabled else 0,
                    updated_at=now_iso(),
                )
            )


def is_chat_mode(source_id):
    with engine.begin() as conn:
        row = conn.execute(
            select(chat_modes.c.enabled).where(chat_modes.c.source_id == source_id)
        ).fetchone()
    return bool(row and row.enabled)


def add_message(source_id, role, content):
    with engine.begin() as conn:
        conn.execute(
            insert(messages).values(
                source_id=source_id,
                role=role,
                content=content,
                created_at=now_iso(),
            )
        )


def recent_messages(source_id, limit=12):
    with engine.begin() as conn:
        rows = conn.execute(
            select(messages.c.role, messages.c.content)
            .where(messages.c.source_id == source_id)
            .order_by(messages.c.id.desc())
            .limit(limit)
        ).fetchall()
    return list(reversed(rows_to_dicts(rows)))


def add_memory(source_id, kind, content):
    content = content.strip()
    if not content:
        return
    with engine.begin() as conn:
        conn.execute(
            insert(memories).values(
                source_id=source_id,
                kind=kind,
                content=content,
                created_at=now_iso(),
            )
        )


def get_memories(source_id, limit=8):
    with engine.begin() as conn:
        rows = conn.execute(
            select(memories.c.id, memories.c.kind, memories.c.content)
            .where(memories.c.source_id == source_id)
            .order_by(memories.c.id.desc())
            .limit(limit)
        ).fetchall()
    return rows_to_dicts(rows)


def delete_memories_like(source_id, keyword):
    keyword = keyword.strip()
    if not keyword:
        return 0
    with engine.begin() as conn:
        result = conn.execute(
            delete(memories).where(
                memories.c.source_id == source_id,
                memories.c.content.like(f"%{keyword}%"),
            )
        )
    return result.rowcount


def clear_memories(source_id):
    with engine.begin() as conn:
        result = conn.execute(delete(memories).where(memories.c.source_id == source_id))
    return result.rowcount


def save_pending_reminder(source_id, target_id, title, remind_at, raw_text, recurrence=None, category=None):
    values = {
        "target_id": target_id,
        "title": title,
        "remind_at": remind_at,
        "recurrence": recurrence,
        "category": category,
        "raw_text": raw_text,
        "created_at": now_iso(),
    }
    with engine.begin() as conn:
        result = conn.execute(
            update(pending_reminders)
            .where(pending_reminders.c.source_id == source_id)
            .values(**values)
        )
        if result.rowcount == 0:
            conn.execute(insert(pending_reminders).values(source_id=source_id, **values))


def get_pending_reminder(source_id):
    with engine.begin() as conn:
        row = conn.execute(
            select(pending_reminders).where(pending_reminders.c.source_id == source_id)
        ).fetchone()
    return row_to_dict(row)


def clear_pending_reminder(source_id):
    with engine.begin() as conn:
        conn.execute(delete(pending_reminders).where(pending_reminders.c.source_id == source_id))


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
        pending.get("category"),
    )
    clear_pending_reminder(source_id)
    return pending


def save_reminder_draft(source_id, target_id, title, raw_text, recurrence=None):
    values = {
        "target_id": target_id,
        "title": title,
        "recurrence": recurrence,
        "raw_text": raw_text,
        "created_at": now_iso(),
    }
    with engine.begin() as conn:
        result = conn.execute(
            update(reminder_drafts)
            .where(reminder_drafts.c.source_id == source_id)
            .values(**values)
        )
        if result.rowcount == 0:
            conn.execute(insert(reminder_drafts).values(source_id=source_id, **values))


def get_reminder_draft(source_id):
    with engine.begin() as conn:
        row = conn.execute(
            select(reminder_drafts).where(reminder_drafts.c.source_id == source_id)
        ).fetchone()
    return row_to_dict(row)


def clear_reminder_draft(source_id):
    with engine.begin() as conn:
        conn.execute(delete(reminder_drafts).where(reminder_drafts.c.source_id == source_id))


def add_reminder(source_id, target_id, title, remind_at, recurrence=None, category=None):
    with engine.begin() as conn:
        result = conn.execute(
            insert(reminders).values(
                source_id=source_id,
                target_id=target_id,
                title=title,
                remind_at=remind_at,
                recurrence=recurrence,
                category=category,
                status="pending",
                created_at=now_iso(),
            )
        )
        return result.inserted_primary_key[0]


def save_repeat_question(source_id, reminder_id):
    values = {"reminder_id": reminder_id, "created_at": now_iso()}
    with engine.begin() as conn:
        result = conn.execute(
            update(repeat_questions)
            .where(repeat_questions.c.source_id == source_id)
            .values(**values)
        )
        if result.rowcount == 0:
            conn.execute(insert(repeat_questions).values(source_id=source_id, **values))


def get_repeat_question(source_id):
    with engine.begin() as conn:
        row = conn.execute(
            select(repeat_questions).where(repeat_questions.c.source_id == source_id)
        ).fetchone()
    return row_to_dict(row)


def clear_repeat_question(source_id):
    with engine.begin() as conn:
        conn.execute(delete(repeat_questions).where(repeat_questions.c.source_id == source_id))


def set_reminder_recurrence(source_id, reminder_id, recurrence):
    with engine.begin() as conn:
        result = conn.execute(
            update(reminders)
            .where(
                reminders.c.source_id == source_id,
                reminders.c.id == reminder_id,
                reminders.c.status == "pending",
            )
            .values(recurrence=recurrence)
        )
    return result.rowcount > 0


def list_pending_reminders(source_id, limit=30):
    with engine.begin() as conn:
        rows = conn.execute(
            select(
                reminders.c.id,
                reminders.c.title,
                reminders.c.remind_at,
                reminders.c.recurrence,
                reminders.c.category,
            )
            .where(reminders.c.source_id == source_id, reminders.c.status == "pending")
            .order_by(reminders.c.remind_at.asc())
            .limit(limit)
        ).fetchall()
    return rows_to_dicts(rows)


def delete_pending_reminders(source_id, reminder_ids):
    reminder_ids = sorted({int(reminder_id) for reminder_id in reminder_ids})
    if not reminder_ids:
        return 0
    with engine.begin() as conn:
        result = conn.execute(
            update(reminders)
            .where(
                reminders.c.source_id == source_id,
                reminders.c.status == "pending",
                reminders.c.id.in_(reminder_ids),
            )
            .values(status="cancelled")
        )
    return result.rowcount


def delete_pending_reminder_range(source_id, start_id, end_id):
    start_id = int(start_id)
    end_id = int(end_id)
    if start_id > end_id:
        start_id, end_id = end_id, start_id
    with engine.begin() as conn:
        result = conn.execute(
            update(reminders)
            .where(
                reminders.c.source_id == source_id,
                reminders.c.status == "pending",
                reminders.c.id.between(start_id, end_id),
            )
            .values(status="cancelled")
        )
    return result.rowcount


def clear_pending_reminders(source_id):
    with engine.begin() as conn:
        result = conn.execute(
            update(reminders)
            .where(reminders.c.source_id == source_id, reminders.c.status == "pending")
            .values(status="cancelled")
        )
    return result.rowcount


def complete_interval_reminders(source_id):
    with engine.begin() as conn:
        result = conn.execute(
            update(reminders)
            .where(
                reminders.c.source_id == source_id,
                reminders.c.status == "pending",
                reminders.c.recurrence.like("interval:%"),
            )
            .values(status="completed", sent_at=now_iso())
        )
    return result.rowcount


def due_reminders():
    with engine.begin() as conn:
        rows = conn.execute(
            select(
                reminders.c.id,
                reminders.c.source_id,
                reminders.c.target_id,
                reminders.c.title,
                reminders.c.remind_at,
                reminders.c.recurrence,
                reminders.c.category,
            )
            .where(reminders.c.status == "pending")
            .order_by(reminders.c.remind_at.asc())
        ).fetchall()
    now = datetime.now().astimezone()
    due = []
    for reminder in rows_to_dicts(rows):
        remind_at = parse_dt(reminder["remind_at"])
        if remind_at and remind_at <= now:
            due.append(reminder)
    return due


def mark_reminder_done(reminder_id, remind_at, recurrence=None):
    next_time = next_occurrence(remind_at, recurrence)
    with engine.begin() as conn:
        if next_time:
            conn.execute(
                update(reminders)
                .where(reminders.c.id == reminder_id)
                .values(remind_at=next_time.isoformat(timespec="seconds"), sent_at=now_iso())
            )
        else:
            conn.execute(
                update(reminders)
                .where(reminders.c.id == reminder_id)
                .values(status="sent", sent_at=now_iso())
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
    if recurrence.startswith("interval:"):
        minutes = int(recurrence.split(":", 1)[1])
        while next_time <= now:
            next_time += timedelta(minutes=minutes)
        return next_time
    return None


def add_month(dt, day):
    year = dt.year + (dt.month // 12)
    month = (dt.month % 12) + 1
    return dt.replace(year=year, month=month, day=min(day, days_in_month(year, month)))


def days_in_month(year, month):
    if month == 12:
        next_month = datetime(year + 1, 1, 1)
    else:
        next_month = datetime(year, month + 1, 1)
    return (next_month - datetime(year, month, 1)).days


def add_task(source_id, title, category=None):
    with engine.begin() as conn:
        result = conn.execute(
            insert(tasks).values(
                source_id=source_id,
                title=title,
                category=category,
                status="open",
                created_at=now_iso(),
            )
        )
        return result.inserted_primary_key[0]


def list_tasks(source_id, status="open", limit=30):
    with engine.begin() as conn:
        rows = conn.execute(
            select(tasks.c.id, tasks.c.title, tasks.c.category)
            .where(tasks.c.source_id == source_id, tasks.c.status == status)
            .order_by(tasks.c.id.asc())
            .limit(limit)
        ).fetchall()
    return rows_to_dicts(rows)


def complete_task(source_id, task_id):
    with engine.begin() as conn:
        result = conn.execute(
            update(tasks)
            .where(tasks.c.source_id == source_id, tasks.c.id == task_id, tasks.c.status == "open")
            .values(status="done", completed_at=now_iso())
        )
    return result.rowcount > 0


def set_user_setting(source_id, key, value):
    if key not in {"city", "tone", "daily_summary_time"}:
        return
    with engine.begin() as conn:
        row = conn.execute(
            select(user_settings.c.source_id).where(user_settings.c.source_id == source_id)
        ).fetchone()
        if not row:
            conn.execute(insert(user_settings).values(source_id=source_id, updated_at=now_iso()))
        conn.execute(
            update(user_settings)
            .where(user_settings.c.source_id == source_id)
            .values(**{key: value, "updated_at": now_iso()})
        )


def get_user_settings(source_id):
    with engine.begin() as conn:
        row = conn.execute(
            select(user_settings).where(user_settings.c.source_id == source_id)
        ).fetchone()
    return row_to_dict(row) or {}


def summary_already_sent(source_id, summary_date):
    with engine.begin() as conn:
        row = conn.execute(
            select(daily_summary_logs.c.source_id).where(
                daily_summary_logs.c.source_id == source_id,
                daily_summary_logs.c.summary_date == summary_date,
            )
        ).fetchone()
    return bool(row)


def mark_summary_sent(source_id, summary_date):
    if summary_already_sent(source_id, summary_date):
        return
    with engine.begin() as conn:
        conn.execute(
            insert(daily_summary_logs).values(
                source_id=source_id,
                summary_date=summary_date,
                sent_at=now_iso(),
            )
        )


def summary_targets():
    with engine.begin() as conn:
        rows = conn.execute(
            select(user_settings.c.source_id, user_settings.c.daily_summary_time).where(
                user_settings.c.daily_summary_time.is_not(None)
            )
        ).fetchall()
    return rows_to_dicts(rows)


def dumps(data):
    return json.dumps(data, ensure_ascii=False)
