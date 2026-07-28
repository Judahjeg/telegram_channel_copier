from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
import urllib.request
import urllib.error
from typing import List, Optional, Dict, Any

logger = logging.getLogger("ChannelCopier")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")


def clean_subject_name(raw_name: str) -> str:
    """Clean group/channel suffixes so title reads nicely e.g. 'Physics' instead of 'Physics crash course group'."""
    name = raw_name.strip()
    for suffix in [" crash course group", " discussion group", " group", " channel"]:
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)].strip()
    return name


def clean_telegram_formatting(text: str) -> str:
    """Strips Markdown hashes (#), stray asterisks (*), and raw AI filler comments for clean Telegram display."""
    if not text:
        return ""

    text = re.sub(r'^#+\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'\s+#+\s*', ' ', text)

    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.*?)\*', r'<i>\1</i>', text)

    for filler in [
        "Here is the answer to your question:",
        "Here is a helpful answer to your question:",
        "Here is a helpful answer:",
        "Here is the solution:",
        "Sure! Here is the answer:",
        "I hope this helps!",
        "Let me know if you have more questions!",
        "Hope this helps!"
    ]:
        text = re.sub(re.escape(filler), '', text, flags=re.IGNORECASE)

    return text.strip()


def sanitize_text(text: str) -> str:
    """Strictly strip tutor phone numbers, WhatsApp handles, Telegram usernames (@user), URLs, and promo comments."""
    if not text:
        return ""

    lines = text.split("\n")
    cleaned_lines = []

    for line in lines:
        l_lower = line.lower()
        if any(kw in l_lower for kw in [
            "whatsapp", "dm me", "message me", "call me", "contact me", "private lesson",
            "private tutor", "join my channel", "t.me/", "subscribe", "vip group",
            "for registration", "tuition fee", "payment details"
        ]):
            continue

        line = re.sub(r'\+?\d{1,4}?[-.\s]?\(?\d{1,3}?\)?[-.\s]?\d{1,4}[-.\s]?\d{1,4}[-.\s]?\d{1,9}', '', line)
        line = re.sub(r'@[a-zA-Z0-9_]{4,}', '', line)
        line = re.sub(r'https?://\S+', '', line)

        cleaned_lines.append(line)

    result = "\n".join(cleaned_lines)
    result = re.sub(r'\n\s*\n+', '\n\n', result).strip()
    return clean_telegram_formatting(result)


def call_deepseek_api(prompt_text: str, timeout_sec: int = 15) -> Optional[str]:
    """Call DeepSeek API (deepseek-chat) if DEEPSEEK_API_KEY is configured."""
    if not DEEPSEEK_API_KEY:
        return None

    url = "https://api.deepseek.com/chat/completions"
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "user", "content": prompt_text}
        ],
        "stream": False
    }

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}"
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            choices = data.get("choices", [])
            if choices:
                content = choices[0].get("message", {}).get("content", "").strip()
                if content:
                    logger.info("[DeepSeek AI] Response generated successfully.")
                    return content
    except Exception as e:
        logger.debug(f"[DeepSeek AI] Request exception: {e}")

    return None


def call_ai_api(prompt_text: str, timeout_sec: int = 15) -> Optional[str]:
    """Call AI provider (DeepSeek API if configured, or Google Gemini API with fallbacks)."""
    if DEEPSEEK_API_KEY:
        ds_res = call_deepseek_api(prompt_text, timeout_sec=timeout_sec)
        if ds_res:
            return ds_res

    if not GEMINI_API_KEY:
        return None

    models = ["gemini-2.0-flash", "gemini-2.0-flash-lite-001", "gemini-2.5-flash", "gemini-1.5-flash"]
    payload = {
        "contents": [
            {
                "parts": [{"text": prompt_text}]
            }
        ]
    }

    for attempt in range(2):
        if attempt > 0:
            time.sleep(2)

        for model in models:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
            try:
                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    candidates = data.get("candidates", [])
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        if parts:
                            text = parts[0].get("text", "").strip()
                            if text:
                                return text
            except urllib.error.HTTPError as he:
                if he.code == 429:
                    logger.warning(f"[Gemini AI] Model {model} rate limited (429). Trying fallback model...")
                    continue
                logger.debug(f"[Gemini AI] Model {model} failed with HTTP {he.code}")
            except Exception as e:
                logger.debug(f"[Gemini AI] Model {model} request exception: {e}")

    return None


def sanitize_and_ocr_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> Optional[str]:
    """Multimodal Vision OCR: Extract lesson text from image and strip any tutor contact numbers or watermarks printed on the image."""
    if not GEMINI_API_KEY or not image_bytes:
        return None

    b64_img = base64.b64encode(image_bytes).decode("utf-8")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"

    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": (
                            "You are an educational content sanitizer. "
                            "Extract all educational lesson notes, math formulas, or questions written in this image. "
                            "STRICT REQUIREMENT: Completely REMOVE any tutor phone numbers, WhatsApp contact info, social media handles (@user), "
                            "watermarks, or promotional text printed on the image. "
                            "Output ONLY the clean educational content:"
                        )
                    },
                    {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": b64_img
                        }
                    }
                ]
            }
        ]
    }

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            candidates = data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                if parts:
                    extracted_text = parts[0].get("text", "").strip()
                    if extracted_text:
                        logger.info("[Vision OCR] Extracted & sanitized image text.")
                        return sanitize_text(extracted_text)
    except Exception as e:
        logger.debug(f"[Vision OCR] Exception: {e}")

    return None


