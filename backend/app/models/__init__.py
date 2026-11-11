"""SQLAlchemy models. Centrally owned by the lead architect: schema changes go through a migration
reviewed against docs/phase1/02-database-schema.md. Implementation agents must not edit this package."""

from app.models.applications import Application, ApplicationEvent, Contact, Interview, Note, Reminder
from app.models.catalog import (
    AppSetting,
    CareerSite,
    Company,
    CompanyAlias,
    ResumeVersion,
    SavedView,
    Source,
    Status,
    Tag,
)
from app.models.jobs import DuplicateSuggestion, Job, JobTag, JobVersion, Posting, SalaryRecord
from app.models.ops import NotificationOutbox, ScrapeRun

__all__ = [
    "AppSetting",
    "Application",
    "ApplicationEvent",
    "CareerSite",
    "Company",
    "CompanyAlias",
    "Contact",
    "DuplicateSuggestion",
    "Interview",
    "Job",
    "JobTag",
    "JobVersion",
    "Note",
    "NotificationOutbox",
    "Posting",
    "Reminder",
    "ResumeVersion",
    "SalaryRecord",
    "SavedView",
    "ScrapeRun",
    "Source",
    "Status",
    "Tag",
]
