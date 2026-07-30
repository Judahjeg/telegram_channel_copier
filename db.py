from __future__ import annotations

import sqlite3
import datetime
import os
from contextlib import contextmanager
from typing import Optional, List, Dict, Any

DB_FILE = os.getenv("DB_PATH", "copier.db")
DATABASE_URL = os.getenv("DATABASE_URL", "")
USE_POSTGRES = bool(DATABASE_URL)

if USE_POSTGRES:
    import psycopg2

# Postgres needs SERIAL for autoincrementing PKs; SQLite needs its own syntax.
# Everything else in this schema (TEXT/INTEGER/REAL columns, ON CONFLICT upserts) is
# portable between the two as written.
_AUTOINCREMENT_PK = "SERIAL PRIMARY KEY" if USE_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"


@contextmanager
def get_conn(db_path: str = DB_FILE):
    """Postgres (DATABASE_URL) in production so data survives redeploys on hosts without a
    persistent disk (e.g. Render's free tier); plain SQLite otherwise - used by local dev and
    by the test suite, which never sets DATABASE_URL and so is unaffected by this at all."""
    if USE_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL)
    else:
        conn = sqlite3.connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def _q(query: str) -> str:
    """Translate SQLite-style '?' placeholders to psycopg2-style '%s' when using Postgres."""
    return query.replace("?", "%s") if USE_POSTGRES else query


