import re
from datetime import datetime, timedelta

from app.models.enums import DatePrecision

_AGO = re.compile(r"(\d+)\s*\+?\s*(minute|hour|day|week|month)s?\s+ago", re.I)
_UNIT = {
    "minute": timedelta(minutes=1),
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(weeks=1),
    "month": timedelta(days=30),
}


def parse_relative_posted(text: str | None, now: datetime) -> tuple[datetime, DatePrecision] | None:
    """Workday-style 'Posted Today / Yesterday / N Days Ago / 30+ Days Ago' -> (approximate datetime, approximate)."""
    if not text:
        return None
    t = text.strip().lower()
    if re.search(r"\b(today|just now|just posted)\b", t):
        return now, DatePrecision.approximate
    if re.search(r"\byesterday\b", t):
        return now - timedelta(days=1), DatePrecision.approximate
    m = _AGO.search(t)
    if m:
        return now - int(m.group(1)) * _UNIT[m.group(2).lower()], DatePrecision.approximate
    return None
