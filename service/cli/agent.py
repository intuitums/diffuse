"""Configure authentication and report on the CLIs an Agent Host can run.

`REVIEW_AGENT=claude|codex` investigations execute only in their isolated
matching Agent Host. Local-branch review is unavailable until it can receive
the same Review Access Grant; there is no direct-model fallback. See
`docs/agents.md`.
"""

from __future__ import annotations

import argparse
import json

from service.review.agent_host import (
    AGENT_CLIS,
    agent_status,
    login,
    logout,
    resolve_cli,
    write_sandbox_settings,
)

RUNTIME_CHOICES = tuple(cli.runtime for cli in AGENT_CLIS)

#: The subcommand that rewrites the sandbox policy. Named here because
#: `agent_host.cli_status` prints it as the remedy for a stale policy, and a
#: remedy naming a command that does not exist is worse than no remedy.
POLICY_COMMAND = "write-policy"


def _vendor_arguments(args: argparse.Namespace) -> list[str]:
    """Everything after the CLI name, with one leading `--` removed.

    `argparse.REMAINDER` keeps the separator, so `diffuse agent login codex --
    --device-auth` would otherwise forward a bare `--` and Codex would read it
    as the end of its own options. Both spellings are supported because the
    unseparated one is what people type and the separated one is what they
    reach for when a flag collides with Diffuse's.
    """

    extra = list(args.vendor_arguments or [])
    if extra and extra[0] == "--":
        extra = extra[1:]
    return extra


def _help_requested(vendor_arguments: list[str]) -> bool:
    """True when REMAINDER captured a help flag that belongs to Diffuse.

    `argparse.REMAINDER` runs after the CLI name, so
    `diffuse agent login codex --help` never reaches argparse's own help
    handling — it would otherwise be forwarded as `codex login --help`.
    """

    return any(argument in {"-h", "--help"} for argument in vendor_arguments)


def _login(args: argparse.Namespace) -> None:
    vendor_arguments = _vendor_arguments(args)
    if _help_requested(vendor_arguments):
        # REMAINDER ate `-h` / `--help`; print Diffuse's login help, not the
        # vendor's. Exit 0 to match argparse's own `--help` behaviour.
        args.login_parser.print_help()
        raise SystemExit(0)
    cli = resolve_cli(args.cli)
    code = login(cli, vendor_arguments)
    if code != 0:
        invocation = " ".join([cli.executable, *cli.login_arguments, *vendor_arguments])
        raise RuntimeError(
            f"`{invocation}` exited {code}; "
            f"Diffuse did not change the credential in {cli.display_name}'s own "
            "configuration directory."
        )


def _status(args: argparse.Namespace) -> None:
    print(json.dumps(agent_status(), indent=2, sort_keys=True))


def _logout(args: argparse.Namespace) -> None:
    vendor_arguments = _vendor_arguments(args)
    if _help_requested(vendor_arguments):
        args.logout_parser.print_help()
        raise SystemExit(0)
    cli = resolve_cli(args.cli)
    code = logout(cli, vendor_arguments)
    if code != 0:
        invocation = " ".join([cli.executable, *cli.logout_arguments, *vendor_arguments])
        raise RuntimeError(
            f"`{invocation}` exited {code}; Diffuse left the credential directory in place "
            "so you can inspect or retry the vendor logout."
        )


def _write_policy(args: argparse.Namespace) -> None:
    cli = resolve_cli(args.cli)
    print(str(write_sandbox_settings(cli)))


def configure_parser(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="agent_command", required=True)

    login_parser = subparsers.add_parser(
        "login",
        help="Sign in to an agent CLI inside the Diffuse-owned configuration directory",
        description=(
            "Run the vendor CLI's own sign-in with its configuration directory pointed\n"
            "at Diffuse's. For Claude that is `claude auth login` (Claude.ai subscription,\n"
            "Anthropic API key, or a third-party / gateway option). For Codex that is\n"
            "`codex login` (ChatGPT OAuth by default; API key via Codex's own\n"
            "`--with-api-key` path if you prefer). Diffuse does not collect API keys or\n"
            "reimplement auth.\n"
            "\n"
            "Any further arguments are forwarded to the vendor command unchanged, so the\n"
            "auth method stays the vendor's to define. `--device-auth` is the one to\n"
            "reach for on a server: the default Codex flow expects a browser that can\n"
            "reach a callback on localhost, which a headless host does not have.\n"
            "\n"
            "This is a second sign-in. Diffuse never reads or writes ~/.claude or\n"
            "~/.codex, so the terminal CLI you already use is untouched, and the\n"
            "credential Diffuse creates is one Diffuse's own process owns."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  diffuse agent login claude\n"
            "  diffuse agent login claude --console\n"
            "  diffuse agent login codex\n"
            "  diffuse agent login codex --device-auth      # headless server\n"
            "  printenv OPENAI_API_KEY | diffuse agent login codex --with-api-key\n"
            "  diffuse agent status\n"
        ),
    )
    login_parser.add_argument(
        "cli",
        choices=RUNTIME_CHOICES,
        metavar="CLI",
        help="Which agent CLI to sign in to (claude or codex)",
    )
    login_parser.add_argument(
        "vendor_arguments",
        nargs=argparse.REMAINDER,
        metavar="-- VENDOR_ARGS",
        help=(
            "Arguments forwarded verbatim to the vendor's login command, "
            "e.g. --device-auth for Codex on a machine with no browser"
        ),
    )
    # Stash the parser so `_login` can print Diffuse help when REMAINDER
    # captures `-h` / `--help` after the CLI name.
    login_parser.set_defaults(handler=_login, login_parser=login_parser)

    logout_parser = subparsers.add_parser(
        "logout",
        help="Sign out of an agent CLI in the Diffuse-owned credential directory",
    )
    logout_parser.add_argument(
        "cli",
        choices=RUNTIME_CHOICES,
        metavar="CLI",
        help="Which agent CLI to sign out of (claude or codex)",
    )
    logout_parser.add_argument(
        "vendor_arguments",
        nargs=argparse.REMAINDER,
        metavar="-- VENDOR_ARGS",
        help="Arguments forwarded verbatim to the vendor's logout command",
    )
    logout_parser.set_defaults(handler=_logout, logout_parser=logout_parser)

    status_parser = subparsers.add_parser(
        "status",
        help="Report which agent CLIs are installed, current, and signed in",
    )
    status_parser.set_defaults(handler=_status)

    policy_parser = subparsers.add_parser(
        POLICY_COMMAND,
        help="Rewrite the sandbox policy in the Diffuse-owned configuration directory",
        description=(
            "`login` writes the policy too. This exists so an operator can restore it\n"
            "after inspecting or editing the file, without signing in again."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    policy_parser.add_argument(
        "cli",
        choices=RUNTIME_CHOICES,
        metavar="CLI",
        help="Which agent CLI's configuration directory to write the policy into",
    )
    policy_parser.set_defaults(handler=_write_policy)
