"""Classify a newly discovered job as fresher-friendly with Ollama or Gemini.

This module deliberately knows nothing about scraping or the database.  It accepts a title and
plain-text JD and returns one small result, which keeps the discovery pipeline easy to follow and
makes providers replaceable.
"""

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from app.core.config import Settings, get_settings


class _ModelAnswer(BaseModel):
    verdict: Literal["yes", "no", "unclear"]
    reason: str = Field(min_length=1, max_length=500)


@dataclass(frozen=True)
class FresherClassification:
    is_fresher: bool | None
    reason: str
    provider: str


SYSTEM_PROMPT = """You classify whether a job is suitable for a fresher.
A fresher is a new graduate or candidate with zero full-time experience. Internships and student
projects do not count as full-time experience. Return yes when the role explicitly welcomes new
graduates, says 0-1 years, or has no required prior experience and is clearly entry-level. Return no
when it requires 2+ years or uses senior/lead/staff/manager wording. Return unclear when evidence is
missing or contradictory. Judge required qualifications, not preferred qualifications. Be concise."""

# Cheap, deterministic decisions run before an LLM call. Keep these intentionally conservative:
# ambiguous jobs still go to Ollama/Gemini instead of being guessed by a large regex ruleset.
SENIOR_TITLE = re.compile(
    r"\b(senior|sr\.?|lead|staff|principal|manager|director|head|architect|"
    r"vice[ -]?president|vp|avp|chief|distinguished|fellow)\b",
    re.IGNORECASE,
)
FRESHER_TITLE = re.compile(
    r"\b(intern(?:ship)?|graduate|new[ -]?grad|fresher|entry[ -]?level|trainee|apprentice)\b",
    re.IGNORECASE,
)
FRESHER_DESCRIPTION = re.compile(
    r"\b(?:freshers? (?:may|can) apply|freshers? (?:are )?(?:welcome|eligible)|"
    r"no (?:prior |professional |work )?experience (?:is )?required|"
    r"(?:0\s*(?:-|\u2013|to)\s*1|zero\s*(?:-|\u2013|to)\s*one) years? (?:of )?experience|"
    r"experience(?: required)?\s*:?\s*(?:0\s*(?:-|\u2013|to)\s*1|zero\s*(?:-|\u2013|to)\s*one) years?|"
    r"(?:(?:new|recent) )?graduates? (?:may|can|are|welcome)|campus (?:hire|hiring|recruitment))\b",
    re.IGNORECASE,
)


def rule_classification(title: str, description: str | None) -> FresherClassification | None:
    """Return an obvious verdict, or None when the model must decide."""
    senior = SENIOR_TITLE.search(title)
    if senior:
        return FresherClassification(False, f"Seniority keyword in title: {senior.group(0)}", "rules:title-seniority")
    fresher = FRESHER_TITLE.search(title)
    if fresher:
        return FresherClassification(True, f"Fresher keyword in title: {fresher.group(0)}", "rules:title-fresher")
    direct = FRESHER_DESCRIPTION.search(description or "")
    if direct:
        return FresherClassification(True, f"Job description explicitly says: {direct.group(0)}", "rules:jd-fresher")
    return None


def _prompt(title: str, description: str | None) -> str:
    # Limit local inference time and avoid sending an unnecessarily large JD to either provider.
    jd = (description or "No job description was provided.").strip()[:16_000]
    return f"Job title: {title.strip()}\n\nJob description:\n{jd}"


def _result(answer: _ModelAnswer, provider: str) -> FresherClassification:
    value = {"yes": True, "no": False, "unclear": None}[answer.verdict]
    return FresherClassification(value, answer.reason.strip(), provider)


def _json_schema() -> dict[str, Any]:
    return _ModelAnswer.model_json_schema()


class JobClassifier:
    def __init__(self, settings: Settings | None = None, client: httpx.AsyncClient | None = None):
        self.settings = settings or get_settings()
        self.client = client

    @property
    def provider(self) -> str:
        configured = self.settings.llm_provider
        if configured == "auto":
            return "gemini" if self.settings.gemini_api_key.get_secret_value() else "ollama"
        return configured

    async def classify(self, title: str, description: str | None) -> FresherClassification:
        ruled = rule_classification(title, description)
        if ruled is not None:
            return ruled
        provider = self.provider
        if provider == "disabled" or self.settings.env == "test":
            return FresherClassification(None, "LLM classification is disabled", "disabled")
        try:
            if provider == "gemini":
                return await self._gemini(title, description)
            return await self._ollama(title, description)
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            # Scraping must remain useful when a local model is stopped or an API is temporarily down.
            return FresherClassification(None, f"Classification unavailable: {str(exc)[:300]}", provider)

    async def _post(self, url: str, **kwargs: Any) -> httpx.Response:
        if self.client is not None:
            response = await self.client.post(url, **kwargs)
        else:
            async with httpx.AsyncClient(timeout=self.settings.llm_timeout_seconds) as client:
                response = await client.post(url, **kwargs)
        response.raise_for_status()
        return response

    async def _ollama(self, title: str, description: str | None) -> FresherClassification:
        response = await self._post(
            f"{self.settings.ollama_url.rstrip('/')}/api/chat",
            json={
                "model": self.settings.ollama_model,
                "stream": False,
                "format": _json_schema(),
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": _prompt(title, description)},
                ],
                "options": {"temperature": 0},
            },
        )
        answer = _ModelAnswer.model_validate_json(response.json()["message"]["content"])
        return _result(answer, f"ollama:{self.settings.ollama_model}")

    async def _gemini(self, title: str, description: str | None) -> FresherClassification:
        key = self.settings.gemini_api_key.get_secret_value()
        if not key:
            raise ValueError("JOBWATCH_GEMINI_API_KEY is not set")
        model = self.settings.gemini_model
        response = await self._post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json={
                "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                "contents": [{"role": "user", "parts": [{"text": _prompt(title, description)}]}],
                "generationConfig": {
                    "temperature": 0,
                    "responseMimeType": "application/json",
                    "responseJsonSchema": _json_schema(),
                },
            },
        )
        text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
        answer = _ModelAnswer.model_validate(json.loads(text))
        return _result(answer, f"gemini:{model}")
