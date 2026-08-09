"""Commands for connecting a self-hosted instance to the Diffuse GitHub App."""

from __future__ import annotations

import argparse
import json

import httpx

from service.scm import normalize_base_url, scm_api_timeout_seconds


def _connect(args: argparse.Namespace) -> None:
    base_url = normalize_base_url(args.url.rstrip("/"), field_name="--url")
    if not base_url.startswith("https://"):
        raise ValueError("--url must be an HTTPS origin")
    try:
        response = httpx.post(
            f"{base_url}/v1/instances/register",
            json={"code": args.code, "display_name": args.name},
            timeout=scm_api_timeout_seconds(),
        )
    except httpx.HTTPError as error:
        raise RuntimeError(f"Could not reach the GitHub Integration Service: {error}") from error
    if response.status_code >= httpx.codes.BAD_REQUEST:
        raise ValueError(
            f"GitHub Integration Service rejected the connection code (HTTP {response.status_code})"
        )
    try:
        payload = response.json()
        instance_token = payload["instance_token"]
        event_signing_key = payload["event_signing_key"]
    except (ValueError, KeyError, TypeError) as error:
        raise RuntimeError(
            "GitHub Integration Service returned an invalid connection response"
        ) from error
    if not isinstance(instance_token, str) or not isinstance(event_signing_key, str):
        raise RuntimeError("GitHub Integration Service returned invalid connection credentials")
    print(
        json.dumps(
            {
                "DIFFUSE_GITHUB_INTEGRATION_URL": base_url,
                "DIFFUSE_GITHUB_INTEGRATION_TOKEN": instance_token,
                "DIFFUSE_GITHUB_DELIVERY_SIGNING_KEY": event_signing_key,
                "installation_id": payload.get("installation_id"),
                "instance_id": payload.get("instance_id"),
            },
            indent=2,
            sort_keys=True,
        )
    )


def configure_parser(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="github_command", required=True)
    connect = subparsers.add_parser(
        "connect",
        help="Connect this self-hosted instance to the Diffuse GitHub App",
    )
    connect.add_argument("code", help="One-time code shown after the GitHub App setup callback")
    connect.add_argument(
        "--name",
        required=True,
        help="Human-readable name for this self-hosted Diffuse instance",
    )
    connect.add_argument(
        "--url",
        default="https://api.diffuse.website",
        help="GitHub Integration Service origin (default: https://api.diffuse.website)",
    )
    connect.set_defaults(handler=_connect)
