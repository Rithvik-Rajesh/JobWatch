from datetime import datetime
from typing import Any

from app.core.schema import APIModel
from app.models.enums import SourceKind


class SourceOut(APIModel):
    id: int
    key: str
    name: str
    kind: SourceKind
    enabled: bool
    config: dict[str, Any]
    homepage: str | None
    jobs_count: int = 0
    last_run_at: datetime | None = None


class SourceUpdate(APIModel):
    enabled: bool | None = None
    config: dict[str, Any] | None = None
