import pytest
from fastapi.testclient import TestClient

from logger import logger as app_logger
from repositories.openai_repository import OpenAIRepository
from main import app

app_logger.disabled = True


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def mock_llm(monkeypatch):
    """Replace the OpenAI client so tests never hit the network. Tier 1
    extraction always succeeds with an empty-but-valid params object,
    matching whatever schema it's asked for — good enough for tests that
    only need the LLM fallback to resolve, not to assert on specific
    extracted values. The classify-only call (services.llm_fallback_service.
    run_category_classification, response_format name "query_category") is
    a distinct schema from extraction's "search_params" — answered with a
    fixed default category so a test that happens to hit the
    confidence==0.0 path doesn't unexpectedly degrade; tests that care which
    category comes back (see test_zero_signal_classification.py) supply
    their own fake_chat instead of this shared one.
    """

    async def fake_chat(messages, model, response_format=None, logprobs=False, max_completion_tokens=None):
        from types import SimpleNamespace

        schema_name = (response_format or {}).get("json_schema", {}).get("name")
        if schema_name == "query_category":
            content = '{"קטגוריה": "רכב"}'
        else:
            content = "{}"
        token_logprobs = [SimpleNamespace(token=character, logprob=-0.01) for character in content]
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=content),
                    logprobs=SimpleNamespace(content=token_logprobs),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2, total_tokens=12),
        )

    async def fake_embed(text, model=None):
        return [1.0, 0.0]

    monkeypatch.setattr(OpenAIRepository, "chat", staticmethod(fake_chat))
    monkeypatch.setattr(OpenAIRepository, "embed", staticmethod(fake_embed))
