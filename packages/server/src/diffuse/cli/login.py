"""The login / logout / status entry points for a self-hosted Diffuse instance.

`diffuse login` is the primary onboarding command: it links the instance to the
hosted Diffuse GitHub App, generates the local deployment secrets (Postgres
password and Agent Dispatch / access-grant keys) that an operator should never
have to invent, and points at the one credential the operator actually brings --
their model-provider login for an Agent Host. `logout` revokes the link and
signs out an agent; `status` reports readiness across both.

These are thin orchestrators. The GitHub Integration Service and Agent Host
mechanics live in `diffuse.cli.github` and `diffuse.cli.agent`; this module only
wires their existing handlers behind friendlier, flag-driven top-level commands
and adds the first-run secret generation the old `github connect` did not do.
"""

from __future__ import annotations

import argparse
import base64
import os
import secrets
import stat
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from diffuse_host.runtime import AGENT_CLIS

from diffuse.cli import agent as agent_cli
from diffuse.cli import github as github_cli

RUNTIME_CHOICES = tuple(cli.runtime for cli in AGENT_CLIS)

# Keys a fresh deployment never needs the operator to invent.
_LOCAL_SECRET_KEYS = (
    "POSTGRES_PASSWORD",
    "DIFFUSE_REVIEW_AGENT_DISPATCH_PRIVATE_KEY",
    "DIFFUSE_REVIEW_AGENT_DISPATCH_PUBLIC_KEY",
    "DIFFUSE_REVIEW_AGENT_CAPABILITY_SIGNING_KEY",
    "DIFFUSE_REVIEW_AGENT_TRANSPORT_SECRET",
)

_LOGIN_NEXT_STEPS = (
    "\nLinked this instance to the Diffuse GitHub App and generated its local "
    "deployment secrets.\n"
    "Next: sign in the model provider that runs reviews:\n"
    "  diffuse login codex -- --device-auth      # or: diffuse login claude\n"
    "Review readiness at any time with: diffuse status\n"
    "Revoke everything with: diffuse logout"
)


def _encode_b64_url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _new_local_secret_values() -> dict[str, str]:
    """A fresh set of local deployment secrets for a new instance."""
    private_key = Ed25519PrivateKey.generate()
    private_raw = private_key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_raw = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return {
        "POSTGRES_PASSWORD": _encode_b64_url(secrets.token_bytes(18)),
        "DIFFUSE_REVIEW_AGENT_DISPATCH_PRIVATE_KEY": _encode_b64_url(private_raw),
        "DIFFUSE_REVIEW_AGENT_DISPATCH_PUBLIC_KEY": _encode_b64_url(public_raw),
        # Any >=32-byte secret is valid for the capability HMAC; hex is kept for
        # parity with `.env.example`.
        "DIFFUSE_REVIEW_AGENT_CAPABILITY_SIGNING_KEY": secrets.token_hex(32),
        "DIFFUSE_REVIEW_AGENT_TRANSPORT_SECRET": _encode_b64_url(
            secrets.token_bytes(32)
        ),
    }


def generate_local_secrets(env_path: Path | str) -> dict[str, str]:
    """Fill any missing local deployment secrets into the env file.

    Keys with no value (an empty placeholder copied from `.env.example`) and
    keys absent entirely are both filled, so re-running never leaves a secret
    undefined and never appends a duplicate definition of an existing key.
    Non-empty values already present are left untouched, so re-running does not
    rotate a value a running deployment depends on. Returns the values that were
    written (empty when the file was already complete).
    """
    env_path = Path(env_path).expanduser()

    existing_text = env_path.read_text() if env_path.exists() else ""
    existing: dict[str, str] = {}
    kept: list[str] = []
    for line in existing_text.splitlines():
        key, separator, value = line.partition("=")
        normalized = key.strip()
        if separator and normalized and value.strip():
            existing.setdefault(normalized, value)
        # Drop empty placeholder lines for our secret keys (e.g. copied from
        # `.env.example`) so the generated value is not appended as a second,
        # duplicate definition of the same key.
        if separator and normalized in _LOCAL_SECRET_KEYS and not value.strip():
            continue
        kept.append(line)

    candidates = _new_local_secret_values()
    missing = [key for key in _LOCAL_SECRET_KEYS if key not in existing]
    if not missing:
        return {}
    additions = {key: candidates[key] for key in missing}

    output = "\n".join(kept)
    if output and not output.endswith("\n"):
        output += "\n"
    for key, value in additions.items():
        output += f"{key}={value}\n"

    env_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        env_path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"not a regular file: {env_path}")
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(output)
    finally:
        if fd >= 0:
            os.close(fd)
    return additions


