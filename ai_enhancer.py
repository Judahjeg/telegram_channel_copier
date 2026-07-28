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
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")


def clean_subject_name(raw_name: str) -> str:
    """Clean group/channel suffixes so title reads nicely e.g. 'Physics' instead of 'Physics crash course group'."""
    name = raw_name.strip()
    for suffix in [" crash course group", " discussion group", " group", " channel"]:
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)].strip()
    return name


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


def enhance_text_with_gemini(
    text: str, subject_name: str, mode: str = "flow", custom_instruction: Optional[str] = None
) -> str:
    """Enhance educational post text using AI according to mode or custom user instructions."""
    if not text or not text.strip() or mode == "off":
        return text

    clean_name = clean_subject_name(subject_name)

    if custom_instruction and custom_instruction.strip():
        prompt_text = (
            f"You are an AI master tutor for {clean_name} educational content. "
            f"Follow these custom user instructions to edit the post text below:\n"
            f"USER INSTRUCTION: {custom_instruction}\n\n"
            f"ORIGINAL POST TEXT:\n{text}\n\n"
            "Output ONLY the final edited post text:"
        )
    else:
        prompts = {
            "flow": (
                f"You are an expert tutor for {clean_name}. "
                "Polish the following lesson post to make sentences flow smoothly, format key terms in bold, and improve readability. "
                "DO NOT change facts, formulas, or add extra commentary. Output ONLY the polished text:\n\n"
                f"{text}"
            ),
            "polish": (
                f"You are an expert tutor for {clean_name}. "
                "Polish the following lesson post to make sentences flow smoothly, format key terms in bold, and improve readability. "
                "Output ONLY the polished text:\n\n"
                f"{text}"
            ),
            "paraphrase": (
                f"You are a master tutor for {clean_name}. "
                "Rewrite the following lesson post to make it highly engaging and clear for students. "
                "Preserve all facts and formulas. Output ONLY the rewritten post:\n\n"
                f"{text}"
            ),
            "summarize": (
                f"You are a study guide generator for {clean_name}. "
                "Summarize the following post into bullet points. Output ONLY the summary:\n\n"
                f"{text}"
            ),
            "hashtags": (
                f"{text}\n\n#{clean_name.replace(' ', '')} #StudyNotes #IconicImpactTutor"
            ),
        }

        if mode == "hashtags":
            return prompts["hashtags"]

        prompt_text = prompts.get(mode, prompts["flow"])

    result = call_ai_api(prompt_text)
    if result:
        logger.info(f"[AI Enhancer] Enhanced post for {clean_name}.")
        return result

    return text


def answer_student_question(
    question: str, subject_name: str, class_context_texts: List[str]
) -> str:
    """Answer student questions in Discussion Groups using Socratic step-by-step guidance."""
    clean_name = clean_subject_name(subject_name)

    context_block = ""
    if class_context_texts:
        formatted_notes = "\n---\n".join(class_context_texts)
        context_block = f"\nCLASS LESSON REFERENCE NOTES:\n{formatted_notes}\n"

    prompt_text = (
        f"You are an encouraging, expert AI Tutor for '{clean_name}'. "
        "A student asked a question in the class discussion group. "
        "Provide a high-impact, step-by-step explanation. "
        "Format your answer with bullet points or numbered steps if solving a problem. "
        "Use the class notes below if relevant, but draw from your general academic knowledge if needed.\n"
        f"{context_block}\n"
        f"STUDENT QUESTION:\n{question}\n\n"
        "TUTOR ANSWER:"
    )

    answer = call_ai_api(prompt_text, timeout_sec=15)
    if answer:
        logger.info(f"[AI Q&A] Answered student question for {clean_name}.")
        return f"🎓 <b>{clean_name} Tutor</b>:\n\n{answer}"

    return f"🎓 <b>{clean_name} Tutor</b>:\n\nThank you for asking! Let me review the formula and get back to you shortly."


def generate_class_quiz(subject_name: str, class_context_texts: List[str]) -> Optional[Dict[str, Any]]:
    """Generate an interactive multiple-choice practice quiz payload based on recent class lessons."""
    clean_name = clean_subject_name(subject_name)
    context_block = "\n".join(class_context_texts) if class_context_texts else "General syllabus concepts"

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

    context_block = "\n---\n".join(class_context_texts)
    prompt_text = (
        f"You are a master study guide author for '{clean_name}'. "
        "Create a comprehensive, beautifully formatted Weekly Exam Review Digest from these lesson notes:\n"
        f"{context_block}\n\n"
        "Include:\n"
        "1. 🔑 Key Concepts & Definitions\n"
        "2. 🧮 Essential Formulas / Core Facts\n"
        "3. 🎯 Top 3 Exam Traps to Avoid\n\n"
        "Format cleanly in HTML tags (<b>, <i>, <code>):"
    )

    res = call_ai_api(prompt_text, timeout_sec=18)
    if res:
        return f"📖 <b>{clean_name} Master Weekly Study Guide</b>\n\n{res}"

    return f"📖 <b>{clean_name} Master Study Guide</b>\n\nFailed to generate summary. Please try again shortly."


def process_admin_conversational_assistant(
    user_message: str, class_pairs_summary: str, is_admin: bool
) -> str:
    """Conversational AI Setup Wizard & Assistant for managing the bot via natural language."""
    if not is_admin:
        return "⛔ <b>Access Denied:</b> Only authorized bot administrators can manage bot settings."

    prompt_text = (
        "You are the Intelligent Setup Wizard & AI Manager Assistant for 'Iconic Impact Tutor' Telegram Bot. "
        "Your job is to guide the user in natural language to set up, configure, and maximize student impact for their classes.\n\n"
        "Available Classes & Config:\n"
        f"{class_pairs_summary}\n\n"
        "If the user wants to perform a bot action, respond by starting your message with one of these ACTION intent codes, followed by a friendly explanation:\n"
        "- `ACTION:SETDELAY|<Class_Name>|<Seconds>` (e.g. `ACTION:SETDELAY|Chemistry 1|180`)\n"
        "- `ACTION:PROMPT|<Class_Name>|<Custom_Instruction>` (e.g. `ACTION:PROMPT|Biology 1|Keep sentences simple`)\n"
        "- `ACTION:ACTIVATE|<Class_Name>` (e.g. `ACTION:ACTIVATE|Physics`)\n"
        "- `ACTION:QUIZ|<Class_Name>` (e.g. `ACTION:QUIZ|Chemistry 1`)\n"
        "- `ACTION:SUMMARY|<Class_Name>` (e.g. `ACTION:SUMMARY|Biology 1`)\n"
        "- `ACTION:HELP` (Setup guidance)\n\n"
        "If the user says 'start', 'help', or asks how to set up, provide a high-energy welcome wizard explaining how you can set up their classes, message delays, custom AI prompts, and practice quizzes!\n\n"
        f"USER MESSAGE: {user_message}\n\n"
        "RESPONSE:"
    )

    result = call_ai_api(prompt_text, timeout_sec=12)
    if result:
        return result

    return (
        "🧙‍♂️ <b>AI Setup Wizard</b>: Welcome! I am your AI Manager Assistant. "
        "I can help you set up your classes, dictate message spacing (e.g. <i>'Set Chemistry 1 spacing to 3 mins'</i>), "
        "custom AI prompts, or generate practice quizzes (<i>'Create a quiz for Chemistry 1'</i>)!"
    )
