"""Helpers for jobs tests."""

from datetime import UTC, datetime

from app.discovery.contracts import PostingInput

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


def posting(company_id: int, external_id: str = "JR1001", **kw) -> PostingInput:
    base = {
        "source_key": "career_site",
        "company_id": company_id,
        "external_id": external_id,
        "source_url": f"https://careers.example.com/job/{external_id}",
        "title": "Backend Engineer",
        "location_text": "Bengaluru, Karnataka, India",
        "description_text": "Build services in Python. 3+ years of experience required.",
    }
    base.update(kw)
    return PostingInput(**base)