def _vendor_arguments(args: argparse.Namespace) -> list[str]:
    """Everything after an optional `--`, with one leading `--` removed."""
    extra = list(args.vendor_arguments or [])
    if extra and extra[0] == "--":
        extra = extra[1:]
    return extra


def _help_requested(vendor_arguments: list[str]) -> bool:
    return any(argument in {"-h", "--help"} for argument in vendor_arguments)


def _github_env_path(args: argparse.Namespace) -> Path:
    if args.write_env:
        return Path(args.write_env).expanduser()
    return github_cli.default_write_env_path()


def _login_github(args: argparse.Namespace) -> None:
    env_path = _github_env_path(args)
    generated = generate_local_secrets(env_path)
    if generated:
        print(
            f"Generated local deployment secrets in {env_path} (mode 0600).",
            file=sys.stderr,
        )
    connect_args = argparse.Namespace(
        # `--code` on `login` maps to the connect one-time-code fallback.
        code=args.code,
        code_flag=args.code,
        name=args.name,
        url=args.url,
        write_env=str(env_path),
        print_secrets=args.print_secrets,
        no_browser=args.no_browser,
    )
    github_cli._connect_entry(connect_args)
    sys.stderr.write(_LOGIN_NEXT_STEPS + "\n")


def _login_agent(args: argparse.Namespace) -> None:
    vendor_arguments = _vendor_arguments(args)
    if _help_requested(vendor_arguments):
        args.login_parser.print_help()
        raise SystemExit(0)
    agent_cli._login(
        argparse.Namespace(
            cli=args.agent,
            vendor_arguments=vendor_arguments,
            login_parser=args.login_parser,
        )
    )


def _login(args: argparse.Namespace) -> None:
    if args.agent:
        _login_agent(args)
    else:
        _login_github(args)


def _logout(args: argparse.Namespace) -> None:
    if args.agent:
        vendor_arguments = _vendor_arguments(args)
        if _help_requested(vendor_arguments):
            args.logout_parser.print_help()
            raise SystemExit(0)
        agent_cli._logout(
            argparse.Namespace(
                cli=args.agent,
                vendor_arguments=vendor_arguments,
                logout_parser=args.logout_parser,
            )
        )
        return
    github_cli._disconnect(argparse.Namespace(url=args.url))


def _status(args: argparse.Namespace) -> None:
    github_ready = True
    try:
        github_cli._status(argparse.Namespace(url=args.url))
    except SystemExit:
        # `github status` raises only when the link is not ready; treat that as
        # a not-ready result rather than letting it abort before the agent check.
        github_ready = False
    except (ValueError, RuntimeError) as error:
        # Not connected / unreachable / bad response: report it cleanly instead
        # of dumping a traceback, but still fail closed.
        print(f"GitHub Integration Service: {error}", file=sys.stderr)
        github_ready = False
    agent_cli._status(argparse.Namespace())
    if not github_ready:
        raise SystemExit(1)


