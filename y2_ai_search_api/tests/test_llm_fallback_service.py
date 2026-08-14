import json
from types import SimpleNamespace

import pytest

from config import settings
from repositories.openai_repository import OpenAIUnavailableError
from repositories.taxonomy_repository import taxonomy_repository
from schema.taxonomy_models import Vertical
from services import llm_fallback_service


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

    async def fake_chat(messages, model, response_format=None, logprobs=False, max_completion_tokens=None):
        calls.append(model)
        return _fake_response({"יצרן": "טויוטה", "דגם": "קורולה"})

    monkeypatch.setattr(llm_fallback_service.OpenAIRepository, "chat", staticmethod(fake_chat))

    result = await llm_fallback_service.run_llm_fallback(Vertical.VEHICLES, "טויוטה קורולה", _rule_path_params())

    assert result.tier_used == "tier1"
    assert result.confidence == 0.75
    assert result.params.model_dump(exclude_none=True) == {"יצרן": "טויוטה", "דגם": "קורולה"}
    assert calls == [settings.openai_fallback_model]


async def test_tier1_validation_failure_does_not_escalate_to_tier2(monkeypatch):
    # A schema-valid response the model couldn't produce once isn't more
    # likely on a second, unrelated attempt -- only api_error escalates.
    calls = []

    async def fake_chat(messages, model, response_format=None, logprobs=False, max_completion_tokens=None):
        calls.append(model)
        # "שנה" (year) must be a number per the taxonomy — this fails
        # required-field/type validation, not an extra-field case.
        return _fake_response({"שנה": "not-a-number"})

    monkeypatch.setattr(llm_fallback_service.OpenAIRepository, "chat", staticmethod(fake_chat))

    rule_params = _rule_path_params()
    result = await llm_fallback_service.run_llm_fallback(Vertical.VEHICLES, "טויוטה קורולה", rule_params)

    assert result.tier_used == "degraded"
    assert calls == [settings.openai_fallback_model]  # tier2 never called
    assert result.params is rule_params


async def test_tier1_validation_failure_degrades_without_calling_tier2(monkeypatch):
    calls = []

    async def fake_chat(messages, model, response_format=None, logprobs=False, max_completion_tokens=None):
        calls.append(model)
        return _fake_response({"שנה": "not-a-number"})

    monkeypatch.setattr(llm_fallback_service.OpenAIRepository, "chat", staticmethod(fake_chat))

    rule_params = _rule_path_params()
    result = await llm_fallback_service.run_llm_fallback(Vertical.VEHICLES, "טויוטה קורולה", rule_params)

    assert result.tier_used == "degraded"
    assert result.confidence == llm_fallback_service.DEGRADED_CONFIDENCE
    assert result.params is rule_params
    assert llm_fallback_service.DEGRADED_NOTE in result.notes
    assert calls == [settings.openai_fallback_model]


async def test_tier1_api_error_escalates_to_tier2(monkeypatch):
    calls = []

    async def fake_chat(messages, model, response_format=None, logprobs=False, max_completion_tokens=None):
        calls.append(model)
        if model == settings.openai_fallback_model:
            raise OpenAIUnavailableError("missing api key")
        return _fake_response({"יצרן": "טויוטה"})

    monkeypatch.setattr(llm_fallback_service.OpenAIRepository, "chat", staticmethod(fake_chat))

    result = await llm_fallback_service.run_llm_fallback(Vertical.VEHICLES, "טויוטה", _rule_path_params())

    assert result.tier_used == "tier2"
    assert calls == [settings.openai_fallback_model, settings.openai_escalation_model]


async def test_tier2_api_error_degrades(monkeypatch):
    async def fake_chat(messages, model, response_format=None, logprobs=False, max_completion_tokens=None):
        raise OpenAIUnavailableError("no key configured")

    monkeypatch.setattr(llm_fallback_service.OpenAIRepository, "chat", staticmethod(fake_chat))

    rule_params = _rule_path_params()
    result = await llm_fallback_service.run_llm_fallback(Vertical.VEHICLES, "טויוטה", rule_params)

    assert result.tier_used == "degraded"
    assert result.confidence == llm_fallback_service.DEGRADED_CONFIDENCE
    assert result.params is rule_params


