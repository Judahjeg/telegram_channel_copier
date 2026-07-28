from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
import urllib.error
from typing import List, Optional, Dict, Any

logger = logging.getLogger("ChannelCopier")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")


def clean_subject_name(raw_name: str) -> str:
    """Clean group/channel suffixes so title reads nicely e.g. 'Physics' instead of 'Physics crash course group'."""
    name = raw_name.strip()
    for suffix in [" crash course group", " discussion group", " group", " channel"]:
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)].strip()
    return name


def call_gemini_api(prompt_text: str, timeout_sec: int = 15) -> Optional[str]:
    """Helper to call Gemini API with automatic model fallback and rate-limit retry."""
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

    # Retry up to 2 passes with a short pause if rate limited (429)
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


def enhance_text_with_gemini(
    text: str, subject_name: str, mode: str = "flow", custom_instruction: Optional[str] = None
) -> str:
    """Enhance educational post text using Google Gemini API."""
    if not text or not text.strip() or mode == "off":
        return text

    clean_name = clean_subject_name(subject_name)

    if custom_instruction and custom_instruction.strip():
        prompt_text = (
            f"You are an AI editor for {clean_name} educational posts. "
            f"Follow these custom user instructions to edit the post text below:\n"
            f"USER INSTRUCTION: {custom_instruction}\n\n"
            f"ORIGINAL POST TEXT:\n{text}\n\n"
            "Output ONLY the final edited post text:"
        )
    else:
        prompts = {
            "flow": (
                f"You are an editor for {clean_name} educational content. "
                "Slightly polish the following post to make sentences flow smoothly and improve readability/formatting. "
                "DO NOT change the meaning, tone, facts, formulas, or remove any details. Keep it very close to original. Output ONLY the polished text:\n\n"
                f"{text}"
            ),
            "polish": (
                f"You are an editor for {clean_name} educational content. "
                "Slightly polish the following post to make sentences flow smoothly and improve readability/formatting. "
                "DO NOT change the meaning, tone, facts, formulas, or remove any details. Keep it very close to original. Output ONLY the polished text:\n\n"
                f"{text}"
            ),
            "paraphrase": (
                f"You are an expert tutor for {clean_name}. "
                "Rewrite the following lesson post to make it highly engaging, clear, and easy for students to study. "
                "Preserve all facts, math formulas, and key details. Output ONLY the rewritten post:\n\n"
                f"{text}"
            ),
            "summarize": (
                f"You are a study guide generator for {clean_name}. "
                "Summarize the following post into key study takeaways with bullet points. Output ONLY the summary:\n\n"
                f"{text}"
            ),
            "hashtags": (
                f"{text}\n\n#{clean_name.replace(' ', '')} #StudyNotes #IconicImpactTutor"
            ),
        }

        if mode == "hashtags":
            return prompts["hashtags"]

        prompt_text = prompts.get(mode, prompts["flow"])

    result = call_gemini_api(prompt_text)
    if result:
        logger.info(f"[AI Enhancer] Enhanced post for {clean_name}.")
        return result

    return text


def answer_student_question(
    question: str, subject_name: str, class_context_texts: List[str]
) -> str:
    """Answer student questions in Discussion Groups using class notes if relevant, or general knowledge."""
    clean_name = clean_subject_name(subject_name)

    context_block = ""
    if class_context_texts:
        formatted_notes = "\n---\n".join(class_context_texts)
        context_block = f"\nCLASS LESSON REFERENCE NOTES:\n{formatted_notes}\n"

    prompt_text = (
        f"You are an encouraging, expert AI Tutor for '{clean_name}'. "
        "A student asked a question in the class discussion group. "
        "Answer their question directly, clearly, accurately, and thoroughly. "
        "Use the class notes below as a helpful reference if relevant, but if the answer is not in the notes, "
        "use your full general academic knowledge to provide a complete answer. "
        "Do NOT use meta-phrases like 'Here is a helpful answer' or 'according to the notes'. Answer the question directly.\n"
        f"{context_block}\n"
        f"STUDENT QUESTION:\n{question}\n\n"
        "TUTOR ANSWER:"
    )

    answer = call_gemini_api(prompt_text, timeout_sec=15)
    if answer:
        logger.info(f"[AI Q&A] Answered student question for {clean_name}.")
        return f"🎓 <b>{clean_name} Tutor</b>:\n\n{answer}"

    return f"🎓 <b>{clean_name} Tutor</b>:\n\nThank you for asking! Let me look into that question and get back to you shortly."


def process_admin_conversational_assistant(
    user_message: str, class_pairs_summary: str, is_admin: bool
) -> str:
    """Conversational AI Assistant that helps administrators configure and manage the bot via natural language."""
    if not is_admin:
        return "⛔ <b>Access Denied:</b> Only authorized bot administrators can manage bot settings."

    prompt_text = (
        "You are an intelligent, friendly AI Manager Assistant for the 'Iconic Impact Tutor' Telegram Bot. "
        "The user is managing their channel copier bot via natural language. "
        "Available Classes & Config:\n"
        f"{class_pairs_summary}\n\n"
        "If the user wants to perform a bot action, respond by starting your message with one of these ACTION intent codes, followed by a friendly explanation:\n"
        "- `ACTION:SETDELAY|<Class_Name>|<Seconds>` (e.g. `ACTION:SETDELAY|Chemistry 1|180`)\n"
        "- `ACTION:PROMPT|<Class_Name>|<Custom_Instruction>` (e.g. `ACTION:PROMPT|Biology 1|Keep sentences simple`)\n"
        "- `ACTION:ACTIVATE|<Class_Name>` (e.g. `ACTION:ACTIVATE|Physics`)\n"
        "- `ACTION:HELP` (General assistance)\n"
        "If the user is asking a general question about how to use the bot, explain clearly in simple English.\n\n"
        f"USER MESSAGE: {user_message}\n\n"
        "RESPONSE:"
    )

    result = call_gemini_api(prompt_text, timeout_sec=12)
    if result:
        return result

    return (
        "🤖 <b>AI Assistant</b>: I am here to help you manage your bot! "
        "You can tell me things like <i>'Set Chemistry 1 delay to 3 minutes'</i> or <i>'Make Biology posts simple'</i>."
    )
