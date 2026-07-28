from __future__ import annotations

import asyncio
import datetime
import http.server
import json
import logging
import os
import random
import re
import socketserver
import sys
import threading
import time
import urllib.request
import urllib.error
from typing import Dict, List, Any, Optional, Set

from telegram import Update, BotCommand
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

# Setup console logging
logging.basicConfig(
    format="%(asctime)s - [%(levelname)s] - %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("ChannelCopier")

START_TIME = time.time()


def start_dummy_web_server():
    """Start an immediate HTTP server on PORT for Render's 100% Free Web Service tier."""
    port = int(os.getenv("PORT", "8080"))

    class HealthHandler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Iconic Impact Tutor Bot is running live 24/7!")

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
        if not args or len(args) < 3 or not args[-1].lstrip("-").isdigit() or not args[-2].lstrip("-").isdigit():
            await update.message.reply_text(
                "📦 <b>How to migrate old/previous materials into a new channel:</b>\n\n"
                "Syntax: <code>/backfill ClassName start_id end_id</code>\n\n"
                "<b>Example:</b>\n"
                "<code>/backfill Mathematics 1 1 850</code>\n\n"
                "<i>Tip: Open the OLD channel, right-click the first and last message you want to migrate, "
                "and choose 'Copy Message Link'. The number at the end of each link is the message ID.</i>\n\n"
                "⚠️ <b>Note:</b> Telegram's Bot API cannot read the *content* of old messages, only copy them "
                "as-is. So AI cleanup/anonymization only runs on messages received live going forward — "
                "backfilled posts keep their original captions untouched.",
                parse_mode="HTML",
            )
            return

        end_id = int(args[-1])
        start_id = int(args[-2])
        class_name = " ".join(args[:-2]).strip()

        found_pair = self.find_pair_by_name(class_name)
        if not found_pair:
            await update.message.reply_text(f"❌ Class '<b>{class_name}</b>' not found.", parse_mode="HTML")
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
            f"📦 <b>Backfill started for {found_pair.name}!</b>\n"
            f"Migrating message IDs <code>{start_id}</code>–<code>{end_id}</code> ({total} messages) "
            f"from the old channel into <b>{found_pair.destination_title}</b>.\n\n"
            f"You'll get progress updates here. This can take a while for large batches "
            f"since it's paced to avoid Telegram's flood limits.",
            parse_mode="HTML",
        )

        asyncio.create_task(
            self._run_backfill(found_pair, start_id, end_id, update.effective_chat.id, context.application)
        )

    async def _run_backfill(
        self, pair: ChannelPair, start_id: int, end_id: int, notify_chat_id: int, application
    ) -> None:
        """Worker that walks a historical message ID range and copies each into the destination,
        pacing itself to stay under Telegram's flood-control limits and surviving transient errors."""
        copied = 0
        skipped = 0
        failed = 0
        total = end_id - start_id + 1

        logger.info(f"[Backfill:{pair.name}] Starting migration of IDs {start_id}-{end_id} ({total} messages).")

        for msg_id in range(start_id, end_id + 1):
            try:
                result = await self._call_with_retry(
                    application.bot.copy_message,
                    chat_id=pair.destination_chat_id,
                    from_chat_id=pair.source_chat_id,
                    message_id=msg_id,
                )
                if result:
                    db.save_message_mapping(pair.name, msg_id, result.message_id)
                    copied += 1
            except BadRequest as e:
                # Expected for gaps: deleted messages, service messages, or IDs that never existed.
                skipped += 1
                logger.debug(f"[Backfill:{pair.name}] Skipped ID {msg_id}: {e}")
            except Exception as e:
                failed += 1
                logger.warning(f"[Backfill:{pair.name}] Failed ID {msg_id}: {e}")

            done = copied + skipped + failed
            if done % 25 == 0 or done == total:
                try:
                    await application.bot.send_message(
                        notify_chat_id,
                        f"⏳ <b>{pair.name} backfill progress:</b> {done}/{total} processed "
                        f"(✅ {copied} copied, ⏭️ {skipped} skipped, ❌ {failed} failed)",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

            await asyncio.sleep(random.uniform(2.5, 4.5))

        logger.info(f"[Backfill:{pair.name}] Finished. Copied={copied} Skipped={skipped} Failed={failed}")
        try:
            await application.bot.send_message(
                notify_chat_id,
                f"🎉 <b>Backfill complete for {pair.name}!</b>\n\n"
                f"✅ Copied: {copied}\n⏭️ Skipped (not found/deleted): {skipped}\n❌ Failed: {failed}",
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

            except Exception as e:
                logger.error(f"Error parsing conversational AI action payload: {e}")

        # Strip hidden <ACTION> tags before sending conversational message to user
        clean_text = re.sub(r'<ACTION>.*?</ACTION>', '', ai_reply, flags=re.DOTALL).strip()
        await message.reply_text(clean_text, parse_mode="HTML")

    async def handle_incoming_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle incoming channel posts and messages."""
        message = update.effective_message
        chat = update.effective_chat

        if not message or not chat:
            return

        if chat.type in ("group", "supergroup"):
            await self.handle_discussion_qa(update, context)
            return

        if chat.type == "private" and message.text and not message.text.startswith("/"):
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
            f"🧙‍♂️ <b>Welcome to Iconic Impact Tutor AI Co-Pilot, {user.first_name if user else 'User'}!</b>\n\n"
            f"Your Telegram User ID: <code>{user.id if user else 'Unknown'}</code> (🟢 Authorized Admin)\n\n"
            "✨ <b>Explicit Pair Setup Active!</b> Configure explicit pairs showing exactly FROM which source channel TO which destination channel content flows.\n\n"
            f"📍 <b>Explicit Connected Channel Pairs:</b>\n"
            f"{locations_block}\n\n"
            f"📊 <b>Bot Overview:</b>\n"
            f"• <b>Total Connected Pairs:</b> {len(self.pairs)}\n"
            f"• 🟢 <b>Active Right Now:</b> {active_count}\n"
            f"• ⚡ <b>24/7 Keep-Alive Engine:</b> 🟢 Active (Always Awake)\n"
            f"• 🛠️ <b>Explicit Pair Setup:</b> 🟢 Active (`/addclass` & `/deleteclass`)\n"
            f"• 💬 <b>Conversational AI Co-Pilot:</b> 🟢 Active\n"
            f"• 📜 <b>Student Interaction Logs:</b> 🟢 Active (`/logs`)\n"
            f"• 🤖 <b>AI Engine:</b> {has_ai}\n\n"
            "💬 <b>Chat with me naturally or run commands:</b>\n"
            "• <code>/addclass ClassName SourceChatID DestinationChatID</code>\n"
            "• <i>'Add a class called Further Maths from source -100123 to dest -100456'</i>\n"
            "• <code>/pairs</code> (View explicit FROM ➔ TO flow)\n\n"
            "👇 <b>Quick Commands Menu:</b>\n"
            "• /addclass - Add an explicit FROM ➔ TO channel pair\n"
            "• /backfill - Migrate previously posted materials from an old channel\n"
            "• /pairs - View explicit FROM ➔ TO channel pair flows\n"
            "• /deleteclass - Delete a channel pair\n"
            "• /clearallclasses - Wipe all channel pairs\n"
            "• /logs - View recent student Q&A interaction logs\n"
            "• /schedule - View & manage weekly class timetables\n"
            "• /quiz - Generate interactive quiz for a class\n"
            "• /summary - Generate weekly master study guide\n"
            "• /prompt - Set custom AI instructions\n"
            "• /setdelay - Dictate message spacing\n"
            "• /activate - Toggle class active state\n"
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
            "<b>2b. Migrate Previously Posted Materials:</b>\n"
            "New pairs only copy messages posted while the bot is running. To bring over materials "
            "already sitting in an old channel, use <code>/backfill ClassName start_id end_id</code>.\n\n"
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
