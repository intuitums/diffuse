from pydantic import BaseModel

from service.model_execution import (
    StoredGenerationStep,
    StructuredGenerationResult,
)
from service.review_engine import generate_structured


class FixtureResponse(BaseModel):
    answer: str


class RecordingGenerator:
    def __init__(self) -> None:
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        return StructuredGenerationResult(
            value=FixtureResponse(answer="generated"),
            prompt_tokens=7,
            completion_tokens=2,
            resolved_model=request.target.requested_model,
            executor_version="fixture/1",
        )


class MemoryStepStore:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], StoredGenerationStep] = {}

    def load(self, *, step_key, request_fingerprint):
        return self.values.get((step_key, request_fingerprint))

    def save(
        self,
        *,
        step_key,
        request_fingerprint,
        response_schema,
        result,
    ):
        assert response_schema.endswith(".FixtureResponse")
        self.values[(step_key, request_fingerprint)] = StoredGenerationStep(
            response=result.value.model_dump(mode="json"),
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            resolved_model=result.resolved_model,
            executor_version=result.executor_version,
        )


def test_generation_step_reuses_only_an_exact_validated_request(monkeypatch):
    monkeypatch.setenv("REVIEW_EXECUTOR", "litellm")
    store = MemoryStepStore()
    generator = RecordingGenerator()

    first_request, first = generate_structured(
        FixtureResponse,
        system_prompt="Trusted instructions",
        user_prompt="Prepared diff and retrieved context",
        model_name="fixture-model",
        generator=generator,
        step_store=store,
        step_key="candidate/security/0",
    )
    second_request, second = generate_structured(
        FixtureResponse,
        system_prompt="Trusted instructions",
        user_prompt="Prepared diff and retrieved context",
        model_name="fixture-model",
        generator=generator,
        step_store=store,
        step_key="candidate/security/0",
    )

    assert len(generator.requests) == 1
    assert first_request.fingerprint == second_request.fingerprint
    assert first.value == second.value
    assert second.metadata == {"cache_hit": True}

    generate_structured(
        FixtureResponse,
        system_prompt="Trusted instructions",
        user_prompt="A changed prepared diff",
        model_name="fixture-model",
        generator=generator,
        step_store=store,
        step_key="candidate/security/0",
    )
    assert len(generator.requests) == 2


def test_structured_request_separates_trusted_and_untrusted_content(monkeypatch):
    monkeypatch.setenv("REVIEW_EXECUTOR", "litellm")
    generator = RecordingGenerator()

    request, _ = generate_structured(
        FixtureResponse,
        system_prompt="Diffuse-owned review policy",
        user_prompt="<untrusted_pull_request_diff>change</untrusted_pull_request_diff>",
        generator=generator,
    )

    assert request.system_prompt == "Diffuse-owned review policy"
    assert "<untrusted_pull_request_diff>" in request.user_prompt
    assert request.response_model is FixtureResponse
