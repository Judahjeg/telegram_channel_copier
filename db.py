from __future__ import annotations

import sqlite3
import datetime
import os
from typing import List

DB_FILE = os.getenv("DB_PATH", "copier.db")


def init_db(db_path: str = DB_FILE) -> None:
    """Initialize SQLite database with pair_progress, message_mapping, class_context, and custom_prompts tables."""
    with sqlite3.connect(db_path) as conn:
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
            CREATE TABLE IF NOT EXISTS message_mapping (
                pair_name TEXT NOT NULL,
                source_msg_id INTEGER NOT NULL,
                dest_msg_id INTEGER NOT NULL,
                PRIMARY KEY (pair_name, source_msg_id)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS class_context (
                pair_name TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                post_text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (pair_name, message_id)
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
        conn.commit()


def get_last_processed_id(pair_name: str, db_path: str = DB_FILE) -> int | None:
    """Retrieve the last processed message ID for a specific pair."""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT last_processed_id FROM pair_progress WHERE pair_name = ?",
            (pair_name,),
        )
        row = cursor.fetchone()
        return row[0] if row else None


def set_last_processed_id(pair_name: str, message_id: int, db_path: str = DB_FILE) -> None:
    """Update or insert the last processed message ID for a specific pair."""
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO pair_progress (pair_name, last_processed_id, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(pair_name) DO UPDATE SET
                last_processed_id = excluded.last_processed_id,
                updated_at = excluded.updated_at
            """,
            (pair_name, message_id, now_iso),
        )
        conn.commit()


def save_message_mapping(pair_name: str, source_msg_id: int, dest_msg_id: int, db_path: str = DB_FILE) -> None:
    """Save mapping between source message ID and copied destination message ID."""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO message_mapping (pair_name, source_msg_id, dest_msg_id)
            VALUES (?, ?, ?)
            ON CONFLICT(pair_name, source_msg_id) DO UPDATE SET
                dest_msg_id = excluded.dest_msg_id
            """,
            (pair_name, source_msg_id, dest_msg_id),
        )
        conn.commit()


def get_dest_msg_id(pair_name: str, source_msg_id: int, db_path: str = DB_FILE) -> int | None:
    """Retrieve destination message ID corresponding to a source message ID."""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT dest_msg_id FROM message_mapping WHERE pair_name = ? AND source_msg_id = ?",
            (pair_name, source_msg_id),
        )
        row = cursor.fetchone()
        return row[0] if row else None


def save_class_context(pair_name: str, message_id: int, post_text: str, db_path: str = DB_FILE) -> None:
    """Save class lesson text to SQLite database for context-aware Q&A."""
    if not post_text or not post_text.strip():
        return
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO class_context (pair_name, message_id, post_text, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(pair_name, message_id) DO UPDATE SET
                post_text = excluded.post_text,
                created_at = excluded.created_at
            """,
            (pair_name, message_id, post_text, now_iso),
        )
        conn.commit()


def get_recent_class_context(pair_name: str, limit: int = 5, db_path: str = DB_FILE) -> List[str]:
    """Retrieve recent lesson posts text for context when answering student questions."""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT post_text FROM class_context
            WHERE pair_name = ?
            ORDER BY message_id DESC
            LIMIT ?
            """,
            (pair_name, limit),
        )
        rows = cursor.fetchall()
        return [r[0] for r in rows]


def save_custom_prompt(pair_name: str, prompt_text: str, db_path: str = DB_FILE) -> None:
    """Save user-customized AI instructions per subject pair."""
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO custom_prompts (pair_name, prompt_text, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(pair_name) DO UPDATE SET
                prompt_text = excluded.prompt_text,
                updated_at = excluded.updated_at
            """,
            (pair_name, prompt_text, now_iso),
        )
        conn.commit()


def get_custom_prompt(pair_name: str, db_path: str = DB_FILE) -> str | None:
    """Retrieve custom AI prompt instructions for a subject pair."""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT prompt_text FROM custom_prompts WHERE pair_name = ?",
            (pair_name,),
        )
        row = cursor.fetchone()
        return row[0] if row else None


def delete_custom_prompt(pair_name: str, db_path: str = DB_FILE) -> None:
    """Reset custom AI prompt for a subject back to default."""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM custom_prompts WHERE pair_name = ?", (pair_name,))
        conn.commit()
