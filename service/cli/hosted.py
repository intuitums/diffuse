"""Enrollment commands for the shared Diffuse-Agent GitHub App."""

from __future__ import annotations

import argparse
import json

import httpx

from service.scm import normalize_base_url, scm_api_timeout_seconds


def _enroll(args: argparse.Namespace) -> None:
    base_url = normalize_base_url(args.url.rstrip("/"), field_name="--url")
    if not base_url.startswith("https://"):
        raise ValueError("--url must be an HTTPS origin")
    try:
        response = httpx.post(
            f"{base_url}/v1/enrollments/claim",
            json={"code": args.code, "display_name": args.name},
            timeout=scm_api_timeout_seconds(),
        )
    except httpx.HTTPError as error:
        raise RuntimeError(f"Could not reach hosted Diffuse-Agent: {error}") from error
    if response.status_code >= httpx.codes.BAD_REQUEST:
        raise ValueError(
            f"Hosted Diffuse-Agent rejected the enrollment code (HTTP {response.status_code})"
        )
    try:
        payload = response.json()
        instance_token = payload["instance_token"]
        event_signing_key = payload["event_signing_key"]
    except (ValueError, KeyError, TypeError) as error:
        raise RuntimeError(
            "Hosted Diffuse-Agent returned an invalid enrollment response"
        ) from error
    if not isinstance(instance_token, str) or not isinstance(event_signing_key, str):
        raise RuntimeError("Hosted Diffuse-Agent returned invalid enrollment credentials")
    print(
        json.dumps(
            {
                "DIFFUSE_HOSTED_RELAY_URL": base_url,
                "DIFFUSE_HOSTED_TOKEN_BROKER_URL": base_url,
                "DIFFUSE_HOSTED_INSTANCE_TOKEN": instance_token,
                "DIFFUSE_HOSTED_EVENT_SIGNING_KEY": event_signing_key,
                "installation_id": payload.get("installation_id"),
                "instance_id": payload.get("instance_id"),
            },
            indent=2,
            sort_keys=True,
        )
    )


def configure_parser(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="hosted_command", required=True)
    enroll = subparsers.add_parser(
        "enroll",
        help="Claim a one-time Diffuse-Agent setup code for this self-hosted instance",
    )
    enroll.add_argument("code", help="One-time code shown after the GitHub setup callback")
    enroll.add_argument(
        "--name",
        required=True,
        help="Human-readable name for this self-hosted Diffuse instance",
    )
    enroll.add_argument(
        "--url",
        default="https://api.diffuse.website",
        help="Hosted Diffuse-Agent origin (default: https://api.diffuse.website)",
    )
    enroll.set_defaults(handler=_enroll)
