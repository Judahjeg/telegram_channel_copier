from __future__ import annotations

import datetime
import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import Channel, Chat, User, MessageMediaPoll, UpdateMessagePoll
from telethon.tl.functions.messages import SendVoteRequest

SESSION_STRING_ENV = "USERBOT_SESSION_STRING"
# Local convenience cache for CLI/terminal use on a real persistent machine, where a plain
# file "just working" across separate script invocations is fine. This is NOT what makes
# things durable on Render - a host without a persistent disk wipes this file on every
# redeploy just like it would any other local file, which is exactly why the environment
# variable is the real, durable mechanism there.
LOCAL_SESSION_CACHE_FILE = os.getenv("USERBOT_SESSION_FILE", ".userbot_session_string")


def _load_session_string() -> str:
    env_value = os.getenv(SESSION_STRING_ENV, "")
    if env_value:
        return env_value
    if os.path.exists(LOCAL_SESSION_CACHE_FILE):
        try:
            with open(LOCAL_SESSION_CACHE_FILE) as f:
                return f.read().strip()
        except Exception:
            pass
    return ""


def get_client() -> TelegramClient:
    """Build a Telethon client from TELEGRAM_API_ID / TELEGRAM_API_HASH.
    Get these once (free) from https://my.telegram.org -> API Development Tools.

    Uses a StringSession (portable text, not Telethon's native binary/sqlite session file)
    so login can survive on hosts without a persistent disk (e.g. Render's free tier) via the
    USERBOT_SESSION_STRING environment variable - a local file there gets wiped on every
    redeploy, silently forcing a fresh login each time. For local/CLI use, the string is also
    cached in a local file so repeated script runs on the same machine "just work" without
    manually managing an environment variable."""
    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        raise RuntimeError(
            "TELEGRAM_API_ID and TELEGRAM_API_HASH environment variables are required. "
            "Get them for free from https://my.telegram.org -> 'API Development Tools'."
        )
    return TelegramClient(StringSession(_load_session_string()), int(api_id), api_hash)


async def request_login_code(phone: str) -> str:
    """Step 1 of login: sends a Telegram login code to the account and returns a
    phone_code_hash needed to complete sign-in. Safe to call from a short-lived script."""
    client = get_client()
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
        return sent.phone_code_hash
    finally:
        await client.disconnect()


async def complete_login(phone: str, code: str, phone_code_hash: str, password: Optional[str] = None) -> Dict[str, str]:
    """Step 2 of login: verifies the code (and 2FA password if enabled) and returns both a
    greeting and the resulting session string, which the caller must show to the admin so they
    can save it as USERBOT_SESSION_STRING - without that, the login only lasts until this
    process restarts."""
    client = get_client()
    await client.connect()
    try:
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        except Exception as e:
            if password and "password" in str(e).lower() or "SessionPasswordNeeded" in type(e).__name__:
                await client.sign_in(password=password)
            else:
                raise
        me = await client.get_me()
        session_string = client.session.save()
        try:
            with open(LOCAL_SESSION_CACHE_FILE, "w") as f:
                f.write(session_string)
        except Exception:
            pass
        return {
            "greeting": f"Logged in as {me.first_name} (@{me.username or me.id})",
            "session_string": session_string,
        }
    finally:
        await client.disconnect()


async def is_logged_in() -> bool:
    client = get_client()
    await client.connect()
    try:
        return await client.is_user_authorized()
    finally:
        await client.disconnect()


@dataclass
class DialogInfo:
    id: int
    title: str
    kind: str  # "channel", "group", "user"
    username: Optional[str] = None


async def list_channels() -> List[DialogInfo]:
    """List every channel/group/chat the logged-in account can see, with the exact
    chat_id needed to configure a pair in the bot (avoids the @RawDataBot dance)."""
    client = get_client()
    await client.start()
    try:
        results = []
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if isinstance(entity, Channel):
                kind = "channel" if entity.broadcast else "supergroup"
            elif isinstance(entity, Chat):
                kind = "group"
            elif isinstance(entity, User):
                continue
            else:
                kind = "unknown"
            results.append(DialogInfo(
                id=dialog.id,
                title=dialog.title or "(untitled)",
                kind=kind,
                username=getattr(entity, "username", None),
            ))
        return results
    finally:
        await client.disconnect()


