"""Operator CLI for inspecting and moderating feedback-derived rules."""

from __future__ import annotations

import argparse
import json
import os
import uuid
from contextlib import closing

from indexer.store import get_conn
from service.learning_store import (
    list_learned_rules,
    load_learned_rule_audit,
    moderate_learned_rule,
    queue_rule_learning_job,
)


def _record_json(record) -> dict[str, object]:
    return {
        "id": record.id,
        "repository_id": record.repository_id,
        "status": record.status,
        "version": record.version,
        "title": record.title,
        "guidance": record.guidance,
        "applies_to": list(record.applies_to),
        "severity": record.severity,
        "category": record.category,
        "evidence_count": record.evidence_count,
    }


def _list(args: argparse.Namespace) -> None:
    with closing(get_conn()) as conn:
        records = list_learned_rules(
            conn,
            repository_id=args.repository_id,
            status=args.status,
        )
    print(json.dumps([_record_json(record) for record in records], indent=2))


def _show(args: argparse.Namespace) -> None:
    with closing(get_conn()) as conn:
        value = load_learned_rule_audit(
            conn,
            repository_id=args.repository_id,
            learned_rule_id=args.rule_id,
        )
    print(json.dumps(value, indent=2))


def _learn(args: argparse.Namespace) -> None:
    with closing(get_conn()) as conn, conn:
        result = queue_rule_learning_job(
            conn,
            repository_id=args.repository_id,
            minimum_evidence=int(os.environ.get("RULE_LEARNING_MIN_EVIDENCE", "10")),
            minimum_pull_requests=int(
                os.environ.get("RULE_LEARNING_MIN_PULL_REQUESTS", "10")
            ),
            evaluation_interval_seconds=int(
                os.environ.get(
                    "RULE_LEARNING_EVALUATION_INTERVAL_SECONDS",
                    "3600",
                )
            ),
        )
    print(json.dumps({"job_id": result.job_id, "state": result.state}, indent=2))


def _moderate(args: argparse.Namespace) -> None:
    with closing(get_conn()) as conn, conn:
        record = moderate_learned_rule(
            conn,
            repository_id=args.repository_id,
            learned_rule_id=args.rule_id,
            action=args.learning_command,
            actor_login=args.actor,
            actor_authority="OPERATOR",
            event_key=args.event_key or f"operator:{uuid.uuid4()}",
            expected_version=args.expected_version,
            title=getattr(args, "title", None),
            guidance=getattr(args, "guidance", None),
            applies_to=(
                tuple(args.applies_to)
                if getattr(args, "applies_to", None) is not None
                else None
            ),
            severity=getattr(args, "severity", None),
            category=getattr(args, "category", None),
            reason=getattr(args, "reason", None),
        )
    print(json.dumps(_record_json(record), indent=2))


def _moderation_parser(
    subparsers,
    command: str,
    *,
    requires_reason: bool = False,
):
    help_text = {
        "edit": "Create a revised suggested-rule version",
        "approve": "Activate a suggested rule",
        "reject": "Reject a suggested rule",
        "deactivate": "Deactivate an approved rule",
        "reactivate": "Reactivate a previously approved rule",
    }
    parser = subparsers.add_parser(command, help=help_text[command])
    parser.add_argument("repository_id", type=int)
    parser.add_argument("rule_id", type=int)
    parser.add_argument("--expected-version", type=int, required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--event-key")
    if requires_reason:
        parser.add_argument("--reason", required=True)
    parser.set_defaults(handler=_moderate)
    return parser


def configure_parser(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(
        dest="learning_command",
        required=True,
    )

    list_parser = subparsers.add_parser(
        "list",
        help="List suggested or moderated learned rules",
    )
    list_parser.add_argument("repository_id", type=int)
    list_parser.add_argument(
        "--status",
        choices=("suggested", "active", "inactive", "rejected"),
    )
    list_parser.set_defaults(handler=_list)

    show_parser = subparsers.add_parser(
        "show",
        help="Show one learned rule and its audit history",
    )
    show_parser.add_argument("repository_id", type=int)
    show_parser.add_argument("rule_id", type=int)
    show_parser.set_defaults(handler=_show)

    learn_parser = subparsers.add_parser(
        "learn",
        help="Queue rule inference from repository feedback",
    )
    learn_parser.add_argument("repository_id", type=int)
    learn_parser.set_defaults(handler=_learn)

    edit_parser = _moderation_parser(subparsers, "edit")
    edit_parser.add_argument("--title")
    edit_parser.add_argument("--guidance")
    edit_parser.add_argument("--applies-to", action="append")
    edit_parser.add_argument(
        "--severity",
        choices=("critical", "high", "medium", "low"),
    )
    edit_parser.add_argument(
        "--category",
        choices=(
            "correctness",
            "security",
            "performance",
            "reliability",
            "testing",
            "architecture",
            "maintainability",
            "api",
        ),
    )
    _moderation_parser(subparsers, "approve")
    _moderation_parser(subparsers, "reject", requires_reason=True)
    _moderation_parser(subparsers, "deactivate", requires_reason=True)
    _moderation_parser(subparsers, "reactivate")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="diffuse-learning")
    configure_parser(parser)
    return parser


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    try:
        args.handler(args)
    except (OSError, RuntimeError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
