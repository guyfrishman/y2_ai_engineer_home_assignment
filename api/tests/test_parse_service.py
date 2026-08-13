import pytest

from app import metrics
from app.repositories.cache_repository import cache_repository
from app.services import parse_service


@pytest.fixture(autouse=True)
def clear_cache_between_tests():
    cache_repository._cache.clear()
    yield


def _counter_value() -> float:
    return metrics.PARSE_ERRORS_TOTAL._value.get()


async def test_exception_mid_pipeline_increments_error_counter_and_still_propagates(monkeypatch):
    before = _counter_value()

    def boom(canonical_query):
        raise RuntimeError("simulated classifier failure")

    monkeypatch.setattr(parse_service, "classify_query", boom)

    with pytest.raises(RuntimeError, match="simulated classifier failure"):
        await parse_service.parse_query("דירה בירושלים")

    assert _counter_value() == before + 1


async def test_successful_request_does_not_increment_error_counter():
    before = _counter_value()
    await parse_service.parse_query("טויוטה קורולה 2018-2021 עד 70 אלף שח צבע לבן")
    assert _counter_value() == before
