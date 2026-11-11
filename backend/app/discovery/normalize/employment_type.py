import re

from app.models.enums import EmploymentType

_RULES: list[tuple[re.Pattern[str], EmploymentType]] = [
    (re.compile(r"\bintern(?:ship|s)?\b|\btrainee\b"), EmploymentType.internship),
    (re.compile(r"\bapprentice(?:ship)?\b"), EmploymentType.apprenticeship),
    (re.compile(r"\bpart\s*time\b"), EmploymentType.part_time),
    (re.compile(r"\bfull\s*time\b|\bpermanent\b|\bregular\b|\bfte\b"), EmploymentType.full_time),
    (re.compile(r"\bcontract(?:or|ual)?\b|\bfixed\s*term\b|\bconsultant\b|\bfreelance\b|\bper\s*diem\b"),
     EmploymentType.contract),
    (re.compile(r"\btemp(?:orary)?\b|\bseasonal\b"), EmploymentType.temporary),
    (re.compile(r"\bother\b|\bvolunteer\b"), EmploymentType.other),
]  # fmt: skip
_TITLE_INTERN = re.compile(r"\bintern(?:ship)?\b", re.I)


def parse_employment_type(value: str | list[str] | None) -> EmploymentType | None:
    """Workday timeType ('Full time'), JSON-LD ('FULL_TIME', 'CONTRACTOR'), free text. None = unrecognised."""
    if not value:
        return None
    values = value if isinstance(value, list) else [value]
    for v in values:
        text = re.sub(r"[_\-]+", " ", str(v).lower())
        for pattern, result in _RULES:
            if pattern.search(text):
                return result
    return None


def infer_employment_type(value: str | list[str] | None, *, title: str | None = None) -> EmploymentType:
    parsed = parse_employment_type(value)
    if parsed:
        return parsed
    if title and _TITLE_INTERN.search(title):
        return EmploymentType.internship
    return EmploymentType.unknown