@dataclass
class MessageMeta:
    id: int
    date: datetime.datetime
    has_media: bool
    text_len: int


@dataclass
class Session:
    """A contiguous cluster of messages (one 'class') separated from the next
    cluster by a gap bigger than gap_minutes."""
    start_date: datetime.datetime
    end_date: datetime.datetime
    messages: List[MessageMeta] = field(default_factory=list)

    @property
    def deltas_seconds(self) -> List[float]:
        """Seconds between each message and the one before it (0.0 for the first)."""
        out = [0.0]
        for prev, cur in zip(self.messages, self.messages[1:]):
            out.append((cur.date - prev.date).total_seconds())
        return out

    @property
    def duration_seconds(self) -> float:
        return (self.end_date - self.start_date).total_seconds()


async def fetch_channel_history(channel_id: int, limit: Optional[int] = None) -> List[MessageMeta]:
    """Pull every message's real timestamp + media flag from a channel. This is the
    thing a Bot API bot fundamentally cannot do for historical messages - Telethon
    (a real user session) can, because it's just reading a channel it's a member of."""
    client = get_client()
    await client.start()
    try:
        out = []
        async for msg in client.iter_messages(channel_id, limit=limit, reverse=True):
            if msg is None:
                continue
            has_media = bool(msg.media) and not getattr(msg, "web_preview", None)
            text_len = len(msg.raw_text or "")
            out.append(MessageMeta(id=msg.id, date=msg.date, has_media=has_media, text_len=text_len))
        return out
    finally:
        await client.disconnect()


def group_into_sessions(messages: List[MessageMeta], gap_minutes: float = 90.0) -> List[Session]:
    """Split a flat message list into class sessions wherever the gap between two
    consecutive messages exceeds gap_minutes (e.g. the overnight gap between days)."""
    if not messages:
        return []

    gap = datetime.timedelta(minutes=gap_minutes)
    sessions: List[Session] = []
    current = Session(start_date=messages[0].date, end_date=messages[0].date, messages=[messages[0]])

    for prev, cur in zip(messages, messages[1:]):
        if cur.date - prev.date > gap:
            current.end_date = prev.date
            sessions.append(current)
            current = Session(start_date=cur.date, end_date=cur.date, messages=[cur])
        else:
            current.messages.append(cur)

    current.end_date = current.messages[-1].date
    sessions.append(current)
    return sessions


def analyze_channel(messages: List[MessageMeta], gap_minutes: float = 90.0) -> Dict[str, Any]:
    """Quick stats used by the 'analyze/test a channel' CLI action before committing to a migration."""
    if not messages:
        return {"message_count": 0, "sessions": []}

    sessions = group_into_sessions(messages, gap_minutes=gap_minutes)
    media_count = sum(1 for m in messages if m.has_media)
    total_text_chars = sum(m.text_len for m in messages)

    session_summaries = []
    for s in sessions:
        session_summaries.append({
            "start": s.start_date.isoformat(),
            "end": s.end_date.isoformat(),
            "message_count": len(s.messages),
            "media_count": sum(1 for m in s.messages if m.has_media),
            "duration_minutes": round(s.duration_seconds / 60, 1),
        })

    return {
        "message_count": len(messages),
        "date_range": (messages[0].date.isoformat(), messages[-1].date.isoformat()),
        "media_count": media_count,
        "total_text_chars": total_text_chars,
        "estimated_tokens": round(total_text_chars / 4),
        "session_count": len(sessions),
        "sessions": session_summaries,
    }


@dataclass
class QuizData:
    question: str
    options: List[str]
    correct_option_id: Optional[int]
    explanation: str


