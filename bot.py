from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import random
import sys
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


def parse_iso_datetime(dt_str: str) -> datetime.datetime:
    """Parse ISO datetime string and ensure it has timezone info (default UTC)."""
    dt = datetime.datetime.fromisoformat(dt_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


class ChannelPair:
    def __init__(self, config: Dict[str, Any]):
        self.name: str = config["name"]
        self.source_chat_id: int = config["source_chat_id"]
        self.destination_chat_id: int = config["destination_chat_id"]
        self.discussion_chat_id: Optional[int] = config.get("discussion_chat_id", None)
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

    def load_config(self) -> None:
        """Load channel pairs and admin user IDs from config.json and environment."""
        if not os.path.exists(self.config_path):
            logger.error(f"Configuration file '{self.config_path}' not found!")
            sys.exit(1)

        with open(self.config_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        pairs_config = data if isinstance(data, list) else data.get("pairs", [])
        raw_admins = [] if isinstance(data, list) else data.get("admin_user_ids", [])

        env_admins = os.getenv("ADMIN_IDS", "")
        if env_admins:
            for aid in env_admins.split(","):
                aid = aid.strip()
                if aid.isdigit():
                    raw_admins.append(int(aid))

        self.admin_user_ids = set(raw_admins)

        for p_cfg in pairs_config:
            pair = ChannelPair(p_cfg)
            self.pairs.append(pair)

            if pair.source_chat_id not in self.source_map:
                self.source_map[pair.source_chat_id] = []
            self.source_map[pair.source_chat_id].append(pair)

            if pair.discussion_chat_id:
                self.discussion_map[pair.discussion_chat_id] = pair

            self.discussion_map[pair.destination_chat_id] = pair

        logger.info(f"Loaded {len(self.pairs)} channel pairs and {len(self.admin_user_ids)} bot admins.")

    def is_user_admin(self, user_id: int) -> bool:
        """Check if a user is an authorized bot administrator."""
        if not self.admin_user_ids:
            return True
        return user_id in self.admin_user_ids

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
        """Dedicated worker loop per subject pair applying custom AI prompt instructions, spacing, and reply linking."""
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
                if raw_text:
                    db.save_class_context(pair.name, item["message_id"], raw_text)

                custom_instruction = db.get_custom_prompt(pair.name)

                if pair.ai_mode != "off" and raw_text:
                    enhanced_text = ai_enhancer.enhance_text_with_gemini(
                        raw_text, pair.name, pair.ai_mode, custom_instruction=custom_instruction
                    )
                    sent_msg = await application.bot.send_message(
                        chat_id=pair.destination_chat_id,
                        text=enhanced_text,
                        reply_to_message_id=dest_reply_id,
                    )
                    if sent_msg:
                        db.save_message_mapping(pair.name, item["message_id"], sent_msg.message_id)

                    logger.info(
                        f"✨ [Pair: {pair.name}] Posted AI-enhanced message #{item['message_id']} to destination chat {pair.destination_chat_id}."
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
                            f"✅ [Pair: {pair.name}] Copied message #{item['message_id']} to destination chat {pair.destination_chat_id}."
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
                            f"✅ [Pair: {pair.name}] Copied album messages {item['message_ids']} to destination chat {pair.destination_chat_id}."
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
        subject_name = pair.name if pair else "Class"

        question = message.text
        if bot_user:
            question = question.replace(f"@{bot_user}", "").strip()

        logger.info(f"Received Q&A question in {chat.title or chat.id} for {subject_name}: '{question}'")

        context_notes = []
        if pair:
            context_notes = db.get_recent_class_context(pair.name, limit=5)

        ai_response = ai_enhancer.answer_student_question(
            question=question,
            subject_name=subject_name,
            class_context_texts=context_notes,
        )

        await message.reply_text(ai_response, parse_mode="HTML")

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

        matching_pairs = self.source_map.get(chat.id, [])
        if not matching_pairs:
            return

        msg_text = message.text or message.caption or ""
        reply_to_id = message.reply_to_message.message_id if message.reply_to_message else None

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
                        "text": msg_text,
                        "reply_to_message_id": reply_to_id,
                    }
                else:
                    pair.pending_albums[mg_id]["message_ids"].append(message.message_id)
            else:
                pair.queue.put_nowait({
                    "type": "single",
                    "message_id": message.message_id,
                    "max_id": message.message_id,
                    "text": msg_text,
                    "reply_to_message_id": reply_to_id,
                })
                logger.info(
                    f"📥 [Pair: {pair.name}] Message #{message.message_id} queued for copying."
                )

    # ------------------------------------------------------------------
    # User-Friendly Interactive Commands
    # ------------------------------------------------------------------

    async def start_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Welcome message and user ID info."""
        user = update.effective_user
        active_count = sum(1 for p in self.pairs if p.is_active)
        dormant_count = len(self.pairs) - active_count
        has_ai = "🟢 Active (Free Google Gemini 1.5 Flash)" if os.getenv("GEMINI_API_KEY") else "⚪ Disabled (Add GEMINI_API_KEY for free AI)"

        msg = (
            f"🤖 <b>Welcome, {user.first_name if user else 'User'}!</b>\n\n"
            f"Your Telegram User ID is: <code>{user.id if user else 'Unknown'}</code>\n\n"
            "I manage message copying for your subjects with custom AI prompts, flow polishing, weekly schedules, and Discussion Group Q&A.\n\n"
            f"📊 <b>Overview:</b>\n"
            f"• <b>Total Subjects:</b> {len(self.pairs)}\n"
            f"• 🟢 <b>Active Subjects:</b> {active_count}\n"
            f"• ⏳ <b>Dormant Subjects:</b> {dormant_count}\n"
            f"• 🎓 <b>Discussion Q&A AI:</b> 🟢 Active\n"
            f"• 🤖 <b>AI Enhancer:</b> {has_ai}\n\n"
            "👇 <b>Easy Commands Menu:</b>\n"
            "• /pairs - View all subjects & status\n"
            "• /prompt - Set custom AI instructions per subject\n"
            "• /ai - Configure AI mode (flow, paraphrase, off)\n"
            "• /setdelay - Dictate message spacing\n"
            "• /schedule - View weekly class timetable\n"
            "• /activate - Toggle subject active state\n"
            "• /addadmin - Authorize another user as admin\n"
            "• /status - View system status\n"
            "• /help - Beginner guide & help tips\n"
        )
        await update.message.reply_text(msg, parse_mode="HTML")

    async def prompt_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Set or view custom AI prompt instructions per subject directly in Telegram."""
        user = update.effective_user
        if user and not self.is_user_admin(user.id):
            await update.message.reply_text("⛔ <b>Access Denied:</b> Only authorized bot admins can customize AI prompts.", parse_mode="HTML")
            return

        args = context.args
        if not args:
            lines = [
                "✏️ <b>Custom AI Prompt Instructions per Subject:</b>\n\n"
                "You can give your own custom editing instructions to the AI without needing code changes!\n\n"
                "<b>How to set a custom prompt:</b>\n"
                "<code>/prompt SubjectName Your custom instructions here...</code>\n\n"
                "<b>Examples:</b>\n"
                "• <code>/prompt Biology Keep sentences short and add 2 study emojis</code>\n"
                "• <code>/prompt Chemistry Focus on highlighting key formulas in bold</code>\n"
                "• <code>/prompt Physics reset</code> <i>(Resets to default)</i>\n\n"
                "<b>Current Custom Prompts:</b>"
            ]
            for p in self.pairs:
                cp = db.get_custom_prompt(p.name)
                cp_str = f"<code>{cp}</code>" if cp else "<i>Default flow polishing</i>"
                lines.append(f"• <b>{p.name}:</b> {cp_str}")

            await update.message.reply_text("\n".join(lines), parse_mode="HTML")
            return

        target_name = args[0].strip().lower()
        found_pair = None
        for p in self.pairs:
            if p.name.lower() == target_name:
                found_pair = p
                break

        if not found_pair:
            await update.message.reply_text(f"❌ Subject '<b>{args[0]}</b>' not found.", parse_mode="HTML")
            return

        instruction_text = " ".join(args[1:]).strip()

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
            f"Subject: <b>{found_pair.name}</b>\n"
            f"New AI Instructions: <code>{instruction_text}</code>",
            parse_mode="HTML",
        )

    async def help_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Explanatory guide."""
        msg = (
            "📖 <b>Custom AI Prompt Instructions Guide</b>\n\n"
            "<b>Customize AI output anytime from Telegram:</b>\n"
            "Send <code>/prompt Biology Make posts bulleted and simple</code> to instantly change how the AI polishes Biology posts!\n\n"
            "<b>Discussion Group Q&A:</b>\n"
            "When students tag <code>@Iconic_impact_tutor_bot</code> in discussion groups, it answers questions using class lesson notes."
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
        has_ai = "🟢 Enabled (Free Gemini API)" if os.getenv("GEMINI_API_KEY") else "⚪ Disabled (Set GEMINI_API_KEY for free AI)"

        msg = (
            "⚙️ <b>Bot System Dashboard</b>\n\n"
            f"• <b>Status:</b> 🟢 Running smoothly\n"
            f"• <b>Uptime:</b> {hours}h {minutes}m {seconds}s\n"
            f"• <b>Total Subjects:</b> {len(self.pairs)}\n"
            f"• 🟢 <b>Active Right Now:</b> {len(active_pairs)}\n"
            f"• 📬 <b>Messages Queued:</b> {total_queued}\n"
            f"• ✏️ <b>Custom AI Prompts:</b> Active\n"
            f"• 🎓 <b>Discussion Q&A AI:</b> 🟢 Enabled\n"
            f"• 🤖 <b>AI Status:</b> {has_ai}\n"
        )
        await update.message.reply_text(msg, parse_mode="HTML")

    async def pairs_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Detailed status of all subject pairs."""
        if not self.pairs:
            await update.message.reply_text("No channel pairs configured.")
            return

        lines = ["📚 <b>Subject Pairs, Spacing & AI Status:</b>\n"]
        for p in self.pairs:
            status_icon = "🟢 <b>ACTIVE</b>" if p.is_active else "⏳ <b>DORMANT</b>"
            q_size = p.queue.qsize()
            cp = db.get_custom_prompt(p.name)
            prompt_summary = f"Custom: <i>{cp[:30]}...</i>" if cp else f"Mode: <code>{p.ai_mode}</code>"

            if p.delay_min_seconds == p.delay_max_seconds:
                spacing_str = f"{p.delay_min_seconds:.0f} sec"
            else:
                spacing_str = f"{p.delay_min_seconds:.0f}–{p.delay_max_seconds:.0f} sec"

            lines.append(
                f"<b>Subject: {p.name}</b>\n"
                f"• Status: {status_icon}\n"
                f"• Spacing: ⏱️ <b>{spacing_str}</b>\n"
                f"• AI Settings: 🤖 {prompt_summary}\n"
                f"• Source ID: <code>{p.source_chat_id}</code>\n"
                f"• Destination ID: <code>{p.destination_chat_id}</code>\n"
                f"• Last Copied Msg ID: <code>{p.last_processed_id}</code>\n"
                f"• Queue Backlog: {q_size} pending\n"
            )

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def schedule_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Display weekly class schedule timetable."""
        lines = ["📅 <b>Weekly Class Schedule Timetable:</b>\n"]
        for p in self.pairs:
            lines.append(f"<b>Subject: {p.name}</b>")
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
                "ℹ️ <b>How to set Free AI Mode for a subject:</b>\n\n"
                "• <b>Flow / Polish</b> (Improves flow & readability without changing content):\n"
                "  <code>/ai Biology flow</code>\n\n"
                "• <b>Custom Instructions</b> (Set custom prompt):\n"
                "  <code>/prompt Biology Keep it concise with key points</code>\n\n"
                "• <b>Turn Off AI</b> (Copy posts cleanly without AI):\n"
                "  <code>/ai Mathematics off</code>",
                parse_mode="HTML",
            )
            return

        target_name = args[0].strip().lower()
        mode_input = args[1].strip().lower()

        valid_modes = {"off", "flow", "polish", "paraphrase", "summarize", "hashtags"}
        if mode_input not in valid_modes:
            await update.message.reply_text(f"❌ Invalid AI mode. Choose from: <code>flow</code>, <code>off</code>, <code>paraphrase</code>, <code>summarize</code>, <code>hashtags</code>.", parse_mode="HTML")
            return

        found_pair = None
        for p in self.pairs:
            if p.name.lower() == target_name:
                found_pair = p
                break

        if not found_pair:
            await update.message.reply_text(f"❌ Subject '<b>{args[0]}</b>' not found.", parse_mode="HTML")
            return

        found_pair.ai_mode = mode_input
        logger.info(f"Updated AI mode for {found_pair.name}: {mode_input}")
        await update.message.reply_text(
            f"🤖 <b>AI Mode Updated!</b>\n\n"
            f"Subject: <b>{found_pair.name}</b>\n"
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
                "• Set fixed delay (e.g. 180 sec = 3 min):\n"
                "  <code>/setdelay Biology 180</code>\n\n"
                "• Set random range delay (e.g. 60 to 120 sec):\n"
                "  <code>/setdelay Biology 60 120</code>",
                parse_mode="HTML",
            )
            return

        subject_input = args[0].strip().lower()
        try:
            val1 = float(args[1])
            val2 = float(args[2]) if len(args) >= 3 else val1
        except ValueError:
            await update.message.reply_text("❌ Delay values must be numbers (in seconds).")
            return

        min_sec = min(val1, val2)
        max_sec = max(val1, val2)

        found_pair = None
        for p in self.pairs:
            if p.name.lower() == subject_input:
                found_pair = p
                break

        if not found_pair:
            await update.message.reply_text(f"❌ Subject '<b>{args[0]}</b>' not found.", parse_mode="HTML")
            return

        found_pair.delay_min_seconds = min_sec
        found_pair.delay_max_seconds = max_sec

        if min_sec == max_sec:
            fmt_desc = f"<b>{min_sec:.0f} seconds</b> ({min_sec/60:.1f} minutes)"
        else:
            fmt_desc = f"<b>{min_sec:.0f} to {max_sec:.0f} seconds</b>"

        logger.info(f"Updated spacing for {found_pair.name}: {min_sec}-{max_sec}s")
        await update.message.reply_text(
            f"✅ <b>Updated Message Spacing!</b>\n\n"
            f"Subject: <b>{found_pair.name}</b>\n"
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
                "ℹ️ <b>Usage:</b> <code>/activate SubjectName</code>\nExample: <code>/activate Biology</code>",
                parse_mode="HTML",
            )
            return

        target_name = " ".join(args).strip().lower()
        found_pair = None
        for p in self.pairs:
            if p.name.lower() == target_name:
                found_pair = p
                break

        if not found_pair:
            await update.message.reply_text(f"❌ Subject '<b>{target_name}</b>' not found.", parse_mode="HTML")
            return

        new_state = not found_pair.is_active
        found_pair.manual_override_active = new_state
        state_word = "ACTIVATED 🟢" if new_state else "DEACTIVATED ⏳"

        logger.info(f"User toggled pair {found_pair.name} to {state_word}")
        await update.message.reply_text(
            f"🎉 Subject '<b>{found_pair.name}</b>' is now <b>{state_word}</b>!",
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
        commands = [
            BotCommand("start", "Welcome message & quick menu"),
            BotCommand("pairs", "View subjects, spacing & AI status"),
            BotCommand("prompt", "Set custom AI instructions per subject"),
            BotCommand("ai", "Configure AI mode (flow, paraphrase, off)"),
            BotCommand("setdelay", "Dictate message spacing (Admin)"),
            BotCommand("schedule", "View weekly class timetable"),
            BotCommand("activate", "Toggle subject active state (Admin)"),
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
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        logger.error("Error: BOT_TOKEN environment variable is missing!")
        sys.exit(1)

    copier_bot = TelegramCopierBot(token=bot_token)
    copier_bot.run()
