"""Commands for connecting a self-hosted instance to the Diffuse GitHub App."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

from service.scm import normalize_base_url, scm_api_timeout_seconds

_ENV_KEYS = (
    "DIFFUSE_GITHUB_INTEGRATION_URL",
    "DIFFUSE_GITHUB_INTEGRATION_TOKEN",
    "DIFFUSE_GITHUB_DELIVERY_SIGNING_KEY",
)


def _connect(args: argparse.Namespace) -> None:
    base_url = normalize_base_url(args.url.rstrip("/"), field_name="--url")
    if not base_url.startswith("https://"):
        raise ValueError("--url must be an HTTPS origin")
    write_path: Path | None = None
    if args.write_env is not None:
        # Validate and lock down the destination before redeeming the one-time
        # enrollment code so a local write failure cannot burn the code.
        write_path = _prepare_write_env_path(Path(args.write_env))
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
    credentials = {
        "DIFFUSE_GITHUB_INTEGRATION_URL": base_url,
        "DIFFUSE_GITHUB_INTEGRATION_TOKEN": instance_token,
        "DIFFUSE_GITHUB_DELIVERY_SIGNING_KEY": event_signing_key,
        "installation_id": payload.get("installation_id"),
        "instance_id": payload.get("instance_id"),
    }
    if write_path is not None:
        try:
            _write_env_file(write_path, credentials)
        except OSError as error:
            print(
                f"Warning: failed to write {write_path}: {error}. "
                "Printing one-time connection secrets to stdout so they are not lost.",
                file=sys.stderr,
            )
            print(json.dumps(credentials, indent=2, sort_keys=True))
            raise RuntimeError(f"Could not write connection secrets to {write_path}") from error
        print(
            json.dumps(
                {
                    "wrote_env": str(write_path),
                    "installation_id": credentials["installation_id"],
                    "instance_id": credentials["instance_id"],
                    "secrets_shown_once": True,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    print(
        "Warning: printing one-time connection secrets to stdout. "
        "Prefer --write-env PATH (mode 0600).",
        file=sys.stderr,
    )
    print(json.dumps(credentials, indent=2, sort_keys=True))


def _prepare_write_env_path(path: Path) -> Path:
    """Ensure PATH is a writable regular file with mode 0600 before redeeming."""
    path = path.expanduser()
    if path.exists() and not path.is_file():
        raise ValueError(f"--write-env must be a regular file path: {path}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
    except OSError as error:
        raise ValueError(f"--write-env path is not writable: {path}") from error
    return path


def _write_env_file(path: Path, credentials: dict[str, object]) -> None:
    lines = [f"{key}={credentials[key]}\n" for key in _ENV_KEYS]
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        # os.open's mode is ignored when the path already exists; lock the
        # descriptor down before any secret bytes are written.
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.writelines(lines)
    finally:
        if fd >= 0:
            os.close(fd)


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
    connect.add_argument(
        "--write-env",
        metavar="PATH",
        help=(
            "Write connection secrets to PATH with mode 0600 instead of printing "
            "them to stdout"
        ),
    )
    connect.set_defaults(handler=_connect)
