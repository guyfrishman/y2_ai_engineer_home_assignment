import json
from types import SimpleNamespace

import pytest

from app.config import settings
from app.repositories.openai_repository import OpenAIUnavailableError
from app.repositories.taxonomy_repository import taxonomy_repository
from app.schema.taxonomy_models import Vertical
from app.services import llm_fallback_service


def _fake_response(content_dict: dict) -> SimpleNamespace:
    content = json.dumps(content_dict, ensure_ascii=False)
    token_logprobs = [SimpleNamespace(token=character, logprob=-0.01) for character in content]
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content),
                logprobs=SimpleNamespace(content=token_logprobs),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


@pytest.fixture(autouse=True)
def mock_confidence(monkeypatch):
    # llm_confidence_service has its own dedicated tests — these tests
    # exercise tier-cascade control flow, not confidence math.
    async def fake_compute_llm_confidence(*args, **kwargs):
        return 0.75

    monkeypatch.setattr(llm_fallback_service, "compute_llm_confidence", fake_compute_llm_confidence)


def _rule_path_params() -> object:
    return taxonomy_repository.params_models[Vertical.VEHICLES](יצרן="טויוטה")


async def test_tier1_success_never_calls_tier2(monkeypatch):
    calls = []

    async def fake_chat(messages, model, response_format=None, logprobs=False):
        calls.append(model)
        return _fake_response({"יצרן": "טויוטה", "דגם": "קורולה"})

    monkeypatch.setattr(llm_fallback_service.OpenAIRepository, "chat", staticmethod(fake_chat))

    result = await llm_fallback_service.run_llm_fallback(Vertical.VEHICLES, "טויוטה קורולה", _rule_path_params())

    assert result.tier_used == "tier1"
    assert result.confidence == 0.75
    assert result.params.model_dump(exclude_none=True) == {"יצרן": "טויוטה", "דגם": "קורולה"}
    assert calls == [settings.openai_fallback_model]


async def test_tier1_validation_failure_escalates_to_tier2(monkeypatch):
    calls = []

    async def fake_chat(messages, model, response_format=None, logprobs=False):
        calls.append(model)
        if model == settings.openai_fallback_model:
            # "שנה" (year) must be a number per the taxonomy — this fails
            # required-field/type validation, not an extra-field case.
            return _fake_response({"שנה": "not-a-number"})
        return _fake_response({"יצרן": "טויוטה", "דגם": "קורולה"})

    monkeypatch.setattr(llm_fallback_service.OpenAIRepository, "chat", staticmethod(fake_chat))

    result = await llm_fallback_service.run_llm_fallback(Vertical.VEHICLES, "טויוטה קורולה", _rule_path_params())

    assert result.tier_used == "tier2"
    assert calls == [settings.openai_fallback_model, settings.openai_escalation_model]
    assert result.params.model_dump(exclude_none=True) == {"יצרן": "טויוטה", "דגם": "קורולה"}


async def test_both_tiers_fail_validation_degrades_to_rule_path(monkeypatch):
    async def fake_chat(messages, model, response_format=None, logprobs=False):
        return _fake_response({"שנה": "not-a-number"})

    monkeypatch.setattr(llm_fallback_service.OpenAIRepository, "chat", staticmethod(fake_chat))

    rule_params = _rule_path_params()
    result = await llm_fallback_service.run_llm_fallback(Vertical.VEHICLES, "טויוטה קורולה", rule_params)

    assert result.tier_used == "degraded"
    assert result.confidence == llm_fallback_service.DEGRADED_CONFIDENCE
    assert result.params is rule_params
    assert llm_fallback_service.DEGRADED_NOTE in result.notes


async def test_tier1_api_error_escalates_to_tier2(monkeypatch):
    calls = []

    async def fake_chat(messages, model, response_format=None, logprobs=False):
        calls.append(model)
        if model == settings.openai_fallback_model:
            raise OpenAIUnavailableError("missing api key")
        return _fake_response({"יצרן": "טויוטה"})

    monkeypatch.setattr(llm_fallback_service.OpenAIRepository, "chat", staticmethod(fake_chat))

    result = await llm_fallback_service.run_llm_fallback(Vertical.VEHICLES, "טויוטה", _rule_path_params())

    assert result.tier_used == "tier2"
    assert calls == [settings.openai_fallback_model, settings.openai_escalation_model]


async def test_tier2_api_error_degrades(monkeypatch):
    async def fake_chat(messages, model, response_format=None, logprobs=False):
        raise OpenAIUnavailableError("no key configured")

    monkeypatch.setattr(llm_fallback_service.OpenAIRepository, "chat", staticmethod(fake_chat))

    rule_params = _rule_path_params()
    result = await llm_fallback_service.run_llm_fallback(Vertical.VEHICLES, "טויוטה", rule_params)

    assert result.tier_used == "degraded"
    assert result.confidence == llm_fallback_service.DEGRADED_CONFIDENCE
    assert result.params is rule_params


def test_strict_json_schema_has_no_optional_fields_and_forbids_extras():
    schema = llm_fallback_service._strict_json_schema(taxonomy_repository.params_models[Vertical.VEHICLES])
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"].keys())
    for def_schema in schema.get("$defs", {}).values():
        if "properties" in def_schema:
            assert def_schema["additionalProperties"] is False
            assert set(def_schema["required"]) == set(def_schema["properties"].keys())
