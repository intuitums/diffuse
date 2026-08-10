"""Commands for connecting a self-hosted instance to the Diffuse GitHub App."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
import webbrowser
from pathlib import Path

import httpx

from service.scm import normalize_base_url, scm_api_timeout_seconds

_ENV_KEYS = (
    "DIFFUSE_GITHUB_INTEGRATION_URL",
    "DIFFUSE_GITHUB_INTEGRATION_TOKEN",
    "DIFFUSE_GITHUB_DELIVERY_SIGNING_KEY",
)
_POLL_INTERVAL_SECONDS = 2.0
_DEFAULT_POLL_TIMEOUT_SECONDS = 15 * 60


def _emit_credentials(
    *,
    base_url: str,
    instance_token: str,
    event_signing_key: str,
    installation_id: object,
    instance_id: object,
    write_path: Path | None,
    write_fd: int | None,
) -> None:
    credentials = {
        "DIFFUSE_GITHUB_INTEGRATION_URL": base_url,
        "DIFFUSE_GITHUB_INTEGRATION_TOKEN": instance_token,
        "DIFFUSE_GITHUB_DELIVERY_SIGNING_KEY": event_signing_key,
        "installation_id": installation_id,
        "instance_id": instance_id,
    }
    if write_path is not None and write_fd is not None:
        try:
            _write_env_file(write_fd, credentials)
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
                    "ready": True,
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


def _connect_with_code(args: argparse.Namespace, *, write_path: Path | None, write_fd: int | None) -> None:
    base_url = normalize_base_url(args.url.rstrip("/"), field_name="--url")
    if not base_url.startswith("https://"):
        raise ValueError("--url must be an HTTPS origin")
    try:
        response = httpx.post(
            f"{base_url}/v1/instances/register",
            json={"code": args.code, "display_name": args.name},
            timeout=scm_api_timeout_seconds(),
        )
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise ValueError(
                "GitHub Integration Service rejected the connection code "
                f"(HTTP {response.status_code})"
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
        _emit_credentials(
            base_url=base_url,
            instance_token=instance_token,
            event_signing_key=event_signing_key,
            installation_id=payload.get("installation_id"),
            instance_id=payload.get("instance_id"),
            write_path=write_path,
            write_fd=write_fd,
        )
    except httpx.HTTPError as error:
        raise RuntimeError(f"Could not reach the GitHub Integration Service: {error}") from error


def _connect_with_browser(
    args: argparse.Namespace, *, write_path: Path | None, write_fd: int | None
) -> None:
    base_url = normalize_base_url(args.url.rstrip("/"), field_name="--url")
    if not base_url.startswith("https://"):
        raise ValueError("--url must be an HTTPS origin")
    try:
        create_response = httpx.post(
            f"{base_url}/v1/connect/sessions",
            json={"display_name": args.name},
            timeout=scm_api_timeout_seconds(),
        )
    except httpx.HTTPError as error:
        raise RuntimeError(f"Could not reach the GitHub Integration Service: {error}") from error
    if create_response.status_code >= httpx.codes.BAD_REQUEST:
        raise ValueError(
            "GitHub Integration Service rejected the connect session "
            f"(HTTP {create_response.status_code})"
        )
    try:
        created = create_response.json()
        session_id = created["session_id"]
        poll_secret = created["poll_secret"]
        browser_url = created["browser_url"]
        expires_in = int(created.get("expires_in_seconds") or _DEFAULT_POLL_TIMEOUT_SECONDS)
    except (ValueError, KeyError, TypeError) as error:
        raise RuntimeError(
            "GitHub Integration Service returned an invalid connect session response"
        ) from error
    if not isinstance(session_id, str) or not isinstance(poll_secret, str):
        raise RuntimeError("GitHub Integration Service returned invalid connect session secrets")
    if not isinstance(browser_url, str) or not browser_url.startswith("https://"):
        raise RuntimeError("GitHub Integration Service returned an invalid browser URL")

    opened = False
    if not args.no_browser:
        try:
            opened = bool(webbrowser.open(browser_url))
        except webbrowser.Error:
            opened = False
    if opened:
        print(f"Opened browser to connect GitHub. Waiting for authorization…", file=sys.stderr)
    else:
        print(
            "Open this URL to connect GitHub:\n"
            f"  {browser_url}\n"
            "Waiting for authorization…",
            file=sys.stderr,
        )

    deadline = time.monotonic() + max(30, expires_in)
    while time.monotonic() < deadline:
        try:
            poll_response = httpx.get(
                f"{base_url}/v1/connect/sessions/{session_id}",
                headers={"Authorization": f"Bearer {poll_secret}"},
                timeout=scm_api_timeout_seconds(),
            )
        except httpx.HTTPError as error:
            raise RuntimeError(
                f"Could not reach the GitHub Integration Service: {error}"
            ) from error
        if poll_response.status_code == httpx.codes.NOT_FOUND:
            raise ValueError("Connect session was not found")
        if poll_response.status_code == httpx.codes.GONE:
            detail = "Connect session expired or credentials were already claimed"
            try:
                detail = str(poll_response.json().get("detail") or detail)
            except ValueError:
                pass
            raise ValueError(detail)
        if poll_response.status_code >= httpx.codes.BAD_REQUEST:
            detail = "connect session failed"
            try:
                detail = str(poll_response.json().get("detail") or detail)
            except ValueError:
                pass
            raise ValueError(detail)
        try:
            payload = poll_response.json()
        except ValueError as error:
            raise RuntimeError(
                "GitHub Integration Service returned invalid connect poll JSON"
            ) from error
        status = payload.get("status")
        if status == "pending":
            time.sleep(_POLL_INTERVAL_SECONDS)
            continue
        if status != "ready":
            raise RuntimeError(f"Unexpected connect session status: {status!r}")
        instance_token = payload.get("instance_token")
        event_signing_key = payload.get("event_signing_key")
        if not isinstance(instance_token, str) or not isinstance(event_signing_key, str):
            raise RuntimeError("GitHub Integration Service returned invalid connection credentials")
        _emit_credentials(
            base_url=base_url,
            instance_token=instance_token,
            event_signing_key=event_signing_key,
            installation_id=payload.get("installation_id"),
            instance_id=payload.get("instance_id"),
            write_path=write_path,
            write_fd=write_fd,
        )
        return
    raise TimeoutError(
        "Timed out waiting for GitHub authorization. Re-run "
        "`diffuse github connect --name …` and complete the browser step."
    )


def _connect(args: argparse.Namespace) -> None:
    write_path: Path | None = None
    write_fd: int | None = None
    if args.write_env is not None:
        # Validate and lock down the destination before redeeming one-time
        # credentials so a local write failure cannot burn the session/code.
        write_path, write_fd = _prepare_write_env_path(Path(args.write_env))
    try:
        if args.code:
            _connect_with_code(args, write_path=write_path, write_fd=write_fd)
            write_fd = None
            return
        _connect_with_browser(args, write_path=write_path, write_fd=write_fd)
        write_fd = None
    finally:
        if write_fd is not None:
            os.close(write_fd)


def _integration_config_from_env(
    *,
    url: str | None = None,
    require: bool = True,
) -> tuple[str, str]:
    base_url = (url or os.environ.get("DIFFUSE_GITHUB_INTEGRATION_URL", "")).strip().rstrip("/")
    token = os.environ.get("DIFFUSE_GITHUB_INTEGRATION_TOKEN", "").strip()
    if not base_url and not token and not require:
        return "", ""
    if not base_url or not token:
        raise ValueError(
            "Set DIFFUSE_GITHUB_INTEGRATION_URL and DIFFUSE_GITHUB_INTEGRATION_TOKEN "
            "(from diffuse github connect), or pass --url with a token in the environment."
        )
    base_url = normalize_base_url(base_url, field_name="DIFFUSE_GITHUB_INTEGRATION_URL")
    if not base_url.startswith("https://"):
        raise ValueError("DIFFUSE_GITHUB_INTEGRATION_URL must be an HTTPS origin")
    return base_url, token


def _status(args: argparse.Namespace) -> None:
    base_url, token = _integration_config_from_env(url=args.url)
    try:
        response = httpx.get(
            f"{base_url}/v1/instances/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=scm_api_timeout_seconds(),
        )
    except httpx.HTTPError as error:
        raise RuntimeError(f"Could not reach the GitHub Integration Service: {error}") from error
    if response.status_code == httpx.codes.UNAUTHORIZED:
        raise ValueError(
            "GitHub Integration Service rejected DIFFUSE_GITHUB_INTEGRATION_TOKEN "
            "(revoked, rotated, or never connected)"
        )
    if response.status_code >= httpx.codes.BAD_REQUEST:
        raise RuntimeError(
            f"GitHub Integration Service status failed with HTTP {response.status_code}"
        )
    try:
        payload = response.json()
    except ValueError as error:
        raise RuntimeError("GitHub Integration Service returned invalid status JSON") from error
    print(json.dumps(payload, indent=2, sort_keys=True))
    if not payload.get("ready"):
        raise SystemExit(1)


def _disconnect(args: argparse.Namespace) -> None:
    base_url, token = _integration_config_from_env(url=args.url)
    try:
        response = httpx.post(
            f"{base_url}/v1/instances/disconnect",
            headers={"Authorization": f"Bearer {token}"},
            timeout=scm_api_timeout_seconds(),
        )
    except httpx.HTTPError as error:
        raise RuntimeError(f"Could not reach the GitHub Integration Service: {error}") from error
    if response.status_code == httpx.codes.UNAUTHORIZED:
        raise ValueError(
            "GitHub Integration Service rejected DIFFUSE_GITHUB_INTEGRATION_TOKEN "
            "(already disconnected or invalid)"
        )
    if response.status_code >= httpx.codes.BAD_REQUEST:
        raise RuntimeError(
            f"GitHub Integration Service disconnect failed with HTTP {response.status_code}"
        )
    print(
        json.dumps(
            {
                "status": "disconnected",
                "next": (
                    "Remove DIFFUSE_GITHUB_INTEGRATION_TOKEN and "
                    "DIFFUSE_GITHUB_DELIVERY_SIGNING_KEY from the deployment environment."
                ),
            },
            indent=2,
            sort_keys=True,
        )
    )


def _prepare_write_env_path(path: Path) -> tuple[Path, int]:
    """Open a writable regular PATH safely before redeeming an enrollment code."""
    path = path.expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError(f"--write-env must be a regular file path: {path}")
            os.fchmod(fd, 0o600)
        except BaseException:
            os.close(fd)
            raise
    except OSError as error:
        raise ValueError(f"--write-env path is not writable: {path}") from error
    return path, fd


def _write_env_file(fd: int, credentials: dict[str, object]) -> None:
    lines = [f"{key}={credentials[key]}\n" for key in _ENV_KEYS]
    try:
        # The descriptor was opened before code redemption, so reopening a
        # swapped path cannot redirect one-time secrets to another file.
        os.fchmod(fd, 0o600)
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
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
    connect.add_argument(
        "code",
        nargs="?",
        default=None,
        help=argparse.SUPPRESS,
    )
    connect.add_argument(
        "--code",
        dest="code_flag",
        default=None,
        help="Optional one-time code from the setup page (advanced; prefer browser flow)",
    )
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
    connect.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the authorization URL instead of opening a browser",
    )
    connect.set_defaults(handler=_connect_entry)

    status = subparsers.add_parser(
        "status",
        help="Show whether this instance is ready with the Diffuse GitHub App",
    )
    status.add_argument(
        "--url",
        default=None,
        help="Override DIFFUSE_GITHUB_INTEGRATION_URL",
    )
    status.set_defaults(handler=_status)

    disconnect = subparsers.add_parser(
        "disconnect",
        help="Revoke this instance's GitHub Integration Service credential",
    )
    disconnect.add_argument(
        "--url",
        default=None,
        help="Override DIFFUSE_GITHUB_INTEGRATION_URL",
    )
    disconnect.set_defaults(handler=_disconnect)


def _connect_entry(args: argparse.Namespace) -> None:
    code = args.code_flag or args.code
    args.code = code
    _connect(args)
