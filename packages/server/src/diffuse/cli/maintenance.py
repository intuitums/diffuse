"""Explicit operator maintenance commands, kept out of daily repo controls."""

from __future__ import annotations

import argparse

from diffuse.cli import repository as repository_cli


def configure_parser(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="maintenance_command", required=True)
    reindex = subparsers.add_parser(
        "reindex",
        help="Fetch and queue a fresh default-branch index",
    )
    reindex.add_argument(
        "repository",
        nargs="?",
        metavar="OWNER/REPO",
        help="Repository full name; omit when using --all",
    )
    reindex.add_argument(
        "--base-url",
        metavar="URL",
        help="GitHub host when the same owner/repo exists on more than one host",
    )
    reindex.add_argument(
        "--all",
        action="store_true",
        help="Reindex every enabled repository after an index-format upgrade",
    )
    reindex.set_defaults(handler=repository_cli._reindex_repository)
