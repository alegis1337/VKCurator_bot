"""Два типа уведомлений:

1. **Алерты главному куратору в личку** (`check_and_alert`) — если на
   сообщение ученика нет ответа дольше N часов. Только в рабочее окно.

2. **Напоминание ученикам в чат** (`send_daily_reminders`) — раз в день
   тегаем не-кураторов и просим написать отчёт. Шлём только в чаты с
   активностью ученика за последние 2 дня.
"""

import logging
import os
import random
from datetime import datetime

import pytz
from vkbottle import API, Bot

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
    """Главный куратор-наблюдатель — получает ВСЕ алерты как копию."""
    raw = os.getenv("ALERT_RECIPIENT_ID", "").strip()
    if raw.lstrip("-").isdigit():
        return int(raw)
    return None


# Кэш: peer_id -> (timestamp, set[curator_user_ids]).
# Чтобы не дёргать VK API при каждой проверке (раз в 15 мин у нас N бесед).
_members_cache: dict[tuple[int, int], tuple[float, set[int]]] = {}
_MEMBERS_CACHE_TTL = 30 * 60  # 30 минут


async def _curators_in_conversation(api: API, peer_id: int, group_id: int, curator_ids: set[int]) -> set[int]:
    """Кураторы, которые состоят в данной беседе. С кэшем."""
    import time

    key = (peer_id, group_id)
    now = time.time()
    cached = _members_cache.get(key)
    if cached and now - cached[0] < _MEMBERS_CACHE_TTL:
        return cached[1] & curator_ids

    try:
        res = await api.messages.get_conversation_members(peer_id=peer_id, group_id=group_id)
        items = getattr(res, "items", None) or (res.get("items") if isinstance(res, dict) else [])
        member_ids = set()
        for it in items:
            mid = it.get("member_id") if isinstance(it, dict) else getattr(it, "member_id", None)
            if mid is not None and mid > 0:
                member_ids.add(mid)
        _members_cache[key] = (now, member_ids)
        return member_ids & curator_ids
    except Exception as exc:
        logger.warning(
            "get_conversation_members failed for peer=%s group=%s: %s",
            peer_id, group_id, type(exc).__name__,
        )
        return set()


def _get_threshold_seconds() -> int:
    hours = float(os.getenv("ALERT_THRESHOLD_HOURS", "2"))
    return int(hours * 3600)


def _chat_url(vk_peer_id: int) -> str:
    """VK URL беседы. peer_id = 2000000000 + chat_id, нам нужен chat_id."""
    chat_id = vk_peer_id - 2_000_000_000
    return f"https://vk.com/im?sel=c{chat_id}"


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


async def check_and_alert(notifier: Notifier, bots: list[Bot] | None = None) -> None:
    """Главная функция: вызывается шедулером каждые N минут.

    Алерты идут:
    - dewal (ALERT_RECIPIENT_ID) — всегда (главный куратор-наблюдатель)
    - Каждому куратору из CURATOR_IDS, если он УЧАСТНИК беседы

    Один алерт на сообщение (alerted_at), независимо от количества получателей.
    """
    if not _is_working_time():
        return

    overseer = _get_recipient_id()
    curator_ids = get_curator_ids()
    threshold_seconds = _get_threshold_seconds()
    threshold_label = (
        f"{threshold_seconds // 3600}ч"
        if threshold_seconds % 3600 == 0
        else f"{threshold_seconds // 60}мин"
    )

    pending = await crud.find_unanswered_student_messages(curator_ids, threshold_seconds)
    if not pending:
        return

    logger.info("Alert candidates: %d conversation(s) with unanswered messages", len(pending))

    # group_id -> bot (для получения участников беседы)
    bot_by_group: dict[int, Bot] = {}
    if bots:
        for b in bots:
            try:
                res = await b.api.groups.get_by_id(group_ids=[])
                if res.groups:
                    bot_by_group[res.groups[0].id] = b
            except Exception:
                continue

    for conv, msg in pending:
        title = conv.title or f"peer_{conv.vk_peer_id}"
        # Текст для кураторов в чате — со ссылкой на беседу
        text_with_link = (
            f'Беседа "{title}": нет ответа на сообщение ученика '
            f"больше {threshold_label}.\n"
            f"Открыть: {_chat_url(conv.vk_peer_id)}"
        )
        # Текст для overseer (dewal) — без ссылки, он не в беседах
        text_no_link = (
            f'Беседа "{title}": нет ответа на сообщение ученика '
            f"больше {threshold_label}."
        )

        in_chat: set[int] = set()
        bot = bot_by_group.get(conv.vk_group_id)
        if bot:
            in_chat = await _curators_in_conversation(
                bot.api, conv.vk_peer_id, conv.vk_group_id, curator_ids
            )

        recipients_count = len(in_chat) + (1 if overseer else 0)
        if recipients_count == 0:
            logger.warning("No recipients for alert in peer=%s", conv.vk_peer_id)
            continue

        delivered_to_any = False
        # Кураторы из беседы — со ссылкой
        for uid in in_chat:
            if await notifier.send_dm(uid, text_with_link):
                delivered_to_any = True
        # Overseer (dewal) — без ссылки (только если он не сам куратор беседы)
        if overseer and overseer not in in_chat:
            if await notifier.send_dm(overseer, text_no_link):
                delivered_to_any = True

        if delivered_to_any:
            await crud.mark_message_alerted(msg.id)
            logger.info(
                "Alert sent: peer_id=%s msg_id=%s recipients=%d",
                conv.vk_peer_id, msg.id, recipients_count,
            )


