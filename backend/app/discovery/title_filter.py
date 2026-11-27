"""Global title filters (setting `title_filters`), applied by the pipeline to discovered postings only."""

import re
from functools import lru_cache

from app.settings.registry import TitleFilterSettings


@lru_cache(maxsize=256)
def _compile(terms: tuple[str, ...]) -> re.Pattern[str] | None:
    parts = []
    for term in terms:
        words = [re.escape(w) for w in term.split() if w]
        if words:
            parts.append(r"\s+".join(words))
    if not parts:
        return None
    return re.compile(r"(?<!\w)(?:" + "|".join(parts) + r")(?!\w)", re.IGNORECASE)


def passes(title: str, cfg: TitleFilterSettings) -> bool:
    """Whole-word / whole-phrase, case-insensitive. Empty include keeps everything; exclude always wins."""
    exclude = _compile(tuple(t.strip().lower() for t in cfg.exclude if t.strip()))
    if exclude is not None and exclude.search(title):
        return False
    include = _compile(tuple(t.strip().lower() for t in cfg.include if t.strip()))
    return include is None or include.search(title) is not None