def init_db(db_path: str = DB_FILE) -> None:
    """Initialize the database and create tables if missing."""
    with get_conn(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS pair_progress (
                pair_name TEXT PRIMARY KEY,
                last_processed_id INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS message_mappings (
                pair_name TEXT NOT NULL,
                source_msg_id INTEGER NOT NULL,
                dest_msg_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (pair_name, source_msg_id)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS custom_prompts (
                pair_name TEXT PRIMARY KEY,
                prompt_text TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS class_context (
                id {_AUTOINCREMENT_PK},
                pair_name TEXT NOT NULL,
                msg_id INTEGER NOT NULL,
                context_text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS student_qa_logs (
                id {_AUTOINCREMENT_PK},
                timestamp TEXT NOT NULL,
                subject_name TEXT NOT NULL,
                user_name TEXT NOT NULL,
                question TEXT NOT NULL,
                answer TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS dynamic_classes (
                name TEXT PRIMARY KEY,
                source_chat_id INTEGER NOT NULL,
                destination_chat_id INTEGER NOT NULL,
                discussion_chat_id INTEGER,
                delay_min_seconds REAL DEFAULT 120.0,
                delay_max_seconds REAL DEFAULT 240.0,
                ai_mode TEXT DEFAULT 'flow',
                start_message_id INTEGER DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS content_pools (
                pair_name TEXT PRIMARY KEY,
                source_chat_id INTEGER NOT NULL,
                dest_chat_id INTEGER NOT NULL,
                cursor_id INTEGER NOT NULL,
                end_id INTEGER NOT NULL,
                session_days TEXT NOT NULL,
                session_start TEXT NOT NULL,
                session_duration_minutes INTEGER NOT NULL,
                notify_chat_id INTEGER NOT NULL,
                last_session_date TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def save_content_pool(
    pair_name: str,
    source_chat_id: int,
    dest_chat_id: int,
    start_id: int,
    end_id: int,
    session_days: str,
    session_start: str,
    session_duration_minutes: int,
    notify_chat_id: int,
    db_path: str = DB_FILE,
) -> None:
    """Create or replace a weekly auto-delivery pool: a range of old messages that gets drip-fed
    into the destination during a recurring weekly time window, a bit at a time, until exhausted."""
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with get_conn(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            _q(
                """
                INSERT INTO content_pools
                    (pair_name, source_chat_id, dest_chat_id, cursor_id, end_id, session_days,
                     session_start, session_duration_minutes, notify_chat_id, last_session_date, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                ON CONFLICT(pair_name) DO UPDATE SET
                    source_chat_id = excluded.source_chat_id,
                    dest_chat_id = excluded.dest_chat_id,
                    cursor_id = excluded.cursor_id,
                    end_id = excluded.end_id,
                    session_days = excluded.session_days,
                    session_start = excluded.session_start,
                    session_duration_minutes = excluded.session_duration_minutes,
                    notify_chat_id = excluded.notify_chat_id,
                    last_session_date = NULL,
                    created_at = excluded.created_at
                """
            ),
            (pair_name, source_chat_id, dest_chat_id, start_id, end_id, session_days,
             session_start, session_duration_minutes, notify_chat_id, now_iso),
        )
        conn.commit()


def get_all_content_pools(db_path: str = DB_FILE) -> List[Dict[str, Any]]:
    """Retrieve all configured weekly auto-delivery pools."""
    with get_conn(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT pair_name, source_chat_id, dest_chat_id, cursor_id, end_id, session_days,
                   session_start, session_duration_minutes, notify_chat_id, last_session_date
            FROM content_pools
            """
        )
        rows = cursor.fetchall()
        return [
            {
                "pair_name": r[0], "source_chat_id": r[1], "dest_chat_id": r[2],
                "cursor_id": r[3], "end_id": r[4], "session_days": r[5],
                "session_start": r[6], "session_duration_minutes": r[7],
                "notify_chat_id": r[8], "last_session_date": r[9],
            }
            for r in rows
        ]


def get_content_pool(pair_name: str, db_path: str = DB_FILE) -> Optional[Dict[str, Any]]:
    """Retrieve one pool's config/progress by pair name."""
    for pool in get_all_content_pools(db_path=db_path):
        if pool["pair_name"] == pair_name:
            return pool
    return None


def update_pool_cursor(pair_name: str, cursor_id: int, db_path: str = DB_FILE) -> None:
    """Advance a pool's delivery cursor after successfully copying a message."""
    with get_conn(db_path) as conn:
        conn.cursor().execute(_q("UPDATE content_pools SET cursor_id = ? WHERE pair_name = ?"), (cursor_id, pair_name))
        conn.commit()


def mark_pool_session_started(pair_name: str, date_str: str, db_path: str = DB_FILE) -> None:
    """Record that today's scheduled session has already started, so it isn't re-triggered."""
    with get_conn(db_path) as conn:
        conn.cursor().execute(_q("UPDATE content_pools SET last_session_date = ? WHERE pair_name = ?"), (date_str, pair_name))
        conn.commit()


def delete_content_pool(pair_name: str, db_path: str = DB_FILE) -> bool:
    """Remove a weekly auto-delivery pool."""
    with get_conn(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(_q("DELETE FROM content_pools WHERE pair_name = ?"), (pair_name,))
        conn.commit()
        return cursor.rowcount > 0


def save_dynamic_class(
    name: str,
    source_chat_id: int,
    destination_chat_id: int,
    discussion_chat_id: Optional[int] = None,
    delay_min_seconds: float = 120.0,
    delay_max_seconds: float = 240.0,
    ai_mode: str = "flow",
    db_path: str = DB_FILE,
) -> None:
    """Save or update a dynamic class configuration."""
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with get_conn(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            _q(
                """
                INSERT INTO dynamic_classes (name, source_chat_id, destination_chat_id, discussion_chat_id, delay_min_seconds, delay_max_seconds, ai_mode, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    source_chat_id = excluded.source_chat_id,
                    destination_chat_id = excluded.destination_chat_id,
                    discussion_chat_id = excluded.discussion_chat_id,
                    delay_min_seconds = excluded.delay_min_seconds,
                    delay_max_seconds = excluded.delay_max_seconds,
                    ai_mode = excluded.ai_mode,
                    created_at = excluded.created_at
                """
            ),
            (name, source_chat_id, destination_chat_id, discussion_chat_id, delay_min_seconds, delay_max_seconds, ai_mode, now_iso),
        )
        conn.commit()


def get_all_dynamic_classes(db_path: str = DB_FILE) -> List[Dict[str, Any]]:
    """Retrieve all dynamic classes configured in the database."""
    with get_conn(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT name, source_chat_id, destination_chat_id, discussion_chat_id, delay_min_seconds, delay_max_seconds, ai_mode, start_message_id
            FROM dynamic_classes
            """
        )
        rows = cursor.fetchall()
        classes = []
        for r in rows:
            classes.append({
                "name": r[0],
                "source_chat_id": r[1],
                "destination_chat_id": r[2],
                "discussion_chat_id": r[3],
                "delay_min_seconds": r[4],
                "delay_max_seconds": r[5],
                "ai_mode": r[6],
                "start_message_id": r[7],
            })
        return classes


def delete_dynamic_class(name: str, db_path: str = DB_FILE) -> bool:
    """Delete a dynamic class configuration."""
    with get_conn(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(_q("DELETE FROM dynamic_classes WHERE LOWER(name) = LOWER(?)"), (name,))
        conn.commit()
        return cursor.rowcount > 0


def clear_all_dynamic_classes(db_path: str = DB_FILE) -> None:
    """Wipe all dynamic classes to allow setting up from scratch."""
    with get_conn(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM dynamic_classes")
        conn.commit()


def get_last_processed_id(pair_name: str, db_path: str = DB_FILE) -> Optional[int]:
    """Retrieve the last processed message ID for a given channel pair."""
    with get_conn(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            _q("SELECT last_processed_id FROM pair_progress WHERE pair_name = ?"),
            (pair_name,),
        )
        row = cursor.fetchone()
        return row[0] if row else None


def set_last_processed_id(pair_name: str, message_id: int, db_path: str = DB_FILE) -> None:
    """Insert or update the last processed message ID for a given pair."""
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with get_conn(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            _q(
                """
                INSERT INTO pair_progress (pair_name, last_processed_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(pair_name) DO UPDATE SET
                    last_processed_id = excluded.last_processed_id,
                    updated_at = excluded.updated_at
                """
            ),
            (pair_name, message_id, now_iso),
        )
        conn.commit()


def save_message_mapping(pair_name: str, source_msg_id: int, dest_msg_id: int, db_path: str = DB_FILE) -> None:
    """Save mapping between source message ID and destination message ID to preserve reply threads."""
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with get_conn(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            _q(
                """
                INSERT INTO message_mappings (pair_name, source_msg_id, dest_msg_id, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(pair_name, source_msg_id) DO UPDATE SET
                    dest_msg_id = excluded.dest_msg_id,
                    created_at = excluded.created_at
                """
            ),
            (pair_name, source_msg_id, dest_msg_id, now_iso),
        )
        conn.commit()


def get_dest_msg_id(pair_name: str, source_msg_id: int, db_path: str = DB_FILE) -> Optional[int]:
    """Find the corresponding destination message ID for a given source message ID."""
    with get_conn(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            _q("SELECT dest_msg_id FROM message_mappings WHERE pair_name = ? AND source_msg_id = ?"),
            (pair_name, source_msg_id),
        )
        row = cursor.fetchone()
        return row[0] if row else None


def save_custom_prompt(pair_name: str, prompt_text: str, db_path: str = DB_FILE) -> None:
    """Save or update custom AI prompt for a pair."""
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with get_conn(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            _q(
                """
                INSERT INTO custom_prompts (pair_name, prompt_text, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(pair_name) DO UPDATE SET
                    prompt_text = excluded.prompt_text,
                    updated_at = excluded.updated_at
                """
            ),
            (pair_name, prompt_text, now_iso),
        )
        conn.commit()


def get_custom_prompt(pair_name: str, db_path: str = DB_FILE) -> Optional[str]:
    """Retrieve custom AI prompt for a pair if set."""
    with get_conn(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            _q("SELECT prompt_text FROM custom_prompts WHERE pair_name = ?"),
            (pair_name,),
        )
        row = cursor.fetchone()
        return row[0] if row else None


def delete_custom_prompt(pair_name: str, db_path: str = DB_FILE) -> None:
    """Reset custom AI prompt for a pair back to default."""
    with get_conn(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(_q("DELETE FROM custom_prompts WHERE pair_name = ?"), (pair_name,))
        conn.commit()


def save_class_context(pair_name: str, msg_id: int, context_text: str, db_path: str = DB_FILE) -> None:
    """Save class lesson text to database for AI Q&A context retrieval."""
    if not context_text or not context_text.strip():
        return
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with get_conn(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            _q(
                """
                INSERT INTO class_context (pair_name, msg_id, context_text, created_at)
                VALUES (?, ?, ?, ?)
                """
            ),
            (pair_name, msg_id, context_text.strip(), now_iso),
        )
        conn.commit()


def get_recent_class_context(pair_name: str, limit: int = 5, db_path: str = DB_FILE) -> List[str]:
    """Retrieve the most recent lesson texts for a pair to ground AI Q&A answers."""
    with get_conn(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            _q("SELECT context_text FROM class_context WHERE pair_name = ? ORDER BY id DESC LIMIT ?"),
            (pair_name, limit),
        )
        rows = cursor.fetchall()
        return [r[0] for r in reversed(rows)]


def save_qa_log(subject_name: str, user_name: str, question: str, answer: str, db_path: str = DB_FILE) -> None:
    """Log student interaction for teacher audit."""
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with get_conn(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            _q(
                """
                INSERT INTO student_qa_logs (timestamp, subject_name, user_name, question, answer)
                VALUES (?, ?, ?, ?, ?)
                """
            ),
            (now_iso, subject_name, user_name, question, answer),
        )
        conn.commit()


def get_recent_qa_logs(limit: int = 15, db_path: str = DB_FILE) -> List[Dict[str, Any]]:
    """Retrieve recent student interaction logs."""
    with get_conn(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            _q(
                """
                SELECT timestamp, subject_name, user_name, question, answer
                FROM student_qa_logs
                ORDER BY id DESC LIMIT ?
                """
            ),
            (limit,),
        )
        rows = cursor.fetchall()
        logs = []
        for r in rows:
            logs.append({
                "timestamp": r[0],
                "subject_name": r[1],
                "user_name": r[2],
                "question": r[3],
                "answer": r[4],
            })
        return logs
