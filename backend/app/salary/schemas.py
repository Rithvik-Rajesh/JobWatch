from datetime import date, datetime
from decimal import Decimal

from pydantic import Field, HttpUrl, model_validator

from app.core.schema import APIModel
from app.models.enums import MatchQuality, SalaryPeriod, SalaryProvenance


class SalaryRecordOut(APIModel):
    id: int
    provenance: SalaryProvenance
    provider: str
    currency: str | None
    period: SalaryPeriod
    amount_min: Decimal | None
    amount_max: Decimal | None
    amount_avg: Decimal | None
    amount_typical: Decimal | None
    raw_text: str | None
    role_queried: str | None
    role_matched: str | None
    role_match: MatchQuality
    experience_band: str | None
    experience_match: MatchQuality
    location: str | None
    sample_size: int | None
    confidence: str | None
    source_url: str | None
    source_updated_at: date | None
    retrieved_at: datetime
    notes: str | None


class JobSalaryView(APIModel):
    """Rendered as two visually distinct blocks in the UI. Never mixed.

    employer_provided -> "Employer-provided compensation"
    estimates         -> "Estimated company salary based on external salary data" (incl. user-entered)
    """

    employer_provided: list[SalaryRecordOut]
    estimates: list[SalaryRecordOut]
    available: bool  # False -> UI shows "Not available"


class SalarySummary(APIModel):
    """Compact form for list rows. `provenance` decides the label/badge."""

    provenance: SalaryProvenance
    currency: str | None
    period: SalaryPeriod
    amount_min: Decimal | None
    amount_max: Decimal | None
    raw_text: str | None


class UserSalaryCreate(APIModel):
    """A figure the user looked up themselves (e.g. on AmbitionBox) — stored as user_entered."""

    currency: str = Field(default="INR", min_length=3, max_length=3)
    period: SalaryPeriod = SalaryPeriod.annual
    amount_min: Decimal | None = Field(default=None, ge=0)
    amount_max: Decimal | None = Field(default=None, ge=0)
    amount_avg: Decimal | None = Field(default=None, ge=0)
    source_name: str = Field(min_length=1, max_length=80)  # e.g. "AmbitionBox"
    source_url: HttpUrl | None = None
    role_matched: str | None = None
    role_match: MatchQuality = MatchQuality.unknown
    experience_band: str | None = None
    experience_match: MatchQuality = MatchQuality.unknown
    sample_size: int | None = Field(default=None, ge=0)
    confidence: str | None = Field(default=None, pattern="^(high|medium|low)$")
    notes: str | None = None

    @model_validator(mode="after")
    def _some_amount(self) -> "UserSalaryCreate":
        if self.amount_min is None and self.amount_max is None and self.amount_avg is None:
            raise ValueError("Provide at least one of amount_min, amount_max, amount_avg")
        if self.amount_min is not None and self.amount_max is not None and self.amount_min > self.amount_max:
            raise ValueError("amount_min must be <= amount_max")
        return self


class SalaryProviderOut(APIModel):
    key: str
    name: str
    provenance: SalaryProvenance
    enabled: bool
    authorized: bool  # False for scraping-based third parties (shown with a warning)
    description: str
