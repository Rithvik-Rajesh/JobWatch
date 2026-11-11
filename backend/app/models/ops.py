"""Operational tables: scrape run telemetry and the notification outbox."""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, ForeignKey, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, IdMixin
from app.models.enums import ExtractorKind, OutboxState, RunStatus, RunTrigger
from app.models.types import pg_enum


class ScrapeRun(IdMixin, Base):
    __tablename__ = "scrape_runs"

    company_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("companies.id", ondelete="CASCADE"))
    source_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("sources.id", ondelete="CASCADE"))
    career_site_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("career_sites.id", ondelete="CASCADE"), index=True
    )
    trigger: Mapped[RunTrigger] = mapped_column(pg_enum(RunTrigger, "run_trigger"))
    status: Mapped[RunStatus] = mapped_column(pg_enum(RunStatus, "run_status"), server_default=RunStatus.queued.value)
    queued_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    extractor: Mapped[ExtractorKind | None] = mapped_column(pg_enum(ExtractorKind, "extractor_kind"))
    extractor_version: Mapped[str | None] = mapped_column(Text)
    complete: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    postings_seen: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    postings_in_scope: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    postings_out_of_scope: Mapped[int] = mapped_column(Integer, server_default=text("0"))  # dropped, not stored
    postings_unknown_scope: Mapped[int] = mapped_column(Integer, server_default=text("0"))  # stored, review queue
    postings_filtered: Mapped[int] = mapped_column(Integer, server_default=text("0"))  # title filters
    postings_new: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    postings_updated: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    postings_closed: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    postings_reopened: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    details_fetched: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    http_requests: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    error_class: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    warnings: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))

    __table_args__ = (
        Index("ix_scrape_runs_company_queued", "company_id", text("queued_at desc")),
        Index(
            "ix_scrape_runs_problems",
            text("queued_at desc"),
            postgresql_where=text("status in ('failed','quarantined','partial')"),
        ),
    )


class NotificationOutbox(IdMixin, Base):
    """Transactional outbox. Inserted in the same transaction as the domain change; the unique
    (channel, dedupe_key) makes duplicate notifications impossible."""

    __tablename__ = "notification_outbox"

    channel: Mapped[str] = mapped_column(Text, server_default="telegram")
    dedupe_key: Mapped[str] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    state: Mapped[OutboxState] = mapped_column(
        pg_enum(OutboxState, "outbox_state"), server_default=OutboxState.pending.value
    )
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    sent_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))

    __table_args__ = (
        UniqueConstraint("channel", "dedupe_key", name="uq_notification_outbox_dedupe"),
        Index("ix_notification_outbox_due", "next_attempt_at", postgresql_where=text("state in ('pending','failed')")),
    )
