import re

_REQ_CODES = re.compile(r"\b(?:jr|req|r|id|job)[-_ ]?\d{3,}\b|\b[a-z]{1,3}\d{4,}\b|#\s?\d+|\b\d{5,}\b")
_SUBS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bsr\b\.?"), "senior"),
    (re.compile(r"\bjr\b\.?"), "junior"),
    (re.compile(r"\b(?:engg|engr)\b\.?"), "engineer"),
    (re.compile(r"\bmgr\b\.?"), "manager"),
    (re.compile(r"\bdev\b"), "developer"),
    (re.compile(r"&"), " and "),
]


def normalize_title(title: str) -> str:
    """Lowercase, strip punctuation / requisition codes, unify common abbreviations, collapse spaces."""
    t = title.lower()
    t = _REQ_CODES.sub(" ", t)
    for pat, repl in _SUBS:
        t = pat.sub(repl, t)
    t = re.sub(r"[^\w+#\s]|_", " ", t)
    return re.sub(r"\s+", " ", t).strip()
