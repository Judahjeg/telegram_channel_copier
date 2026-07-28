from __future__ import annotations

import json
import logging
import os
import urllib.request
import urllib.error
from typing import List, Optional

logger = logging.getLogger("ChannelCopier")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")


def call_gemini_api(prompt_text: str, timeout_sec: int = 10) -> Optional[str]:
    """Helper to call Gemini API with fallback across model endpoints."""
    if not GEMINI_API_KEY:
        return None

    # Model endpoints to try in order
    models = ["gemini-2.0-flash", "gemini-2.5-flash", "gemini-1.5-flash"]
    payload = {
        "contents": [
            {
                "parts": [{"text": prompt_text}]
            }
        ]
    }

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

    if custom_instruction and custom_instruction.strip():
        prompt_text = (
            f"You are an AI editor for {subject_name} educational posts. "
            f"Follow these custom user instructions to edit the post text below:\n"
            f"USER INSTRUCTION: {custom_instruction}\n\n"
            f"ORIGINAL POST TEXT:\n{text}\n\n"
            "Output ONLY the final edited post text:"
        )
    else:
        prompts = {
            "flow": (
                f"You are an editor for {subject_name} educational content. "
                "Slightly polish the following post to make sentences flow smoothly and improve readability/formatting. "
                "DO NOT change the meaning, tone, facts, formulas, or remove any details. Keep it very close to original. Output ONLY the polished text:\n\n"
                f"{text}"
            ),
            "polish": (
                f"You are an editor for {subject_name} educational content. "
                "Slightly polish the following post to make sentences flow smoothly and improve readability/formatting. "
                "DO NOT change the meaning, tone, facts, formulas, or remove any details. Keep it very close to original. Output ONLY the polished text:\n\n"
                f"{text}"
            ),
            "paraphrase": (
                f"You are an expert tutor for {subject_name}. "
                "Rewrite the following lesson post to make it highly engaging, clear, and easy for students to study. "
                "Preserve all facts, math formulas, and key details. Output ONLY the rewritten post:\n\n"
                f"{text}"
            ),
            "summarize": (
                f"You are a study guide generator for {subject_name}. "
                "Summarize the following post into key study takeaways with bullet points. Output ONLY the summary:\n\n"
                f"{text}"
            ),
            "hashtags": (
                f"{text}\n\n#{subject_name.replace(' ', '')} #StudyNotes #IconicImpactTutor"
            ),
        }

        if mode == "hashtags":
            return prompts["hashtags"]

        prompt_text = prompts.get(mode, prompts["flow"])

    result = call_gemini_api(prompt_text)
    if result:
        logger.info(f"[AI Enhancer] Enhanced post for {subject_name}.")
        return result

    return text


def answer_student_question(
    question: str, subject_name: str, class_context_texts: List[str]
) -> str:
    """Answer student questions in Discussion Groups primarily using recent class lesson context."""
    context_block = ""
    if class_context_texts:
        formatted_notes = "\n---\n".join(class_context_texts)
        context_block = f"\nRECENT CLASS LESSON NOTES & CONTEXT:\n{formatted_notes}\n"

    prompt_text = (
        f"You are an encouraging, expert AI Tutor for the subject '{subject_name}'. "
        "A student asked a question in the class discussion group. "
        "Answer their question clearly, concisely, and accurately. "
        "Primarily base your answer on the class lesson notes provided below when relevant:\n"
        f"{context_block}\n"
        f"STUDENT QUESTION:\n{question}\n\n"
        "Provide a helpful tutor response tailored to a student:"
    )

    answer = call_gemini_api(prompt_text, timeout_sec=12)
    if answer:
        logger.info(f"[AI Q&A] Answered student question for {subject_name}.")
        return answer

    return f"🎓 <b>{subject_name} Tutor</b>: Thank you for your question! Let me review the lesson notes and get back to you shortly."
