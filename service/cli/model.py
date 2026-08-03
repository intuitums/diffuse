"""Safe model readiness and live connectivity checks."""

from __future__ import annotations

import argparse
import json
import os

from service.model_providers import model_family, resolve_provider
from service.review.engine import (
    model_capabilities,
    resolve_review_depth_support,
    review_model,
    review_verifier_model,
    verify_model_connection,
)


def _credential_status(model: str) -> tuple[str, tuple[str, ...], bool]:
    record = resolve_provider(model)
    credential_names = record.credential_env_names
    configured_names = tuple(name for name in credential_names if os.environ.get(name))
    configured = bool(configured_names) or not record.credential_required
    if model.strip().casefold().startswith("vertex_ai/"):
        # Vertex uses Google application-default credentials rather than an API
        # key. The credential file is optional when gcloud or workload identity
        # supplies ADC, while project and location are the normal routing hints.
        configured = bool(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")) or bool(
            os.environ.get("VERTEXAI_PROJECT")
            and os.environ.get("VERTEXAI_LOCATION")
        )
    return record.provider_id, credential_names, configured


def model_status() -> dict[str, object]:
    model = review_model()
    verifier_model = review_verifier_model()
    provider, credential_names, configured = _credential_status(model)
    (
        verifier_provider,
        verifier_credential_names,
        verifier_configured,
    ) = _credential_status(verifier_model)
    depth_support = resolve_review_depth_support()
    return {
        "schema_version": "diffuse-model-status-v3",
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
        # What each model actually supports, probed offline. `diffuse init`
        # (W5.2) shows this before writing any configuration, so an operator
        # picks a model knowing what it can be asked to do.
        "capabilities": model_capabilities(model).as_dict(),
        "verifier_capabilities": model_capabilities(verifier_model).as_dict(),
        "review_depth": {
            "requested": depth_support.depth,
            "variable": depth_support.variable if depth_support.depth else None,
            "fully_honored": depth_support.fully_honored,
            "report": list(depth_support.report_lines()),
        },
        "live_verified": False,
    }


def _run(args: argparse.Namespace) -> None:
    status = model_status()
    if args.live:
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
