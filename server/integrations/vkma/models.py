"""SQLAlchemy-модели для 9 vkma_* таблиц.

Используется server.db.Base (общий метаданные с aiche). Импортируется
из server/integrations/vkma/__init__.py чтобы Base.metadata знал про эти
таблицы (важно для create_all() и для тестов).

Стиль — aiche-классический (Column, не Mapped/typing), sync SQLAlchemy.
Соответствует server/models.py, не vk_saas/backend/app/models/*.py
который был на async ORM.

PK — String(36) для UUID-значений с Python-default через uuid.uuid4().
JSON-поля — sa.JSON (портабельно на PG=JSONB + SQLite=TEXT).
FK на users.id — Integer (как aiche), не UUID.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, Column, Date, DateTime, ForeignKey, Index, Integer,
    JSON, Numeric, SmallInteger, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import relationship

from server.db import Base


def _uuid_str() -> str:
    return str(uuid.uuid4())


# ── vkma_communities ─────────────────────────────────────────────────────


class VkmaCommunity(Base):
    """Подключённое VK-сообщество. Один user может подключить несколько,
    но один vk_group_id — только раз на юзера."""
    __tablename__ = "vkma_communities"
    __table_args__ = (
        UniqueConstraint("user_id", "vk_group_id",
                         name="uq_vkma_communities_user_vk_group"),
    )

    id = Column(String(36), primary_key=True, default=_uuid_str)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    vk_group_id = Column(BigInteger, nullable=False)
    group_name = Column(String(255), nullable=True)
    group_avatar = Column(String, nullable=True)
    # Зашифрованный AES-256-GCM access_token сообщества (см. auth.encrypt_vk_token)
    access_token_encrypted = Column(String, nullable=False)
    token_permissions = Column(JSON, nullable=True)  # list[str]
    callback_server_id = Column(Integer, nullable=True)
    callback_confirm_code = Column(String(20), nullable=True)
    callback_secret = Column(String(64), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    connected_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    subscribers = relationship("VkmaSubscriber", back_populates="community",
                                cascade="all, delete-orphan")
    bot_flows = relationship("VkmaBotFlow", back_populates="community",
                              cascade="all, delete-orphan")
    mailings = relationship("VkmaMailing", back_populates="community",
                             cascade="all, delete-orphan")
    ai_agents = relationship("VkmaAIAgent", back_populates="community",
                              cascade="all, delete-orphan")
    content_posts = relationship("VkmaContentPost", back_populates="community",
                                  cascade="all, delete-orphan")


# ── vkma_subscribers ─────────────────────────────────────────────────────


class VkmaSubscriber(Base):
    """Подписчик VK-сообщества. Сегментация, переменные, 24h-окно для писем."""
    __tablename__ = "vkma_subscribers"
    __table_args__ = (
        UniqueConstraint("community_id", "vk_user_id",
                         name="uq_vkma_subscribers_community_vk_user"),
    )

    id = Column(String(36), primary_key=True, default=_uuid_str)
    community_id = Column(String(36),
                           ForeignKey("vkma_communities.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    vk_user_id = Column(BigInteger, nullable=False, index=True)
    first_name = Column(String(100), nullable=True)
    last_name = Column(String(100), nullable=True)
    sex = Column(SmallInteger, nullable=True)  # 1=female, 2=male
    city = Column(String(100), nullable=True)
    birth_date = Column(Date, nullable=True)
    tags = Column(JSON, nullable=True)  # list[str]
    variables = Column(JSON, nullable=False, default=dict)  # dict[str, Any]
    last_interaction_at = Column(DateTime, nullable=True)
    can_message = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    community = relationship("VkmaCommunity", back_populates="subscribers")
    bot_states = relationship("VkmaBotState", back_populates="subscriber",
                               cascade="all, delete-orphan")
    conversations = relationship("VkmaAgentConversation", back_populates="subscriber",
                                  cascade="all, delete-orphan")


# ── vkma_ai_agents + vkma_knowledge_base_documents + vkma_agent_conversations ──


class VkmaAIAgent(Base):
    """ИИ-агент привязан к VK-сообществу. role: consultant/sales/lead_qualifier/support."""
    __tablename__ = "vkma_ai_agents"

    id = Column(String(36), primary_key=True, default=_uuid_str)
    community_id = Column(String(36),
                           ForeignKey("vkma_communities.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    name = Column(String(255), nullable=False)
    role = Column(String(100), nullable=False)
    system_prompt = Column(Text, nullable=True)
    tone = Column(String(50), nullable=True)
    # 'openai' | 'anthropic' — какого aiche-провайдера использовать
    llm_provider = Column(String(50), nullable=False, default="openai")
    llm_model = Column(String(100), nullable=True)
    temperature = Column(Numeric(2, 1), nullable=False, default=0.7)
    confidence_threshold = Column(Numeric(3, 2), nullable=False, default=0.6)
    # 'transfer_operator' | 'static_message' — что делать если уверенность ниже порога
    fallback_action = Column(String(50), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    community = relationship("VkmaCommunity", back_populates="ai_agents")
    documents = relationship("VkmaKnowledgeBaseDocument", back_populates="agent",
                              cascade="all, delete-orphan")
    conversations = relationship("VkmaAgentConversation", back_populates="agent",
                                  cascade="all, delete-orphan")


class VkmaKnowledgeBaseDocument(Base):
    """Документ базы знаний — источник для RAG. source_type: file|url|manual|wall_post."""
    __tablename__ = "vkma_knowledge_base_documents"

    id = Column(String(36), primary_key=True, default=_uuid_str)
    agent_id = Column(String(36),
                       ForeignKey("vkma_ai_agents.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    source_type = Column(String(50), nullable=False)
    source_url = Column(Text, nullable=True)
    file_name = Column(String(255), nullable=True)
    file_size = Column(Integer, nullable=True)
    content_text = Column(Text, nullable=True)
    chunks_count = Column(Integer, nullable=True)
    qdrant_collection = Column(String(100), nullable=True)
    indexed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    agent = relationship("VkmaAIAgent", back_populates="documents")


class VkmaAgentConversation(Base):
    """История диалога ИИ-агента с подписчиком. status: active|closed|transferred.

    subscriber_id NULL означает test-chat от владельца через Mini App
    (без привязки к реальному VK-подписчику)."""
    __tablename__ = "vkma_agent_conversations"

    id = Column(String(36), primary_key=True, default=_uuid_str)
    agent_id = Column(String(36),
                       ForeignKey("vkma_ai_agents.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    subscriber_id = Column(String(36),
                            ForeignKey("vkma_subscribers.id", ondelete="CASCADE"),
                            nullable=True, index=True)
    messages = Column(JSON, nullable=False)  # [{role, content, timestamp}, ...]
    tokens_in = Column(Integer, nullable=True)
    tokens_out = Column(Integer, nullable=True)
    credits_spent = Column(Numeric(10, 4), nullable=True)
    status = Column(String(20), nullable=False, default="active")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow,
                         onupdate=datetime.utcnow)

    agent = relationship("VkmaAIAgent", back_populates="conversations")
    subscriber = relationship("VkmaSubscriber", back_populates="conversations")


# ── vkma_bot_flows + vkma_bot_states ─────────────────────────────────────


class VkmaBotFlow(Base):
    """Сценарий чат-бота: JSON-граф нод. trigger_type: keyword|start_command|subscribe|manual."""
    __tablename__ = "vkma_bot_flows"

    id = Column(String(36), primary_key=True, default=_uuid_str)
    community_id = Column(String(36),
                           ForeignKey("vkma_communities.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    trigger_type = Column(String(50), nullable=False)
    trigger_value = Column(Text, nullable=True)
    flow_graph = Column(JSON, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow,
                         onupdate=datetime.utcnow)

    community = relationship("VkmaCommunity", back_populates="bot_flows")
    states = relationship("VkmaBotState", back_populates="flow",
                           cascade="all, delete-orphan")


class VkmaBotState(Base):
    """Состояние подписчика в боте. Hot-path в Redis, БД как fallback."""
    __tablename__ = "vkma_bot_states"
    __table_args__ = (
        UniqueConstraint("flow_id", "subscriber_id",
                         name="uq_vkma_bot_states_flow_subscriber"),
    )

    id = Column(String(36), primary_key=True, default=_uuid_str)
    flow_id = Column(String(36),
                      ForeignKey("vkma_bot_flows.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    subscriber_id = Column(String(36),
                            ForeignKey("vkma_subscribers.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    current_node_id = Column(String(100), nullable=True)
    context = Column(JSON, nullable=False, default=dict)
    started_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_activity_at = Column(DateTime, nullable=False, default=datetime.utcnow,
                                onupdate=datetime.utcnow)

    flow = relationship("VkmaBotFlow", back_populates="states")
    subscriber = relationship("VkmaSubscriber", back_populates="bot_states")


# ── vkma_mailings ────────────────────────────────────────────────────────


class VkmaMailing(Base):
    """Рассылка: текст + сегмент + расписание + счётчики.
    status: draft|scheduled|sending|completed|failed."""
    __tablename__ = "vkma_mailings"
    __table_args__ = (
        Index("ix_vkma_mailings_status_scheduled", "status", "scheduled_at"),
    )

    id = Column(String(36), primary_key=True, default=_uuid_str)
    community_id = Column(String(36),
                           ForeignKey("vkma_communities.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    name = Column(String(255), nullable=False)
    message_text = Column(Text, nullable=True)
    attachments = Column(JSON, nullable=True)
    segment_filter = Column(JSON, nullable=True)
    scheduled_at = Column(DateTime, nullable=True)
    status = Column(String(20), nullable=False, default="draft")
    total_recipients = Column(Integer, nullable=False, default=0)
    sent_count = Column(Integer, nullable=False, default=0)
    failed_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    community = relationship("VkmaCommunity", back_populates="mailings")


# ── vkma_content_posts ───────────────────────────────────────────────────


class VkmaContentPost(Base):
    """Пост сообщества — черновик/запланирован/опубликован.
    status: draft|scheduled|published|failed."""
    __tablename__ = "vkma_content_posts"

    id = Column(String(36), primary_key=True, default=_uuid_str)
    community_id = Column(String(36),
                           ForeignKey("vkma_communities.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    text = Column(Text, nullable=True)
    image_urls = Column(JSON, nullable=True)  # list[str]
    scheduled_at = Column(DateTime, nullable=True, index=True)
    published_at = Column(DateTime, nullable=True)
    vk_post_id = Column(BigInteger, nullable=True)
    status = Column(String(20), nullable=False, default="draft")
    ai_generated = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    community = relationship("VkmaCommunity", back_populates="content_posts")
