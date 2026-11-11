import re

from app.models.enums import WorkArrangement

_HYBRID = re.compile(r"\bhybrid\b", re.I)
_REMOTE = re.compile(r"\b(?:remote|work\s+from\s+home|wfh|telecommut\w*|work\s+from\s+anywhere)\b", re.I)
_ONSITE = re.compile(r"\b(?:on[\s-]?site|in[\s-]?office|work\s+from\s+office|office[\s-]based|in[\s-]person)\b", re.I)
_DESC_REMOTE = re.compile(
    r"\b(?:fully|100%|completely)\s+remote\b|\bremote\s+(?:role|position|job|opportunity)\b"
    r"|\bthis\s+(?:is\s+a\s+)?remote\b|\bwork\s+from\s+home\b",
    re.I,
)
_DESC_ONSITE = re.compile(
    r"\bwork\s+from\s+office\b|\bon[\s-]?site\s+(?:role|position|job)\b|\bthis\s+(?:is\s+an?\s+)?on[\s-]?site\b", re.I
)


def parse_work_arrangement(value: str | None) -> WorkArrangement | None:
    """From an explicit field ('Remote', 'HYBRID', 'On-site', 'TELECOMMUTE') or short text. None = no signal."""
    if not value:
        return None
    if _HYBRID.search(value):
        return WorkArrangement.hybrid
    if _REMOTE.search(value):
        return WorkArrangement.remote
    if _ONSITE.search(value):
        return WorkArrangement.onsite
    return None


def infer_from_description(text: str | None) -> WorkArrangement | None:
    """Conservative: only explicit statements, since descriptions mention 'remote teams' etc. in passing."""
    if not text:
        return None
    if _HYBRID.search(text):
        return WorkArrangement.hybrid
    if _DESC_REMOTE.search(text):
        return WorkArrangement.remote
    if _DESC_ONSITE.search(text):
        return WorkArrangement.onsite
    return None


def infer_work_arrangement(*short_texts: str | None, description: str | None = None) -> WorkArrangement:
    """Title / location text first, then explicit statements in the description; unknown otherwise."""
    for t in short_texts:
        found = parse_work_arrangement(t)
        if found:
            return found
    return infer_from_description(description) or WorkArrangement.unknown
