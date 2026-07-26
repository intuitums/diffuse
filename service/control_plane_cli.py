"""CLI for publishing bounded data-plane status projections."""

from __future__ import annotations

import argparse
from pathlib import Path

from service.control_plane import load_snapshot, publish_snapshot


def _publish(args: argparse.Namespace) -> None:
    publish_snapshot(load_snapshot(args.snapshot))
    print("Control-plane snapshot accepted.")


def configure_parser(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="control_plane_command", required=True)
    publish = subparsers.add_parser("publish", help="Publish an allowlisted JSON snapshot")
    publish.add_argument("snapshot", type=Path)
    publish.set_defaults(handler=_publish)
