"""Pure normalizers used by the ingest service (no I/O)."""

from app.discovery.normalize.dates import parse_relative_posted
from app.discovery.normalize.employment_type import infer_employment_type, parse_employment_type
from app.discovery.normalize.experience import Experience, parse_experience
from app.discovery.normalize.html import html_to_text, sanitize_html, text_to_html
from app.discovery.normalize.location import country_codes, country_for, parse_locations
from app.discovery.normalize.salary import ParsedSalary, parse_compensation
from app.discovery.normalize.skills import extract_skills
from app.discovery.normalize.title import normalize_title
from app.discovery.normalize.work_arrangement import infer_work_arrangement, parse_work_arrangement

__all__ = [
    "Experience",
    "ParsedSalary",
    "country_codes",
    "country_for",
    "extract_skills",
    "html_to_text",
    "infer_employment_type",
    "infer_work_arrangement",
    "normalize_title",
    "parse_compensation",
    "parse_employment_type",
    "parse_experience",
    "parse_locations",
    "parse_relative_posted",
    "parse_work_arrangement",
    "sanitize_html",
    "text_to_html",
]
