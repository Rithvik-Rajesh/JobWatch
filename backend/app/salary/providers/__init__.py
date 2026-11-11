"""Salary providers. v1 ships employer-provided (parsed from the posting) and user-entered figures.
External estimate providers are modelled by the `SalaryProvider` protocol but none is implemented:
AmbitionBox has no authorized access, so it is listed as disabled."""

from typing import Protocol, runtime_checkable

from app.models.enums import SalaryProvenance
from app.salary.schemas import SalaryProviderOut, SalaryRecordOut


@runtime_checkable
class SalaryProvider(Protocol):
    """Future external estimate providers implement this. Must only be enabled when authorized."""

    key: str
    name: str
    provenance: SalaryProvenance
    enabled: bool
    authorized: bool

    async def estimate(self, *, company: str, title: str, location: str | None) -> list[SalaryRecordOut]: ...


def list_providers() -> list[SalaryProviderOut]:
    return [
        SalaryProviderOut(
            key="job_posting",
            name="Employer-provided (job posting)",
            provenance=SalaryProvenance.employer_provided,
            enabled=True,
            authorized=True,
            description="Compensation stated by the employer in the job posting text.",
        ),
        SalaryProviderOut(
            key="user",
            name="Entered by you",
            provenance=SalaryProvenance.user_entered,
            enabled=True,
            authorized=True,
            description="Figures you looked up yourself and recorded, with their source.",
        ),
        SalaryProviderOut(
            key="ambitionbox_api",
            name="AmbitionBox (third-party API)",
            provenance=SalaryProvenance.external_estimate,
            enabled=False,
            authorized=False,
            description="Third-party scraper of AmbitionBox data with no authorized access; "
            "disabled by policy. Add figures manually instead.",
        ),
    ]
