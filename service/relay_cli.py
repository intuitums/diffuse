"""Operator and node commands for the Diffuse integration relay."""

from __future__ import annotations

import argparse
import json
import os
import socket
from contextlib import closing

import httpx

from indexer.store import get_conn
from service.relay_store import NODE_TOKEN_PATTERN, create_pairing_code
from service.scm import normalize_base_url, scm_api_timeout_seconds

MAX_PAIRING_RESPONSE_BYTES = 64_000


def _pair(args: argparse.Namespace) -> None:
    base_url = normalize_base_url(
        args.gateway or os.environ.get("DIFFUSE_RELAY_URL", ""),
        field_name="--gateway",
    )
    code = args.code or os.environ.get("DIFFUSE_RELAY_PAIRING_CODE", "")
    if not code:
        raise ValueError(
            "Pairing code is required with --code or DIFFUSE_RELAY_PAIRING_CODE"
        )
    try:
        response = httpx.post(
            f"{base_url}/relay/v1/pair",
            json={"code": code, "name": args.name},
            headers={
                "Accept": "application/json",
                "User-Agent": "diffuse-node-pairing",
            },
            timeout=scm_api_timeout_seconds(),
        )
    except httpx.HTTPError as error:
        raise RuntimeError("Could not reach the Diffuse integration relay") from error
    if response.status_code >= httpx.codes.BAD_REQUEST:
        raise ValueError(
            "The relay rejected the pairing request; the code may be expired or already used"
        )
    if len(response.content) > MAX_PAIRING_RESPONSE_BYTES:
        raise RuntimeError("The relay returned an implausibly large pairing response")
    try:
        payload = response.json()
        token = payload["nodeToken"]
        node_id = payload["nodeId"]
        installation_id = payload["githubInstallationId"]
    except (ValueError, KeyError, TypeError) as error:
        raise RuntimeError("The relay returned an unreadable pairing response") from error
    if (
        not isinstance(token, str)
        or not NODE_TOKEN_PATTERN.fullmatch(token)
        or isinstance(node_id, bool)
        or not isinstance(node_id, int)
        or node_id <= 0
        or isinstance(installation_id, bool)
        or not isinstance(installation_id, int)
        or installation_id <= 0
    ):
        raise RuntimeError("The relay returned invalid pairing metadata")
    output = {
        "schema_version": "diffuse-relay-pair-v1",
        "gateway_url": base_url,
        "node_id": node_id,
        "github_installation_id": installation_id,
        "node_token": token,
        "next": (
            "Store gateway_url as DIFFUSE_RELAY_URL and node_token as "
            "DIFFUSE_RELAY_TOKEN in the node's secret manager, then restart app and worker."
        ),
    }
    print(json.dumps(output, sort_keys=True, indent=2))


def _issue_code(args: argparse.Namespace) -> None:
    with closing(get_conn()) as conn, conn:
        code = create_pairing_code(
            conn,
            user_id=args.user_id,
            github_installation_id=args.installation_id,
            ttl_seconds=args.ttl_seconds,
        )
    print(
        json.dumps(
            {
                "schema_version": "diffuse-relay-pairing-code-v1",
                "github_installation_id": args.installation_id,
                "pairing_code": code,
                "expires_in_seconds": args.ttl_seconds,
            },
            sort_keys=True,
            indent=2,
        )
    )


def configure_parser(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="relay_command", required=True)

    pair = commands.add_parser(
        "pair",
        help="Pair this self-hosted node with an installed Diffuse GitHub App",
    )
    pair.add_argument(
        "--gateway",
        help="Public Diffuse integration-relay URL (or DIFFUSE_RELAY_URL)",
    )
    pair.add_argument(
        "--code",
        help="Single-use pairing code (or DIFFUSE_RELAY_PAIRING_CODE)",
    )
    pair.add_argument(
        "--name",
        default=socket.gethostname(),
        help="Human-readable node name (default: this hostname)",
    )
    pair.set_defaults(handler=_pair)

    issue = commands.add_parser(
        "issue-code",
        help="Issue a replacement pairing code from the hosted relay database",
    )
    issue.add_argument(
        "--user-id",
        type=int,
        required=True,
        help="Relay database user id that owns the installation",
    )
    issue.add_argument(
        "--installation-id",
        type=int,
        required=True,
        help="GitHub App installation id to pair",
    )
    issue.add_argument(
        "--ttl-seconds",
        type=int,
        default=600,
        help="Pairing-code lifetime in seconds (default: 600)",
    )
    issue.set_defaults(handler=_issue_code)
