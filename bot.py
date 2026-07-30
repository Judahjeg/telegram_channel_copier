from __future__ import annotations

import asyncio
import datetime
from html import escape as html_escape
import http.server
import json
import logging
import os
import random
import re
import secrets
import socketserver
import sys
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
from typing import Dict, List, Any, Optional, Set

from telegram import Update, BotCommand, MessageOriginChannel
from telegram.error import RetryAfter, BadRequest, TimedOut, NetworkError
from telegram.request import HTTPXRequest
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
)

import db
import ai_enhancer
import userbot_client as ub

# Setup console logging
logging.basicConfig(
    format="%(asctime)s - [%(levelname)s] - %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("ChannelCopier")

START_TIME = time.time()
USERBOT_LOGIN_STATE_FILE = ".login_state"

WEEKDAY_MAP = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}
WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


VERIFY_FORM_HTML = """<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect Account</title></head><body style="font-family:sans-serif;max-width:420px;margin:40px auto;padding:0 16px;">
<h2>Connect your Telegram account</h2>
<p>Enter the login code Telegram just sent you. <b>Do not paste this code into any Telegram chat</b> -
Telegram automatically blocks a code the moment it appears in a message, even one sent to your own bot.</p>
<form method="GET" action="/verify">
<input type="hidden" name="token" value="{token}">
<label>Code:</label><br>
<input type="text" name="code" style="font-size:1.4em;padding:8px;width:100%;box-sizing:border-box;" autofocus><br><br>
<label>2FA password (only if you have one enabled):</label><br>
<input type="password" name="password" style="padding:8px;width:100%;box-sizing:border-box;"><br><br>
<button type="submit" style="font-size:1.1em;padding:10px 20px;">Connect</button>
</form>
{message}
</body></html>"""


def start_dummy_web_server():
    """Start an immediate HTTP server on PORT for Render's 100% Free Web Service tier.
    Also serves /verify, a plain web page for completing the userbot login - Telegram
    auto-invalidates any login code the instant it's typed into a Telegram message, so
    the code must be entered somewhere outside Telegram entirely."""
    port = int(os.getenv("PORT", "8080"))

    class HealthHandler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path == "/verify":
                self._handle_verify(parsed)
                return

            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Iconic Impact Tutor Bot is running live 24/7!")

        def _handle_verify(self, parsed):
            qs = urllib.parse.parse_qs(parsed.query)
            token = qs.get("token", [""])[0]
            code = qs.get("code", [""])[0]
            password = qs.get("password", [""])[0] or None

            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()

            if not os.path.exists(USERBOT_LOGIN_STATE_FILE):
                self.wfile.write(b"<h3>No pending login request. Send /userbotlogin +yourphone to the bot first.</h3>")
                return

            try:
                with open(USERBOT_LOGIN_STATE_FILE) as f:
                    state = json.load(f)
            except Exception:
                self.wfile.write(b"<h3>Could not read pending login state. Try /userbotlogin again.</h3>")
                return

            if not token or token != state.get("token"):
                self.wfile.write(b"<h3>Invalid or expired link. Send /userbotlogin +yourphone to the bot again for a fresh link.</h3>")
                return

            if not code:
                page_html = VERIFY_FORM_HTML.format(token=token, message="")
                self.wfile.write(page_html.encode("utf-8"))
                return

            try:
                result = asyncio.run(
                    ub.complete_login(state["phone"], code, state["phone_code_hash"], password=password)
                )
                os.remove(USERBOT_LOGIN_STATE_FILE)
                greeting = html_escape(result["greeting"])
                session_string = html_escape(result["session_string"])
                self.wfile.write(
                    (
                        f"<h2>&#9989; {greeting}</h2>"
                        f"<p><b>One more step</b> to keep this working after the server restarts or "
                        f"redeploys: copy the text below and save it as the "
                        f"<code>USERBOT_SESSION_STRING</code> environment variable on your server "
                        f"(same place as BOT_TOKEN). Treat it like a password.</p>"
                        f"<textarea readonly style='width:100%;height:100px;font-family:monospace;' "
                        f"onclick='this.select()'>{session_string}</textarea>"
                        f"<p>You can close this page and go back to Telegram once that's saved.</p>"
                    ).encode("utf-8")
                )
            except Exception as e:
                page_html = VERIFY_FORM_HTML.format(token=token, message=f"<p style='color:red'>Failed: {html_escape(str(e))}. Try again below.</p>")
                self.wfile.write(page_html.encode("utf-8"))

        def log_message(self, format, *args):
            pass

    def run_server():
        try:
            with socketserver.TCPServer(("0.0.0.0", port), HealthHandler) as httpd:
                logger.info(f"🌐 Health check web server active on 0.0.0.0:{port} for Render Free Tier.")
                httpd.serve_forever()
        except Exception as e:
            logger.warning(f"Dummy web server info: {e}")

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()


def start_self_keep_alive_loop():
    """24/7 Automated Self-Keeper Loop: Pings the server every 3 minutes to prevent Render from sleeping."""
    def ping_loop():
        time.sleep(10)
        port = int(os.getenv("PORT", "8080"))
        render_url = os.getenv("RENDER_EXTERNAL_URL", "")

        while True:
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{port}/", headers={"User-Agent": "SelfKeepAlive/1.0"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    pass

                if render_url:
                    req_ext = urllib.request.Request(render_url, headers={"User-Agent": "SelfKeepAlive/1.0"})
                    with urllib.request.urlopen(req_ext, timeout=10) as resp_ext:
                        pass
                logger.debug("⏱️ [Keep-Alive] 24/7 self-ping successful. Container active.")
            except Exception as e:
                logger.debug(f"[Keep-Alive] Ping note: {e}")

            time.sleep(180)  # Ping every 3 minutes (180 seconds)

    thread = threading.Thread(target=ping_loop, daemon=True)
    thread.start()
    logger.info("⚡ [Keep-Alive Engine] 24/7 automated ping loop activated (3 min interval).")


