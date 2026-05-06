import asyncio
import logging
from datetime import datetime

from vkbottle import API, Bot
from vkbottle.bot import Message

from bot.classifier import is_delayed_response, requires_response
from bot.config import get_curator_ids, get_head_curator_id, get_today_local
from db import crud

logger = logging.getLogger(__name__)


def build_bot(token: str, group_id: int, label: str = "") -> Bot:
    if not token:
        raise RuntimeError("VK token is empty")

    bot = Bot(token=token)
    bot_label = label or f"group{group_id}"

    def _is_head_curator(user_id: int) -> bool:
        head = get_head_curator_id()
        return head is not None and user_id == head

    @bot.on.chat_message(text="/start")
    async def cmd_start(message: Message):
        if not _is_head_curator(message.from_id):
            return

        title = await _fetch_chat_title(bot.api, message.peer_id)
        await crud.add_conversation(message.peer_id, group_id, title)
        await message.answer(
            f'Беседа "{title or message.peer_id}" добавлена в мониторинг.'
        )
        logger.info(
            "[%s] Conversation activated: peer_id=%s group_id=%s",
            bot_label, message.peer_id, group_id,
        )

    @bot.on.chat_message(text="/stop")
    async def cmd_stop(message: Message):
        if not _is_head_curator(message.from_id):
            return

        ok = await crud.deactivate_conversation(message.peer_id, group_id)
        if ok:
            await message.answer("Беседа снята с мониторинга.")
            logger.info(
                "[%s] Conversation deactivated: peer_id=%s group_id=%s",
                bot_label, message.peer_id, group_id,
            )
        else:
            await message.answer("Эта беседа не была в мониторинге.")

    @bot.on.chat_message(text="/delete")
    async def cmd_delete(message: Message):
        if not _is_head_curator(message.from_id):
            return

        deleted, msgs = await crud.delete_conversation(message.peer_id, group_id)
        if deleted:
            await message.answer(
                f"Беседа удалена из БД. Удалено сообщений: {msgs}.\n"
                "Накопленные за сегодня данные потеряны, в саммари не попадут."
            )
            logger.info(
                "[%s] Conversation DELETED: peer_id=%s group_id=%s msgs=%d",
                bot_label, message.peer_id, group_id, msgs,
            )
        else:
            await message.answer("Беседа не найдена в БД.")

    @bot.on.chat_message(text="/sync")
    async def cmd_sync(message: Message):
        if not _is_head_curator(message.from_id):
            return

        added = 0
        updated = 0
        scanned = 0
        # Перебираем chat_id 1..50 (хватит на лимит ВК + запас)
        for i in range(1, 51):
            peer = 2_000_000_000 + i
            try:
                res = await bot.api.messages.get_conversations_by_id(peer_ids=[peer])
                items = getattr(res, "items", None) or (res.get("items") if isinstance(res, dict) else [])
                if not items:
                    continue
                first = items[0]
                cs = first.get("chat_settings") if isinstance(first, dict) else getattr(first, "chat_settings", None)
                title = (cs.get("title") if isinstance(cs, dict) else getattr(cs, "title", None)) if cs else None
                if not title:
                    continue
                scanned += 1
                existing = await crud.get_conversation_by_peer_id(peer, group_id)
                if existing:
                    if existing.title != title or not existing.is_active:
                        await crud.add_conversation(peer, group_id, title)
                        updated += 1
                else:
                    await crud.add_conversation(peer, group_id, title)
                    added += 1
            except Exception:
                continue

        await message.answer(
            f"Синхронизация завершена.\n"
            f"Найдено: {scanned}, добавлено новых: {added}, обновлено: {updated}."
        )
        logger.info(
            "[%s] /sync: scanned=%d added=%d updated=%d",
            bot_label, scanned, added, updated,
        )

    @bot.on.chat_message(text="/status")
    async def cmd_status(message: Message):
        if not _is_head_curator(message.from_id):
            return

        conv = await crud.get_conversation_by_peer_id(message.peer_id, group_id)
        if not conv:
            await message.answer("Беседа не отслеживается. Напишите /start.")
            return
        count = await crud.count_messages_for_day(conv.id, get_today_local())
        state = "активна" if conv.is_active else "отключена"
        await message.answer(
            f'Беседа "{conv.title or conv.vk_peer_id}" — {state}.\n'
            f"Сообщений за сегодня: {count}"
        )

    @bot.on.chat_message()
    async def on_any_chat_message(message: Message):
        if message.text and message.text.strip().startswith("/"):
            return

        conv = await crud.get_conversation_by_peer_id(message.peer_id, group_id)
        if not conv or not conv.is_active:
            return

        # Если title не подтянулся при /start (VK ещё не синкнулся) —
        # пробуем сейчас, в фоне, и обновляем БД
        if not conv.title:
            title = await _fetch_chat_title(bot.api, message.peer_id, retries=1)
            if title:
                await crud.add_conversation(message.peer_id, group_id, title)
                logger.info("[%s] Title late-resolved for peer_id=%s: %s",
                            bot_label, message.peer_id, title)

        sender_name = await _fetch_user_name(bot.api, message.from_id)
        is_curator = message.from_id in get_curator_ids()
        role = "curator" if is_curator else "student"
        await crud.upsert_participant(message.from_id, sender_name, role=role)

        ts = message.date if isinstance(message.date, datetime) else datetime.fromtimestamp(message.date)
        if ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)
        saved = await crud.save_message(
            conversation_id=conv.id,
            vk_message_id=message.conversation_message_id or message.id,
            sender_id=message.from_id,
            sender_name=sender_name,
            text=message.text or "",
            timestamp=ts,
            is_from_student=not is_curator,
        )

        # Если это сообщение от куратора — в фоне проверяем "отвечу позже".
        # Не блокируем основной поток; результат запишется в БД позже.
        if is_curator and saved.text:
            asyncio.create_task(_classify_curator_message(saved.id, saved.text))
        # Если от ученика — в фоне классифицируем, требуется ли ответ.
        # Отчёты, благодарности, констатации не должны триггерить алерты.
        elif not is_curator and saved.text:
            asyncio.create_task(_classify_student_message(saved.id, saved.text))

    async def _classify_curator_message(msg_id: int, text: str) -> None:
        try:
            if await is_delayed_response(text):
                await crud.mark_message_delayed(msg_id)
                logger.info("Message %s flagged as delayed-response", msg_id)
        except Exception as exc:
            logger.warning(
                "Classifier failed for msg_id=%s: %s: %s",
                msg_id, type(exc).__name__, exc,
            )

    async def _classify_student_message(msg_id: int, text: str) -> None:
        try:
            needs = await requires_response(text)
            await crud.mark_message_requires_response(msg_id, needs)
            if not needs:
                logger.info("Message %s flagged as NOT requiring response", msg_id)
        except Exception as exc:
            logger.warning(
                "requires_response classifier failed for msg_id=%s: %s: %s",
                msg_id, type(exc).__name__, exc,
            )

    return bot


