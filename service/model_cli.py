"""Safe model readiness and live connectivity checks."""

from __future__ import annotations

import argparse
import json
import os

from service.review_engine import (
    review_model,
    review_verifier_model,
    verify_model_connection,
)
from service.review_provenance import model_family


def _provider(model: str) -> tuple[str, tuple[str, ...], bool]:
    lowered = model.casefold()
    if lowered.startswith("openrouter/"):
        return "openrouter", ("OPENROUTER_API_KEY",), True
    if lowered.startswith(("openai/", "gpt-", "o1", "o3", "o4")):
        return "openai", ("OPENAI_API_KEY", "OPENAI_KEY"), True
    if lowered.startswith(("anthropic/", "claude")):
        return "anthropic", ("ANTHROPIC_API_KEY",), True
    if lowered.startswith(("gemini/", "google/")):
        return "google", ("GEMINI_API_KEY",), True
    if lowered.startswith("azure/"):
        return "azure", ("AZURE_API_KEY",), True
    if lowered.startswith("bedrock/"):
        return (
            "aws-bedrock",
            ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_PROFILE"),
            True,
        )
    if lowered.startswith(("ollama/", "hosted_vllm/")):
        return "self-hosted", ("REVIEW_API_BASE",), False
    return "custom", ("REVIEW_API_BASE",), False


def _credential_status(model: str) -> tuple[str, tuple[str, ...], bool]:
    provider, credential_names, requires_credential = _provider(model)
    configured_names = tuple(name for name in credential_names if os.environ.get(name))
    configured = bool(configured_names) or not requires_credential
    if provider == "aws-bedrock":
        configured = bool(os.environ.get("AWS_PROFILE")) or bool(
            os.environ.get("AWS_ACCESS_KEY_ID")
            and os.environ.get("AWS_SECRET_ACCESS_KEY")
        )
    return provider, credential_names, configured


def model_status() -> dict[str, object]:
    model = review_model()
    verifier_model = review_verifier_model()
    provider, credential_names, configured = _credential_status(model)
    (
        verifier_provider,
        verifier_credential_names,
        verifier_configured,
    ) = _credential_status(verifier_model)
    return {
        "schema_version": "diffuse-model-status-v2",
        "model": model,
        "provider": provider,
        "credential_env_names": credential_names,
        "credential_configured": configured,
        "verifier_model": verifier_model,
        "verifier_provider": verifier_provider,
        "verifier_credential_env_names": verifier_credential_names,
        "verifier_credential_configured": verifier_configured,
        "cross_family_review": (
            model_family(model) is not None
            and model_family(verifier_model) is not None
            and model_family(model) != model_family(verifier_model)
        ),
        "api_base_configured": bool(os.environ.get("REVIEW_API_BASE")),
        "live_verified": False,
    }


def _run(args: argparse.Namespace) -> None:
    status = model_status()
    if args.live:
        missing: set[str] = set()
        if not status["credential_configured"]:
            missing.update(status["credential_env_names"])
        if not status["verifier_credential_configured"]:
            missing.update(status["verifier_credential_env_names"])
        if missing:
            names = ", ".join(sorted(missing))
            raise ValueError(f"model credential is not configured; set one of: {names}")
        for model in dict.fromkeys(
            (str(status["model"]), str(status["verifier_model"]))
        ):
            verify_model_connection(model)
        status["live_verified"] = True
    print(json.dumps(status, indent=2, sort_keys=True))


def configure_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--live",
        action="store_true",
        help="Make a small structured request to each configured review model",
    )
    parser.set_defaults(handler=_run)
