import asyncio
import json
import logging
import os
import re
from datetime import date

from openai import OpenAI

from db.models import Conversation, Message

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://polza.ai/api/v1"
DEFAULT_MODEL = "deepseek-chat"

PROMPT_TEMPLATE = """Ты анализируешь переписку в учебной беседе ВКонтакте.
В беседе участвуют кураторы и ученик.

Переписка за {date}, беседа "{conversation_title}":
{messages_text}

Верни ТОЛЬКО валидный JSON без markdown-обёртки:
{{
  "date": "DD.MM.YYYY",
  "conversation": "название беседы",
  "task": "какое задание было дано ученику (или 'не выдано')",
  "messages_count": 0,
  "active_participants": ["имя1", "имя2"],
  "key_points": "краткое описание главного за день (2-3 предложения)"
}}"""


def _format_messages(messages: list[Message], curator_ids: set[int]) -> str:
    lines = []
    for m in messages:
        role = "куратор" if m.sender_id in curator_ids else "ученик"
        ts = m.timestamp.strftime("%H:%M")
        name = m.sender_name or f"id{m.sender_id}"
        text = (m.text or "").replace("\n", " ").strip()
        lines.append(f"[{ts}] {name} ({role}): {text}")
    return "\n".join(lines)


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def _fallback(target_day: date, conv_title: str, count: int, error: str) -> dict:
    safe_error = (error or "")[:120]
    return {
        "date": target_day.strftime("%d.%m.%Y"),
        "conversation": conv_title,
        "task": "не выдано",
        "messages_count": count,
        "active_participants": [],
        "key_points": f"не удалось сгенерировать саммари ({safe_error})",
    }


async def generate_summary(
    conversation: Conversation,
    messages: list[Message],
    curator_ids: set[int],
    target_day: date,
) -> dict:
    title = conversation.title or f"peer_{conversation.vk_peer_id}"

    if not messages:
        return {
            "date": target_day.strftime("%d.%m.%Y"),
            "conversation": title,
            "task": "не выдано",
            "messages_count": 0,
            "active_participants": [],
            "key_points": "за день не было сообщений",
        }

    api_key = os.getenv("POLZA_API_KEY", "")
    if not api_key:
        return _fallback(target_day, title, len(messages), "POLZA_API_KEY не задан")

    base_url = os.getenv("POLZA_BASE_URL", DEFAULT_BASE_URL)
    model = os.getenv("POLZA_MODEL", DEFAULT_MODEL)

    prompt = PROMPT_TEMPLATE.format(
        date=target_day.strftime("%d.%m.%Y"),
        conversation_title=title,
        messages_text=_format_messages(messages, curator_ids),
    )

    client = OpenAI(api_key=api_key, base_url=base_url, timeout=60.0)

    def _call():
        return client.chat.completions.create(
            model=model,
            max_tokens=1500,
            temperature=0.3,
            messages=[
                {
                    "role": "system",
                    "content": "Ты возвращаешь ТОЛЬКО валидный JSON без пояснений и без markdown.",
                },
                {"role": "user", "content": prompt},
            ],
        )

    try:
        response = await asyncio.to_thread(_call)
        raw = response.choices[0].message.content if response.choices else ""
        data = _extract_json(raw or "")
        data.setdefault("messages_count", len(messages))
        return data
    except json.JSONDecodeError as exc:
        logger.exception("Failed to parse model response")
        return _fallback(target_day, title, len(messages), f"JSON: {exc}")
    except Exception as exc:
        logger.exception("LLM API error")
        return _fallback(target_day, title, len(messages), type(exc).__name__)
