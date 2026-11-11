from datetime import datetime
from typing import Any, Literal

from pydantic import Field, HttpUrl

from app.core.schema import APIModel


class SettingOut(APIModel):
    key: str
    value: dict[str, Any]


class SavedViewBase(APIModel):
    page: Literal["jobs", "inbox", "applications", "companies"]
    name: str = Field(min_length=1, max_length=80)
    filters: dict[str, Any] = Field(default_factory=dict)  # same keys as the page's query params
    sort: list[Any] = Field(default_factory=list)
    columns: list[Any] | None = None
    is_default: bool = False


class SavedViewCreate(SavedViewBase):
    pass


class SavedViewUpdate(APIModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    filters: dict[str, Any] | None = None
    sort: list[Any] | None = None
    columns: list[Any] | None = None
    is_default: bool | None = None
    position: int | None = None


class SavedViewOut(SavedViewBase):
    id: int
    position: int
    created_at: datetime


class ResumeVersionCreate(APIModel):
    label: str = Field(min_length=1, max_length=120)
    file_url: HttpUrl | None = None
    notes: str | None = None


class ResumeVersionUpdate(APIModel):
    label: str | None = Field(default=None, min_length=1, max_length=120)
    file_url: HttpUrl | None = None
    notes: str | None = None
    archived: bool | None = None


class ResumeVersionOut(APIModel):
    id: int
    label: str
    file_url: str | None
    notes: str | None
    created_at: datetime
    archived_at: datetime | None


class TagCreate(APIModel):
    name: str = Field(min_length=1, max_length=40)
    color: str = "gray"


class TagUpdate(APIModel):
    name: str | None = Field(default=None, min_length=1, max_length=40)
    color: str | None = None


class TagOut(APIModel):
    id: int
    name: str
    color: str
