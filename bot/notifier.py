"""Уведомления главному куратору о беседах, в которых нет ответа на сообщение
ученика дольше порогового времени.

- Запускается раз в N минут шедулером.
- Шлёт алерты только в рабочее окно (по умолчанию ПН-СБ 11:00-19:00 МСК).
- Один алерт на одно сообщение ученика (через `messages.alerted_at`).
"""

import logging
import os
import random
from datetime import datetime

import pytz
from vkbottle import API

from bot.config import get_curator_ids
from db import crud

logger = logging.getLogger(__name__)


def _is_working_time() -> bool:
    tz = pytz.timezone(os.getenv("TIMEZONE", "Europe/Moscow"))
    now = datetime.now(tz)
    weekday = now.weekday()  # 0=Mon .. 6=Sun
    # ВС — выходной
    if weekday == 6:
        return False

    work_start = int(os.getenv("WORK_HOURS_START", "11"))
    work_end = int(os.getenv("WORK_HOURS_END", "19"))
    return work_start <= now.hour < work_end


def _get_recipient_id() -> int | None:
    raw = os.getenv("ALERT_RECIPIENT_ID", "").strip()
    if raw.lstrip("-").isdigit():
        return int(raw)
    return None


def _get_threshold_seconds() -> int:
    hours = float(os.getenv("ALERT_THRESHOLD_HOURS", "2"))
    return int(hours * 3600)


class Notifier:
    """Хранит API-инстансы всех ботов и пробует отправлять через каждый,
    пока не получится."""

    def __init__(self, apis: list[API]):
        self.apis = apis

    async def send_dm(self, user_id: int, text: str) -> bool:
        """Возвращает True если хоть один бот доставил сообщение."""
        last_err: Exception | None = None
        for api in self.apis:
            try:
                await api.messages.send(
                    user_id=user_id,
                    random_id=random.randint(-(2**31), 2**31 - 1),
                    message=text,
                )
                return True
            except Exception as exc:
                last_err = exc
                continue
        logger.warning(
            "Failed to send DM to user_id=%s via any bot: %s",
            user_id, last_err,
        )
        return False


async def check_and_alert(notifier: Notifier) -> None:
    """Главная функция: вызывается шедулером каждые N минут."""
    if not _is_working_time():
        return

    recipient = _get_recipient_id()
    if not recipient:
        logger.warning("ALERT_RECIPIENT_ID not set — skip alerting")
        return

    curator_ids = get_curator_ids()
    threshold_seconds = _get_threshold_seconds()
    threshold_label = f"{threshold_seconds // 3600}ч" if threshold_seconds % 3600 == 0 else f"{threshold_seconds // 60}мин"

    pending = await crud.find_unanswered_student_messages(curator_ids, threshold_seconds)
    if not pending:
        return

    logger.info("Alert candidates: %d conversation(s) with unanswered messages", len(pending))

    for conv, msg in pending:
        title = conv.title or f"peer_{conv.vk_peer_id}"
        text = (
            f'Беседа "{title}": нет ответа на сообщение ученика '
            f"больше {threshold_label}."
        )
        ok = await notifier.send_dm(recipient, text)
        if ok:
            await crud.mark_message_alerted(msg.id)
            logger.info(
                "Alert sent: peer_id=%s msg_id=%s student_msg_ts=%s",
                conv.vk_peer_id, msg.id, msg.timestamp,
            )