def configure_parser(
    subparsers: argparse._SubParsersAction,
) -> dict[str, argparse.ArgumentParser]:
    """Add `login`, `logout`, and `status`; return them by command name."""
    login = subparsers.add_parser(
        "login",
        help="Link this instance to the Diffuse GitHub App and sign in model providers",
        description=(
            "The one onboarding command. With no flags it links this self-hosted "
            "instance to the hosted Diffuse GitHub App (opening a browser to authorize "
            "GitHub), generates the local deployment secrets it needs (Postgres "
            "password, Agent Dispatch and access-grant keys), and prints the single "
            "remaining step: signing in the model provider. Use --agent to sign in a "
            "provider directly, or --github to re-link GitHub alone.\n"
            "\n"
            "There is no Diffuse user account. This command links the *instance* to the "
            "services it talks to; your Git account and your model provider key are "
            "the only identities involved."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  diffuse login                # link GitHub + generate secrets\n"
            "  diffuse login claude         # sign in Claude (account, API key, 3rd-party)\n"
            "  diffuse login codex -- --device-auth   # sign in Codex headless\n"
            "  diffuse status\n"
        ),
    )
    login.add_argument(
        "--github",
        action="store_true",
        help="Link (or re-link) the Diffuse GitHub App integration (the default path)",
    )
    login.add_argument(
        "agent",
        nargs="?",
        choices=RUNTIME_CHOICES,
        default=None,
        metavar="CLI",
        help=(
            "claude or codex to sign in a model provider: run the vendor's own auth "
            "selector (account, API key, or third-party gateway). Omit to link GitHub."
        ),
    )
    login.add_argument(
        "vendor_arguments",
        nargs=argparse.REMAINDER,
        metavar="-- VENDOR_ARGS",
        help="Arguments forwarded verbatim to the vendor login, e.g. --device-auth",
    )
    _add_connect_options(login)
    login.set_defaults(handler=_login, login_parser=login)

    logout = subparsers.add_parser(
        "logout",
        help="Revoke the GitHub integration and sign out model providers",
        description=(
            "By default revokes this instance's GitHub Integration Service credential. "
            "Use --agent to sign out a model provider's agent CLI instead."
        ),
    )
    logout.add_argument(
        "--github",
        action="store_true",
        help="Revoke the GitHub App integration (the default path)",
    )
    logout.add_argument(
        "agent",
        nargs="?",
        choices=RUNTIME_CHOICES,
        default=None,
        metavar="CLI",
        help="claude or codex to sign out that model provider (default: revoke GitHub)",
    )
    logout.add_argument(
        "vendor_arguments",
        nargs=argparse.REMAINDER,
        metavar="-- VENDOR_ARGS",
        help="Arguments forwarded verbatim to the vendor logout",
    )
    logout.add_argument(
        "--url",
        default=None,
        help="Override DIFFUSE_GITHUB_INTEGRATION_URL",
    )
    logout.set_defaults(handler=_logout, logout_parser=logout)

    status = subparsers.add_parser(
        "status",
        help="Report GitHub and model-provider readiness",
        description=(
            "Reachability against the GitHub Integration Service plus the signed-in "
            "state of each installed agent CLI. Exits non-zero when the GitHub link is "
            "not ready."
        ),
    )
    status.add_argument(
        "--url",
        default=None,
        help="Override DIFFUSE_GITHUB_INTEGRATION_URL",
    )
    status.set_defaults(handler=_status)

    return {"login": login, "logout": logout, "status": status}


def _add_connect_options(parser: argparse.ArgumentParser) -> None:
    """Mirror the `github connect` overrides so `login` offers the same controls."""
    parser.add_argument(
        "--code",
        dest="code",
        default=None,
        help="Optional one-time code from the setup page (advanced; prefer browser flow)",
    )
    parser.add_argument(
        "--name",
        default=None,
        help=(
            "Optional label for this instance in status/diagnostics "
            f"(default: this machine's hostname, e.g. {github_cli.default_instance_name()})"
        ),
    )
    parser.add_argument(
        "--url",
        default="https://api.diffuse.website",
        help="GitHub Integration Service origin (default: https://api.diffuse.website)",
    )
    parser.add_argument(
        "--write-env",
        metavar="PATH",
        default=None,
        help=(
            "Override where secrets are written (default: ./"
            f"{'.env'}, mode 0600)"
        ),
    )
    parser.add_argument(
        "--print-secrets",
        action="store_true",
        help="Print one-time secrets to stdout instead of writing an env file",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the authorization URL instead of opening a browser",
    )