async def test_scoped_schema_narrows_what_is_asked_not_what_is_allowed(monkeypatch):
    """The scoped schema sent to OpenAI only narrows what the LLM is ASKED
    to produce (already-known fields are omitted from the wire schema) --
    it must never narrow what's ALLOWED through validation. Proven with the
    one check that only fires on the full merged object, not on the LLM's
    own scoped response in isolation: UsedGoodsParams' sector/subcategory
    cross-field validator (see docs/DESIGN.md). סקטור is already
    known from the rule path, so the scoped schema never even asks the LLM
    about it; the LLM's own scoped response supplies a real, individually-
    valid תת_קטגוריה that simply doesn't belong to that סקטור. If
    validation only checked the LLM's own scoped fields -- never re-merging
    with and re-checking against the full model -- this mismatch would
    never be caught."""
    rule_path_params = taxonomy_repository.params_models[Vertical.USED_GOODS](סקטור="מוסיקה_וכלים")

    async def fake_chat(messages, model, response_format=None, logprobs=False, max_completion_tokens=None):
        assert "סקטור" not in response_format["json_schema"]["schema"]["properties"], (
            "already-known field leaked into the scoped wire schema"
        )
        # A real, individually-valid subcategory -- just not one that
        # belongs to "מוסיקה_וכלים" (it belongs to "אלקטרוניקה").
        return _fake_response({"תת_קטגוריה": "מחשבים_ניידים"})

    monkeypatch.setattr(llm_fallback_service.OpenAIRepository, "chat", staticmethod(fake_chat))

    result = await llm_fallback_service.run_llm_fallback(Vertical.USED_GOODS, "מחשב", rule_path_params)

    assert result.tier_used == "degraded"
    assert result.params is rule_path_params


def test_scoped_schema_excludes_already_known_fields_from_the_wire_schema():
    model_class = taxonomy_repository.params_models[Vertical.VEHICLES]
    full_schema = llm_fallback_service._strict_json_schema(model_class)
    scoped = llm_fallback_service._scoped_strict_json_schema(model_class, frozenset({"יצרן", "דגם"}))

    assert "יצרן" not in scoped["properties"]
    assert "דגם" not in scoped["properties"]
    assert "יצרן" not in scoped["required"]
    # Everything else about the schema (additionalProperties, other fields'
    # own constraints) is untouched -- this narrows the top-level property
    # list only, not the strict-mode shape.
    untouched_fields = set(full_schema["properties"]) - {"יצרן", "דגם"}
    assert set(scoped["properties"]) == untouched_fields
    assert scoped["additionalProperties"] is False


def test_scoped_schema_falls_back_to_the_full_schema_when_nothing_is_left_to_scope():
    model_class = taxonomy_repository.params_models[Vertical.VEHICLES]
    full_schema = llm_fallback_service._strict_json_schema(model_class)
    all_fields = frozenset(full_schema["properties"].keys())

    scoped = llm_fallback_service._scoped_strict_json_schema(model_class, all_fields)

    assert scoped == full_schema


def test_strict_json_schema_has_no_optional_fields_and_forbids_extras():
    schema = llm_fallback_service._strict_json_schema(taxonomy_repository.params_models[Vertical.VEHICLES])
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"].keys())
    for def_schema in schema.get("$defs", {}).values():
        if "properties" in def_schema:
            assert def_schema["additionalProperties"] is False
            assert set(def_schema["required"]) == set(def_schema["properties"].keys())


async def test_category_classification_success_logs_the_chosen_vertical(monkeypatch):
    # Observability: for a routing call, "what did it decide" is the
    # headline fact -- it must be in the success log itself, not something
    # only recoverable by correlating a later, separate parse_decision log
    # line via trace_id.
    logged = {}

    def fake_log_event(**fields):
        logged.update(fields)

    async def fake_chat(messages, model, response_format=None, logprobs=False, max_completion_tokens=None):
        return _fake_response({"קטגוריה": Vertical.VEHICLES.value})

    monkeypatch.setattr(llm_fallback_service, "log_event", fake_log_event)
    monkeypatch.setattr(llm_fallback_service.OpenAIRepository, "chat", staticmethod(fake_chat))

    result = await llm_fallback_service.run_category_classification("ג'יפ קטן")

    assert result.vertical == Vertical.VEHICLES
    assert result.failed is False
    assert logged["event"] == "llm_call_outcome"
    assert logged["tier"] == "classify"
    assert logged["outcome"] == "success"
    assert logged["vertical"] == Vertical.VEHICLES.value
