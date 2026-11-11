"""Code-driving enumerations. Each maps to a native Postgres enum of the same (snake_case) name.

User-extensible vocabularies (application statuses, tags) are lookup tables instead — see models.
"""

from enum import StrEnum


class ExtractorKind(StrEnum):
    workday = "workday"
    jsonld = "jsonld"
    html = "html"
    custom = "custom"
    none = "none"


class HealthStatus(StrEnum):
    unknown = "unknown"
    healthy = "healthy"
    degraded = "degraded"
    failing = "failing"
    paused = "paused"


class SourceKind(StrEnum):
    career_site = "career_site"
    job_board_api = "job_board_api"
    feed = "feed"
    email_alert = "email_alert"
    capture = "capture"  # bookmarklet: page the user is viewing in their own browser
    manual = "manual"


class Availability(StrEnum):
    """Employer-side availability. System-owned; NEVER derived from or written to application status."""

    open = "open"
    closed = "closed"
    unknown = "unknown"


class EmploymentType(StrEnum):
    full_time = "full_time"
    part_time = "part_time"
    contract = "contract"
    internship = "internship"
    temporary = "temporary"
    apprenticeship = "apprenticeship"
    other = "other"
    unknown = "unknown"


class WorkArrangement(StrEnum):
    onsite = "onsite"
    hybrid = "hybrid"
    remote = "remote"
    unknown = "unknown"


class DatePrecision(StrEnum):
    """How trustworthy jobs.posted_at is.

    exact/day  -> absolute date published by the source (preserved, never overwritten by an estimate)
    approximate -> derived from relative text such as "Posted 3 Days Ago"
    unknown    -> no posting date (posted_at is NULL)
    """

    exact = "exact"
    day = "day"
    approximate = "approximate"
    unknown = "unknown"


class ScopeMatch(StrEnum):
    """Result of the single LocationScope policy (default: India)."""

    in_scope = "in_scope"
    out_of_scope = "out_of_scope"
    unknown = "unknown"  # no parseable location; shown in the inbox, flagged


class StatusCategory(StrEnum):
    inbox = "inbox"
    considering = "considering"
    applied = "applied"
    in_process = "in_process"
    offer = "offer"
    closed_negative = "closed_negative"
    closed_neutral = "closed_neutral"
    archived = "archived"


class RunTrigger(StrEnum):
    schedule = "schedule"
    manual = "manual"
    retry = "retry"
    test = "test"


class RunStatus(StrEnum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    partial = "partial"
    failed = "failed"
    skipped = "skipped"
    quarantined = "quarantined"


class SalaryProvenance(StrEnum):
    employer_provided = "employer_provided"
    external_estimate = "external_estimate"
    user_entered = "user_entered"


class SalaryPeriod(StrEnum):
    annual = "annual"
    monthly = "monthly"
    hourly = "hourly"
    unknown = "unknown"


class MatchQuality(StrEnum):
    exact = "exact"
    close = "close"
    broad = "broad"
    company_wide = "company_wide"
    unknown = "unknown"


class ReminderKind(StrEnum):
    deadline = "deadline"
    follow_up = "follow_up"
    interview_prep = "interview_prep"
    interview = "interview"
    recruiter = "recruiter"
    custom = "custom"


class ReminderState(StrEnum):
    pending = "pending"
    done = "done"
    dismissed = "dismissed"


class OutboxState(StrEnum):
    pending = "pending"
    sending = "sending"
    sent = "sent"
    failed = "failed"
    suppressed = "suppressed"


class DuplicateState(StrEnum):
    pending = "pending"
    confirmed = "confirmed"  # merged
    dismissed = "dismissed"  # user said "different jobs" — never suggested again


class EventType(StrEnum):
    discovered = "discovered"
    status_changed = "status_changed"
    reviewed = "reviewed"
    field_changed = "field_changed"
    note_added = "note_added"
    interview_scheduled = "interview_scheduled"
    interview_updated = "interview_updated"
    reminder_created = "reminder_created"
    reminder_completed = "reminder_completed"
    job_updated = "job_updated"
    job_closed = "job_closed"
    job_reopened = "job_reopened"
    job_merged = "job_merged"
    salary_added = "salary_added"
    manual = "manual"


class Actor(StrEnum):
    user = "user"
    system = "system"
