"""Sign in and report on the agent CLIs Diffuse can host for local review.

Host plumbing only: configuration directory, sandbox policy, version floor, and
login/status. No agent CLI is a selectable `REVIEW_RUNTIME` until its adapter
lands — see `docs/agent-runtimes.md`.
"""

from __future__ import annotations

import argparse
import json

from service.review.agent_host import (
    AGENT_CLIS,
    agent_status,
    login,
    resolve_cli,
    write_sandbox_settings,
)

RUNTIME_CHOICES = tuple(cli.runtime for cli in AGENT_CLIS)

#: The subcommand that rewrites the sandbox policy. Named here because
#: `agent_host.cli_status` prints it as the remedy for a stale policy, and a
#: remedy naming a command that does not exist is worse than no remedy.
POLICY_COMMAND = "write-policy"


def _login(args: argparse.Namespace) -> None:
    cli = resolve_cli(args.runtime)
    code = login(cli)
    if code != 0:
        raise RuntimeError(
            f"`{cli.executable} {' '.join(cli.login_arguments)}` exited {code}; "
            f"Diffuse did not change the credential in {cli.display_name}'s own "
            "configuration directory."
        )


def _status(args: argparse.Namespace) -> None:
    print(json.dumps(agent_status(), indent=2, sort_keys=True))


def _write_policy(args: argparse.Namespace) -> None:
    cli = resolve_cli(args.runtime)
    print(str(write_sandbox_settings(cli)))


def configure_parser(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="agent_command", required=True)

    login_parser = subparsers.add_parser(
        "login",
        help="Sign in to an agent CLI inside the Diffuse-owned configuration directory",
        description=(
            "Drive the vendor's own sign-in with its configuration directory pointed at\n"
            "Diffuse's. This is a second sign-in: Diffuse never reads or writes the CLI's\n"
            "default directory, so the terminal CLI you already use is untouched, and the\n"
            "credential Diffuse creates is one Diffuse's own process owns."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    login_parser.add_argument(
        "runtime",
        choices=RUNTIME_CHOICES,
        help="Which agent CLI to sign in to",
    )
    login_parser.set_defaults(handler=_login)

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
        "runtime",
        choices=RUNTIME_CHOICES,
        help="Which agent CLI's configuration directory to write the policy into",
    )
    policy_parser.set_defaults(handler=_write_policy)