def _extract_correct_option(poll, results) -> Optional[int]:
    """Match each answer's opaque `option` byte-id against the results' per-option `correct`
    flag. Telegram only populates `correct` once the quiz's answer has been revealed to this
    account - either because the poll is closed, or because this account has voted on it."""
    if not results or not results.results:
        return None
    for i, answer in enumerate(poll.answers):
        for voter in results.results:
            if voter.option == answer.option and voter.correct:
                return i
    return None


def _text_of(field) -> str:
    return field.text if hasattr(field, "text") else str(field or "")


async def get_quiz_poll_data(chat_id: int, msg_id: int) -> Optional[QuizData]:
    """Reads a quiz poll's real question/options/correct-answer straight from the channel via
    the userbot account - something the Bot API can never see for a quiz it didn't create itself.
    If the correct answer isn't visible yet (poll still open, this account hasn't voted), casts
    one vote to force Telegram to reveal it - quiz results become visible to an account the
    moment it votes, whether or not the poll has been closed by its creator."""
    client = get_client()
    await client.start()
    try:
        msg = await client.get_messages(chat_id, ids=msg_id)
        if isinstance(msg, list):
            msg = msg[0] if msg else None
        if not msg or not isinstance(msg.media, MessageMediaPoll):
            return None

        poll = msg.media.poll
        results = msg.media.results
        if not poll.quiz:
            return None

        options = [_text_of(a.text) for a in poll.answers]
        correct_idx = _extract_correct_option(poll, results)

        if correct_idx is None and not poll.closed and poll.answers:
            try:
                input_peer = await client.get_input_entity(chat_id)
                update = await client(SendVoteRequest(peer=input_peer, msg_id=msg_id, options=[poll.answers[0].option]))
                for u in getattr(update, "updates", []) or []:
                    if isinstance(u, UpdateMessagePoll) and u.poll_id == poll.id:
                        correct_idx = _extract_correct_option(poll, u.results)
                        if correct_idx is not None:
                            break
            except Exception as e:
                logging.getLogger("ChannelCopier").debug(f"[QuizRecovery] Vote-to-reveal failed for msg {msg_id}: {e}")

        return QuizData(
            question=_text_of(poll.question),
            options=options,
            correct_option_id=correct_idx,
            explanation=(results.solution if results and results.solution else ""),
        )
    finally:
        await client.disconnect()


async def describe_message(chat_id: int, msg_id: int) -> str:
    """Diagnostic helper for when get_quiz_poll_data comes back empty: explains what the
    referenced message actually is, so a wrong channel/message ID is obvious instead of a
    bare 'not a quiz' with no way to tell why."""
    client = get_client()
    await client.start()
    try:
        msg = await client.get_messages(chat_id, ids=msg_id)
        if isinstance(msg, list):
            msg = msg[0] if msg else None

        if not msg:
            return (
                f"No message with ID {msg_id} was found in chat {chat_id}. Double-check both "
                f"numbers - a common mistake is copying the ID from the wrong message, or from "
                f"the wrong channel."
            )

        if isinstance(msg.media, MessageMediaPoll):
            poll = msg.media.poll
            kind = "quiz poll" if poll.quiz else "regular (non-quiz) poll"
            return f"This IS a poll, but it's a {kind}, not a quiz - question: \"{_text_of(poll.question)}\""

        if msg.text:
            return f"This is a plain text message, not a poll: \"{msg.text[:120]}\""
        if msg.photo:
            cap = f" (caption: \"{msg.text[:100]}\")" if msg.text else ""
            return f"This is a photo message, not a poll{cap}."
        if msg.video:
            cap = f" (caption: \"{msg.text[:100]}\")" if msg.text else ""
            return f"This is a video message, not a poll{cap}."
        if msg.document:
            cap = f" (caption: \"{msg.text[:100]}\")" if msg.text else ""
            return f"This is a document/file message, not a poll{cap}."

        return "This message exists but isn't a poll of any kind."
    except Exception as e:
        return f"Couldn't read that message at all: {e}"
    finally:
        await client.disconnect()
