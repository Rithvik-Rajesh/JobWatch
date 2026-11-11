"""Seed system statuses, sources and default settings.

Revision ID: 0002
Revises: 0001
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# key, label, category, color, sort_order, show_on_board
STATUSES = [
    ("new", "New", "inbox", "blue", 10, False),
    ("interested", "Interested", "considering", "sky", 20, True),
    ("shortlisted", "Shortlisted", "considering", "violet", 30, True),
    ("applied", "Applied", "applied", "amber", 40, True),
    ("assessment", "Online Assessment", "in_process", "orange", 50, True),
    ("interviewing", "Interviewing", "in_process", "fuchsia", 60, True),
    ("offer", "Offer Received", "offer", "green", 70, True),
    ("rejected", "Rejected", "closed_negative", "red", 80, True),
    ("withdrawn", "Withdrawn", "closed_neutral", "slate", 90, False),
    ("not_interested", "Not Interested", "archived", "gray", 100, False),
]

# key, name, kind, enabled, homepage
SOURCES = [
    ("career_site", "Company career site", "career_site", True, None),
    ("manual", "Manual entry", "manual", True, None),
    ("capture", "Browser capture (bookmarklet)", "capture", True, None),
    ("adzuna", "Adzuna", "job_board_api", False, "https://www.adzuna.in"),
]

SETTINGS = {
    "location_scope": '{"countries": ["IN"], "include_remote_anywhere": false, "include_unknown": true}',
    "timezone": '{"name": "Asia/Kolkata"}',
    "reminder_defaults": '{"follow_up_after_applied_days": 7, "notify_lead_minutes": 60, '
    '"interview_prep_lead_hours": 24}',
    "notifications": '{"enabled": false, "new_jobs": true, "job_updates": true, "scraper_failures": true, '
    '"reminders": true, "quiet_hours": null, "title_include": [], "title_exclude": []}',
}


def upgrade() -> None:
    for key, label, category, color, order, board in STATUSES:
        op.execute(
            f"insert into statuses (key, label, category, color, sort_order, show_on_board, is_system) "
            f"values ('{key}', '{label}', '{category}', '{color}', {order}, {str(board).lower()}, true) "
            f"on conflict (key) do nothing"
        )
    for key, name, kind, enabled, homepage in SOURCES:
        hp = f"'{homepage}'" if homepage else "null"
        op.execute(
            f"insert into sources (key, name, kind, enabled, homepage) "
            f"values ('{key}', '{name}', '{kind}', {str(enabled).lower()}, {hp}) on conflict (key) do nothing"
        )
    for key, value in SETTINGS.items():
        op.execute(f"insert into settings (key, value) values ('{key}', '{value}'::jsonb) on conflict do nothing")


def downgrade() -> None:
    op.execute("delete from settings")
    op.execute("delete from sources where key in ('career_site','manual','capture','adzuna')")
    op.execute("delete from statuses where is_system")
