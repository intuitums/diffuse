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

_DEFAULT_POLL_SECONDS = 2.0


def _connect(args: argparse.Namespace) -> None:
    base_url = normalize_base_url(args.url.rstrip("/"), field_name="--url")
    if not base_url.startswith("https://"):
        raise ValueError("--url must be an HTTPS origin")
    write_path: Path | None = None
    write_fd: int | None = None
    if args.write_env is not None:
        # Validate and lock down the destination before redeeming one-time
        # credentials so a local write failure cannot burn them.
        write_path, write_fd = _prepare_write_env_path(Path(args.write_env))
    try:
        if args.code:
            credentials = _redeem_connection_code(
                base_url,
                code=args.code,
                display_name=args.name,
            )
        else:
            credentials = _connect_via_browser_session(
                base_url,
                display_name=args.name,
                open_browser=not args.no_browser,
                poll_seconds=args.poll_seconds,
            )
        # _emit_credentials always consumes a preopened write descriptor.
        owned_fd, write_fd = write_fd, None
        _emit_credentials(credentials, write_path=write_path, write_fd=owned_fd)
    except httpx.HTTPError as error:
        raise RuntimeError(f"Could not reach the GitHub Integration Service: {error}") from error
    finally:
        if write_fd is not None:
            os.close(write_fd)


def _redeem_connection_code(
    base_url: str,
    *,
    code: str,
    display_name: str,
) -> dict[str, object]:
    response = httpx.post(
        f"{base_url}/v1/instances/register",
        json={"code": code, "display_name": display_name},
        timeout=scm_api_timeout_seconds(),
    )
    if response.status_code >= httpx.codes.BAD_REQUEST:
        raise ValueError(
            "GitHub Integration Service rejected the connection code "
            f"(HTTP {response.status_code})"
        )
    return _credentials_from_payload(base_url, response.json())


def _connect_via_browser_session(
    base_url: str,
    *,
    display_name: str,
    open_browser: bool,
    poll_seconds: float,
) -> dict[str, object]:
    if poll_seconds <= 0:
        raise ValueError("--poll-seconds must be positive")
    create_response = httpx.post(
        f"{base_url}/v1/connect/sessions",
        json={"display_name": display_name},
        timeout=scm_api_timeout_seconds(),
    )
    if create_response.status_code >= httpx.codes.BAD_REQUEST:
        raise ValueError(
            "GitHub Integration Service could not start a connect session "
            f"(HTTP {create_response.status_code})"
        )
    try:
        created = create_response.json()
        session_id = created["session_id"]
        poll_token = created["poll_token"]
        browser_url = created["browser_url"]
        expires_in = int(created.get("expires_in_seconds") or 900)
    except (ValueError, KeyError, TypeError) as error:
        raise RuntimeError(
            "GitHub Integration Service returned an invalid connect session response"
        ) from error
    if not isinstance(session_id, str) or not isinstance(poll_token, str):
        raise RuntimeError("GitHub Integration Service returned invalid connect session secrets")
    if not isinstance(browser_url, str) or not browser_url.startswith("https://"):
        raise RuntimeError("GitHub Integration Service returned an invalid browser URL")

    print(f"Open this URL to authorize the Diffuse GitHub App:\n{browser_url}", file=sys.stderr)
    if open_browser:
        try:
            webbrowser.open(browser_url)
        except webbrowser.Error:
            print(
                "Could not open a browser automatically; open the URL above manually.",
                file=sys.stderr,
            )
    print("Waiting for the browser flow to finish…", file=sys.stderr)

    deadline = time.monotonic() + max(expires_in, 1)
    while time.monotonic() < deadline:
        poll_response = httpx.get(
            f"{base_url}/v1/connect/sessions/{session_id}",
            headers={"Authorization": f"Bearer {poll_token}"},
            timeout=scm_api_timeout_seconds(),
        )
        if poll_response.status_code == httpx.codes.UNAUTHORIZED:
            raise ValueError("GitHub Integration Service rejected the connect session credential")
        if poll_response.status_code == httpx.codes.NOT_FOUND:
            raise ValueError("Connect session was not found; run diffuse github connect again")
        if poll_response.status_code >= httpx.codes.BAD_REQUEST:
            raise RuntimeError(
                "GitHub Integration Service connect poll failed "
                f"(HTTP {poll_response.status_code})"
            )
        try:
            payload = poll_response.json()
            status_name = payload["status"]
        except (ValueError, KeyError, TypeError) as error:
            raise RuntimeError(
                "GitHub Integration Service returned an invalid connect poll response"
            ) from error
        if status_name == "pending":
            time.sleep(poll_seconds)
            continue
        if status_name == "ready":
            return _credentials_from_payload(base_url, payload)
        if status_name == "expired":
            raise TimeoutError(
                "Connect session expired before authorization finished. "
                "Run diffuse github connect again."
            )
        if status_name == "consumed":
            raise RuntimeError(
                "Connect session credentials were already claimed. "
                "Run diffuse github connect again."
            )
        raise RuntimeError(f"Connect session failed ({status_name})")
    raise TimeoutError(
        "Timed out waiting for GitHub App authorization. Run diffuse github connect again."
    )


def _credentials_from_payload(base_url: str, payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise RuntimeError("GitHub Integration Service returned an invalid connection response")
    try:
        instance_token = payload["instance_token"]
        event_signing_key = payload["event_signing_key"]
    except KeyError as error:
        raise RuntimeError(
            "GitHub Integration Service returned an invalid connection response"
        ) from error
    if not isinstance(instance_token, str) or not isinstance(event_signing_key, str):
        raise RuntimeError("GitHub Integration Service returned invalid connection credentials")
    return {
        "DIFFUSE_GITHUB_INTEGRATION_URL": base_url,
        "DIFFUSE_GITHUB_INTEGRATION_TOKEN": instance_token,
        "DIFFUSE_GITHUB_DELIVERY_SIGNING_KEY": event_signing_key,
        "installation_id": payload.get("installation_id"),
        "instance_id": payload.get("instance_id"),
    }


def _emit_credentials(
    credentials: dict[str, object],
    *,
    write_path: Path | None,
    write_fd: int | None,
) -> None:
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
        finally:
            # _write_env_file always consumes and closes the descriptor.
            write_fd = None
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
    if write_fd is not None:
        os.close(write_fd)
    print(
        "Warning: printing one-time connection secrets to stdout. "
        "Prefer --write-env PATH (mode 0600).",
        file=sys.stderr,
    )
    print(json.dumps(credentials, indent=2, sort_keys=True))


def _prepare_write_env_path(path: Path) -> tuple[Path, int]:
    """Open a writable regular PATH safely before redeeming credentials."""
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
        # The descriptor was opened before credential delivery, so reopening a
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
        help=(
            "Deprecated one-time code from an older setup page. "
            "Omit this and let the CLI open a browser instead."
        ),
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
        help="Print the authorization URL without opening a browser",
    )
    connect.add_argument(
        "--poll-seconds",
        type=float,
        default=_DEFAULT_POLL_SECONDS,
        help="Seconds between connect-session polls (default: 2)",
    )
    connect.set_defaults(handler=_connect)
