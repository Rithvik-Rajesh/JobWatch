"""Companies, sources, statuses, settings and other configuration-like tables."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, IdMixin, TimestampMixin
from app.models.enums import ExtractorKind, HealthStatus, SourceKind, StatusCategory
from app.models.types import pg_enum


class Company(IdMixin, TimestampMixin, Base):
    __tablename__ = "companies"

    name: Mapped[str] = mapped_column(CITEXT)
    slug: Mapped[str] = mapped_column(CITEXT, unique=True)
    website_url: Mapped[str | None] = mapped_column(Text)
    careers_url: Mapped[str | None] = mapped_column(Text)
    logo_url: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    monitoring_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    extractor: Mapped[ExtractorKind] = mapped_column(
        pg_enum(ExtractorKind, "extractor_kind"), server_default=ExtractorKind.none.value
    )
    # Validated by the extractor's Pydantic Config model; never free-form at the API boundary.
    extractor_config: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    check_interval_min: Mapped[int] = mapped_column(Integer, server_default=text("30"))
    next_check_at: Mapped[datetime | None]
    health: Mapped[HealthStatus] = mapped_column(
        pg_enum(HealthStatus, "health_status"), server_default=HealthStatus.unknown.value
    )
    consecutive_failures: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_checked_at: Mapped[datetime | None]
    last_success_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    open_jobs_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    baseline_jobs_count: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)
    archived_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint("check_interval_min between 10 and 1440", name="check_interval_range"),
        CheckConstraint("extractor = 'none' or careers_url is not null", name="extractor_requires_url"),
        Index(
            "ix_companies_due",
            "next_check_at",
            postgresql_where=text("monitoring_enabled and archived_at is null"),
        ),
        Index("ix_companies_name_trgm", "name", postgresql_using="gin", postgresql_ops={"name": "gin_trgm_ops"}),
    )


class CompanyAlias(Base):
    """Alternative names used to resolve aggregator / captured jobs to a company."""

    __tablename__ = "company_aliases"

    company_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True
    )
    alias: Mapped[str] = mapped_column(CITEXT, primary_key=True)

    __table_args__ = (UniqueConstraint("alias"),)


class Source(IdMixin, TimestampMixin, Base):
    """Origin platform. All company career sites share the single 'career_site' row."""

    __tablename__ = "sources"

    key: Mapped[str] = mapped_column(CITEXT, unique=True)
    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[SourceKind] = mapped_column(pg_enum(SourceKind, "source_kind"))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    homepage: Mapped[str | None] = mapped_column(Text)


class Status(Base):
    """User-configurable application status. Behaviour keys off `category`, never `label`."""

    __tablename__ = "statuses"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(CITEXT, unique=True)
    label: Mapped[str] = mapped_column(Text)
    category: Mapped[StatusCategory] = mapped_column(pg_enum(StatusCategory, "status_category"))
    color: Mapped[str] = mapped_column(Text, server_default="gray")
    sort_order: Mapped[int] = mapped_column(SmallInteger)
    show_on_board: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    is_system: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    archived_at: Mapped[datetime | None]


class ResumeVersion(IdMixin, Base):
    __tablename__ = "resume_versions"

    label: Mapped[str] = mapped_column(Text, unique=True)
    file_url: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    archived_at: Mapped[datetime | None]


class Tag(IdMixin, Base):
    __tablename__ = "tags"

    name: Mapped[str] = mapped_column(CITEXT, unique=True)
    color: Mapped[str] = mapped_column(Text, server_default="gray")


class AppSetting(Base):
    """Typed key/value preferences; each key is validated by a Pydantic model in app.settings."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(server_default=text("now()"), onupdate=text("now()"))


class SavedView(IdMixin, Base):
    __tablename__ = "saved_views"

    page: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    filters: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    sort: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    columns: Mapped[list[Any] | None] = mapped_column(JSONB)
    position: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    is_default: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))

    __table_args__ = (
        UniqueConstraint("page", "name"),
        CheckConstraint("page in ('jobs','inbox','applications','companies')", name="page_valid"),
    )
