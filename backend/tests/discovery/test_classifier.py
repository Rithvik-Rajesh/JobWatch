import httpx

from app.core.config import Settings
from app.discovery.classifier import FresherClassifierSettings, JobClassifier, rule_classification


def settings(**values):
    defaults = {"env": "development", "llm_provider": "ollama", "ollama_model": "test-model"}
    return Settings(**(defaults | values))


async def test_ollama_classification_uses_structured_answer():
    async def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        assert body["format"]["properties"]["verdict"]["enum"] == ["yes", "no", "unclear"]
        assert "Software Engineer" in body["messages"][1]["content"]
        return httpx.Response(200, json={"message": {"content": '{"verdict":"yes","reason":"New grads."}'}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await JobClassifier(settings(), client).classify("Software Engineer", "Build internal APIs.")

    assert result.is_fresher is True
    assert result.provider == "ollama:test-model"


async def test_gemini_classification_and_key_header():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-goog-api-key"] == "secret"
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": '{"verdict":"no","reason":"3+ years."}'}]}}]},
        )

    cfg = settings(llm_provider="gemini", gemini_api_key="secret", gemini_model="gemini-test")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await JobClassifier(cfg, client).classify("Engineer", "Requires 3 years")

    assert result.is_fresher is False
    assert result.provider == "gemini:gemini-test"


async def test_provider_failure_becomes_unknown():
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="offline")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await JobClassifier(settings(), client).classify("Engineer", "A job")

    assert result.is_fresher is None
    assert result.provider == "ollama"
    assert result.reason.startswith("Classification unavailable")


def test_rules_skip_model_for_obvious_senior_titles():
    for title in ("Engineering Manager", "Staff Engineer", "Tech Lead", "Principal Architect"):
        result = rule_classification(title, "Freshers can apply")
        assert result is not None and result.is_fresher is False
        assert result.provider == "rules:title-rejection"


def test_rules_skip_model_for_explicit_fresher_roles():
    title_result = rule_classification("Graduate Software Engineer", None)
    jd_result = rule_classification("Software Engineer", "Freshers are welcome to apply for this position.")
    experience_result = rule_classification("Software Engineer", "Required experience: 0-1 years.")
    assert title_result is not None and title_result.is_fresher is True
    assert title_result.provider == "rules:title-acceptance"
    assert jd_result is not None and jd_result.is_fresher is True
    assert jd_result.provider == "rules:jd-acceptance"
    assert experience_result is not None and experience_result.is_fresher is True


def test_ambiguous_role_is_left_for_the_model():
    assert rule_classification("Software Engineer", "Build APIs using Python and PostgreSQL.") is None


def test_custom_words_replace_defaults():
    rules = FresherClassifierSettings(rejection_words=["wizard"], acceptance_words=["starter role"])
    assert rule_classification("Python Wizard", None, rules).is_fresher is False
    assert rule_classification("Engineering Manager", None, rules) is None
    assert rule_classification("Engineer", "This is a starter role", rules).is_fresher is True
