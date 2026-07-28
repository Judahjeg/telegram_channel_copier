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
from typing import Dict, List, Any, Optional, Set

from telegram import Update, BotCommand
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
            self.wfile.write(b"Iconic Impact Tutor Bot is running live!")

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
            f"[Pair: {self.name}] Loaded pair. Spacing: {self.delay_min_seconds:.0f}-{self.delay_max_seconds:.0f}s. "
            f"AI Mode: '{self.ai_mode}'. Last processed msg ID: {self.last_processed_id}"
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

    def sync_classes_from_db(self) -> None:
        """Load and synchronize dynamic class configurations from SQLite database into memory maps."""
        db_classes = db.get_all_dynamic_classes()

        # Re-build memory maps
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
        
        # Wipe all default classes as requested by user
        db.clear_all_dynamic_classes()
        self.sync_classes_from_db()
        logger.info(f"Clean slate active. Loaded {len(self.pairs)} channel pairs and {len(self.admin_user_ids)} bot admins.")

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
        """Check if a user is an authorized bot administrator."""
        if not self.admin_user_ids:
            return True
        return user_id in self.admin_user_ids

    def get_classes_summary(self) -> str:
        """Generate summary string of all classes for the AI assistant."""
        if not self.pairs:
            return "No active classes right now. The administrator has a clean slate."
        lines = []
        for p in self.pairs:
            cp = db.get_custom_prompt(p.name)
            cp_str = f"Prompt: '{cp}'" if cp else "Prompt: default"
            lines.append(f"- Class: {p.name} | Source: {p.source_chat_id} | Dest: {p.destination_chat_id} | Discussion: {p.discussion_chat_id} | Spacing: {p.delay_min_seconds:.0f}s | Active: {p.is_active} | {cp_str}")
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

                if pair.ai_mode != "off" and sanitized_text:
                    enhanced_text = ai_enhancer.enhance_text_with_gemini(
                        sanitized_text, pair.name, pair.ai_mode, custom_instruction=custom_instruction
                    )
                    sent_msg = await application.bot.send_message(
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
                    if item["type"] == "single":
                        copied_msg_id_obj = await application.bot.copy_message(
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
                    elif item["type"] == "album":
                        copied_msgs = await application.bot.copy_messages(
                            chat_id=pair.destination_chat_id,
                            from_chat_id=pair.source_chat_id,
                            message_ids=item["message_ids"],
                        )
                        if copied_msgs and len(copied_msgs) == len(item["message_ids"]):
                            for src_id, dst_msg in zip(item["message_ids"], copied_msgs):
                                db.save_message_mapping(pair.name, src_id, dst_msg.message_id)

                        logger.info(
                            f"✅ [Pair: {pair.name}] Copied album messages {item['message_ids']} to chat {pair.destination_chat_id}."
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
        if user and not self.is_user_admin(user.id):
            await update.message.reply_text("⛔ <b>Access Denied:</b> Only authorized bot admins can add class channels.", parse_mode="HTML")
            return

        args = context.args
        if not args or len(args) < 3:
            await update.message.reply_text(
                "➕ <b>How to Add/Set a Custom Class Channel:</b>\n\n"
                "Syntax: <code>/addclass ClassName SourceChatID DestinationChatID [DiscussionChatID]</code>\n\n"
                "<b>Example:</b>\n"
                "<code>/addclass Mathematics 1 -100123456789 -100987654321 -100555444333</code>\n\n"
                "<i>Tip: You can also just tell the AI in private chat: 'Add a new class called Mathematics 1 with source -100123... and dest -100987...'</i>",
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

            disc_str = f" | Discussion: <code>{disc_id}</code>" if disc_id else ""
            await update.message.reply_text(
                f"🎉 <b>Success!</b> Custom Class <b>{c_name}</b> has been set up!\n\n"
                f"• Source ID: <code>{source_id}</code>\n"
                f"• Destination ID: <code>{dest_id}</code>{disc_str}\n"
                f"• Status: 🟢 Active and listening now!",
                parse_mode="HTML",
            )
        except ValueError:
            await update.message.reply_text("❌ Source, Destination, and Discussion Chat IDs must be valid numeric IDs (e.g., -100123456789).")

    async def deleteclass_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Interactively delete a class channel configuration."""
        user = update.effective_user
        if user and not self.is_user_admin(user.id):
            await update.message.reply_text("⛔ <b>Access Denied:</b> Only authorized bot admins can delete class channels.", parse_mode="HTML")
            return

        args = context.args
        if not args:
            await update.message.reply_text("🗑️ <b>Usage:</b> <code>/deleteclass ClassName</code>\nExample: <code>/deleteclass Physics 1</code>", parse_mode="HTML")
            return

        target_name = " ".join(args).strip()
        deleted = db.delete_dynamic_class(target_name)
        if deleted:
            self.sync_classes_from_db()
            await update.message.reply_text(f"🗑️ <b>Deleted!</b> Class channel <b>{target_name}</b> was removed successfully.", parse_mode="HTML")
        else:
            await update.message.reply_text(f"❌ Class <b>{target_name}</b> not found.", parse_mode="HTML")

    async def clearallclasses_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Wipe all existing class configurations to start completely from scratch."""
        user = update.effective_user
        if user and not self.is_user_admin(user.id):
            await update.message.reply_text("⛔ <b>Access Denied:</b> Only authorized bot admins can clear all classes.", parse_mode="HTML")
            return

        db.clear_all_dynamic_classes()
        self.sync_classes_from_db()
        await update.message.reply_text("🧹 <b>Clean Slate Activated!</b> All default and custom classes have been deleted. You can now add your own classes from scratch using `/addclass`!", parse_mode="HTML")

    async def handle_admin_conversational_chat(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Conversational AI Manager Co-Pilot Handler: Understands natural English, adds/deletes custom classes, and executes actions."""
        message = update.effective_message
        user = update.effective_user

        if not message or not user or not message.text:
            return

        if not self.is_user_admin(user.id):
            await message.reply_text("⛔ <b>Access Denied:</b> Only authorized bot administrators can manage bot settings.", parse_mode="HTML")
            return

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
        active_count = sum(1 for p in self.pairs if p.is_active)
        has_ai = "🟢 Active (Gemini / DeepSeek API)" if (os.getenv("GEMINI_API_KEY") or os.getenv("DEEPSEEK_API_KEY")) else "⚪ Disabled"

        channel_locations = []
        for p in self.pairs:
            disc_str = f" | Discussion ID: <code>{p.discussion_chat_id}</code>" if p.discussion_chat_id else ""
            channel_locations.append(
                f"• <b>{p.name}:</b> Source <code>{p.source_chat_id}</code> ➔ Destination <code>{p.destination_chat_id}</code>{disc_str}"
            )

        locations_block = "\n".join(channel_locations) if channel_locations else "<i>No classes connected yet. Use /addclass to add your first class!</i>"

        msg = (
            f"🧙‍♂️ <b>Welcome to Iconic Impact Tutor AI Co-Pilot, {user.first_name if user else 'User'}!</b>\n\n"
            f"Your Telegram User ID: <code>{user.id if user else 'Unknown'}</code>\n\n"
            "✨ <b>Clean Slate Active!</b> All default classes have been removed. You can now set up your own classes from scratch!\n\n"
            f"📍 <b>Where Bot is Currently Added & Active:</b>\n"
            f"{locations_block}\n\n"
            f"📊 <b>Bot Overview:</b>\n"
            f"• <b>Total Connected Classes:</b> {len(self.pairs)}\n"
            f"• 🟢 <b>Active Right Now:</b> {active_count}\n"
            f"• 🛠️ <b>Dynamic Class Setup:</b> 🟢 Active (`/addclass` & `/deleteclass`)\n"
            f"• 💬 <b>Conversational AI Co-Pilot:</b> 🟢 Active\n"
            f"• 📜 <b>Student Interaction Logs:</b> 🟢 Active (`/logs`)\n"
            f"• 🤖 <b>AI Engine:</b> {has_ai}\n\n"
            "💬 <b>Chat with me naturally or run commands:</b>\n"
            "• <code>/addclass ClassName SourceChatID DestinationChatID</code>\n"
            "• <i>'Add a class called Further Maths with source -100123 and dest -100456'</i>\n"
            "• <code>/deleteclass ClassName</code>\n"
            "• <code>/clearallclasses</code> (Wipe all classes)\n\n"
            "👇 <b>Quick Commands Menu:</b>\n"
            "• /addclass - Add/Set up a new custom class channel\n"
            "• /deleteclass - Delete a class channel\n"
            "• /clearallclasses - Wipe all classes to start over\n"
            "• /logs - View recent student Q&A interaction logs\n"
            "• /schedule - View & manage weekly class timetables\n"
            "• /quiz - Generate interactive quiz for a class\n"
            "• /summary - Generate weekly master study guide\n"
            "• /pairs - View connected channels & spacing\n"
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
        if user and not self.is_user_admin(user.id):
            await update.message.reply_text("⛔ <b>Access Denied:</b> Only authorized bot admins can view student logs.", parse_mode="HTML")
            return

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
        if user and not self.is_user_admin(user.id):
            await update.message.reply_text("⛔ <b>Access Denied:</b> Only authorized bot admins can generate practice quizzes.", parse_mode="HTML")
            return

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
        if user and not self.is_user_admin(user.id):
            await update.message.reply_text("⛔ <b>Access Denied:</b> Only authorized bot admins can customize AI prompts.", parse_mode="HTML")
            return

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
                lines.append("<i>No active classes. Use /addclass to add your first class!</i>")

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
            "📖 <b>Dynamic Class Setup & Conversational AI Guide</b>\n\n"
            "<b>1. Add / Set Up Any Class Channel:</b>\n"
            "Tell the AI in chat or use <code>/addclass ClassName source_id dest_id [discussion_id]</code>\n\n"
            "<b>2. Delete Any Class Channel:</b>\n"
            "Tell the AI in chat or use <code>/deleteclass ClassName</code>\n\n"
            "<b>3. Wipe All Classes to Start Fresh:</b>\n"
            "Use <code>/clearallclasses</code>\n\n"
            "<b>4. Priority Lesson Notes Search:</b>\n"
            "AI tutor scans class channel notes first before external knowledge.\n\n"
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
            f"• <b>Total Class Channels:</b> {len(self.pairs)}\n"
            f"• 🟢 <b>Active Right Now:</b> {len(active_pairs)}\n"
            f"• 📬 <b>Messages Queued:</b> {total_queued}\n"
            f"• 🛠️ <b>Dynamic Class Setup:</b> 🟢 Enabled (`/addclass` & `/deleteclass`)\n"
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
        """Detailed status of all subject pairs."""
        if not self.pairs:
            await update.message.reply_text("No channel pairs configured. Use `/addclass ClassName source_id dest_id` to add one!", parse_mode="HTML")
            return

        lines = ["📚 <b>Exact Class Channels, Locations & Spacing:</b>\n"]
        for p in self.pairs:
            status_icon = "🟢 <b>ACTIVE</b>" if p.is_active else "⏳ <b>DORMANT</b>"
            q_size = p.queue.qsize()
            cp = db.get_custom_prompt(p.name)
            prompt_summary = f"Custom: <i>{cp[:30]}...</i>" if cp else f"Mode: <code>{p.ai_mode}</code>"

            if p.delay_min_seconds == p.delay_max_seconds:
                spacing_str = f"{p.delay_min_seconds:.0f} sec"
            else:
                spacing_str = f"{p.delay_min_seconds:.0f}–{p.delay_max_seconds:.0f} sec"

            disc_info = f"<code>{p.discussion_chat_id}</code>" if p.discussion_chat_id else "None"

            lines.append(
                f"<b>Class: {p.name}</b>\n"
                f"• Status: {status_icon}\n"
                f"• Spacing: ⏱️ <b>{spacing_str}</b>\n"
                f"• AI Settings: 🤖 {prompt_summary}\n"
                f"• Source Channel ID: <code>{p.source_chat_id}</code>\n"
                f"• Destination Channel ID: <code>{p.destination_chat_id}</code>\n"
                f"• Discussion Group ID: {disc_info}\n"
                f"• Last Copied Msg ID: <code>{p.last_processed_id}</code>\n"
                f"• Queue Backlog: {q_size} pending\n"
            )

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

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
        if user and not self.is_user_admin(user.id):
            await update.message.reply_text("⛔ <b>Access Denied:</b> Only authorized bot admins can configure AI settings.", parse_mode="HTML")
            return

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
        if user and not self.is_user_admin(user.id):
            await update.message.reply_text("⛔ <b>Access Denied:</b> Only authorized bot admins can change message spacing.", parse_mode="HTML")
            return

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
        if user and not self.is_user_admin(user.id):
            await update.message.reply_text("⛔ <b>Access Denied:</b> Only authorized bot admins can activate/deactivate subjects.", parse_mode="HTML")
            return

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
        if user and not self.is_user_admin(user.id):
            await update.message.reply_text("⛔ <b>Access Denied:</b> Only existing bot admins can add new admins.", parse_mode="HTML")
            return

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
        """Start worker loops, scheduler, and register bot commands menu."""
        self.application = application

        commands = [
            BotCommand("start", "Location overview & AI setup wizard"),
            BotCommand("addclass", "Add/set up a new custom class channel"),
            BotCommand("deleteclass", "Delete a class channel"),
            BotCommand("clearallclasses", "Wipe all classes to start over"),
            BotCommand("logs", "View student Q&A interaction logs"),
            BotCommand("quiz", "Generate interactive practice quiz poll"),
            BotCommand("summary", "Generate weekly master study guide"),
            BotCommand("pairs", "View connected channels & spacing"),
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

    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        logger.error("Error: BOT_TOKEN environment variable is missing!")
        sys.exit(1)

    copier_bot = TelegramCopierBot(token=bot_token)
    copier_bot.run()
