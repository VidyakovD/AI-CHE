"""add vkma_* tables: communities, agents, bots, mailings, subscribers, content_posts.

Импорт VK Mini App (aichevk.ru) в aiche-кодбейз как surface "VKMA".
До этого момента VK MiniApp жил отдельным процессом, общался через HMAC
Internal API. После — единый процесс, прямые вызовы server.billing/ai.

Все vkma_* таблицы имеют `user_id INTEGER FK → users(id)` — раньше в VK
проекте было UUID. На существующих 1 vk-юзере (Денис, aiche_user_id=7)
backfill через отдельный скрипт переноса данных (раздел 7
MERGE_INTO_AICHE.md).

Также добавляем колонку users.pd_consent_at (152-ФЗ согласие на ПД,
opt-in галочка в VK Mini App).

Портабельность: миграция написана под PG (прод) + SQLite (dev).
- UUID PK → String(36), default через Python (без gen_random_uuid())
- JSONB → JSON (sa.JSON)
- ARRAY → JSON (encoded list)
- TIMESTAMPTZ → DateTime (на SQLite tz игнорируется)

Revision ID: f1a3c5b7d2e9
Revises: 506fa9eb9a82
Create Date: 2026-06-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f1a3c5b7d2e9"
down_revision: Union[str, Sequence[str], None] = "506fa9eb9a82"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _user_id_fk_type() -> sa.types.TypeEngine:
    """users.id — INTEGER на aiche (см. server/models.py:User.id).
    Все FK в vkma_* таблицах используют тот же тип."""
    return sa.Integer()


def upgrade() -> None:
    # ── ALTER users — единый VK Mini App PD согласие (152-ФЗ) ─────────
    # Колонка добавляется только если её ещё нет (для случая когда
    # LIGHTWEIGHT_MIGRATIONS уже создала). Используем reflection + check.
    # NOTE: на свежей dev-БД (без users) пропускаем — таблица users будет
    # создана через Base.metadata.create_all() из server/db.py. Alembic
    # — это для последующих изменений схемы (см. baseline.py docstring).
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "users" in inspector.get_table_names():
        user_cols = {c["name"] for c in inspector.get_columns("users")}
        if "pd_consent_at" not in user_cols:
            op.add_column("users",
                          sa.Column("pd_consent_at", sa.DateTime(), nullable=True))

    # ── vkma_communities ─────────────────────────────────────────────
    op.create_table(
        "vkma_communities",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", _user_id_fk_type(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("vk_group_id", sa.BigInteger(), nullable=False),
        sa.Column("group_name", sa.String(255), nullable=True),
        sa.Column("group_avatar", sa.String(), nullable=True),
        sa.Column("access_token_encrypted", sa.String(), nullable=False),
        sa.Column("token_permissions", sa.JSON(), nullable=True),
        sa.Column("callback_server_id", sa.Integer(), nullable=True),
        sa.Column("callback_confirm_code", sa.String(20), nullable=True),
        sa.Column("callback_secret", sa.String(64), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("connected_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "vk_group_id",
                            name="uq_vkma_communities_user_vk_group"),
    )

    # ── vkma_subscribers ─────────────────────────────────────────────
    op.create_table(
        "vkma_subscribers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("community_id", sa.String(36),
                  sa.ForeignKey("vkma_communities.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("vk_user_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("first_name", sa.String(100), nullable=True),
        sa.Column("last_name", sa.String(100), nullable=True),
        sa.Column("sex", sa.SmallInteger(), nullable=True),
        sa.Column("city", sa.String(100), nullable=True),
        sa.Column("birth_date", sa.Date(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("variables", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("last_interaction_at", sa.DateTime(), nullable=True),
        sa.Column("can_message", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("community_id", "vk_user_id",
                            name="uq_vkma_subscribers_community_vk_user"),
    )

    # ── vkma_ai_agents ───────────────────────────────────────────────
    op.create_table(
        "vkma_ai_agents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("community_id", sa.String(36),
                  sa.ForeignKey("vkma_communities.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("role", sa.String(100), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("tone", sa.String(50), nullable=True),
        sa.Column("llm_provider", sa.String(50), nullable=False,
                  server_default="openai"),
        sa.Column("llm_model", sa.String(100), nullable=True),
        sa.Column("temperature", sa.Numeric(2, 1), nullable=False,
                  server_default="0.7"),
        sa.Column("confidence_threshold", sa.Numeric(3, 2), nullable=False,
                  server_default="0.6"),
        sa.Column("fallback_action", sa.String(50), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
    )

    # ── vkma_knowledge_base_documents ─────────────────────────────────
    op.create_table(
        "vkma_knowledge_base_documents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("agent_id", sa.String(36),
                  sa.ForeignKey("vkma_ai_agents.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("source_type", sa.String(50), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("file_name", sa.String(255), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("content_text", sa.Text(), nullable=True),
        sa.Column("chunks_count", sa.Integer(), nullable=True),
        sa.Column("qdrant_collection", sa.String(100), nullable=True),
        sa.Column("indexed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
    )

    # ── vkma_agent_conversations ─────────────────────────────────────
    op.create_table(
        "vkma_agent_conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("agent_id", sa.String(36),
                  sa.ForeignKey("vkma_ai_agents.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("subscriber_id", sa.String(36),
                  sa.ForeignKey("vkma_subscribers.id", ondelete="CASCADE"),
                  nullable=True, index=True),
        sa.Column("messages", sa.JSON(), nullable=False),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("credits_spent", sa.Numeric(10, 4), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
    )

    # ── vkma_bot_flows ───────────────────────────────────────────────
    op.create_table(
        "vkma_bot_flows",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("community_id", sa.String(36),
                  sa.ForeignKey("vkma_communities.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("trigger_type", sa.String(50), nullable=False),
        sa.Column("trigger_value", sa.Text(), nullable=True),
        sa.Column("flow_graph", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
    )

    # ── vkma_bot_states ──────────────────────────────────────────────
    op.create_table(
        "vkma_bot_states",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("flow_id", sa.String(36),
                  sa.ForeignKey("vkma_bot_flows.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("subscriber_id", sa.String(36),
                  sa.ForeignKey("vkma_subscribers.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("current_node_id", sa.String(100), nullable=True),
        sa.Column("context", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("started_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("last_activity_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("flow_id", "subscriber_id",
                            name="uq_vkma_bot_states_flow_subscriber"),
    )

    # ── vkma_mailings ────────────────────────────────────────────────
    op.create_table(
        "vkma_mailings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("community_id", sa.String(36),
                  sa.ForeignKey("vkma_communities.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("message_text", sa.Text(), nullable=True),
        sa.Column("attachments", sa.JSON(), nullable=True),
        sa.Column("segment_filter", sa.JSON(), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("total_recipients", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sent_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_vkma_mailings_status_scheduled", "vkma_mailings",
                    ["status", "scheduled_at"])

    # ── vkma_content_posts ───────────────────────────────────────────
    op.create_table(
        "vkma_content_posts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("community_id", sa.String(36),
                  sa.ForeignKey("vkma_communities.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("image_urls", sa.JSON(), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(), nullable=True, index=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("vk_post_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("ai_generated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
    )


def downgrade() -> None:
    """Откатить миграцию — дропнуть все 9 vkma_* таблиц в обратном порядке FK."""
    op.drop_index("ix_vkma_mailings_status_scheduled", table_name="vkma_mailings")
    op.drop_table("vkma_content_posts")
    op.drop_table("vkma_mailings")
    op.drop_table("vkma_bot_states")
    op.drop_table("vkma_bot_flows")
    op.drop_table("vkma_agent_conversations")
    op.drop_table("vkma_knowledge_base_documents")
    op.drop_table("vkma_ai_agents")
    op.drop_table("vkma_subscribers")
    op.drop_table("vkma_communities")

    # pd_consent_at — оставляем, на случай если в LIGHTWEIGHT_MIGRATIONS уже
    # был. Полный rollback delete-column'a — отдельной миграцией если надо.
