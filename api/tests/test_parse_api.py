import pytest

from app.config import settings
from app.repositories.cache_repository import cache_repository
from app.services.llm_fallback_service import DEGRADED_CONFIDENCE


@pytest.fixture(autouse=True)
def clear_cache_between_tests():
    cache_repository._cache.clear()
    yield


def test_health_is_open_and_reports_taxonomy_version(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["taxonomy_version"]


def test_parse_vehicle_golden_example_resolves_via_rules(client):
    response = client.post("/parse", json={"q": "טויוטה קורולה 2018-2021 עד 70 אלף שח צבע לבן"})
    assert response.status_code == 200
    assert response.headers["x-parse-path"] == "rules"
    body = response.json()
    assert body["category"] == "רכב"
    assert body["params"] == {
        "יצרן": "טויוטה",
        "דגם": "קורולה",
        "שנה": {"min": 2018, "max": 2021},
        "מחיר": {"max": 70000},
        "צבע": "לבן",
    }
    assert 0.0 <= body["confidence"] <= 1.0


def test_parse_without_openai_key_degrades_gracefully_instead_of_500ing(client, monkeypatch):
    # Explicitly clear the key rather than relying on it being absent from
    # the environment — local/CI runs may have a real api/.env with a real
    # OPENAI_API_KEY for the live-verification steps, and this test's
    # premise (no key configured) must hold regardless of that ambient state.
    monkeypatch.setattr(settings, "openai_api_key", "")
    # A low-confidence query should still return 200 with a usable (if
    # low-confidence) result, never fail the request just because the LLM
    # fallback was unreachable.
    response = client.post("/parse", json={"q": "דירת 3 חדרים בירושלים עד מליון שח"})
    assert response.status_code == 200
    assert response.headers["x-parse-path"] == "llm"
    body = response.json()
    assert body["category"] == "נדל״ן"
    # Both tiers fail (no key) -> degrade path -> the exact fixed constant,
    # not just "some low number" -- asserting equality against the real
    # constant catches drift immediately instead of silently tolerating it.
    assert body["confidence"] == DEGRADED_CONFIDENCE
    assert body["notes"]


def test_parse_with_mocked_llm_resolves_via_llm_path(client, mock_llm):
    response = client.post("/parse", json={"q": "דירת 3 חדרים בירושלים עד מליון שח"})
    assert response.status_code == 200
    assert response.headers["x-parse-path"] == "llm"


def test_repeated_query_hits_cache_on_second_call(client):
    query = {"q": "טויוטה קורולה 2018-2021 עד 70 אלף שח צבע לבן"}

    first = client.post("/parse", json=query)
    assert first.headers["x-parse-path"] == "rules"

    second = client.post("/parse", json=query)
    assert second.headers["x-parse-path"] == "cache"
    assert second.json() == first.json()


def test_empty_query_is_rejected_with_400(client):
    response = client.post("/parse", json={"q": "   "})
    assert response.status_code == 400


def test_missing_q_field_is_rejected_with_422(client):
    response = client.post("/parse", json={})
    assert response.status_code == 422


def test_metrics_endpoint_is_open_and_exposes_custom_counters(client):
    client.post("/parse", json={"q": "טויוטה קורולה 2018-2021 עד 70 אלף שח צבע לבן"})
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "parse_requests_total" in response.text
    assert "parse_cache_result_total" in response.text
