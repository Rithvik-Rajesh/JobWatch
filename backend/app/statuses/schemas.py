from pydantic import Field

from app.core.schema import APIModel
from app.models.enums import StatusCategory


class StatusOut(APIModel):
    id: int
    key: str
    label: str
    category: StatusCategory
    color: str
    sort_order: int
    show_on_board: bool
    is_system: bool


class StatusCreate(APIModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,40}$")
    label: str = Field(min_length=1, max_length=60)
    category: StatusCategory
    color: str = "gray"
    show_on_board: bool = True


class StatusUpdate(APIModel):
    label: str | None = Field(default=None, min_length=1, max_length=60)
    color: str | None = None
    show_on_board: bool | None = None
    # category is immutable for system statuses (code depends on it)
    category: StatusCategory | None = None


class StatusReorder(APIModel):
    ordered_ids: list[int] = Field(min_length=1)
