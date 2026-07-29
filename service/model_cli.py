"""Safe model readiness and live connectivity checks."""

from __future__ import annotations

import argparse
import json
import os

from service.model_config import ModelExecutor, load_model_execution_config
from service.model_providers import model_family, resolve_provider
from service.model_runner_client import runner_health
from service.review_engine import verify_model_connection


def _credential_status(model: str) -> tuple[str, tuple[str, ...], bool]:
    record = resolve_provider(model)
    credential_names = record.credential_env_names
    configured_names = tuple(name for name in credential_names if os.environ.get(name))
    configured = bool(configured_names) or not record.credential_required
    if record.provider_id == "aws-bedrock":
        # Bedrock accepts either a shared profile or an explicit key pair, so a
        # single non-empty name is not sufficient evidence.
        configured = bool(os.environ.get("AWS_PROFILE")) or bool(
            os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY")
        )
    elif model.strip().casefold().startswith("vertex_ai/"):
        # Vertex uses Google application-default credentials rather than an API
        # key. The credential file is optional when gcloud or workload identity
        # supplies ADC, while project and location are the normal routing hints.
        configured = bool(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")) or bool(
            os.environ.get("VERTEXAI_PROJECT") and os.environ.get("VERTEXAI_LOCATION")
        )
    return record.provider_id, credential_names, configured


def _authentication_kind(model: str) -> str:
    """Describe the selected provider boundary without exposing a credential."""
    record = resolve_provider(model)
    if not record.credential_required:
        return "none"
    if record.credential_value_is_api_key:
        return "api_key"
    if record.ambient_credentials_supported:
        return "ambient"
    return "external"


def model_status() -> dict[str, object]:
    config = load_model_execution_config()
    model = config.review_model
    verifier_model = config.verifier_model
    if config.executor is not ModelExecutor.LITELLM:
        return {
            "schema_version": "diffuse-model-status-v4",
            "executor": config.executor.value,
            "execution_mode": "cli_managed",
            "model": model,
            "provider": config.executor.value,
            "authentication_kind": "cli_managed",
            "credential_env_names": (),
            "credential_configured": False,
            "verifier_model": verifier_model,
            "verifier_provider": config.executor.value,
            "verifier_authentication_kind": "cli_managed",
            "verifier_credential_env_names": (),
            "verifier_credential_configured": False,
            "cross_family_review": False,
            "api_base_configured": False,
            "runner_socket": str(config.runner_socket),
            "runner_reachable": False,
            "live_verified": False,
        }
    provider, credential_names, configured = _credential_status(model)
    (
        verifier_provider,
        verifier_credential_names,
        verifier_configured,
    ) = _credential_status(verifier_model)
    return {
        "schema_version": "diffuse-model-status-v4",
        "executor": config.executor.value,
        "execution_mode": "provider_api",
        "model": model,
        "provider": provider,
        "authentication_kind": _authentication_kind(model),
        "credential_env_names": credential_names,
        "credential_configured": configured,
        "verifier_model": verifier_model,
        "verifier_provider": verifier_provider,
        "verifier_authentication_kind": _authentication_kind(verifier_model),
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
        if status["execution_mode"] == "cli_managed":
            config = load_model_execution_config()
            health = runner_health(config.runner_socket)
            if config.executor.value not in health.supported_executors:
                raise ValueError(f"model runner does not support {config.executor.value}")
            for model in dict.fromkeys((config.review_model, config.verifier_model)):
                verify_model_connection(model)
            status["runner_reachable"] = True
            status["credential_configured"] = True
            status["verifier_credential_configured"] = True
            status["live_verified"] = True
            print(json.dumps(status, indent=2, sort_keys=True))
            return
        missing: set[str] = set()
        unrecognized: list[str] = []
        for prefix in ("", "verifier_"):
            model = str(status[f"{prefix}model"])
            configured = bool(status[f"{prefix}credential_configured"])
            record = resolve_provider(model)
            if configured or record.ambient_credentials_supported:
                continue
            if record.credential_env_names:
                missing.update(status[f"{prefix}credential_env_names"])
            else:
                # No credential name to suggest: the identifier names neither a
                # provider Diffuse recognises nor a local deployment.
                unrecognized.append(model)
        if unrecognized:
            names = ", ".join(sorted(set(unrecognized)))
            raise ValueError(
                f"model identifier names no known provider: {names}; use a LiteLLM "
                "provider prefix, a self-hosted prefix, or an unprefixed "
                "REVIEW_API_BASE deployment name"
            )
        if missing:
            names = ", ".join(sorted(missing))
            raise ValueError(f"model credential is not configured; set one of: {names}")
        for model in dict.fromkeys((str(status["model"]), str(status["verifier_model"]))):
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