def parse_iso_datetime(dt_str: str) -> datetime.datetime:
    """Parse ISO datetime string and ensure it has timezone info (default UTC)."""
    dt = datetime.datetime.fromisoformat(dt_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


class ChannelPair:
    def __init__(self, config: Dict[str, Any]):
        self.name: str = config["name"]
        self.source_chat_id: int = int(config["source_chat_id"])
        self.destination_chat_id: int = int(config["destination_chat_id"])
        self.discussion_chat_id: Optional[int] = int(config["discussion_chat_id"]) if config.get("discussion_chat_id") else None
        self.start_message_id: int = config.get("start_message_id", 1)

        # Explicit Channel Titles for clear "FROM ➔ TO" display
        self.source_title: str = config.get("source_title", f"Source Channel ({self.source_chat_id})")
        self.destination_title: str = config.get("destination_title", f"Destination Channel ({self.destination_chat_id})")
        self.discussion_title: str = config.get("discussion_title", f"Discussion Group ({self.discussion_chat_id})" if self.discussion_chat_id else "None")

        # Message Spacing / Delay settings (in seconds)
        self.delay_min_seconds: float = float(config.get("delay_min_seconds", 120.0))
        self.delay_max_seconds: float = float(config.get("delay_max_seconds", 240.0))
        if self.delay_max_seconds < self.delay_min_seconds:
            self.delay_max_seconds = self.delay_min_seconds

        # AI Mode ('flow', 'paraphrase', 'summarize', 'hashtags', 'off')
        self.ai_mode: str = config.get("ai_mode", "flow").lower()

        # Activation Date (optional)
        self.activate_at: Optional[datetime.datetime] = None
        if "activate_at" in config and config["activate_at"]:
            self.activate_at = parse_iso_datetime(config["activate_at"])

        # Weekly Class Schedule (optional)
        self.schedule: Optional[Dict[str, Any]] = config.get("schedule", None)

        # Check DB for existing progress or set default
        stored_last_id = db.get_last_processed_id(self.name)
        if stored_last_id is not None:
            self.last_processed_id = max(stored_last_id, self.start_message_id - 1)
        else:
            self.last_processed_id = self.start_message_id - 1
            db.set_last_processed_id(self.name, self.last_processed_id)

        # Manual override state
        self.manual_override_active: Optional[bool] = None

        # Independent Async Queue & Task
        self.queue: asyncio.Queue = asyncio.Queue()
        self.worker_task: asyncio.Task | None = None
        self.pending_albums: Dict[str, Dict[str, Any]] = {}

        logger.info(
            f"[Pair: {self.name}] Loaded pair. FROM {self.source_chat_id} ➔ TO {self.destination_chat_id}. "
            f"Spacing: {self.delay_min_seconds:.0f}-{self.delay_max_seconds:.0f}s."
        )

    def get_explicit_flow_description(self) -> str:
        """Explicit description showing FROM source ➔ TO destination."""
        disc_str = f"\n  💬 <b>Discussion Group:</b> {self.discussion_title} (<code>{self.discussion_chat_id}</code>)" if self.discussion_chat_id else ""
        return (
            f"• <b>{self.name} Pair Flow:</b>\n"
            f"  📤 <b>FROM (Source):</b> {self.source_title} (ID: <code>{self.source_chat_id}</code>)\n"
            f"  📥 <b>TO (Destination):</b> {self.destination_title} (ID: <code>{self.destination_chat_id}</code>)"
            f"{disc_str}"
        )

    def check_is_active(self, now: Optional[datetime.datetime] = None) -> bool:
        """Evaluate whether this subject pair should be active based on override, activate_at, and weekly schedule."""
        if self.manual_override_active is not None:
            return self.manual_override_active

        if now is None:
            now = datetime.datetime.now(datetime.timezone.utc)

        if self.activate_at and now < self.activate_at:
            return False

        if self.schedule:
            active_days = [d.capitalize() for d in self.schedule.get("active_days", [])]
            current_day = now.strftime("%A")

            if active_days and current_day not in active_days:
                return False

            start_str = self.schedule.get("start_time")
            end_str = self.schedule.get("end_time")
            if start_str and end_str:
                start_t = datetime.time.fromisoformat(start_str)
                end_t = datetime.time.fromisoformat(end_str)
                current_t = now.time()
                if not (start_t <= current_t <= end_t):
                    return False

        return True

    @property
    def is_active(self) -> bool:
        return self.check_is_active()


class TelegramCopierBot:
    def __init__(self, token: str, config_path: str = "config.json"):
        self.token = token
        self.config_path = config_path
        self.pairs: List[ChannelPair] = []
        self.source_map: Dict[int, List[ChannelPair]] = {}
        self.discussion_map: Dict[int, ChannelPair] = {}
        self.admin_user_ids: Set[int] = set()
        self.application = None
        # Per-admin in-progress migration built from forwarded messages instead of
        # manually-typed chat IDs/message IDs - see handle_forwarded_reference.
        self.pending_migration_refs: Dict[int, Dict[str, Any]] = {}
        # Per-admin in-progress /autodeliver setup wizard - see _handle_schedule_wizard_reply.
        self.pending_schedule_wizard: Dict[int, Dict[str, Any]] = {}
        # pair_names with a scheduled weekly session currently delivering, so the
        # scheduler loop doesn't start a second overlapping run for the same pool.
        self._running_pool_sessions: Set[str] = set()

    async def update_pair_titles(self, pair: ChannelPair) -> None:
        """Fetch official chat titles from Telegram API so pairs display exact channel names."""
        if not self.application or not self.application.bot:
            return
        try:
            s_chat = await self.application.bot.get_chat(pair.source_chat_id)
            if s_chat and s_chat.title:
                pair.source_title = s_chat.title
        except Exception as e:
            logger.debug(f"Source chat title fetch note: {e}")

        try:
            d_chat = await self.application.bot.get_chat(pair.destination_chat_id)
            if d_chat and d_chat.title:
                pair.destination_title = d_chat.title
        except Exception as e:
            logger.debug(f"Dest chat title fetch note: {e}")

        if pair.discussion_chat_id:
            try:
                disc_chat = await self.application.bot.get_chat(pair.discussion_chat_id)
                if disc_chat and disc_chat.title:
                    pair.discussion_title = disc_chat.title
            except Exception as e:
                logger.debug(f"Disc chat title fetch note: {e}")

    def sync_classes_from_db(self) -> None:
        """Load and synchronize dynamic class configurations from SQLite database into memory maps."""
        db_classes = db.get_all_dynamic_classes()

        self.source_map.clear()
        self.discussion_map.clear()

        existing_pairs_by_name = {p.name.lower(): p for p in self.pairs}
        new_pairs = []

        for p_cfg in db_classes:
            p_name = p_cfg["name"]
            if p_name.lower() in existing_pairs_by_name:
                pair = existing_pairs_by_name[p_name.lower()]
                pair.source_chat_id = int(p_cfg["source_chat_id"])
                pair.destination_chat_id = int(p_cfg["destination_chat_id"])
                pair.discussion_chat_id = int(p_cfg["discussion_chat_id"]) if p_cfg.get("discussion_chat_id") else None
                pair.delay_min_seconds = float(p_cfg.get("delay_min_seconds", 120.0))
                pair.delay_max_seconds = float(p_cfg.get("delay_max_seconds", 240.0))
                pair.ai_mode = p_cfg.get("ai_mode", "flow")
            else:
                pair = ChannelPair(p_cfg)
                if self.application:
                    pair.worker_task = asyncio.create_task(self.pair_worker_loop(pair, self.application))
                    asyncio.create_task(self.update_pair_titles(pair))

            new_pairs.append(pair)

            if pair.source_chat_id not in self.source_map:
                self.source_map[pair.source_chat_id] = []
            self.source_map[pair.source_chat_id].append(pair)

            if pair.discussion_chat_id:
                self.discussion_map[pair.discussion_chat_id] = pair

            self.discussion_map[pair.destination_chat_id] = pair

        self.pairs = new_pairs
        logger.info(f"Synchronized {len(self.pairs)} dynamic classes in memory.")

    def load_config(self) -> None:
        """Load admin user IDs and dynamic class channels."""
        raw_admins = []
        if os.path.exists(self.config_path):
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            raw_admins = [] if isinstance(data, list) else data.get("admin_user_ids", [])

        env_admins = os.getenv("ADMIN_IDS", "")
        if env_admins:
            for aid in env_admins.split(","):
                aid = aid.strip()
                if aid.isdigit():
                    raw_admins.append(int(aid))

        self.admin_user_ids = set(raw_admins)
        self.sync_classes_from_db()
        logger.info(f"Loaded {len(self.pairs)} channel pairs and {len(self.admin_user_ids)} bot admins.")

    def find_pair_by_name(self, search_term: str) -> Optional[ChannelPair]:
        """Flexible name lookup supporting exact match and partial fuzzy match."""
        term = search_term.strip().lower()
        if not term:
            return None

        for p in self.pairs:
            if p.name.lower() == term:
                return p

        for p in self.pairs:
            if term in p.name.lower():
                return p

        normalized_term = term.replace("chem", "chemistry").replace("bio", "biology").replace("phys", "physics").replace("math", "mathematics")
        for p in self.pairs:
            if normalized_term in p.name.lower() or p.name.lower() in normalized_term:
                return p

        return None

    def is_user_admin(self, user_id: int) -> bool:
        """Check if a user is an authorized bot administrator. Auto-registers active users as admins so they are never blocked."""
        if not self.admin_user_ids:
            self.admin_user_ids.add(user_id)
            return True
        if user_id in self.admin_user_ids:
            return True

        self.admin_user_ids.add(user_id)
        logger.info(f"Auto-authorized Telegram User ID {user_id} as Bot Admin.")
        return True

    def get_classes_summary(self) -> str:
        """Generate summary string of all classes for the AI assistant with explicit FROM ➔ TO detail."""
        if not self.pairs:
            return "No active classes right now. The administrator has a clean slate."
        lines = []
        for p in self.pairs:
            cp = db.get_custom_prompt(p.name)
            cp_str = f"Prompt: '{cp}'" if cp else "Prompt: default"
            lines.append(
                f"- Class: {p.name} | FROM Source '{p.source_title}' ({p.source_chat_id}) ➔ TO Dest '{p.destination_title}' ({p.destination_chat_id}) | Spacing: {p.delay_min_seconds:.0f}s | Active: {p.is_active} | {cp_str}"
            )
        return "\n".join(lines)

    async def activation_checker_loop(self) -> None:
        """Periodic loop logging status changes."""
        last_states: Dict[str, bool] = {}
        while True:
            await asyncio.sleep(30)
            now = datetime.datetime.now(datetime.timezone.utc)
            for pair in self.pairs:
                curr_active = pair.check_is_active(now)
                prev_active = last_states.get(pair.name, None)
                if prev_active is not None and curr_active != prev_active:
                    state_str = "🟢 ACTIVE" if curr_active else "⏳ DORMANT"
                    logger.info(f"[Pair: {pair.name}] State changed to {state_str}")
                last_states[pair.name] = curr_active

    async def content_pool_scheduler_loop(self, application) -> None:
        """Checks every minute for weekly auto-delivery pools whose scheduled session window has
        begun today, and kicks off that day's paced chunk automatically - the whole point being
        the admin never has to manually trigger a session when class time comes around."""
        while True:
            await asyncio.sleep(60)
            try:
                now = datetime.datetime.now(datetime.timezone.utc)
                today_str = now.strftime("%Y-%m-%d")
                weekday = now.weekday()

                for pool in db.get_all_content_pools():
                    pair_name = pool["pair_name"]
                    if pool["cursor_id"] > pool["end_id"]:
                        continue
                    if pair_name in self._running_pool_sessions:
                        continue
                    if pool["last_session_date"] == today_str:
                        continue

                    try:
                        days = json.loads(pool["session_days"])
                    except Exception:
                        continue
                    if weekday not in days:
                        continue

                    start_t = datetime.time.fromisoformat(pool["session_start"])
                    start_dt = now.replace(hour=start_t.hour, minute=start_t.minute, second=0, microsecond=0)
                    end_dt = start_dt + datetime.timedelta(minutes=pool["session_duration_minutes"])
                    if not (start_dt <= now <= end_dt):
                        continue

                    self._running_pool_sessions.add(pair_name)
                    db.mark_pool_session_started(pair_name, today_str)
                    asyncio.create_task(self._run_scheduled_session(pool, end_dt, application))
            except Exception as e:
                logger.error(f"[ContentPoolScheduler] Error in scheduler tick: {e}", exc_info=True)

    async def _run_scheduled_session(
        self, pool: Dict[str, Any], end_dt: datetime.datetime, application
    ) -> None:
        """Delivers one day's chunk of a content pool, shuffled 2-5 min apart, stopping
        automatically at the window's end time or when the pool runs dry - whichever first."""
        pair_name = pool["pair_name"]
        source_id = pool["source_chat_id"]
        dest_id = pool["dest_chat_id"]
        notify_chat_id = pool["notify_chat_id"]
        cursor_id = pool["cursor_id"]
        end_id = pool["end_id"]
        delivered = 0

        skipped_polls = 0
        recreated_quizzes = 0
        skip_samples: List[str] = []
        recreated_samples: List[str] = []

        try:
            try:
                await application.bot.send_message(
                    notify_chat_id,
                    f"🔔 <b>{pair_name}:</b> today's scheduled session is starting - delivering content now.",
                    parse_mode="HTML",
                )
            except Exception:
                pass

            while cursor_id <= end_id:
                if datetime.datetime.now(datetime.timezone.utc) >= end_dt:
                    break

                try:
                    result, recreated, quiz_q = await self._copy_or_recover_quiz(application, source_id, dest_id, cursor_id)
                    if result:
                        db.save_message_mapping(pair_name, cursor_id, result.message_id)
                        delivered += 1
                        if recreated:
                            recreated_quizzes += 1
                            if len(recreated_samples) < 5:
                                recreated_samples.append(f"#{cursor_id}: {quiz_q[:70]}")
                except BadRequest as e:
                    if self._is_poll_copy_restriction(e):
                        skipped_polls += 1
                    elif len(skip_samples) < 10:
                        skip_samples.append(f"#{cursor_id}: {str(e)[:80]}")
                    logger.debug(f"[ScheduledSession:{pair_name}] Skipped ID {cursor_id}: {e}")
                except Exception as e:
                    logger.warning(f"[ScheduledSession:{pair_name}] Failed ID {cursor_id}: {e}")

                cursor_id += 1
                db.update_pool_cursor(pair_name, cursor_id)

                if cursor_id > end_id:
                    break

                remaining_window = (end_dt - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
                if remaining_window <= 60:
                    # Not enough runway left for another natural gap - stop cleanly here rather
                    # than squeezing the next message in with a near-zero delay (which would look
                    # like an obvious burst). The rest continues at the next scheduled session.
                    break

                await asyncio.sleep(random.uniform(120, 300))

            pool_exhausted = cursor_id > end_id
            done_note = " The full backlog is now fully delivered! 🎉" if pool_exhausted else " More continues next scheduled session."
            poll_note = ""
            if recreated_quizzes:
                poll_note += f" 🧠 {recreated_quizzes} quiz poll(s) recreated with their real answer."
            if skipped_polls:
                poll_note += (
                    f" ({skipped_polls} quiz poll(s) skipped — Telegram only lets the bot that "
                    f"created a quiz copy it, and the answer couldn't be recovered; use /quiz to "
                    f"generate fresh ones.)"
                )
            recreated_note = f"\n\n<b>Recovered:</b>\n<code>{chr(10).join(recreated_samples)}</code>" if recreated_samples else ""
            samples_note = f"\n\n<code>{chr(10).join(skip_samples)}</code>" if skip_samples else ""
            try:
                await application.bot.send_message(
                    notify_chat_id,
                    f"✅ <b>{pair_name}:</b> today's session delivered {delivered} message(s).{done_note}{poll_note}{recreated_note}{samples_note}",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        finally:
            self._running_pool_sessions.discard(pair_name)

    @staticmethod
    def _is_poll_copy_restriction(exc: Exception) -> bool:
        """Telegram only reveals a quiz poll's correct answer to the bot that originally created
        it - copying a quiz posted by a different bot/account always fails with a Bad Request,
        no matter what. Detecting that here turns a silent skip into an explained one."""
        text = str(exc).lower()
        return "poll" in text or "quiz" in text

    async def _copy_or_recover_quiz(self, application, source_id: int, dest_id: int, msg_id: int):
        """Copies a message; if it's a quiz poll Telegram refuses to copy (the original-creator
        restriction), tries to recover the real question/options/correct-answer via the connected
        userbot account and recreate it as a fresh quiz instead of losing it outright. Returns
        (result, recreated, question) - recreated=True means the quiz-recovery path was used, and
        question is the recovered text (for the caller to surface, so recovered quizzes are
        spot-checkable rather than just a bare count). Re-raises the original error if recovery
        isn't possible (not logged in, or the answer can't be read)."""
        try:
            result = await self._call_with_retry(
                application.bot.copy_message,
                chat_id=dest_id,
                from_chat_id=source_id,
                message_id=msg_id,
            )
            return result, False, None
        except BadRequest as e:
            if not self._is_poll_copy_restriction(e) or not await ub.is_logged_in():
                raise

            quiz_data = await ub.get_quiz_poll_data(source_id, msg_id)
            if not quiz_data or quiz_data.correct_option_id is None or not quiz_data.options:
                raise

            options = [o[:95] for o in quiz_data.options][:10]
            result = await self._call_with_retry(
                application.bot.send_poll,
                chat_id=dest_id,
                question=(quiz_data.question or "Quiz Question")[:290],
                options=options,
                type="quiz",
                correct_option_id=min(quiz_data.correct_option_id, len(options) - 1),
                explanation=(quiz_data.explanation[:195] if quiz_data.explanation else None),
                is_anonymous=False,
            )
            return result, True, quiz_data.question

    async def _call_with_retry(self, coro_func, max_attempts: int = 4, **kwargs):
        """Call a Telegram Bot API method, transparently retrying on flood-control (RetryAfter)
        and transient network errors instead of silently dropping the message."""
        for attempt in range(max_attempts):
            try:
                return await coro_func(**kwargs)
            except RetryAfter as e:
                wait_s = float(e.retry_after) + 1.0
                logger.warning(f"⏱️ Flood control hit. Waiting {wait_s:.0f}s before retrying...")
                await asyncio.sleep(wait_s)
            except (TimedOut, NetworkError) as e:
                if attempt == max_attempts - 1:
                    raise
                logger.debug(f"Transient network error ({e}), retrying (attempt {attempt + 1})...")
                await asyncio.sleep(3 * (attempt + 1))
        return None

    async def pair_worker_loop(self, pair: ChannelPair, application) -> None:
        """Dedicated worker loop per subject pair applying custom AI prompt instructions, spacing, sanitization, and reply linking."""
        logger.info(f"[Pair: {pair.name}] Worker task started.")
        while True:
            item = await pair.queue.get()
            try:
                max_id = item["max_id"]

                if max_id <= pair.last_processed_id:
                    pair.queue.task_done()
                    continue

                if pair.delay_min_seconds == pair.delay_max_seconds:
                    delay_sec = pair.delay_min_seconds
                else:
                    delay_sec = random.uniform(pair.delay_min_seconds, pair.delay_max_seconds)

                item_desc = (
                    f"Album {item['message_ids']}"
                    if item["type"] == "album"
                    else f"Message #{item['message_id']}"
                )

                logger.info(
                    f"⏳ [Pair: {pair.name}] Received {item_desc}. "
                    f"Applying message spacing of {delay_sec:.1f}s before posting to {pair.destination_chat_id}..."
                )

                await asyncio.sleep(delay_sec)

                source_reply_id = item.get("reply_to_message_id")
                dest_reply_id = None
                if source_reply_id:
                    dest_reply_id = db.get_dest_msg_id(pair.name, source_reply_id)

                raw_text = item.get("text", "")

                sanitized_text = ai_enhancer.sanitize_text(raw_text)

                if sanitized_text:
                    db.save_class_context(pair.name, item["message_id"], sanitized_text)

                custom_instruction = db.get_custom_prompt(pair.name)

                # A message carries media (photo/video/document/etc.) if it arrived as an
                # album, or was flagged as such when queued. Media must ALWAYS go through
                # copy_message/copy_messages so the attachment itself is never dropped -
                # send_message only transmits text and would silently discard the file.
                has_media = item["type"] == "album" or item.get("has_media", False)

                enhanced_text: Optional[str] = None
                if pair.ai_mode != "off" and sanitized_text:
                    enhanced_text = ai_enhancer.enhance_text_with_gemini(
                        sanitized_text, pair.name, pair.ai_mode, custom_instruction=custom_instruction
                    )

                if has_media:
                    if item["type"] == "single":
                        copied_msg_id_obj = await self._call_with_retry(
                            application.bot.copy_message,
                            chat_id=pair.destination_chat_id,
                            from_chat_id=pair.source_chat_id,
                            message_id=item["message_id"],
                            reply_to_message_id=dest_reply_id,
                            caption=enhanced_text if enhanced_text else None,
                            parse_mode="HTML" if enhanced_text else None,
                        )
                        if copied_msg_id_obj:
                            db.save_message_mapping(pair.name, item["message_id"], copied_msg_id_obj.message_id)

                        logger.info(
                            f"✅ [Pair: {pair.name}] Copied media message #{item['message_id']} "
                            f"({'AI caption' if enhanced_text else 'original caption'}) to chat {pair.destination_chat_id}."
                        )
                    elif item["type"] == "album":
                        copied_msgs = await self._call_with_retry(
                            application.bot.copy_messages,
                            chat_id=pair.destination_chat_id,
                            from_chat_id=pair.source_chat_id,
                            message_ids=item["message_ids"],
                        )
                        if copied_msgs and len(copied_msgs) == len(item["message_ids"]):
                            for src_id, dst_msg in zip(item["message_ids"], copied_msgs):
                                db.save_message_mapping(pair.name, src_id, dst_msg.message_id)

                            if enhanced_text:
                                try:
                                    await application.bot.edit_message_caption(
                                        chat_id=pair.destination_chat_id,
                                        message_id=copied_msgs[0].message_id,
                                        caption=enhanced_text,
                                        parse_mode="HTML",
                                    )
                                except Exception as cap_err:
                                    logger.debug(f"[Pair: {pair.name}] Could not apply AI caption to album: {cap_err}")

                        logger.info(
                            f"✅ [Pair: {pair.name}] Copied album messages {item['message_ids']} to chat {pair.destination_chat_id}."
                        )
                elif enhanced_text:
                    sent_msg = await self._call_with_retry(
                        application.bot.send_message,
                        chat_id=pair.destination_chat_id,
                        text=enhanced_text,
                        reply_to_message_id=dest_reply_id,
                    )
                    if sent_msg:
                        db.save_message_mapping(pair.name, item["message_id"], sent_msg.message_id)

                    logger.info(
                        f"✨ [Pair: {pair.name}] Posted AI-sanitized message #{item['message_id']} to chat {pair.destination_chat_id}."
                    )
                else:
                    copied_msg_id_obj = await self._call_with_retry(
                        application.bot.copy_message,
                        chat_id=pair.destination_chat_id,
                        from_chat_id=pair.source_chat_id,
                        message_id=item["message_id"],
                        reply_to_message_id=dest_reply_id,
                    )
                    if copied_msg_id_obj:
                        db.save_message_mapping(pair.name, item["message_id"], copied_msg_id_obj.message_id)

                    logger.info(
                        f"✅ [Pair: {pair.name}] Copied message #{item['message_id']} to chat {pair.destination_chat_id}."
                    )

                pair.last_processed_id = max_id
                db.set_last_processed_id(pair.name, max_id)

            except Exception as e:
                logger.error(
                    f"❌ [Pair: {pair.name}] Error copying message item {item}: {e}",
                    exc_info=True,
                )
                pair.last_processed_id = item["max_id"]
                db.set_last_processed_id(pair.name, item["max_id"])

            finally:
                pair.queue.task_done()

    def _flush_album(self, pair: ChannelPair, media_group_id: str) -> None:
        """Flush buffered media group items to pair queue."""
        album_data = pair.pending_albums.pop(media_group_id, None)
        if not album_data:
            return

        msg_ids = sorted(album_data["message_ids"])
        max_id = max(msg_ids)

        if max_id <= pair.last_processed_id:
            return

        pair.queue.put_nowait({
            "type": "album",
            "message_ids": msg_ids,
            "max_id": max_id,
            "text": album_data.get("text", ""),
            "reply_to_message_id": album_data.get("reply_to_message_id"),
        })

    async def handle_discussion_qa(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle AI Q&A when students tag the bot or reply to bot in Discussion Groups."""
        message = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        bot_user = context.bot.username

        if not message or not chat or not message.text:
            return

        is_mentioned = bot_user and f"@{bot_user.lower()}" in message.text.lower()
        is_reply_to_bot = (
            message.reply_to_message
            and message.reply_to_message.from_user
            and message.reply_to_message.from_user.id == context.bot.id
        )

        if not (is_mentioned or is_reply_to_bot):
            return

        pair = self.discussion_map.get(chat.id)
        exact_class_name = pair.name if pair else (chat.title or "Class")

        question = message.text
        if bot_user:
            question = question.replace(f"@{bot_user}", "").strip()

        user_display_name = user.first_name if user else "Student"
        logger.info(f"Received Q&A question from {user_display_name} in '{chat.title or chat.id}' for exact class '{exact_class_name}': '{question}'")

        context_notes = []
        if pair:
            context_notes = db.get_recent_class_context(pair.name, limit=5)

        ai_response = ai_enhancer.answer_student_question(
            question=question,
            subject_name=exact_class_name,
            class_context_texts=context_notes,
        )

        db.save_qa_log(
            subject_name=exact_class_name,
            user_name=user_display_name,
            question=question,
            answer=ai_response,
        )

        await message.reply_text(ai_response, parse_mode="HTML")

    async def addclass_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Interactively add or update a custom class channel directly via Telegram command."""
        user = update.effective_user
        if user:
            self.is_user_admin(user.id)

        args = context.args
        if not args or len(args) < 3:
            await update.message.reply_text(
                "➕ <b>How to Add/Set a Custom Class Pair:</b>\n\n"
                "Syntax: <code>/addclass ClassName SourceChatID DestinationChatID [DiscussionChatID]</code>\n\n"
                "<b>Example:</b>\n"
                "<code>/addclass Mathematics 1 -100123456789 -100987654321 -100555444333</code>\n\n"
                "<i>Tip: You can also just tell the AI in private chat: 'Add a new class called Mathematics 1 from source -100123... to destination -100987...'</i>",
                parse_mode="HTML",
            )
            return

        c_name = args[0]
        arg_offset = 1
        if len(args) >= 4 and not args[1].lstrip("-").isdigit():
            c_name = f"{args[0]} {args[1]}"
            arg_offset = 2

        try:
            source_id = int(args[arg_offset])
            dest_id = int(args[arg_offset + 1])
            disc_id = int(args[arg_offset + 2]) if len(args) > arg_offset + 2 else None

            db.save_dynamic_class(
                name=c_name,
                source_chat_id=source_id,
                destination_chat_id=dest_id,
                discussion_chat_id=disc_id,
            )
            self.sync_classes_from_db()

            disc_str = f"\n💬 <b>Discussion Group:</b> <code>{disc_id}</code>" if disc_id else ""
            await update.message.reply_text(
                f"🎉 <b>Explicit Pair Configured!</b>\n\n"
                f"📚 <b>Class Name:</b> <b>{c_name}</b>\n"
                f"📤 <b>FROM (Source Channel):</b> <code>{source_id}</code>\n"
                f"📥 <b>TO (Destination Channel):</b> <code>{dest_id}</code>{disc_str}\n\n"
                f"🟢 Status: Connected & listening live now!",
                parse_mode="HTML",
            )
        except ValueError:
            await update.message.reply_text("❌ Source, Destination, and Discussion Chat IDs must be valid numeric IDs (e.g., -100123456789).")

    async def deleteclass_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Interactively delete a class channel configuration."""
        user = update.effective_user
        if user:
            self.is_user_admin(user.id)

        args = context.args
        if not args:
            await update.message.reply_text("🗑️ <b>Usage:</b> <code>/deleteclass ClassName</code>\nExample: <code>/deleteclass Physics 1</code>", parse_mode="HTML")
            return

        target_name = " ".join(args).strip()
        deleted = db.delete_dynamic_class(target_name)
        if deleted:
            self.sync_classes_from_db()
            await update.message.reply_text(f"🗑️ <b>Deleted!</b> Class channel pair <b>{target_name}</b> was removed successfully.", parse_mode="HTML")
        else:
            await update.message.reply_text(f"❌ Class <b>{target_name}</b> not found.", parse_mode="HTML")

    async def clearallclasses_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Wipe all existing class configurations to start completely from scratch."""
        user = update.effective_user
        if user:
            self.is_user_admin(user.id)

        db.clear_all_dynamic_classes()
        self.sync_classes_from_db()
        await update.message.reply_text("🧹 <b>Clean Slate Activated!</b> All class pairs have been deleted. You can now add your own explicit pairs from scratch using `/addclass`!", parse_mode="HTML")

    async def backfill_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Bulk-migrate PAST/EXISTING messages already sitting in an old source channel into the
        new destination channel. Real-time copying only reacts to messages posted while the bot is
        running, so previously posted lesson materials need this explicit one-time migration."""
        user = update.effective_user
        if user:
            self.is_user_admin(user.id)

        args = context.args
        usage_text = (
            "📦 <b>How to migrate old/previous materials into a new channel:</b>\n\n"
            "<b>Option A - using an already-registered class:</b>\n"
            "<code>/backfill ClassName start_id end_id</code>\n"
            "Example: <code>/backfill Mathematics 1 1 850</code>\n\n"
            "<b>Option B - raw channel IDs, no /addclass needed first:</b>\n"
            "<code>/backfill source_channel_id dest_channel_id start_id end_id</code>\n"
            "Example: <code>/backfill -1001111111111 -1002222222222 545 590</code>\n\n"
            "<i>Tip: Open the OLD channel, right-click the first and last message you want to migrate, "
            "and choose 'Copy Message Link'. The number at the end of each link is the message ID.</i>\n\n"
            "⏱️ <b>Pacing:</b> messages post 2-5 minutes apart (shuffled), so migrated content lands with "
            "a natural rhythm instead of an obvious rapid-fire dump - which means a few hundred messages "
            "takes several hours, not minutes. If interrupted (e.g. a redeploy), just re-run the same "
            "command - anything already copied is skipped automatically.\n\n"
            "⚠️ <b>Note:</b> Telegram's Bot API cannot read the *content* of old messages, only copy them "
            "as-is. So AI cleanup/anonymization only runs on messages received live going forward — "
            "backfilled posts keep their original captions untouched."
        )

        if not args or len(args) < 3:
            await update.message.reply_text(usage_text, parse_mode="HTML")
            return

        def is_int(s: str) -> bool:
            return s.lstrip("-").isdigit()

        # Option B: exactly 4 numeric args -> source_id dest_id start_id end_id, no registered pair needed.
        if len(args) == 4 and all(is_int(a) for a in args):
            source_id, dest_id, start_id, end_id = (int(a) for a in args)
            pair_name = f"backfill_{source_id}_{dest_id}"
            dest_label = str(dest_id)
        elif is_int(args[-1]) and is_int(args[-2]):
            # Option A: ClassName start_id end_id, using an already-registered pair.
            end_id = int(args[-1])
            start_id = int(args[-2])
            class_name = " ".join(args[:-2]).strip()

            found_pair = self.find_pair_by_name(class_name)
            if not found_pair:
                await update.message.reply_text(
                    f"❌ Class '<b>{class_name}</b>' not found. Either register it first with "
                    f"<code>/addclass</code>, or use raw channel IDs instead: "
                    f"<code>/backfill source_id dest_id start_id end_id</code>.",
                    parse_mode="HTML",
                )
                return

            source_id = found_pair.source_chat_id
            dest_id = found_pair.destination_chat_id
            pair_name = found_pair.name
            dest_label = found_pair.destination_title
        else:
            await update.message.reply_text(usage_text, parse_mode="HTML")
            return

        if start_id > end_id:
            start_id, end_id = end_id, start_id

        total = end_id - start_id + 1
        if total > 3000:
            await update.message.reply_text(
                f"❌ Range too large ({total} messages). Please split into smaller batches (max 3000 per run)."
            )
            return

        await update.message.reply_text(
            f"📦 <b>Backfill started for {pair_name}!</b>\n"
            f"Migrating message IDs <code>{start_id}</code>–<code>{end_id}</code> ({total} messages) "
            f"from <code>{source_id}</code> into <b>{dest_label}</b>.\n\n"
            f"You'll get progress updates here. This can take a while for large batches "
            f"since it's paced to avoid Telegram's flood limits.",
            parse_mode="HTML",
        )

        asyncio.create_task(
            self._run_backfill(pair_name, source_id, dest_id, start_id, end_id, update.effective_chat.id, context.application)
        )

    async def _run_backfill(
        self, pair_name: str, source_id: int, dest_id: int, start_id: int, end_id: int, notify_chat_id: int, application
    ) -> None:
        """Worker that walks a historical message ID range and copies each into the destination,
        pacing itself to stay under Telegram's flood-control limits and surviving transient errors."""
        copied = 0
        skipped = 0
        failed = 0
        skipped_polls = 0
        recreated_quizzes = 0
        skip_samples: List[str] = []
        recreated_samples: List[str] = []
        total = end_id - start_id + 1

        logger.info(f"[Backfill:{pair_name}] Starting migration of IDs {start_id}-{end_id} ({total} messages).")

        for msg_id in range(start_id, end_id + 1):
            try:
                result, recreated, quiz_q = await self._copy_or_recover_quiz(application, source_id, dest_id, msg_id)
                if result:
                    db.save_message_mapping(pair_name, msg_id, result.message_id)
                    copied += 1
                    if recreated:
                        recreated_quizzes += 1
                        if len(recreated_samples) < 5:
                            recreated_samples.append(f"#{msg_id}: {quiz_q[:70]}")
            except BadRequest as e:
                # Expected for gaps: deleted messages, service messages, or IDs that never existed -
                # plus quiz polls that couldn't be recovered (no userbot connected, or Telegram
                # still won't reveal the answer even after voting).
                skipped += 1
                if self._is_poll_copy_restriction(e):
                    skipped_polls += 1
                elif len(skip_samples) < 10:
                    skip_samples.append(f"#{msg_id}: {str(e)[:80]}")
                logger.debug(f"[Backfill:{pair_name}] Skipped ID {msg_id}: {e}")
            except Exception as e:
                failed += 1
                logger.warning(f"[Backfill:{pair_name}] Failed ID {msg_id}: {e}")

            done = copied + skipped + failed
            if done % 10 == 0 or done == total:
                try:
                    await application.bot.send_message(
                        notify_chat_id,
                        f"⏳ <b>{pair_name} backfill progress:</b> {done}/{total} processed "
                        f"(✅ {copied} copied, ⏭️ {skipped} skipped, ❌ {failed} failed)",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

            # 2-5 minute shuffled spacing so migrated posts land with a natural rhythm
            # instead of an obvious rapid-fire bulk dump.
            await asyncio.sleep(random.uniform(120, 300))

        logger.info(f"[Backfill:{pair_name}] Finished. Copied={copied} Skipped={skipped} Failed={failed}")

        poll_note = ""
        if recreated_quizzes:
            poll_note += f"\n\n🧠 <b>{recreated_quizzes} quiz poll(s) recreated</b> with their real question, options, and correct answer (recovered via your connected account)."
        if skipped_polls:
            poll_note += (
                f"\n\n📊 <b>{skipped_polls} quiz poll(s) skipped</b> — Telegram won't let this bot "
                f"copy them, and the correct answer couldn't be recovered "
                f"{'(connect your account with /userbotlogin to enable recovery)' if not await ub.is_logged_in() else '(Telegram still would not reveal the answer)'}. "
                f"If this channel is registered as a class, use <code>/quiz ClassName</code> to "
                f"generate a fresh AI practice quiz instead."
            )
        recreated_note = f"\n\n<b>Recovered:</b>\n<code>{chr(10).join(recreated_samples)}</code>" if recreated_samples else ""
        samples_note = f"\n\n<code>{chr(10).join(skip_samples)}</code>" if skip_samples else ""

        try:
            await application.bot.send_message(
                notify_chat_id,
                f"🎉 <b>Backfill complete for {pair_name}!</b>\n\n"
                f"✅ Copied: {copied}\n⏭️ Skipped (not found/deleted/restricted): {skipped}\n❌ Failed: {failed}"
                f"{poll_note}{recreated_note}{samples_note}",
                parse_mode="HTML",
            )
        except Exception:
            pass

    async def autodeliver_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Set up a weekly recurring auto-delivery pool: old channel materials drip out
        automatically during a scheduled day/time window every week, no manual trigger needed."""
        user = update.effective_user
        if user:
            self.is_user_admin(user.id)

        args = context.args
        usage = (
            "🗓️ <b>Schedule automatic weekly content delivery:</b>\n\n"
            "Syntax: <code>/autodeliver source_id dest_id start_id end_id days start_time duration_minutes [Name]</code>\n\n"
            "<b>Example:</b>\n"
            "<code>/autodeliver -1001111111111 -1002222222222 1 850 Mon,Wed,Fri 15:00 120 Physics</code>\n\n"
            "This delivers messages #1-850 automatically every Mon/Wed/Fri, starting at 15:00 "
            "<b>UTC</b>, spread naturally (2-5 min apart) across a 120-minute window — picking up "
            "where it left off each session until the whole range has been delivered.\n\n"
            "⚠️ <b>Times are in UTC.</b> Nigeria (WAT) is UTC+1, so a 4pm local class is "
            "<code>15:00</code> here.\n\n"
            "Use <code>/stopautodeliver</code> to see active schedules, or "
            "<code>/stopautodeliver Name</code> to cancel one."
        )

        if not args or len(args) < 7:
            await update.message.reply_text(usage, parse_mode="HTML")
            return

        try:
            source_id = int(args[0])
            dest_id = int(args[1])
            start_id = int(args[2])
            end_id = int(args[3])
        except ValueError:
            await update.message.reply_text("❌ source_id, dest_id, start_id, and end_id must all be numeric.", parse_mode="HTML")
            return

        day_tokens = [d.strip().lower() for d in args[4].split(",") if d.strip()]
        days = sorted({WEEKDAY_MAP[d] for d in day_tokens if d in WEEKDAY_MAP})
        if not days:
            await update.message.reply_text("❌ Couldn't understand the days. Use e.g. <code>Mon,Wed,Fri</code>.", parse_mode="HTML")
            return

        try:
            datetime.time.fromisoformat(args[5])
        except ValueError:
            await update.message.reply_text("❌ start_time must be HH:MM in 24-hour UTC, e.g. <code>15:00</code>.", parse_mode="HTML")
            return
        session_start = args[5]

        try:
            duration_minutes = int(args[6])
        except ValueError:
            await update.message.reply_text("❌ duration_minutes must be a number.", parse_mode="HTML")
            return

        if start_id > end_id:
            start_id, end_id = end_id, start_id

        display_name = " ".join(args[7:]).strip() or f"autodeliver_{source_id}_{dest_id}"

        db.save_content_pool(
            pair_name=display_name,
            source_chat_id=source_id,
            dest_chat_id=dest_id,
            start_id=start_id,
            end_id=end_id,
            session_days=json.dumps(days),
            session_start=session_start,
            session_duration_minutes=duration_minutes,
            notify_chat_id=update.effective_chat.id,
        )

        days_str = ", ".join(WEEKDAY_NAMES[d] for d in days)
        await update.message.reply_text(
            f"✅ <b>Scheduled!</b>\n\n"
            f"📚 <b>{display_name}</b>\n"
            f"📤 FROM: <code>{source_id}</code> (messages #{start_id}–{end_id})\n"
            f"📥 TO: <code>{dest_id}</code>\n"
            f"🗓️ Every <b>{days_str}</b>, starting <b>{session_start} UTC</b>, for <b>{duration_minutes} min</b>\n\n"
            f"It starts automatically at the next scheduled time — no need to trigger it yourself. "
            f"You'll get a message here when each session starts and finishes.",
            parse_mode="HTML",
        )

    async def stopautodeliver_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """View active weekly auto-delivery schedules, or cancel one by name."""
        user = update.effective_user
        if user:
            self.is_user_admin(user.id)

        args = context.args
        if not args:
            pools = db.get_all_content_pools()
            if not pools:
                await update.message.reply_text("No auto-delivery schedules configured. Use /autodeliver to set one up.")
                return
            lines = ["🗓️ <b>Active Auto-Delivery Schedules:</b>\n"]
            for p in pools:
                remaining = max(p["end_id"] - p["cursor_id"] + 1, 0)
                status = "✅ Complete" if remaining == 0 else f"{remaining} messages remaining"
                lines.append(f"• <b>{p['pair_name']}</b>: {status}")
            lines.append("\nUse <code>/stopautodeliver Name</code> to cancel one.")
            await update.message.reply_text("\n".join(lines), parse_mode="HTML")
            return

        name = " ".join(args).strip()
        if db.delete_content_pool(name):
            await update.message.reply_text(f"🗑️ Stopped auto-delivery for <b>{name}</b>.", parse_mode="HTML")
        else:
            await update.message.reply_text(f"❌ No auto-delivery schedule named '<b>{name}</b>' found.", parse_mode="HTML")

    # ------------------------------------------------------------------
    # Userbot-powered commands: reading old channels' real history/timing
    # (something the Bot API fundamentally cannot do) and replaying it with
    # authentic pacing, all controllable from Telegram itself - no separate
    # CLI/terminal needed since you're already chatting with the bot on your phone.
    # ------------------------------------------------------------------

    async def userbotlogin_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Step 1: request a Telegram login code for the userbot account that can read old channel history."""
        user = update.effective_user
        if user:
            self.is_user_admin(user.id)

        args = context.args
        if not args:
            await update.message.reply_text(
                "🔐 <b>Connect your account to read old channel history:</b>\n\n"
                "Syntax: <code>/userbotlogin +2348012345678</code>\n\n"
                "<i>Requires TELEGRAM_API_ID and TELEGRAM_API_HASH to be set on the server "
                "(free from my.telegram.org). This is separate from your bot token - it lets "
                "the bot read the real content/timestamps of messages already sitting in an "
                "old channel, which the Bot API alone cannot do.</i>",
                parse_mode="HTML",
            )
            return

        phone = args[0]
        try:
            phone_code_hash = await ub.request_login_code(phone)
            token = secrets.token_urlsafe(12)
            with open(USERBOT_LOGIN_STATE_FILE, "w") as f:
                json.dump({"phone": phone, "phone_code_hash": phone_code_hash, "token": token}, f)

            render_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
            if render_url:
                verify_link = f"{render_url}/verify?token={token}"
                await update.message.reply_text(
                    f"📲 Login code sent to <b>{phone}</b>!\n\n"
                    f"⚠️ <b>Do NOT type the code here in Telegram</b> - Telegram automatically blocks a "
                    f"login code the instant it's typed into any Telegram message, even one sent to this bot.\n\n"
                    f"Instead, open this link in your phone's browser and enter the code there:\n"
                    f"{verify_link}",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            else:
                await update.message.reply_text(
                    f"📲 Login code sent to <b>{phone}</b>!\n\n"
                    f"⚠️ <b>Do NOT type the code here in Telegram</b> - it will get auto-blocked. "
                    f"RENDER_EXTERNAL_URL isn't set on the server, so I can't give you a direct link - "
                    f"set that env var (Render sets it automatically for web services) and try "
                    f"/userbotlogin again, or complete this via <code>migrate_cli.py login-verify</code> "
                    f"in a terminal instead.",
                    parse_mode="HTML",
                )
        except Exception as e:
            await update.message.reply_text(f"❌ Could not send login code: {e}")

    async def userbotverify_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Deliberately does NOT attempt sign-in from chat: Telegram auto-invalidates any login
        code the instant it appears in a Telegram message (even one sent to this bot), so trying
        here would just burn the code. Points to the /verify web page instead, where the code
        never touches Telegram's messaging layer at all."""
        user = update.effective_user
        if user:
            self.is_user_admin(user.id)

        if not os.path.exists(USERBOT_LOGIN_STATE_FILE):
            await update.message.reply_text("❌ No pending login request. Start with <code>/userbotlogin +yourphone</code> first.", parse_mode="HTML")
            return

        try:
            with open(USERBOT_LOGIN_STATE_FILE) as f:
                state = json.load(f)
        except Exception:
            await update.message.reply_text("❌ Could not read the pending login. Try <code>/userbotlogin +yourphone</code> again.", parse_mode="HTML")
            return

        render_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
        token = state.get("token")
        if render_url and token:
            await update.message.reply_text(
                f"⚠️ <b>Don't type your code here in Telegram</b> - Telegram auto-blocks a login code "
                f"the instant it appears in any Telegram message, even one sent to this bot, and the code "
                f"gets burned even if you got it right. Use this link in your phone's browser instead:\n\n"
                f"{render_url}/verify?token={token}",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        else:
            await update.message.reply_text(
                "⚠️ Typing your code here will get it auto-blocked by Telegram. RENDER_EXTERNAL_URL "
                "isn't set on the server, so complete this with "
                "<code>python migrate_cli.py login-verify &lt;code&gt;</code> in a terminal instead.",
                parse_mode="HTML",
            )

    async def mychannels_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List every channel/group the logged-in userbot account can see, with exact chat IDs."""
        user = update.effective_user
        if user:
            self.is_user_admin(user.id)

        if not await ub.is_logged_in():
            await update.message.reply_text("❌ Not connected yet. Run <code>/userbotlogin +yourphone</code> first.", parse_mode="HTML")
            return

        await update.message.reply_text("🔍 Fetching your channels...")
        try:
            channels = await ub.list_channels()
        except Exception as e:
            await update.message.reply_text(f"❌ Could not list channels: {e}")
            return

        if not channels:
            await update.message.reply_text("No channels/groups found.")
            return

        lines = ["📚 <b>Your Channels & Groups:</b>\n"]
        for c in channels:
            uname = f" (@{c.username})" if c.username else ""
            lines.append(f"• <code>{c.id}</code> [{c.kind}] {c.title}{uname}")

        # Telegram messages cap at ~4096 chars - send in chunks if needed
        full_text = "\n".join(lines)
        for i in range(0, len(full_text), 3500):
            await update.message.reply_text(full_text[i:i + 3500], parse_mode="HTML")

    async def analyzechannel_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Test/inspect a channel before migrating it: message count, media volume, detected class sessions."""
        user = update.effective_user
        if user:
            self.is_user_admin(user.id)

        args = context.args
        if not args:
            await update.message.reply_text(
                "ℹ️ Usage: <code>/analyzechannel channel_id [gap_minutes]</code>\n"
                "Example: <code>/analyzechannel -1001234567890 90</code>",
                parse_mode="HTML",
            )
            return

        if not await ub.is_logged_in():
            await update.message.reply_text("❌ Not connected yet. Run <code>/userbotlogin +yourphone</code> first.", parse_mode="HTML")
            return

        try:
            channel_id = int(args[0])
        except ValueError:
            await update.message.reply_text("❌ channel_id must be a numeric chat ID.")
            return
        gap_minutes = float(args[1]) if len(args) > 1 else 90.0

        await update.message.reply_text(f"🔍 Reading history for <code>{channel_id}</code>... this can take a while for large channels.", parse_mode="HTML")

        try:
            messages = await ub.fetch_channel_history(channel_id)
            stats = ub.analyze_channel(messages, gap_minutes=gap_minutes)
        except Exception as e:
            await update.message.reply_text(f"❌ Could not analyze channel: {e}")
            return

        if stats["message_count"] == 0:
            await update.message.reply_text("No messages found (check the ID and that your account is a member).")
            return

        lines = [
            f"📊 <b>Channel Analysis: {channel_id}</b>\n",
            f"Messages: <b>{stats['message_count']}</b>",
            f"Date range: {stats['date_range'][0]} → {stats['date_range'][1]}",
            f"Media messages: {stats['media_count']}",
            f"Approx text volume: ~{stats['estimated_tokens']} tokens",
            f"Detected sessions (gap &gt; {gap_minutes:.0f}min): <b>{stats['session_count']}</b>\n",
        ]
        for i, s in enumerate(stats["sessions"][:20]):
            lines.append(
                f"  [{i}] {s['start']} → {s['end']} "
                f"({s['message_count']} msgs, {s['media_count']} media, {s['duration_minutes']}min)"
            )
        if stats["session_count"] > 20:
            lines.append(f"  ... and {stats['session_count'] - 20} more sessions")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def previewtiming_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Preview the real message-by-message timing of one detected session before replaying it."""
        user = update.effective_user
        if user:
            self.is_user_admin(user.id)

        args = context.args
        if not args:
            await update.message.reply_text(
                "ℹ️ Usage: <code>/previewtiming channel_id [session_index] [gap_minutes]</code>\n"
                "Example: <code>/previewtiming -1001234567890 0 90</code>",
                parse_mode="HTML",
            )
            return

        if not await ub.is_logged_in():
            await update.message.reply_text("❌ Not connected yet. Run <code>/userbotlogin +yourphone</code> first.", parse_mode="HTML")
            return

        try:
            channel_id = int(args[0])
            session_index = int(args[1]) if len(args) > 1 else 0
            gap_minutes = float(args[2]) if len(args) > 2 else 90.0
        except ValueError:
            await update.message.reply_text("❌ channel_id and session_index must be numbers.")
            return

        try:
            messages = await ub.fetch_channel_history(channel_id)
            sessions = ub.group_into_sessions(messages, gap_minutes=gap_minutes)
        except Exception as e:
            await update.message.reply_text(f"❌ Could not read channel: {e}")
            return

        if not sessions:
            await update.message.reply_text("No messages found in this channel.")
            return
        if session_index >= len(sessions):
            await update.message.reply_text(f"❌ Only {len(sessions)} session(s) detected (valid index range: 0-{len(sessions) - 1}).")
            return

        session = sessions[session_index]
        deltas = session.deltas_seconds
        lines = [
            f"⏱️ <b>Session [{session_index}] Timing:</b> {session.start_date.strftime('%Y-%m-%d %H:%M')} "
            f"→ {session.end_date.strftime('%Y-%m-%d %H:%M')}\n"
            f"{len(session.messages)} messages, {session.duration_seconds / 60:.1f} min total\n",
        ]
        preview = list(zip(session.messages, deltas))
        head = preview[:15]
        tail = preview[-5:] if len(preview) > 20 else []
        for msg, delta in head:
            kind = "🖼️" if msg.has_media else "📝"
            lines.append(f"  +{delta:>6.0f}s {kind} msg #{msg.id}")
        if tail:
            lines.append(f"  ... {len(preview) - 20} more messages ...")
            for msg, delta in tail:
                kind = "🖼️" if msg.has_media else "📝"
                lines.append(f"  +{delta:>6.0f}s {kind} msg #{msg.id}")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def testquiz_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Preview exactly what quiz-recovery would produce for one specific poll message -
        verifies the real question/options/correct-answer are readable before trusting this
        across a whole migration, without posting anything anywhere."""
        user = update.effective_user
        if user:
            self.is_user_admin(user.id)

        args = context.args
        usage = (
            "🧪 <b>Test quiz recovery on one message:</b>\n\n"
            "Easiest way — just paste the message's <b>Copy Message Link</b> straight from Telegram:\n"
            "<code>/testquiz https://t.me/c/1234567890/545</code>\n\n"
            "Or give the channel ID and message ID directly:\n"
            "<code>/testquiz channel_id message_id</code>\n"
            "Example: <code>/testquiz -1001234567890 545</code>\n\n"
            "Reads that message and shows exactly what would be recovered - question, options, "
            "and correct answer - without posting anything. Use this to confirm quiz recovery "
            "works on your real content before running a full migration."
        )

        if not args:
            await update.message.reply_text(usage, parse_mode="HTML")
            return

        if not await ub.is_logged_in():
            await update.message.reply_text("❌ Not connected yet. Run <code>/userbotlogin +yourphone</code> first.", parse_mode="HTML")
            return

        channel_id: Optional[int] = None
        message_id: Optional[int] = None

        if len(args) == 1:
            try:
                resolved = await self._resolve_message_link(args[0], context)
            except ValueError as e:
                await update.message.reply_text(f"❌ {e}")
                return
            if resolved:
                channel_id, message_id, _title = resolved

        if channel_id is None or message_id is None:
            if len(args) >= 2:
                try:
                    channel_id = int(args[0])
                    message_id = int(args[1])
                except ValueError:
                    channel_id = None

        if channel_id is None or message_id is None:
            await update.message.reply_text(
                "❌ Couldn't understand that. Paste a message link, or give "
                "<code>channel_id message_id</code> as plain numbers.\n\n" + usage,
                parse_mode="HTML",
            )
            return

        await update.message.reply_text("🔍 Reading and testing recovery on that message...")

        try:
            quiz_data = await ub.get_quiz_poll_data(channel_id, message_id)
        except Exception as e:
            await update.message.reply_text(f"❌ Error reading message: {e}")
            return

        if quiz_data is None:
            try:
                reason = await ub.describe_message(channel_id, message_id)
            except Exception as e:
                reason = f"(couldn't get more detail: {e})"
            await update.message.reply_text(
                f"❌ That's not readable as a quiz poll.\n\n🔎 <b>What it actually is:</b> {reason}",
                parse_mode="HTML",
            )
            return

        lines = [f"🧠 <b>Quiz Recovery Test — message #{message_id}</b>\n", f"❓ <b>{quiz_data.question}</b>\n"]
        for i, opt in enumerate(quiz_data.options):
            marker = "✅" if quiz_data.correct_option_id == i else "▫️"
            lines.append(f"{marker} {opt}")
        if quiz_data.explanation:
            lines.append(f"\n💡 <i>{quiz_data.explanation}</i>")

        if quiz_data.correct_option_id is None:
            lines.append(
                "\n\n⚠️ <b>Correct answer not recoverable.</b> This shouldn't normally happen — "
                "either the vote-to-reveal attempt failed, or the poll has an unusual state. "
                "This specific quiz would be skipped (not recreated) during a real migration."
            )
        else:
            lines.append("\n\n✅ This is exactly what would be recreated on the destination channel during a real migration.")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def timedbackfill_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Replay a detected session's REAL original message timing into a destination channel,
        so the delivery feels like the live class actually happening rather than a bulk dump."""
        user = update.effective_user
        if user:
            self.is_user_admin(user.id)

        args = context.args
        if not args or len(args) < 3:
            await update.message.reply_text(
                "ℹ️ <b>Usage:</b>\n"
                "<code>/timedbackfill source_id dest_id session_index [gap_minutes] [max_gap_seconds]</code>\n\n"
                "<b>Example:</b>\n"
                "<code>/timedbackfill -1001234567890 -1009876543210 0 90 1200</code>\n\n"
                "<i>Use /analyzechannel first to see how many sessions are detected, and "
                "/previewtiming to check a session's rhythm before running it for real.</i>",
                parse_mode="HTML",
            )
            return

        if not await ub.is_logged_in():
            await update.message.reply_text("❌ Not connected yet. Run <code>/userbotlogin +yourphone</code> first.", parse_mode="HTML")
            return

        try:
            source_id = int(args[0])
            dest_id = int(args[1])
            session_index = int(args[2])
            gap_minutes = float(args[3]) if len(args) > 3 else 90.0
            max_gap_seconds = float(args[4]) if len(args) > 4 else 1200.0
        except ValueError:
            await update.message.reply_text("❌ source_id, dest_id, and session_index must be numbers.")
            return

        try:
            messages = await ub.fetch_channel_history(source_id)
            sessions = ub.group_into_sessions(messages, gap_minutes=gap_minutes)
        except Exception as e:
            await update.message.reply_text(f"❌ Could not read source channel: {e}")
            return

        if not sessions:
            await update.message.reply_text("No messages found in this channel.")
            return
        if session_index >= len(sessions):
            await update.message.reply_text(f"❌ Only {len(sessions)} session(s) detected (valid index range: 0-{len(sessions) - 1}).")
            return

        session = sessions[session_index]
        await update.message.reply_text(
            f"📦 <b>Timed replay started!</b>\n"
            f"Session [{session_index}]: {len(session.messages)} messages from <code>{source_id}</code> "
            f"→ <code>{dest_id}</code>, using the class's real original pacing (capped at {max_gap_seconds:.0f}s/gap).\n\n"
            f"You'll get progress updates here.",
            parse_mode="HTML",
        )

        asyncio.create_task(
            self._run_timed_backfill_from_userbot(
                session, source_id, dest_id, session_index, max_gap_seconds, update.effective_chat.id, context.application
            )
        )

    async def _run_timed_backfill_from_userbot(
        self, session: ub.Session, source_id: int, dest_id: int, session_index: int,
        max_gap_seconds: float, notify_chat_id: int, application,
    ) -> None:
        """Worker that replays one userbot-derived session with its real timing, posting via the
        bot's own copy_message so media stays intact - resume-safe against message_mappings."""
        pair_name = f"userbot_{source_id}_{dest_id}_s{session_index}"
        deltas = session.deltas_seconds
        copied = 0
        skipped = 0
        skipped_polls = 0
        recreated_quizzes = 0
        skip_samples: List[str] = []
        recreated_samples: List[str] = []
        total = len(session.messages)

        logger.info(f"[TimedBackfill:{pair_name}] Starting replay of {total} messages.")

        for (msg, delta) in zip(session.messages, deltas):
            wait_s = min(delta, max_gap_seconds)
            if wait_s > 0:
                await asyncio.sleep(wait_s)

            already = db.get_dest_msg_id(pair_name, msg.id)
            if already:
                skipped += 1
                continue

            try:
                result, recreated, quiz_q = await self._copy_or_recover_quiz(application, source_id, dest_id, msg.id)
                if result:
                    db.save_message_mapping(pair_name, msg.id, result.message_id)
                    copied += 1
                    if recreated:
                        recreated_quizzes += 1
                        if len(recreated_samples) < 5:
                            recreated_samples.append(f"#{msg.id}: {quiz_q[:70]}")
            except BadRequest as e:
                skipped += 1
                if self._is_poll_copy_restriction(e):
                    skipped_polls += 1
                elif len(skip_samples) < 10:
                    skip_samples.append(f"#{msg.id}: {str(e)[:80]}")
                logger.debug(f"[TimedBackfill:{pair_name}] Skipped ID {msg.id}: {e}")
            except Exception as e:
                skipped += 1
                logger.warning(f"[TimedBackfill:{pair_name}] Failed ID {msg.id}: {e}")

            done = copied + skipped
            if done % 20 == 0 or done == total:
                try:
                    await application.bot.send_message(
                        notify_chat_id,
                        f"⏳ <b>Timed replay progress:</b> {done}/{total} (✅ {copied} copied, ⏭️ {skipped} skipped)",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

        logger.info(f"[TimedBackfill:{pair_name}] Finished. Copied={copied} Skipped={skipped}")
        poll_note = ""
        if recreated_quizzes:
            poll_note += f"\n\n🧠 <b>{recreated_quizzes} quiz poll(s) recreated</b> with their real question, options, and correct answer."
        if skipped_polls:
            poll_note += (
                f"\n\n📊 <b>{skipped_polls} quiz poll(s) skipped</b> — Telegram won't let this bot "
                f"copy them and the answer couldn't be recovered. "
                f"Use <code>/quiz ClassName</code> to generate fresh AI practice quizzes instead."
            )
        recreated_note = f"\n\n<b>Recovered:</b>\n<code>{chr(10).join(recreated_samples)}</code>" if recreated_samples else ""
        samples_note = f"\n\n<code>{chr(10).join(skip_samples)}</code>" if skip_samples else ""
        try:
            await application.bot.send_message(
                notify_chat_id,
                f"🎉 <b>Timed replay complete!</b>\n\n✅ Copied: {copied}\n⏭️ Skipped: {skipped}{poll_note}{recreated_note}{samples_note}",
                parse_mode="HTML",
            )
        except Exception:
            pass

    async def handle_admin_conversational_chat(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Conversational AI Manager Co-Pilot Handler: Understands natural English, adds/deletes custom classes, and executes actions."""
        message = update.effective_message
        user = update.effective_user

        if not message or not user or not message.text:
            return

        self.is_user_admin(user.id)

        classes_summary = self.get_classes_summary()
        ai_reply = ai_enhancer.process_admin_conversational_assistant(
            user_message=message.text,
            class_pairs_summary=classes_summary,
            is_admin=True,
        )

        # Extract and execute hidden <ACTION> JSON payloads
        action_matches = re.findall(r'<ACTION>(.*?)</ACTION>', ai_reply, re.DOTALL)
        for act_str in action_matches:
            try:
                act_data = json.loads(act_str.strip())
                act_type = act_data.get("type")
                target_name = act_data.get("name") or act_data.get("class")
                target_pair = self.find_pair_by_name(target_name) if target_name else None

                if act_type == "addclass" and target_name:
                    s_id = int(act_data["source_chat_id"])
                    d_id = int(act_data["destination_chat_id"])
                    disc_id = int(act_data["discussion_chat_id"]) if act_data.get("discussion_chat_id") else None
                    db.save_dynamic_class(name=target_name, source_chat_id=s_id, destination_chat_id=d_id, discussion_chat_id=disc_id)
                    self.sync_classes_from_db()
                    logger.info(f"Conversational AI created class {target_name}")

                elif act_type == "deleteclass" and target_name:
                    db.delete_dynamic_class(target_name)
                    self.sync_classes_from_db()
                    logger.info(f"Conversational AI deleted class {target_name}")

                elif act_type == "activate" and target_pair:
                    target_pair.manual_override_active = bool(act_data.get("value", True))
                    logger.info(f"Conversational AI executed activate for {target_pair.name}: {target_pair.manual_override_active}")

                elif act_type == "setdelay" and target_pair:
                    val = float(act_data.get("value", 180))
                    target_pair.delay_min_seconds = val
                    target_pair.delay_max_seconds = val
                    db.save_dynamic_class(
                        name=target_pair.name,
                        source_chat_id=target_pair.source_chat_id,
                        destination_chat_id=target_pair.destination_chat_id,
                        discussion_chat_id=target_pair.discussion_chat_id,
                        delay_min_seconds=val,
                        delay_max_seconds=val,
                    )
                    logger.info(f"Conversational AI executed setdelay for {target_pair.name}: {val}s")

                elif act_type == "setprompt" and target_pair:
                    p_text = str(act_data.get("value", ""))
                    db.save_custom_prompt(target_pair.name, p_text)
                    logger.info(f"Conversational AI executed setprompt for {target_pair.name}: '{p_text}'")

                elif act_type == "quiz" and target_pair:
                    await self.post_class_quiz(target_pair, context)

                elif act_type == "logs":
                    await self.logs_command(update, context)
                    return

                elif act_type == "listchannels":
                    context.args = []
                    await self.mychannels_command(update, context)
                    return

                elif act_type == "analyzechannel" and act_data.get("channel_id"):
                    args = [str(act_data["channel_id"])]
                    if act_data.get("gap_minutes"):
                        args.append(str(act_data["gap_minutes"]))
                    context.args = args
                    await self.analyzechannel_command(update, context)
                    return

                elif act_type == "previewtiming" and act_data.get("channel_id"):
                    args = [str(act_data["channel_id"]), str(act_data.get("session_index", 0))]
                    if act_data.get("gap_minutes"):
                        args.append(str(act_data["gap_minutes"]))
                    context.args = args
                    await self.previewtiming_command(update, context)
                    return

                elif act_type == "backfill" and act_data.get("source_id") and act_data.get("dest_id"):
                    context.args = [str(act_data["source_id"]), str(act_data["dest_id"]), str(act_data["start_id"]), str(act_data["end_id"])]
                    await self.backfill_command(update, context)
                    return

                elif act_type == "backfill" and target_name:
                    context.args = target_name.split() + [str(act_data["start_id"]), str(act_data["end_id"])]
                    await self.backfill_command(update, context)
                    return

                elif act_type == "timedbackfill" and act_data.get("source_id") and act_data.get("dest_id") is not None:
                    args = [str(act_data["source_id"]), str(act_data["dest_id"]), str(act_data.get("session_index", 0))]
                    if act_data.get("gap_minutes"):
                        args.append(str(act_data["gap_minutes"]))
                        if act_data.get("max_gap_seconds"):
                            args.append(str(act_data["max_gap_seconds"]))
                    context.args = args
                    await self.timedbackfill_command(update, context)
                    return

                elif act_type == "autodeliver" and act_data.get("source_id") and act_data.get("dest_id") is not None:
                    args = [
                        str(act_data["source_id"]), str(act_data["dest_id"]),
                        str(act_data["start_id"]), str(act_data["end_id"]),
                        str(act_data["days"]), str(act_data["start_time"]), str(act_data["duration_minutes"]),
                    ]
                    if act_data.get("name"):
                        args.append(str(act_data["name"]))
                    context.args = args
                    await self.autodeliver_command(update, context)
                    return

                elif act_type == "stopautodeliver":
                    context.args = [act_data["name"]] if act_data.get("name") else []
                    await self.stopautodeliver_command(update, context)
                    return

            except Exception as e:
                logger.error(f"Error parsing conversational AI action payload: {e}")

        # Strip hidden <ACTION> tags before sending conversational message to user
        clean_text = re.sub(r'<ACTION>.*?</ACTION>', '', ai_reply, flags=re.DOTALL).strip()
        await message.reply_text(clean_text, parse_mode="HTML")

    async def _ingest_migration_ref(self, user_id: int, chat_id: int, title: str, msg_id: int) -> str:
        """Shared state machine behind both forwarding and pasting a 'Copy Message Link':
        collects (source start, source end, destination) one message at a time and reports
        back what to send next, ending in a ready-to-confirm /backfill."""
        refs = self.pending_migration_refs.setdefault(user_id, {})

        if "source" not in refs:
            refs["source"] = {"chat_id": chat_id, "title": title}
            refs["start_id"] = msg_id
            return (
                f"📌 Got it — <b>{title}</b> (<code>{chat_id}</code>), starting from message #{msg_id}.\n\n"
                f"Now send me the LAST message you want migrated from this same old channel "
                f"(forward it, or paste its 'Copy Message Link')."
            )

        if chat_id == refs["source"]["chat_id"] and "end_id" not in refs:
            refs["end_id"] = msg_id
            return (
                f"📌 Got the end point — message #{msg_id}.\n\n"
                f"Now send me any message from the NEW channel you want this copied into."
            )

        if "dest" not in refs:
            refs["dest"] = {"chat_id": chat_id, "title": title}
            refs["ready"] = True
            start = refs["start_id"]
            end = refs.get("end_id", start)
            return (
                f"✅ <b>Got everything I need!</b>\n\n"
                f"📤 FROM: <b>{refs['source']['title']}</b> (<code>{refs['source']['chat_id']}</code>)\n"
                f"📥 TO: <b>{title}</b> (<code>{chat_id}</code>)\n"
                f"Range: messages #{start}–{end}\n\n"
                f"Reply <b>yes</b> to migrate this right now, <b>schedule</b> to set up a recurring "
                f"weekly delivery instead, or send a different message to start over."
            )

        self.pending_migration_refs[user_id] = {"source": {"chat_id": chat_id, "title": title}, "start_id": msg_id}
        return (
            f"🔄 Starting a fresh migration reference: <b>{title}</b>, message #{msg_id}. "
            f"Send the LAST message from this channel next."
        )

    @staticmethod
    def _parse_duration_minutes(text: str) -> Optional[int]:
        """Accepts plain minutes ('120'), hours ('2 hours', '2h', '1.5h'), or minutes spelled out
        ('90 minutes', '90 mins', '90m') - deterministic, no AI call needed for this one."""
        text = text.strip().lower()
        try:
            return int(text)
        except ValueError:
            pass
        m = re.search(r'(\d+(?:\.\d+)?)\s*h', text)
        if m:
            return round(float(m.group(1)) * 60)
        m = re.search(r'(\d+(?:\.\d+)?)\s*m', text)
        if m:
            return round(float(m.group(1)))
        return None

    async def _handle_schedule_wizard_reply(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str
    ) -> None:
        """Guided, step-by-step setup for a weekly auto-delivery schedule: asks for days, then
        time+timezone (converted to UTC via AI), then session length, then a label - explicitly
        confirming everything understood before anything gets scheduled."""
        message = update.effective_message
        wiz = self.pending_schedule_wizard.get(user_id)
        if not wiz:
            return
        text = text.strip()
        step = wiz["step"]

        if step == "days":
            if text.lower() in ("every day", "everyday", "daily", "all", "all week"):
                days = list(range(7))
            else:
                tokens = [d.strip().lower() for d in text.split(",") if d.strip()]
                days = sorted({WEEKDAY_MAP[d] for d in tokens if d in WEEKDAY_MAP})
            if not days:
                await message.reply_text(
                    "❌ I didn't recognize those days. Try e.g. <code>Mon,Wed,Fri</code> or 'every day'.",
                    parse_mode="HTML",
                )
                return
            wiz["days"] = days
            wiz["step"] = "time"
            await message.reply_text(
                "🕐 What time should each session start, and what timezone are you in?\n"
                "e.g. <i>'4pm WAT'</i>, <i>'16:00 GMT+1'</i>, <i>'10am Lagos time'</i>",
                parse_mode="HTML",
            )
            return

        if step == "time":
            utc_time = ai_enhancer.convert_local_time_to_utc(text)
            if not utc_time:
                await message.reply_text(
                    "❌ I couldn't pin down a specific time from that. Try again, e.g. <i>'4pm WAT'</i> "
                    "or <i>'16:00 UTC'</i>.",
                    parse_mode="HTML",
                )
                return
            wiz["start_time"] = utc_time
            wiz["local_time_text"] = text
            wiz["step"] = "duration"
            await message.reply_text(
                f"✅ Got it — that's <b>{utc_time} UTC</b>.\n\n"
                f"How long should each session run? e.g. <code>120</code> or <i>'2 hours'</i>",
                parse_mode="HTML",
            )
            return

        if step == "duration":
            duration = self._parse_duration_minutes(text)
            if not duration:
                await message.reply_text(
                    "❌ Couldn't read that as a duration. Try e.g. <code>120</code> or <i>'2 hours'</i>.",
                    parse_mode="HTML",
                )
                return
            wiz["duration_minutes"] = duration
            wiz["step"] = "name"
            await message.reply_text(
                "📚 What should I call this schedule? (or reply <b>skip</b> for an auto-generated name)",
                parse_mode="HTML",
            )
            return

        if step == "name":
            wiz["name"] = None if text.lower() == "skip" else text
            wiz["step"] = "confirm"
            days_str = ", ".join(WEEKDAY_NAMES[d] for d in wiz["days"])
            name_display = wiz["name"] or f"autodeliver_{wiz['source']['chat_id']}_{wiz['dest']['chat_id']}"
            await message.reply_text(
                f"✅ <b>Here's what I've got — please confirm:</b>\n\n"
                f"📚 <b>{name_display}</b>\n"
                f"📤 FROM: <b>{wiz['source']['title']}</b> (<code>{wiz['source']['chat_id']}</code>)\n"
                f"📥 TO: <b>{wiz['dest']['title']}</b> (<code>{wiz['dest']['chat_id']}</code>)\n"
                f"Messages: #{wiz['start_id']}–{wiz.get('end_id', wiz['start_id'])}\n"
                f"🗓️ Every <b>{days_str}</b>, starting <b>{wiz['start_time']} UTC</b> "
                f"(from \"{wiz['local_time_text']}\"), for <b>{wiz['duration_minutes']} min</b>\n\n"
                f"Reply <b>yes</b> to schedule this, or anything else to cancel.",
                parse_mode="HTML",
            )
            return

        if step == "confirm":
            if text.lower() not in ("yes", "y", "confirm", "go", "yes please"):
                self.pending_schedule_wizard.pop(user_id, None)
                await message.reply_text("Cancelled — nothing was scheduled.")
                return

            days_str = ",".join(WEEKDAY_NAMES[d] for d in wiz["days"])
            args = [
                str(wiz["source"]["chat_id"]), str(wiz["dest"]["chat_id"]),
                str(wiz["start_id"]), str(wiz.get("end_id", wiz["start_id"])),
                days_str, wiz["start_time"], str(wiz["duration_minutes"]),
            ]
            if wiz.get("name"):
                args.append(wiz["name"])
            context.args = args
            self.pending_schedule_wizard.pop(user_id, None)
            await self.autodeliver_command(update, context)
            return

    async def handle_forwarded_reference(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """No IDs to hunt for, no login needed: forward a message from the old channel (twice, to
        mark the start and end of the range), then one from the new channel, and this builds the
        exact backfill for you - reusing the plain Bot API /backfill under the hood."""
        message = update.effective_message
        user = update.effective_user
        origin = message.forward_origin

        if not isinstance(origin, MessageOriginChannel):
            await message.reply_text(
                "🤔 I can read forwards from channels, but couldn't find the original channel/message "
                "on this one (maybe it's not from a channel, or the channel hides forward info). Try "
                "forwarding an actual post from the channel, or paste its 'Copy Message Link' instead."
            )
            return

        reply = await self._ingest_migration_ref(user.id, origin.chat.id, origin.chat.title, origin.message_id)
        await message.reply_text(reply, parse_mode="HTML")

    TG_MESSAGE_LINK_RE = re.compile(
        r"(?:https?://)?t\.me/(?:c/(?P<internal_id>\d+)|(?P<username>[A-Za-z0-9_]{5,32}))/(?P<msg_id>\d+)"
    )

    async def _resolve_message_link(self, link_text: str, context: ContextTypes.DEFAULT_TYPE):
        """Parses a pasted 'Copy Message Link' (e.g. https://t.me/c/1234567890/1500 or
        https://t.me/somepublicchannel/1500) into (chat_id, msg_id, title). Returns None if the
        text doesn't contain a recognizable link. Raises ValueError if a public @username link
        can't be resolved (bot not a member of that channel)."""
        match = self.TG_MESSAGE_LINK_RE.search(link_text or "")
        if not match:
            return None

        msg_id = int(match.group("msg_id"))
        if match.group("internal_id"):
            chat_id = int(f"-100{match.group('internal_id')}")
            title = str(chat_id)
            try:
                chat = await context.bot.get_chat(chat_id)
                if chat and chat.title:
                    title = chat.title
            except Exception:
                pass
        else:
            username = match.group("username")
            try:
                chat = await context.bot.get_chat(f"@{username}")
                chat_id = chat.id
                title = chat.title or username
            except Exception as e:
                raise ValueError(
                    f"Couldn't look up @{username} - make sure the bot is added to that channel as an admin. ({e})"
                )

        return chat_id, msg_id, title

    async def handle_message_link_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> bool:
        """Parses a pasted 'Copy Message Link' directly - the simplest option, since long-press
        -> Copy Message Link is something people already know how to do, and the link alone
        already carries both the channel and message ID. Returns True if the text was a message
        link and has been handled (as a migration reference - see _ingest_migration_ref)."""
        message = update.effective_message
        user = update.effective_user

        try:
            resolved = await self._resolve_message_link(message.text or "", context)
        except ValueError as e:
            await message.reply_text(f"❌ {e}")
            return True
        if not resolved:
            return False

        chat_id, msg_id, title = resolved
        reply = await self._ingest_migration_ref(user.id, chat_id, title, msg_id)
        await message.reply_text(reply, parse_mode="HTML")
        return True

    async def handle_incoming_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle incoming channel posts and messages."""
        message = update.effective_message
        chat = update.effective_chat
        user = update.effective_user

        if not message or not chat:
            return

        if chat.type in ("group", "supergroup"):
            await self.handle_discussion_qa(update, context)
            return

        if chat.type == "private" and message.forward_origin:
            await self.handle_forwarded_reference(update, context)
            return

        if chat.type == "private" and message.text and not message.text.startswith("/"):
            user_id = user.id if user else 0

            if user_id in self.pending_schedule_wizard:
                await self._handle_schedule_wizard_reply(update, context, user_id, message.text)
                return

            if await self.handle_message_link_text(update, context):
                return

            refs = self.pending_migration_refs.get(user_id)
            if refs and refs.get("ready"):
                reply_lower = message.text.strip().lower()
                if reply_lower in ("yes", "y", "confirm", "go", "start", "yes please"):
                    context.args = [
                        str(refs["source"]["chat_id"]), str(refs["dest"]["chat_id"]),
                        str(refs["start_id"]), str(refs.get("end_id", refs["start_id"])),
                    ]
                    self.pending_migration_refs.pop(user_id, None)
                    await self.backfill_command(update, context)
                    return
                if reply_lower in ("schedule", "weekly", "recurring", "every week", "auto", "autodeliver"):
                    self.pending_schedule_wizard[user_id] = {
                        "step": "days",
                        "source": refs["source"],
                        "dest": refs["dest"],
                        "start_id": refs["start_id"],
                        "end_id": refs.get("end_id", refs["start_id"]),
                    }
                    self.pending_migration_refs.pop(user_id, None)
                    await message.reply_text(
                        "🗓️ <b>Let's set up a weekly schedule.</b>\n\n"
                        "Which days should this run? e.g. <code>Mon,Wed,Fri</code> or 'every day'",
                        parse_mode="HTML",
                    )
                    return

            await self.handle_admin_conversational_chat(update, context)
            return

        matching_pairs = self.source_map.get(chat.id, [])
        if not matching_pairs:
            return

        msg_text = message.text or message.caption or ""
        reply_to_id = message.reply_to_message.message_id if message.reply_to_message else None
        has_media = bool(
            message.photo or message.video or message.document or message.audio
            or message.voice or message.animation or message.video_note or message.sticker
        )

        if message.photo and os.getenv("GEMINI_API_KEY"):
            try:
                photo_file = await context.bot.get_file(message.photo[-1].file_id)
                photo_bytes = await photo_file.download_as_bytearray()
                ocr_text = ai_enhancer.sanitize_and_ocr_image(bytes(photo_bytes))
                if ocr_text:
                    msg_text = f"{ocr_text}\n\n{msg_text}".strip()
            except Exception as e:
                logger.debug(f"Vision OCR processing note: {e}")

        sanitized_msg_text = ai_enhancer.sanitize_text(msg_text)

        for pair in matching_pairs:
            if not pair.is_active:
                logger.debug(
                    f"[Pair: {pair.name}] Ignoring message #{message.message_id} (Subject currently inactive)."
                )
                continue

            if message.message_id < pair.start_message_id:
                continue

            if message.message_id <= pair.last_processed_id:
                continue

            if message.media_group_id:
                mg_id = message.media_group_id
                if mg_id not in pair.pending_albums:
                    timer = context.application.job_queue.run_once(
                        lambda ctx: self._flush_album(pair, mg_id),
                        when=2.0,
                    )
                    pair.pending_albums[mg_id] = {
                        "message_ids": [message.message_id],
                        "job": timer,
                        "text": sanitized_msg_text,
                        "reply_to_message_id": reply_to_id,
                    }
                else:
                    pair.pending_albums[mg_id]["message_ids"].append(message.message_id)
            else:
                pair.queue.put_nowait({
                    "type": "single",
                    "message_id": message.message_id,
                    "max_id": message.message_id,
                    "text": sanitized_msg_text,
                    "reply_to_message_id": reply_to_id,
                    "has_media": has_media,
                })
                logger.info(
                    f"📥 [Pair: {pair.name}] Message #{message.message_id} queued for copying."
                )

    # ------------------------------------------------------------------
    # User-Friendly Interactive Commands & Setup Wizard
    # ------------------------------------------------------------------

    async def start_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Welcome message, bot channels location overview, and AI Setup Wizard."""
        user = update.effective_user
        if user:
            self.is_user_admin(user.id)

        active_count = sum(1 for p in self.pairs if p.is_active)
        has_ai = "🟢 Active (Gemini / DeepSeek API)" if (os.getenv("GEMINI_API_KEY") or os.getenv("DEEPSEEK_API_KEY")) else "⚪ Disabled"

        channel_locations = []
        for p in self.pairs:
            channel_locations.append(p.get_explicit_flow_description())

        locations_block = "\n\n".join(channel_locations) if channel_locations else "<i>No class pairs connected yet. Use /addclass to add your first explicit pair!</i>"

        msg = (
            f"🧙‍♂️ <b>Welcome, {user.first_name if user else 'User'}!</b>\n\n"
            f"📍 <b>Your Connected Channel Pairs:</b>\n"
            f"{locations_block}\n\n"
            f"<b>Total pairs:</b> {len(self.pairs)} | <b>Active now:</b> {active_count} | <b>AI:</b> {has_ai}\n\n"
            "📦 <b>To bring over an old channel's materials — the easy way:</b>\n"
            "1. In the OLD channel, long-press the FIRST message you want migrated → <b>Copy Message "
            "Link</b> → paste that link to me right here.\n"
            "2. Do the same for the LAST message you want migrated.\n"
            "3. Send me one message/link from the NEW channel.\n"
            "4. I'll show you what I understood — reply <b>yes</b> to run it now, or <b>schedule</b> "
            "to set it up as a recurring weekly delivery instead (I'll ask the days/time/duration "
            "step by step).\n\n"
            "That's it — no chat IDs to look up, no setup required first.\n\n"
            "💬 Or just tell me what you want in plain English, e.g. <i>\"add a class called Further "
            "Maths from source -100123 to dest -100456\"</i>.\n\n"
            "👇 <b>A few useful commands</b> (send /help for the full list):\n"
            "• /pairs - View your channel pairs\n"
            "• /addclass - Manually register a channel pair by ID\n"
            "• /status - System dashboard\n"
        )
        await update.message.reply_text(msg, parse_mode="HTML")

    async def logs_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """View recent student Q&A interaction logs across all discussion groups."""
        user = update.effective_user
        if user:
            self.is_user_admin(user.id)

        logs = db.get_recent_qa_logs(limit=10)
        if not logs:
            await update.message.reply_text("📜 <b>Student Interaction Logs:</b>\n\nNo student Q&A interactions recorded yet.", parse_mode="HTML")
            return

        lines = ["📜 <b>Recent Student Interaction Logs:</b>\n"]
        for log in logs:
            lines.append(
                f"👤 <b>{log['user_name']}</b> ({log['subject_name']}) - <i>{log['timestamp']}</i>\n"
                f"❓ <b>Q:</b> <code>{log['question']}</code>\n"
                f"💡 <b>A:</b> <i>{log['answer'][:150]}...</i>\n"
            )

        await update.message.reply_text("\n---\n".join(lines), parse_mode="HTML")

    async def post_class_quiz(self, pair: ChannelPair, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """Helper to generate and post a Telegram native quiz into the destination chat."""
        notes = db.get_recent_class_context(pair.name, limit=6)
        quiz_data = ai_enhancer.generate_class_quiz(pair.name, notes)

        if quiz_data:
            try:
                await context.bot.send_poll(
                    chat_id=pair.destination_chat_id,
                    question=f"🧠 [{pair.name} Practice Quiz] {quiz_data['question'][:250]}",
                    options=quiz_data["options"][:10],
                    type="quiz",
                    correct_option_id=quiz_data.get("correct_option_id", 0),
                    explanation=quiz_data.get("explanation", "")[:200],
                    is_anonymous=False,
                )
                logger.info(f"Posted interactive practice quiz to {pair.name}")
                return True
            except Exception as e:
                logger.error(f"Error posting poll quiz to {pair.name}: {e}")

        return False

    async def quiz_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Generate and post an interactive practice quiz for a class."""
        user = update.effective_user
        if user:
            self.is_user_admin(user.id)

        args = context.args
        if not args:
            await update.message.reply_text(
                "ℹ️ <b>How to generate a Practice Quiz:</b>\n\n"
                "<code>/quiz ClassName</code>\n"
                "Example: <code>/quiz Mathematics 1</code>",
                parse_mode="HTML",
            )
            return

        search_query = " ".join(args).strip()
        found_pair = self.find_pair_by_name(search_query)

        if not found_pair:
            await update.message.reply_text(f"❌ Class '<b>{search_query}</b>' not found.", parse_mode="HTML")
            return

        await update.message.reply_text(f"🧠 Generating AI practice quiz for <b>{found_pair.name}</b>...", parse_mode="HTML")
        success = await self.post_class_quiz(found_pair, context)

        if success:
            await update.message.reply_text(f"🎉 <b>Success!</b> Interactive practice quiz posted to <b>{found_pair.name}</b> channel!", parse_mode="HTML")
        else:
            await update.message.reply_text("❌ Failed to generate quiz. Make sure class lesson notes are recorded.", parse_mode="HTML")

    async def summary_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Generate a Weekly Master Study Guide for exam review."""
        args = context.args
        if not args:
            await update.message.reply_text(
                "ℹ️ <b>How to generate a Weekly Master Study Guide:</b>\n\n"
                "<code>/summary ClassName</code>\n"
                "Example: <code>/summary Mathematics 1</code>",
                parse_mode="HTML",
            )
            return

        search_query = " ".join(args).strip()
        found_pair = self.find_pair_by_name(search_query)

        if not found_pair:
            await update.message.reply_text(f"❌ Class '<b>{search_query}</b>' not found.", parse_mode="HTML")
            return

        await update.message.reply_text(f"📖 Generating Weekly Master Study Guide for <b>{found_pair.name}</b>...", parse_mode="HTML")
        notes = db.get_recent_class_context(found_pair.name, limit=10)
        summary_text = ai_enhancer.generate_weekly_summary(found_pair.name, notes)

        await update.message.reply_text(summary_text, parse_mode="HTML")

    async def prompt_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Set or view custom AI prompt instructions per exact class channel."""
        user = update.effective_user
        if user:
            self.is_user_admin(user.id)

        args = context.args
        if not args:
            lines = [
                "✏️ <b>Custom AI Prompt Instructions per Class Channel:</b>\n\n"
                "You can specify instructions for your class channels!\n\n"
                "<b>How to set a custom prompt:</b>\n"
                "<code>/prompt ClassName Your custom instructions here...</code>\n\n"
                "<b>Examples:</b>\n"
                "• <code>/prompt Mathematics 1 Focus on basic formulas</code>\n"
                "• <code>/prompt Mathematics 1 reset</code> <i>(Resets to default)</i>\n\n"
                "<b>Current Custom Prompts:</b>"
            ]
            for p in self.pairs:
                cp = db.get_custom_prompt(p.name)
                cp_str = f"<code>{cp}</code>" if cp else "<i>Default flow polishing</i>"
                lines.append(f"• <b>{p.name}:</b> {cp_str}")

            if not self.pairs:
                lines.append("<i>No active classes. Use /addclass to add your first explicit pair!</i>")

            await update.message.reply_text("\n".join(lines), parse_mode="HTML")
            return

        search_query = args[0]
        instruction_start_idx = 1

        if len(args) >= 2 and args[1].isdigit():
            search_query = f"{args[0]} {args[1]}"
            instruction_start_idx = 2

        found_pair = self.find_pair_by_name(search_query)

        if not found_pair:
            await update.message.reply_text(f"❌ Class channel '<b>{search_query}</b>' not found.", parse_mode="HTML")
            return

        instruction_text = " ".join(args[instruction_start_idx:]).strip()

        if not instruction_text:
            cp = db.get_custom_prompt(found_pair.name)
            cp_str = f"<code>{cp}</code>" if cp else "<i>Default flow polishing</i>"
            await update.message.reply_text(
                f"ℹ️ Custom prompt for <b>{found_pair.name}</b> is currently: {cp_str}",
                parse_mode="HTML",
            )
            return

        if instruction_text.lower() in ("reset", "default", "clear"):
            db.delete_custom_prompt(found_pair.name)
            logger.info(f"Reset custom AI prompt for {found_pair.name}")
            await update.message.reply_text(
                f"🔄 <b>Reset!</b> Custom AI prompt for <b>{found_pair.name}</b> has been reset to default flow mode.",
                parse_mode="HTML",
            )
            return

        db.save_custom_prompt(found_pair.name, instruction_text)
        logger.info(f"Saved custom AI prompt for {found_pair.name}: '{instruction_text}'")
        await update.message.reply_text(
            f"✅ <b>Updated AI Prompt Instructions!</b>\n\n"
            f"Class Channel: <b>{found_pair.name}</b>\n"
            f"New AI Instructions: <code>{instruction_text}</code>",
            parse_mode="HTML",
        )

    async def help_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Explanatory guide."""
        msg = (
            "📖 <b>Explicit Channel Pair Setup & Conversational AI Guide</b>\n\n"
            "<b>1. Add / Set Up Explicit Channel Pair:</b>\n"
            "Tell the AI in chat or use <code>/addclass ClassName source_id dest_id [discussion_id]</code>\n\n"
            "<b>2. View Explicit Pair Flows (FROM ➔ TO):</b>\n"
            "Use <code>/pairs</code> or <code>/start</code>\n\n"
            "<b>2b. Migrate Previously Posted Materials (easiest way):</b>\n"
            "New pairs only copy messages posted while the bot is running, so bringing over an old "
            "channel's materials takes one extra step. In the OLD channel, long-press the FIRST message "
            "you want migrated → <b>Copy Message Link</b> → paste that link to me here (or just forward "
            "the message instead, works the same). Do the same for the LAST message, then one message "
            "from the NEW channel. I'll show you what I understood — reply <b>yes</b> to run it. No IDs "
            "to type, no /addclass needed.\n\n"
            "<i>(Prefer typing exact numbers? <code>/backfill source_id dest_id start_id end_id</code> "
            "works too, or <code>/backfill ClassName start_id end_id</code> for an already-registered class.)</i>\n\n"
            "<b>2c. Auto-Deliver On a Weekly Schedule (no manual triggering):</b>\n"
            "After pasting the 3 links/forwards above, reply <b>schedule</b> instead of <b>yes</b> and "
            "I'll ask you the rest step by step: which days, what time (in your own words - e.g. "
            "<i>'4pm WAT'</i>, I'll convert it), how long each session should run, and what to call it - "
            "then show you exactly what I understood before setting anything up.\n"
            "<i>(Prefer one command? <code>/autodeliver source_id dest_id start_id end_id days start_time "
            "duration_minutes [Name]</code>, e.g. <code>/autodeliver -100111 -100222 1 850 Mon,Wed,Fri 15:00 "
            "120 Physics</code> - note this form needs the time already in UTC.)</i> Delivers that message "
            "range automatically, spread 2-5 min apart across the session window, picking up where it left "
            "off each time until fully delivered. Use <code>/stopautodeliver</code> to view or cancel schedules.\n\n"
            "<b>2d. Replay Old Classes With Their Real Timing (advanced):</b>\n"
            "1. <code>/userbotlogin +yourphone</code> then open the web link it gives you to enter the "
            "code (never type the code into Telegram itself - it gets auto-blocked). One-time, needs "
            "TELEGRAM_API_ID/TELEGRAM_API_HASH set on the server.\n"
            "2. <code>/mychannels</code> to find chat IDs, <code>/analyzechannel id</code> to see detected class sessions.\n"
            "3. <code>/previewtiming id session_index</code> to preview the real rhythm, then "
            "<code>/timedbackfill source_id dest_id session_index</code> to replay it for real.\n"
            "4. Quiz polls get automatically recovered with their real question/answer once connected - "
            "test one first with <code>/testquiz channel_id message_id</code>.\n\n"
            "<b>3. Delete Any Channel Pair:</b>\n"
            "Tell the AI in chat or use <code>/deleteclass ClassName</code>\n\n"
            "<b>4. Wipe All Classes to Start Fresh:</b>\n"
            "Use <code>/clearallclasses</code>\n\n"
            "<b>5. Student Interaction Logs (`/logs`):</b>\n"
            "View recent student questions and AI answers."
        )
        await update.message.reply_text(msg, parse_mode="HTML")

    async def status_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Dashboard overview."""
        uptime_sec = int(time.time() - START_TIME)
        hours, remainder = divmod(uptime_sec, 3600)
        minutes, seconds = divmod(remainder, 60)

        active_pairs = [p for p in self.pairs if p.is_active]
        total_queued = sum(p.queue.qsize() for p in self.pairs)
        has_ai = "🟢 Enabled (Gemini / DeepSeek API)" if (os.getenv("GEMINI_API_KEY") or os.getenv("DEEPSEEK_API_KEY")) else "⚪ Disabled"

        msg = (
            "⚙️ <b>Bot System Dashboard</b>\n\n"
            f"• <b>Status:</b> 🟢 Running smoothly\n"
            f"• <b>Uptime:</b> {hours}h {minutes}m {seconds}s\n"
            f"• <b>Total Explicit Channel Pairs:</b> {len(self.pairs)}\n"
            f"• 🟢 <b>Active Right Now:</b> {len(active_pairs)}\n"
            f"• 📬 <b>Messages Queued:</b> {total_queued}\n"
            f"• ⚡ <b>24/7 Keep-Alive Engine:</b> 🟢 Active (Always Awake)\n"
            f"• 🛠️ <b>Explicit Pair Setup:</b> 🟢 Enabled (`/addclass` & `/pairs`)\n"
            f"• 💬 <b>Conversational AI Co-Pilot:</b> 🟢 Enabled\n"
            f"• 🛡️ <b>Anonymization & Vision OCR:</b> 🟢 Enabled\n"
            f"• 📜 <b>Student Interaction Logs:</b> 🟢 Enabled (`/logs`)\n"
            f"• 🧠 <b>Practice Quizzes:</b> 🟢 Enabled (`/quiz`)\n"
            f"• 📖 <b>Master Study Guides:</b> 🟢 Enabled (`/summary`)\n"
            f"• 🤖 <b>AI Status:</b> {has_ai}\n"
        )
        await update.message.reply_text(msg, parse_mode="HTML")

    async def pairs_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Detailed status of all subject pairs with explicit FROM ➔ TO detail."""
        if not self.pairs:
            await update.message.reply_text("No channel pairs configured. Use `/addclass ClassName source_id dest_id` to add your first explicit pair!", parse_mode="HTML")
            return

        lines = ["📚 <b>Explicit Channel Pairs (FROM ➔ TO Flow):</b>\n"]
        for p in self.pairs:
            status_icon = "🟢 <b>ACTIVE</b>" if p.is_active else "⏳ <b>DORMANT</b>"
            q_size = p.queue.qsize()
            cp = db.get_custom_prompt(p.name)
            prompt_summary = f"Custom: <i>{cp[:30]}...</i>" if cp else f"Mode: <code>{p.ai_mode}</code>"

            if p.delay_min_seconds == p.delay_max_seconds:
                spacing_str = f"{p.delay_min_seconds:.0f} sec"
            else:
                spacing_str = f"{p.delay_min_seconds:.0f}–{p.delay_max_seconds:.0f} sec"

            disc_info = f"<code>{p.discussion_chat_id}</code> ({p.discussion_title})" if p.discussion_chat_id else "None"

            lines.append(
                f"<b>Pair: {p.name}</b>\n"
                f"• Status: {status_icon}\n"
                f"• 📤 <b>FROM (Source):</b> {p.source_title} (<code>{p.source_chat_id}</code>)\n"
                f"• 📥 <b>TO (Destination):</b> {p.destination_title} (<code>{p.destination_chat_id}</code>)\n"
                f"• 💬 <b>Discussion Group:</b> {disc_info}\n"
                f"• ⏱️ Message Spacing: <b>{spacing_str}</b>\n"
                f"• 🤖 AI Mode: {prompt_summary}\n"
                f"• 📬 Queue Backlog: {q_size} pending\n"
            )

        await update.message.reply_text("\n---\n".join(lines), parse_mode="HTML")

    async def schedule_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Display weekly class schedule timetable."""
        if not self.pairs:
            await update.message.reply_text("📅 <b>Weekly Class Schedule Timetable:</b>\n\nNo classes configured yet. Use `/addclass ClassName source_id dest_id` to add one!", parse_mode="HTML")
            return

        lines = ["📅 <b>Weekly Class Schedule Timetable:</b>\n"]
        for p in self.pairs:
            lines.append(f"<b>Class: {p.name}</b>")
            if p.schedule:
                days = ", ".join(p.schedule.get("active_days", []))
                st = p.schedule.get("start_time", "00:00")
                et = p.schedule.get("end_time", "23:59")
                lines.append(f"• 🗓️ <b>Days:</b> {days}")
                lines.append(f"• ⏰ <b>Class Hours:</b> {st} – {et}")
            elif p.activate_at:
                lines.append(f"• 🗓️ <b>One-Time Start:</b> {p.activate_at.strftime('%Y-%m-%d %H:%M UTC')}")
            else:
                lines.append("• 🗓️ <b>Schedule:</b> Always Active (24/7)")

            status_str = "🟢 Currently Active" if p.is_active else "⏳ Currently Dormant"
            lines.append(f"• Current State: {status_str}\n")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def ai_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Configure AI enhancement mode for a subject."""
        user = update.effective_user
        if user:
            self.is_user_admin(user.id)

        args = context.args
        if not args or len(args) < 2:
            await update.message.reply_text(
                "ℹ️ <b>How to set Free AI Mode for a class:</b>\n\n"
                "• <b>Flow / Polish</b> (Improves flow & readability):\n"
                "  <code>/ai Mathematics 1 flow</code>\n\n"
                "• <b>Custom Instructions</b>:\n"
                "  <code>/prompt Mathematics 1 Keep it concise with key points</code>\n\n"
                "• <b>Turn Off AI</b>:\n"
                "  <code>/ai Mathematics 1 off</code>",
                parse_mode="HTML",
            )
            return

        search_query = args[0]
        mode_idx = 1
        if len(args) >= 3 and args[1].isdigit():
            search_query = f"{args[0]} {args[1]}"
            mode_idx = 2

        mode_input = args[mode_idx].strip().lower()

        valid_modes = {"off", "flow", "polish", "paraphrase", "summarize", "hashtags"}
        if mode_input not in valid_modes:
            await update.message.reply_text(f"❌ Invalid AI mode. Choose from: <code>flow</code>, <code>off</code>, <code>paraphrase</code>, <code>summarize</code>, <code>hashtags</code>.", parse_mode="HTML")
            return

        found_pair = self.find_pair_by_name(search_query)

        if not found_pair:
            await update.message.reply_text(f"❌ Class '<b>{search_query}</b>' not found.", parse_mode="HTML")
            return

        found_pair.ai_mode = mode_input
        logger.info(f"Updated AI mode for {found_pair.name}: {mode_input}")
        await update.message.reply_text(
            f"🤖 <b>AI Mode Updated!</b>\n\n"
            f"Class: <b>{found_pair.name}</b>\n"
            f"New AI Mode: <code>{mode_input}</code>",
            parse_mode="HTML",
        )

    async def setdelay_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Dictate message spacing (delay) directly from Telegram."""
        user = update.effective_user
        if user:
            self.is_user_admin(user.id)

        args = context.args
        if not args or len(args) < 2:
            await update.message.reply_text(
                "ℹ️ <b>How to dictate message spacing (delay):</b>\n\n"
                "• Set fixed delay for exact class (e.g. 180 sec):\n"
                "  <code>/setdelay Mathematics 1 180</code>\n\n"
                "• Set random range delay:\n"
                "  <code>/setdelay Mathematics 1 60 120</code>",
                parse_mode="HTML",
            )
            return

        search_query = args[0]
        val_idx = 1
        if len(args) >= 3 and args[1].isdigit():
            search_query = f"{args[0]} {args[1]}"
            val_idx = 2

        try:
            val1 = float(args[val_idx])
            val2 = float(args[val_idx + 1]) if len(args) > val_idx + 1 else val1
        except ValueError:
            await update.message.reply_text("❌ Delay values must be numbers (in seconds).")
            return

        min_sec = min(val1, val2)
        max_sec = max(val1, val2)

        found_pair = self.find_pair_by_name(search_query)

        if not found_pair:
            await update.message.reply_text(f"❌ Class '<b>{search_query}</b>' not found.", parse_mode="HTML")
            return

        found_pair.delay_min_seconds = min_sec
        found_pair.delay_max_seconds = max_sec
        db.save_dynamic_class(
            name=found_pair.name,
            source_chat_id=found_pair.source_chat_id,
            destination_chat_id=found_pair.destination_chat_id,
            discussion_chat_id=found_pair.discussion_chat_id,
            delay_min_seconds=min_sec,
            delay_max_seconds=max_sec,
        )

        if min_sec == max_sec:
            fmt_desc = f"<b>{min_sec:.0f} seconds</b> ({min_sec/60:.1f} minutes)"
        else:
            fmt_desc = f"<b>{min_sec:.0f} to {max_sec:.0f} seconds</b>"

        logger.info(f"Updated spacing for {found_pair.name}: {min_sec}-{max_sec}s")
        await update.message.reply_text(
            f"✅ <b>Updated Message Spacing!</b>\n\n"
            f"Class Channel: <b>{found_pair.name}</b>\n"
            f"New Spacing: {fmt_desc} between messages.",
            parse_mode="HTML",
        )

    async def activate_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Manually activate or toggle a subject."""
        user = update.effective_user
        if user:
            self.is_user_admin(user.id)

        args = context.args
        if not args:
            await update.message.reply_text(
                "ℹ️ <b>Usage:</b> <code>/activate ClassName</code>\nExample: <code>/activate Mathematics 1</code>",
                parse_mode="HTML",
            )
            return

        search_query = " ".join(args).strip()
        found_pair = self.find_pair_by_name(search_query)

        if not found_pair:
            await update.message.reply_text(f"❌ Class '<b>{search_query}</b>' not found.", parse_mode="HTML")
            return

        new_state = not found_pair.is_active
        found_pair.manual_override_active = new_state
        state_word = "ACTIVATED 🟢" if new_state else "DEACTIVATED ⏳"

        logger.info(f"User toggled pair {found_pair.name} to {state_word}")
        await update.message.reply_text(
            f"🎉 Class '<b>{found_pair.name}</b>' is now <b>{state_word}</b>!",
            parse_mode="HTML",
        )

    async def addadmin_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Authorize another user ID as a bot admin."""
        user = update.effective_user
        if user:
            self.is_user_admin(user.id)

        args = context.args
        if not args or not args[0].isdigit():
            await update.message.reply_text(
                "ℹ️ <b>How to add another Admin to the bot:</b>\n\n"
                "1. Ask your co-teacher or assistant to start a chat with this bot and send <code>/start</code>.\n"
                "2. The bot will show their <b>User ID</b>.\n"
                "3. Run command: <code>/addadmin 123456789</code>",
                parse_mode="HTML",
            )
            return

        new_admin_id = int(args[0])
        self.admin_user_ids.add(new_admin_id)

        logger.info(f"Added new bot admin: {new_admin_id}")
        await update.message.reply_text(
            f"👑 <b>Success!</b> Telegram User ID <code>{new_admin_id}</code> is now an authorized Bot Admin!",
            parse_mode="HTML",
        )

    # ------------------------------------------------------------------
    # Application Setup & Startup
    # ------------------------------------------------------------------

    async def post_init(self, application) -> None:
        """Start worker loops, scheduler, keep-alive engine, and register bot commands menu."""
        self.application = application

        commands = [
            BotCommand("start", "Explicit pair location overview"),
            BotCommand("addclass", "Add explicit FROM ➔ TO channel pair"),
            BotCommand("backfill", "Migrate old/existing messages into new channel"),
            BotCommand("autodeliver", "Schedule weekly auto-delivery of old content"),
            BotCommand("stopautodeliver", "View or cancel weekly auto-delivery schedules"),
            BotCommand("userbotlogin", "Connect account to read old channel history"),
            BotCommand("userbotverify", "Get the web link to finish connecting"),
            BotCommand("mychannels", "List your channels with chat IDs"),
            BotCommand("analyzechannel", "Test a channel: volume & detected sessions"),
            BotCommand("previewtiming", "Preview a session's real message timing"),
            BotCommand("testquiz", "Preview quiz recovery on one message, no posting"),
            BotCommand("timedbackfill", "Replay a session with its real original pacing"),
            BotCommand("pairs", "View explicit FROM ➔ TO channel pairs"),
            BotCommand("deleteclass", "Delete a channel pair"),
            BotCommand("clearallclasses", "Wipe all channel pairs"),
            BotCommand("logs", "View student Q&A interaction logs"),
            BotCommand("quiz", "Generate interactive practice quiz poll"),
            BotCommand("summary", "Generate weekly master study guide"),
            BotCommand("prompt", "Set custom AI instructions per class"),
            BotCommand("setdelay", "Dictate message spacing per class"),
            BotCommand("schedule", "View weekly class timetables"),
            BotCommand("activate", "Toggle class active state (Admin)"),
            BotCommand("addadmin", "Authorize another bot admin (Admin)"),
            BotCommand("status", "System status & message queues"),
            BotCommand("help", "Beginner guide & help tips"),
        ]
        await application.bot.set_my_commands(commands)

        for pair in self.pairs:
            pair.worker_task = asyncio.create_task(
                self.pair_worker_loop(pair, application)
            )
            asyncio.create_task(self.update_pair_titles(pair))

        asyncio.create_task(self.activation_checker_loop())
        asyncio.create_task(self.content_pool_scheduler_loop(application))

    def run(self) -> None:
        """Initialize PTB Application and start polling."""
        db.init_db()
        self.load_config()

        request = HTTPXRequest(connect_timeout=20.0, read_timeout=20.0, write_timeout=20.0)

        application = (
            ApplicationBuilder()
            .token(self.token)
            .request(request)
            .post_init(self.post_init)
            .build()
        )

        application.add_handler(CommandHandler("start", self.start_command))
        application.add_handler(CommandHandler("help", self.help_command))
        application.add_handler(CommandHandler("addclass", self.addclass_command))
        application.add_handler(CommandHandler("backfill", self.backfill_command))
        application.add_handler(CommandHandler("autodeliver", self.autodeliver_command))
        application.add_handler(CommandHandler("stopautodeliver", self.stopautodeliver_command))
        application.add_handler(CommandHandler("userbotlogin", self.userbotlogin_command))
        application.add_handler(CommandHandler("userbotverify", self.userbotverify_command))
        application.add_handler(CommandHandler("mychannels", self.mychannels_command))
        application.add_handler(CommandHandler("analyzechannel", self.analyzechannel_command))
        application.add_handler(CommandHandler("previewtiming", self.previewtiming_command))
        application.add_handler(CommandHandler("testquiz", self.testquiz_command))
        application.add_handler(CommandHandler("timedbackfill", self.timedbackfill_command))
        application.add_handler(CommandHandler("deleteclass", self.deleteclass_command))
        application.add_handler(CommandHandler("clearallclasses", self.clearallclasses_command))
        application.add_handler(CommandHandler("logs", self.logs_command))
        application.add_handler(CommandHandler("quiz", self.quiz_command))
        application.add_handler(CommandHandler("summary", self.summary_command))
        application.add_handler(CommandHandler("status", self.status_command))
        application.add_handler(CommandHandler("pairs", self.pairs_command))
        application.add_handler(CommandHandler("prompt", self.prompt_command))
        application.add_handler(CommandHandler("schedule", self.schedule_command))
        application.add_handler(CommandHandler("setdelay", self.setdelay_command))
        application.add_handler(CommandHandler("ai", self.ai_command))
        application.add_handler(CommandHandler("activate", self.activate_command))
        application.add_handler(CommandHandler("addadmin", self.addadmin_command))

        application.add_handler(
            MessageHandler(filters.ALL & ~filters.COMMAND, self.handle_incoming_message)
        )

        logger.info("🤖 Starting Telegram Channel Copier Bot (Polling mode)...")
        application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    start_dummy_web_server()
    start_self_keep_alive_loop()

    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        logger.error("Error: BOT_TOKEN environment variable is missing!")
        sys.exit(1)

    copier_bot = TelegramCopierBot(token=bot_token)
    copier_bot.run()