def enhance_text_with_gemini(
    text: str, subject_name: str, mode: str = "flow", custom_instruction: Optional[str] = None
) -> str:
    """Enhance educational post text using AI according to mode or custom user instructions, enforcing strict anonymization and clean Telegram HTML."""
    if not text or not text.strip():
        return ""

    sanitized = sanitize_text(text)
    if not sanitized or mode == "off":
        return sanitized

    clean_name = clean_subject_name(subject_name)

    if custom_instruction and custom_instruction.strip():
        prompt_text = (
            f"You are a strict content anonymizer and AI tutor for {clean_name}. "
            "Clean the post below. Remove all tutor phone numbers, WhatsApp links, personal @usernames, or promo text. "
            f"Follow these custom instructions: {custom_instruction}\n"
            "STRICT FORMATTING RULE: Do NOT use hashtags (#) or markdown headings. Use clean HTML bold tags (<b>term</b>) if needed.\n\n"
            f"POST TEXT:\n{sanitized}\n\n"
            "Output ONLY the final clean educational post text:"
        )
    else:
        prompts = {
            "flow": (
                f"You are an expert tutor for {clean_name}. "
                "Polish the following lesson post to make sentences flow smoothly and improve readability. "
                "STRICT REQUIREMENTS:\n"
                "1. Do NOT use markdown hashes (#) or special heading characters.\n"
                "2. Remove any tutor contact numbers, WhatsApp details, or promotional commentary.\n"
                "3. Output ONLY the polished text:\n\n"
                f"{sanitized}"
            ),
            "polish": (
                f"You are an expert tutor for {clean_name}. "
                "Polish the following lesson post to make sentences flow smoothly and improve readability. "
                "Do NOT use markdown hashes (#). Output ONLY the polished text:\n\n"
                f"{sanitized}"
            ),
            "paraphrase": (
                f"You are a master tutor for {clean_name}. "
                "Rewrite the following lesson post to make it highly engaging and clear for students. "
                "Preserve all facts and formulas. Do NOT use hashes (#). Output ONLY the rewritten post:\n\n"
                f"{sanitized}"
            ),
            "summarize": (
                f"You are a study guide generator for {clean_name}. "
                "Summarize the following post into bullet points. Do NOT use hashes (#). Output ONLY the summary:\n\n"
                f"{sanitized}"
            ),
            "hashtags": (
                f"{sanitized}\n\n#{clean_name.replace(' ', '')} #StudyNotes #IconicImpactTutor"
            ),
        }

        if mode == "hashtags":
            return prompts["hashtags"]

        prompt_text = prompts.get(mode, prompts["flow"])

    result = call_ai_api(prompt_text)
    if result:
        logger.info(f"[AI Enhancer] Enhanced & sanitized post for {clean_name}.")
        return sanitize_text(result)

    return sanitized


def answer_student_question(
    question: str, subject_name: str, class_context_texts: List[str]
) -> str:
    """Answer student questions: FIRST scan channel class notes. Only if answer is NOT in class notes, bring in general knowledge."""
    clean_name = clean_subject_name(subject_name)

    context_block = ""
    if class_context_texts:
        formatted_notes = "\n---\n".join([sanitize_text(t) for t in class_context_texts])
        context_block = f"\nCLASS LESSON REFERENCE NOTES (CHANNEL MATERIALS):\n{formatted_notes}\n"

    prompt_text = (
        f"You are an expert AI Tutor for '{clean_name}'. "
        "A student asked a question in the class discussion group.\n"
        "STRICT SOURCING RULE:\n"
        "1. FIRST scan the CLASS LESSON REFERENCE NOTES below for the answer. If the answer is found in the class materials, answer strictly from those notes.\n"
        "2. ONLY if the answer is NOT found in the class notes, bring in external academic knowledge to provide an accurate answer.\n\n"
        "STRICT FORMATTING RULE:\n"
        "- Provide a SHORT, CONCISE, DIRECT answer (maximum 2-3 short bullet points or 1 brief paragraph).\n"
        "- Go STRAIGHT to the answer. Do NOT include intro filler (e.g. 'Here is your answer') or outro comments.\n"
        "- Do NOT use markdown hashes (#).\n\n"
        f"{context_block}\n"
        f"STUDENT QUESTION:\n{question}\n\n"
        "DIRECT ANSWER:"
    )

    answer = call_ai_api(prompt_text, timeout_sec=15)
    if answer:
        clean_ans = sanitize_text(answer)
        logger.info(f"[AI Q&A] Answered student question for {clean_name}.")
        return f"🎓 <b>{clean_name} Tutor</b>:\n\n{clean_ans}"

    return f"🎓 <b>{clean_name} Tutor</b>:\n\nThank you for asking! Let me review the formula and get back to you shortly."


