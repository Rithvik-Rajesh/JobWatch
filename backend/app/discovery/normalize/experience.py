# ruff: noqa: RUF001
import re
from dataclasses import dataclass
from decimal import Decimal

_NUM = r"(\d{1,2}(?:\.\d)?)"
_YRS = r"(?:years?|yrs?)"
_PATTERNS = [
    # 2-5 years / 2 to 5 years / 2 - 5+ yrs
    re.compile(rf"{_NUM}\s*\+?\s*(?:-|–|—|to)\s*{_NUM}\s*\+?\s*{_YRS}", re.I),
    # minimum of 4 years / at least 4 years / min. 4 yrs
    re.compile(rf"(?:minimum|min\.?|at\s+least)\s*(?:of\s+)?{_NUM}\s*\+?\s*{_YRS}", re.I),
    # 3+ years
    re.compile(rf"{_NUM}\s*\+\s*{_YRS}", re.I),
    # 3 years of experience
    re.compile(rf"{_NUM}\s*{_YRS}\s+(?:of\s+)?(?:\w+\s+){{0,3}}?(?:experience|exp)\b", re.I),
]
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+|[•·]\s*")


@dataclass(frozen=True)
class Experience:
    min_years: Decimal
    max_years: Decimal | None
    sentence: str


def _dec(s: str) -> Decimal:
    return Decimal(s)


def parse_experience(text: str | None) -> Experience | None:
    """First experience requirement in the text: range, 'N+' or 'minimum of N' -> min/max + sentence."""
    if not text:
        return None
    candidates: list[tuple[int, int, Experience]] = []
    for sentence in (s.strip() for s in _SENTENCE_SPLIT.split(text)):
        if not sentence:
            continue
        has_exp_word = bool(re.search(r"experience|exp\b", sentence, re.I))
        for idx, pat in enumerate(_PATTERNS):
            m = pat.search(sentence)
            if not m:
                continue
            groups = m.groups()
            lo = _dec(groups[0])
            hi = _dec(groups[1]) if idx == 0 else None
            if idx == 0 and hi is not None and hi < lo:
                lo, hi = hi, lo
            if lo > 60 or (hi is not None and hi > 60):
                continue
            candidates.append((0 if has_exp_word else 1, len(candidates), Experience(lo, hi, sentence[:500])))
            break
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[0], c[1]))
    return candidates[0][2]
