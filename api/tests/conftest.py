import pytest
from fastapi.testclient import TestClient

from app.logger import logger as app_logger
from app.repositories.openai_repository import OpenAIRepository
from main import app

app_logger.disabled = True


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def mock_llm(monkeypatch):
    """Replace the OpenAI client so tests never hit the network. Tier 1
    always succeeds with an empty-but-valid params object, matching
    whatever schema it's asked for — good enough for tests that only need
    the LLM fallback to resolve, not to assert on specific extracted values.
    """

    async def fake_chat(messages, model, response_format=None, logprobs=False):
        from types import SimpleNamespace

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
