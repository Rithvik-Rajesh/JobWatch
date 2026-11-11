"""Typed application settings stored in the `settings` table (one JSON document per key).

CENTRAL CONTRACT — owned by the lead. Every key has a Pydantic model; reads fall back to defaults,
writes are validated. Other modules must read settings through `get_setting` only.
"""

from typing import Any

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting


class LocationScopeSettings(BaseModel):
    """The single definition of "jobs I care about by location". Default: India.

    Used by discovery (Workday country facet), ingest (jobs.location_scope), the default UI
    filters, the inbox and notifications. Changing it triggers a re-evaluation of all jobs.
    """

    countries: list[str] = Field(default_factory=lambda: ["IN"], min_length=1)  # ISO 3166-1 alpha-2
    include_remote_anywhere: bool = False  # remote jobs with no in-scope country count as in scope
    include_unknown: bool = True  # jobs with no parseable location are shown (flagged) rather than hidden

    @field_validator("countries")
    @classmethod
    def _upper(cls, v: list[str]) -> list[str]:
        out = sorted({c.strip().upper() for c in v if c.strip()})
        if any(len(c) != 2 for c in out):
            raise ValueError("countries must be ISO 3166-1 alpha-2 codes")
        return out


class TimezoneSettings(BaseModel):
    name: str = "Asia/Kolkata"


class ReminderDefaults(BaseModel):
    follow_up_after_applied_days: int | None = Field(default=7, ge=1, le=90)  # None disables auto follow-up
    notify_lead_minutes: int = Field(default=60, ge=0, le=10080)
    interview_prep_lead_hours: int = Field(default=24, ge=0, le=336)


class QuietHours(BaseModel):
    start: str = Field(pattern=r"^\d{2}:\d{2}$")  # local time, e.g. "23:00"
    end: str = Field(pattern=r"^\d{2}:\d{2}$")


class NotificationSettings(BaseModel):
    enabled: bool = False
    new_jobs: bool = True
    job_updates: bool = True
    scraper_failures: bool = True
    reminders: bool = True
    quiet_hours: QuietHours | None = None
    title_include: list[str] = Field(default_factory=list)
    title_exclude: list[str] = Field(default_factory=list)


SETTINGS_MODELS: dict[str, type[BaseModel]] = {
    "location_scope": LocationScopeSettings,
    "timezone": TimezoneSettings,
    "reminder_defaults": ReminderDefaults,
    "notifications": NotificationSettings,
}


async def get_setting[M: BaseModel](session: AsyncSession, model: type[M]) -> M:
    key = key_for(model)
    value = await session.scalar(select(AppSetting.value).where(AppSetting.key == key))
    return model.model_validate(value or {})


async def put_setting(session: AsyncSession, key: str, value: dict[str, Any]) -> BaseModel:
    model = SETTINGS_MODELS[key]
    validated = model.model_validate(value)
    stmt = insert(AppSetting).values(key=key, value=validated.model_dump(mode="json"))
    stmt = stmt.on_conflict_do_update(index_elements=[AppSetting.key], set_={"value": stmt.excluded.value})
    await session.execute(stmt)
    return validated


def key_for(model: type[BaseModel]) -> str:
    for key, m in SETTINGS_MODELS.items():
        if m is model:
            return key
    raise KeyError(model.__name__)