async def check_delayed_responses(notifier: Notifier) -> None:
    """Каждые ~10 мин в рабочее окно: проверяет сообщения куратора, помеченные
    is_delayed_response=True (через `bot/classifier.py`). Если прошёл час и
    куратор не написал в эту беседу новых сообщений — отправляем ему в личку
    напоминание."""
    if not _is_working_time():
        return

    threshold_hours = float(os.getenv("DELAYED_THRESHOLD_HOURS", "1"))
    threshold_seconds = int(threshold_hours * 3600)

    pending = await crud.find_pending_delayed_responses(threshold_seconds)
    if not pending:
        return

    logger.info("Delayed-response check: %d candidate(s)", len(pending))

    overseer = _get_recipient_id()
    for conv, msg in pending:
        title = conv.title or f"peer_{conv.vk_peer_id}"
        # Куратору-автору — со ссылкой на беседу
        text_with_link = (
            f'Напоминание: в беседе "{title}" {threshold_hours:g}ч назад '
            f"ты сказал «отвечу позже». Не забудь ответить.\n"
            f"Открыть: {_chat_url(conv.vk_peer_id)}"
        )
        # Overseer (dewal) — без ссылки
        text_no_link = (
            f'Напоминание: в беседе "{title}" куратор {threshold_hours:g}ч '
            f"назад сказал «отвечу позже» и пока не ответил."
        )

        delivered_to_any = False
        if await notifier.send_dm(msg.sender_id, text_with_link):
            delivered_to_any = True
        if overseer and overseer != msg.sender_id:
            if await notifier.send_dm(overseer, text_no_link):
                delivered_to_any = True

        if delivered_to_any:
            await crud.mark_delayed_alerted(msg.id)
            logger.info(
                "Delayed-response alert sent: peer=%s msg=%s curator=%s",
                conv.vk_peer_id, msg.id, msg.sender_id,
            )


# ============================================================
# Ежедневные напоминания ученикам в чат
# ============================================================

REMINDER_TEMPLATE = "Привет, {mentions}! Пришли, пожалуйста, отчёт о работе за сегодня."


async def _get_chat_member_ids(api: API, peer_id: int, group_id: int) -> list[int]:
    """Получить user_id всех участников беседы (только пользователи, без сообществ).
    Возвращает только положительные user_id."""
    try:
        res = await api.messages.get_conversation_members(peer_id=peer_id, group_id=group_id)
        items = getattr(res, "items", None) or (res.get("items") if isinstance(res, dict) else [])
        ids = []
        for it in items:
            mid = it.get("member_id") if isinstance(it, dict) else getattr(it, "member_id", None)
            if mid is not None and mid > 0:
                ids.append(mid)
        return ids
    except Exception as exc:
        logger.warning(
            "get_conversation_members failed for peer=%s group=%s: %s: %s",
            peer_id, group_id, type(exc).__name__, exc,
        )
        return []


async def _get_user_first_names(api: API, user_ids: list[int]) -> dict[int, str]:
    """Возвращает {user_id: first_name} для упоминаний."""
    if not user_ids:
        return {}
    try:
        res = await api.users.get(user_ids=user_ids)
        names = {}
        for u in res:
            uid = u.id if hasattr(u, "id") else u.get("id")
            fn = u.first_name if hasattr(u, "first_name") else u.get("first_name", "")
            names[uid] = fn or "ученик"
        return names
    except Exception as exc:
        logger.warning("users.get failed: %s: %s", type(exc).__name__, exc)
        return {uid: "ученик" for uid in user_ids}


async def send_daily_reminders(bots: list[Bot]) -> None:
    """В 20:00 ЕКБ: для каждой беседы с активностью ученика за последние 2 дня
    тегаем всех не-кураторов с просьбой написать отчёт."""
    threshold_hours = float(os.getenv("REMINDER_ACTIVITY_DAYS", "2")) * 24
    threshold_seconds = int(threshold_hours * 3600)

    convs = await crud.get_conversations_for_reminder(threshold_seconds)
    if not convs:
        logger.info("Daily reminder: no conversations with student activity in last 2 days")
        return

    logger.info("Daily reminder: %d conversation(s) eligible", len(convs))

    curator_ids = get_curator_ids()
    # group_id -> bot
    bot_by_group: dict[int, Bot] = {}
    for b in bots:
        try:
            res = await b.api.groups.get_by_id(group_ids=[])
            gid = res.groups[0].id
            bot_by_group[gid] = b
        except Exception as exc:
            logger.warning("Cannot resolve group_id for bot: %s", exc)

    for conv in convs:
        bot = bot_by_group.get(conv.vk_group_id)
        if not bot:
            logger.warning("No bot for group_id=%s, skipping conv=%s", conv.vk_group_id, conv.vk_peer_id)
            continue

        member_ids = await _get_chat_member_ids(bot.api, conv.vk_peer_id, conv.vk_group_id)
        students = [uid for uid in member_ids if uid not in curator_ids]
        if not students:
            logger.info("No students in conv peer=%s, skipping", conv.vk_peer_id)
            continue

        names = await _get_user_first_names(bot.api, students)
        # Формат VK-упоминания: [id123|Имя]
        mentions = ", ".join(f"[id{uid}|{names.get(uid, 'ученик')}]" for uid in students)
        text = REMINDER_TEMPLATE.format(mentions=mentions)

        try:
            await bot.api.messages.send(
                peer_id=conv.vk_peer_id,
                random_id=random.randint(-(2**31), 2**31 - 1),
                message=text,
                disable_mentions=0,
            )
            logger.info(
                "Reminder sent: peer=%s group=%s students=%d",
                conv.vk_peer_id, conv.vk_group_id, len(students),
            )
        except Exception as exc:
            logger.warning(
                "Failed to send reminder to peer=%s: %s: %s",
                conv.vk_peer_id, type(exc).__name__, exc,
            )
