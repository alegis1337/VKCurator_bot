from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("vk_peer_id", "vk_group_id", name="uq_conv_peer_group"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vk_peer_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    vk_group_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    title: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # Время последнего сообщения от ученика (не-куратора). Используется
    # для решения: слать ли ежедневное напоминание про отчёт в этот чат.
    last_student_message_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    messages: Mapped[list["Message"]] = relationship(back_populates="conversation")


class Participant(Base):
    __tablename__ = "participants"
    __table_args__ = (
        CheckConstraint("role IN ('curator', 'student', 'unknown')", name="role_check"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vk_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="unknown")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("idx_messages_conversation_timestamp", "conversation_id", "timestamp"),
        Index("idx_messages_timestamp", "timestamp"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("conversations.id"), nullable=False
    )
    vk_message_id: Mapped[int | None] = mapped_column(BigInteger)
    sender_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sender_name: Mapped[str | None] = mapped_column(String(255))
    text: Mapped[str | None] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # Когда мы уже алертили главного куратора по этому сообщению (NULL = ещё нет)
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Сообщение от куратора типа "отвечу позже" без указания конкретного времени
    is_delayed_response: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Когда напомнили куратору про этот "отложенный" ответ (NULL = ещё нет)
    delayed_alerted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Для сообщений ученика: требуется ли ответ куратора?
    # NULL = ещё не классифицировано (по умолчанию считаем что требует),
    # True = вопрос/просьба, False = отчёт/констатация/благодарность.
    requires_response: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")
