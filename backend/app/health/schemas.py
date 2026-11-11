from datetime import datetime

from app.core.schema import APIModel


class HealthOverview(APIModel):
    companies_total: int
    companies_active: int
    companies_healthy: int
    companies_degraded: int
    companies_failing: int
    runs_24h: int
    runs_failed_24h: int
    runs_quarantined_24h: int
    success_rate_24h: float | None
    success_rate_7d: float | None
    avg_duration_ms_24h: int | None
    last_successful_run_at: datetime | None
    pending_tasks: int  # queued scrape runs (Phase 3: + queue depth)
    notifications_pending: int
    notifications_failed: int


class NotificationOut(APIModel):
    id: int
    channel: str
    event_type: str
    dedupe_key: str
    state: str
    attempts: int
    last_error: str | None
    created_at: datetime
    sent_at: datetime | None
