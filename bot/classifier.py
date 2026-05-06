"""Классификатор сообщений куратора: «отвечу позже» без конкретного времени.

Гибрид:
- Сначала regex-prefilter — большинство сообщений отсеиваются мгновенно.
- Подозрительные отправляются в polza.ai (DeepSeek) для финального решения.
"""

import asyncio
import json
import logging
import os
import re

from openai import OpenAI

logger = logging.getLogger(__name__)

# Триггер-слова: если их нет в сообщении — точно НЕ "отвечу позже".
# Если есть — отправляем в LLM.
_PREFILTER = re.compile(
    r"\b("
    r"позже|попозже|потом|чуть|скоро|"
    r"отвеч|отпиш|посмотр|напиш|гл[яю]н|"
    r"занят|сейчас не|немного по"
    r")",
    re.IGNORECASE,
)

PROMPT = """Ты анализируешь сообщение куратора в учебной беседе ВКонтакте.

Сообщение: "{text}"

Определи: это сообщение типа "отвечу позже / посмотрю позже / потом гляну" БЕЗ
указания КОНКРЕТНОГО времени (часов, минут, сегодня вечером, завтра, и т.п.)?

Примеры ОТЛОЖЕННОГО ответа БЕЗ времени → is_delayed=true:
- "отвечу позже"
- "посмотрю чуть попозже"
- "напишу потом"
- "сейчас занят, отвечу позже"
- "гляну скоро"

Примеры с КОНКРЕТНЫМ временем → is_delayed=false:
- "отвечу через час"
- "отвечу к 18:00"
- "посмотрю вечером"
- "ответ завтра"
- "через 30 минут гляну"

Примеры ОБЫЧНЫХ ответов (не отложка) → is_delayed=false:
- "хорошо"
- "понял, спасибо"
- "да, давай"
- "посмотри в чате"

Верни ТОЛЬКО валидный JSON: {{"is_delayed": true}} или {{"is_delayed": false}}."""


def _looks_suspicious(text: str) -> bool:
    """Быстрый prefilter: содержит ли текст хоть один из триггеров."""
    if not text or len(text) > 500:
        # пустые или слишком длинные сообщения вряд ли "отвечу позже"
        return False
    return bool(_PREFILTER.search(text))


def _extract_json(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


async def is_delayed_response(text: str) -> bool:
    """Возвращает True если текст — это «отвечу позже» без конкретного времени."""
    if not _looks_suspicious(text):
        return False

    api_key = os.getenv("POLZA_API_KEY", "")
    if not api_key:
        logger.warning("POLZA_API_KEY not set — delayed-response check skipped")
        return False

    base_url = os.getenv("POLZA_BASE_URL", "https://polza.ai/api/v1")
    model = os.getenv("POLZA_MODEL", "deepseek-chat")
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=20.0)

    def _call():
        return client.chat.completions.create(
            model=model,
            max_tokens=50,
            temperature=0.0,
            messages=[
                {
                    "role": "system",
                    "content": "Возвращай ТОЛЬКО валидный JSON, без пояснений и markdown.",
                },
                {"role": "user", "content": PROMPT.format(text=text[:300])},
            ],
        )

    try:
        response = await asyncio.to_thread(_call)
        raw = response.choices[0].message.content if response.choices else ""
        data = _extract_json(raw or "")
        return bool(data.get("is_delayed"))
    except Exception as exc:
        logger.warning(
            "Delayed classifier failed: %s: %s", type(exc).__name__, exc
        )
        return False


# ============================================================
# Требует ли сообщение ученика ответа куратора?
# ============================================================

REQUIRES_RESPONSE_PROMPT = """Ты анализируешь сообщение ученика в учебной беседе ВКонтакте.

Сообщение: "{text}"

Определи: на это сообщение куратору НУЖНО ОБЯЗАТЕЛЬНО ответить?

Требуют ответа (requires_response=true):
- Вопросы (что, как, почему, когда, где, и т.п., особенно с "?")
- Просьбы помочь, подсказать, проверить, посмотреть
- Запросы на обратную связь
- Уточнения "правильно ли я делаю"

НЕ требуют обязательного ответа (requires_response=false):
- Отчёты о проделанной работе ("сделал то-то", "разобрался", "выполнил")
- Информирование/констатации ("я приду позже", "я в дороге")
- Благодарности ("спасибо")
- Короткие реакции ("ок", "понял", "хорошо")
- Извинения

Верни ТОЛЬКО валидный JSON: {{"requires_response": true}} или {{"requires_response": false}}."""


async def requires_response(text: str) -> bool:
    """Возвращает True если сообщение ученика требует ответа куратора.

    Дефолт при ошибках LLM — True (на всякий случай алертим, лучше
    лишний алерт чем пропустить вопрос)."""
    if not text or not text.strip():
        return False

    # Если есть знак вопроса — почти точно вопрос
    if "?" in text or "？" in text:
        return True

    api_key = os.getenv("POLZA_API_KEY", "")
    if not api_key:
        return True  # safe default

    base_url = os.getenv("POLZA_BASE_URL", "https://polza.ai/api/v1")
    model = os.getenv("POLZA_MODEL", "deepseek-chat")
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=20.0)

    def _call():
        return client.chat.completions.create(
            model=model,
            max_tokens=50,
            temperature=0.0,
            messages=[
                {
                    "role": "system",
                    "content": "Возвращай ТОЛЬКО валидный JSON.",
                },
                {"role": "user", "content": REQUIRES_RESPONSE_PROMPT.format(text=text[:500])},
            ],
        )

    try:
        response = await asyncio.to_thread(_call)
        raw = response.choices[0].message.content if response.choices else ""
        data = _extract_json(raw or "")
        return bool(data.get("requires_response", True))
    except Exception as exc:
        logger.warning("requires_response classifier failed: %s: %s", type(exc).__name__, exc)
        return True  # safe default