async def _fetch_chat_title(api: API, peer_id: int, retries: int = 3) -> str | None:
    """VK API не сразу видит беседу после того как бот в неё добавлен.
    Повторяем несколько раз с задержкой."""
    for attempt in range(1, retries + 1):
        try:
            result = await api.messages.get_conversations_by_id(peer_ids=[peer_id])
            if isinstance(result, dict):
                items = result.get("items", [])
            else:
                items = getattr(result, "items", None) or []
            if items:
                first = items[0]
                if isinstance(first, dict):
                    cs = first.get("chat_settings") or {}
                    title = cs.get("title") if isinstance(cs, dict) else None
                else:
                    cs = getattr(first, "chat_settings", None)
                    title = getattr(cs, "title", None) if cs else None
                if title:
                    return title
        except Exception as exc:
            logger.warning(
                "fetch_title peer_id=%s attempt=%d failed: %s: %s",
                peer_id, attempt, type(exc).__name__, exc,
            )

        if attempt < retries:
            await asyncio.sleep(2)
    logger.warning(
        "fetch_title gave up for peer_id=%s after %d attempts (likely VK API hadn't propagated membership yet)",
        peer_id, retries,
    )
    return None


async def _fetch_user_name(api: API, user_id: int) -> str | None:
    if user_id < 0:
        try:
            result = await api.groups.get_by_id(group_ids=[abs(user_id)])
            return result[0].name if result else None
        except Exception:
            return None
    try:
        result = await api.users.get(user_ids=[user_id])
        if result:
            u = result[0]
            return f"{u.first_name} {u.last_name}".strip()
    except Exception as exc:
        logger.warning("Failed to fetch user name: %s", type(exc).__name__)
    return None