def generate_class_quiz(subject_name: str, class_context_texts: List[str]) -> Optional[Dict[str, Any]]:
    """Generate an interactive multiple-choice practice quiz payload based on recent class lessons."""
    clean_name = clean_subject_name(subject_name)
    context_block = "\n".join([sanitize_text(t) for t in class_context_texts]) if class_context_texts else "General syllabus concepts"

    prompt_text = (
        f"Generate a high-impact multiple choice practice quiz question for '{clean_name}' based on these notes:\n"
        f"{context_block}\n\n"
        "Return ONLY a JSON object with this exact structure (no markdown fences):\n"
        '{"question": "What is ...?", "options": ["Option A", "Option B", "Option C", "Option D"], "correct_option_id": 0, "explanation": "Explanation why Option A is correct."}'
    )

    res = call_ai_api(prompt_text, timeout_sec=15)
    if res:
        try:
            clean_json = res.replace("```json", "").replace("```", "").strip()
            data = json.loads(clean_json)
            if "question" in data and "options" in data and "correct_option_id" in data:
                return data
        except Exception as e:
            logger.warning(f"Error parsing quiz JSON: {e}")

    return None


def generate_weekly_summary(subject_name: str, class_context_texts: List[str]) -> str:
    """Generate a Weekly Master Study Guide for exam review."""
    clean_name = clean_subject_name(subject_name)
    if not class_context_texts:
        return f"📖 <b>{clean_name} Master Study Guide</b>\n\nNo class lesson posts recorded yet for this week."

    context_block = "\n---\n".join([sanitize_text(t) for t in class_context_texts])
    prompt_text = (
        f"You are a master study guide author for '{clean_name}'. "
        "Create a concise Weekly Exam Review Digest from these lesson notes:\n"
        f"{context_block}\n\n"
        "Include:\n"
        "1. 🔑 Key Concepts & Definitions\n"
        "2. 🧮 Essential Formulas / Core Facts\n"
        "3. 🎯 Top 3 Exam Traps to Avoid\n\n"
        "Format cleanly in HTML tags (<b>, <i>, <code>). Do NOT use markdown hashes (#):"
    )

    res = call_ai_api(prompt_text, timeout_sec=18)
    if res:
        return f"📖 <b>{clean_name} Master Weekly Study Guide</b>\n\n{sanitize_text(res)}"

    return f"📖 <b>{clean_name} Master Study Guide</b>\n\nFailed to generate summary. Please try again shortly."


def process_admin_conversational_assistant(
    user_message: str, class_pairs_summary: str, is_admin: bool
) -> str:
    """Conversational AI Setup Wizard & Navigation Assistant for managing the bot via natural language."""
    if not is_admin:
        return "⛔ <b>Access Denied:</b> Only authorized bot administrators can manage bot settings."

    prompt_text = (
        "You are the Intelligent Setup Wizard & AI Manager Assistant for 'Iconic Impact Tutor' Telegram Bot. "
        "Your job is to guide the user in natural language to set up, configure, view interaction logs, and navigate the bot.\n\n"
        "Available Classes & Config:\n"
        f"{class_pairs_summary}\n\n"
        "If the user wants to perform a bot action, respond by starting your message with one of these ACTION intent codes, followed by a friendly explanation:\n"
        "- `ACTION:SETDELAY|<Class_Name>|<Seconds>` (e.g. `ACTION:SETDELAY|Chemistry 1|180`)\n"
        "- `ACTION:PROMPT|<Class_Name>|<Custom_Instruction>` (e.g. `ACTION:PROMPT|Biology 1|Keep sentences simple`)\n"
        "- `ACTION:ACTIVATE|<Class_Name>` (e.g. `ACTION:ACTIVATE|Physics`)\n"
        "- `ACTION:QUIZ|<Class_Name>` (e.g. `ACTION:QUIZ|Chemistry 1`)\n"
        "- `ACTION:SUMMARY|<Class_Name>` (e.g. `ACTION:SUMMARY|Biology 1`)\n"
        "- `ACTION:LOGS` (View student interaction logs)\n"
        "- `ACTION:HELP` (Setup guidance)\n\n"
        "If the user says 'start', 'help', or asks how to set up, provide a friendly welcome wizard explaining where the bot is added, timetable setup, custom AI prompts, and student interaction logs!\n\n"
        f"USER MESSAGE: {user_message}\n\n"
        "RESPONSE:"
    )

    result = call_ai_api(prompt_text, timeout_sec=12)
    if result:
        return sanitize_text(result)

    return (
        "🧙‍♂️ <b>AI Setup Wizard</b>: Welcome! I am your AI Manager Assistant. "
        "I can help you view where the bot is added, set class timetables, dictate message spacing (e.g. <i>'Set Chemistry 1 spacing to 3 mins'</i>), "
        "or view student Q&A logs (<i>'Show student logs'</i>)!"
    )
