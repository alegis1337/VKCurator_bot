from datetime import date, datetime, time, timedelta

from sqlalchemy import delete, select, update

from db.database import async_session_factory
from db.models import Conversation, Message, Participant


async def add_conversation(
    vk_peer_id: int, vk_group_id: int, title: str | None
) -> Conversation:
    async with async_session_factory() as session:
        existing = await session.scalar(
            select(Conversation).where(
                Conversation.vk_peer_id == vk_peer_id,
                Conversation.vk_group_id == vk_group_id,
            )
        )
        if existing:
            existing.is_active = True
            if title:
                existing.title = title
            await session.commit()
            return existing

        conv = Conversation(
            vk_peer_id=vk_peer_id,
            vk_group_id=vk_group_id,
            title=title,
            is_active=True,
        )
        session.add(conv)
        await session.commit()
        await session.refresh(conv)
        return conv


async def delete_conversation(vk_peer_id: int, vk_group_id: int) -> tuple[bool, int]:
    """Полное удаление беседы + всех её сообщений из БД.
    Возвращает (удалена_ли_беседа, количество_удалённых_сообщений)."""
    async with async_session_factory() as session:
        conv = await session.scalar(
            select(Conversation).where(
                Conversation.vk_peer_id == vk_peer_id,
                Conversation.vk_group_id == vk_group_id,
            )
        )
        if not conv:
            return False, 0
        msgs_deleted = await session.execute(
            delete(Message).where(Message.conversation_id == conv.id)
        )
        await session.delete(conv)
        await session.commit()
        return True, msgs_deleted.rowcount or 0


async def deactivate_conversation(vk_peer_id: int, vk_group_id: int) -> bool:
    async with async_session_factory() as session:
        result = await session.execute(
            update(Conversation)
            .where(
                Conversation.vk_peer_id == vk_peer_id,
                Conversation.vk_group_id == vk_group_id,
            )
            .values(is_active=False)
        )
        await session.commit()
        return result.rowcount > 0


async def get_conversation_by_peer_id(
    vk_peer_id: int, vk_group_id: int
) -> Conversation | None:
    async with async_session_factory() as session:
        return await session.scalar(
            select(Conversation).where(
                Conversation.vk_peer_id == vk_peer_id,
                Conversation.vk_group_id == vk_group_id,
            )
        )


async def get_active_conversations() -> list[Conversation]:
    async with async_session_factory() as session:
        result = await session.scalars(
            select(Conversation).where(Conversation.is_active.is_(True))
        )
        return list(result.all())


async def save_message(
    conversation_id: int,
    vk_message_id: int | None,
    sender_id: int,
    sender_name: str | None,
    text: str | None,
    timestamp: datetime,
) -> None:
    async with async_session_factory() as session:
        msg = Message(
            conversation_id=conversation_id,
            vk_message_id=vk_message_id,
            sender_id=sender_id,
            sender_name=sender_name,
            text=text,
            timestamp=timestamp,
        )
        session.add(msg)
        await session.commit()


async def get_messages_for_day(conversation_id: int, day: date) -> list[Message]:
    start = datetime.combine(day, time.min)
    end = start + timedelta(days=1)
    async with async_session_factory() as session:
        result = await session.scalars(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.timestamp >= start,
                Message.timestamp < end,
            )
            .order_by(Message.timestamp.asc())
        )
        return list(result.all())


async def count_messages_for_day(conversation_id: int, day: date) -> int:
    return len(await get_messages_for_day(conversation_id, day))


async def delete_messages_for_day(conversation_id: int, day: date) -> int:
    start = datetime.combine(day, time.min)
    end = start + timedelta(days=1)
    async with async_session_factory() as session:
        result = await session.execute(
            delete(Message).where(
                Message.conversation_id == conversation_id,
                Message.timestamp >= start,
                Message.timestamp < end,
            )
        )
        await session.commit()
        return result.rowcount or 0


async def delete_old_messages(retention_days: int) -> int:
    threshold = datetime.utcnow() - timedelta(days=retention_days)
    async with async_session_factory() as session:
        result = await session.execute(
            delete(Message).where(Message.timestamp < threshold)
        )
        await session.commit()
        return result.rowcount or 0


async def find_unanswered_student_messages(
    curator_ids: set[int], threshold_seconds: int
):
    """Для каждой активной беседы возвращает САМОЕ РАННЕЕ сообщение ученика,
    на которое нет ответа куратора, оно старше threshold секунд, и про него
    ещё не было алерта.

    Возвращает list[(Conversation, Message)].
    """
    from datetime import datetime, timedelta

    threshold_ts = datetime.utcnow() - timedelta(seconds=threshold_seconds)

    async with async_session_factory() as session:
        convs = (
            await session.scalars(
                select(Conversation).where(Conversation.is_active.is_(True))
            )
        ).all()

        result = []
        for conv in convs:
            # Все сообщения беседы по возрастанию — для определения "после
            # последнего ответа куратора"
            msgs = (
                await session.scalars(
                    select(Message)
                    .where(Message.conversation_id == conv.id)
                    .order_by(Message.timestamp.asc())
                )
            ).all()
            if not msgs:
                continue

            # время последнего ответа куратора в этой беседе
            last_curator_ts = None
            for m in msgs:
                if m.sender_id in curator_ids:
                    last_curator_ts = m.timestamp

            # самое раннее сообщение ученика после last_curator_ts,
            # старше threshold, ещё не алертенное
            for m in msgs:
                if m.sender_id in curator_ids:
                    continue
                if last_curator_ts and m.timestamp <= last_curator_ts:
                    continue
                if m.alerted_at is not None:
                    continue
                if m.timestamp > threshold_ts:
                    # сообщение слишком свежее, ждём
                    break
                result.append((conv, m))
                break  # достаточно одного сообщения на беседу

        return result


async def mark_message_alerted(message_id: int) -> None:
    from datetime import datetime

    async with async_session_factory() as session:
        await session.execute(
            update(Message)
            .where(Message.id == message_id)
            .values(alerted_at=datetime.utcnow())
        )
        await session.commit()


async def upsert_participant(
    vk_user_id: int, full_name: str | None, role: str = "unknown"
) -> None:
    async with async_session_factory() as session:
        existing = await session.scalar(
            select(Participant).where(Participant.vk_user_id == vk_user_id)
        )
        if existing:
            if full_name and existing.full_name != full_name:
                existing.full_name = full_name
            if role != "unknown" and existing.role != role:
                existing.role = role
            await session.commit()
            return

        session.add(
            Participant(vk_user_id=vk_user_id, full_name=full_name, role=role)
        )
        await session.commit()
