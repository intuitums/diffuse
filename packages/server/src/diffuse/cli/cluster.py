"""Operator CLI for same-host repository context clusters."""

from __future__ import annotations

import argparse
import json
from contextlib import closing

from diffuse.repository.cross_repository import (
    add_repository_cluster_member,
    create_repository_cluster,
    delete_repository_cluster,
    list_repository_clusters,
    remove_repository_cluster_member,
)
from diffuse.repository.indexing.store import get_conn


def _create(args: argparse.Namespace) -> None:
    with closing(get_conn()) as conn, conn:
        cluster = create_repository_cluster(
            conn,
            name=args.name,
            description=args.description,
            repository_ids=tuple(args.repository_id),
            actor_login=args.actor,
        )
    print(f"Created repository cluster {cluster.id}: {cluster.name}")


def _list(_args: argparse.Namespace) -> None:
    with closing(get_conn()) as conn:
        clusters = list_repository_clusters(conn)
    print(
        json.dumps(
            [
                {
                    "id": cluster.id,
                    "provider": cluster.scm_provider,
                    "base_url": cluster.scm_base_url,
                    "name": cluster.name,
                    "description": cluster.description,
                    "repository_ids": cluster.repository_ids,
                    "repository_names": cluster.repository_names,
                }
                for cluster in clusters
            ],
            indent=2,
        )
    )


def _add(args: argparse.Namespace) -> None:
    with closing(get_conn()) as conn, conn:
        changed = add_repository_cluster_member(
            conn,
            cluster_id=args.cluster_id,
            repository_id=args.repository_id,
            actor_login=args.actor,
        )
    state = "Added" if changed else "Already present"
    print(f"{state}: repository {args.repository_id} in cluster {args.cluster_id}.")


def _remove(args: argparse.Namespace) -> None:
    with closing(get_conn()) as conn, conn:
        changed = remove_repository_cluster_member(
            conn,
            cluster_id=args.cluster_id,
            repository_id=args.repository_id,
        )
    if not changed:
        raise ValueError("Cluster membership does not exist")
    print(f"Removed repository {args.repository_id} from cluster {args.cluster_id}.")


def _delete(args: argparse.Namespace) -> None:
    with closing(get_conn()) as conn, conn:
        changed = delete_repository_cluster(conn, cluster_id=args.cluster_id)
    if not changed:
        raise ValueError("Repository cluster does not exist")
    print(f"Deleted repository cluster {args.cluster_id}.")


def configure_parser(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(
        dest="cluster_command",
        required=True,
    )

    create_parser = subparsers.add_parser(
        "create",
        help="Create a same-host cross-repository context cluster",
    )
    create_parser.add_argument(
        "name",
        help="Human-readable cluster name, unique per installation",
    )
    create_parser.add_argument(
        "--repository-id",
        type=int,
        action="append",
        required=True,
        help="Repeat for each onboarded repository (2 to 8 total).",
    )
    create_parser.add_argument(
        "--actor",
        required=True,
        help="Operator identity recorded in the immutable audit trail",
    )
    create_parser.add_argument(
        "--description",
        help="Optional description stored with the cluster",
    )
    create_parser.set_defaults(handler=_create)

    list_parser = subparsers.add_parser(
        "list",
        help="List repository context clusters",
    )
    list_parser.set_defaults(handler=_list)

    add_parser = subparsers.add_parser(
        "add",
        help="Add a repository to a cluster",
    )
    add_parser.add_argument(
        "cluster_id",
        type=int,
        help="Numeric cluster id from `diffuse cluster list`",
    )
    add_parser.add_argument(
        "repository_id",
        type=int,
        help="Numeric repository id from `diffuse repository list`",
    )
    add_parser.add_argument(
        "--actor",
        required=True,
        help="Operator identity recorded in the immutable audit trail",
    )
    add_parser.set_defaults(handler=_add)

    remove_parser = subparsers.add_parser(
        "remove",
        help="Remove a repository from a cluster",
    )
    remove_parser.add_argument(
        "cluster_id",
        type=int,
        help="Numeric cluster id from `diffuse cluster list`",
    )
    remove_parser.add_argument(
        "repository_id",
        type=int,
        help="Numeric repository id to detach from the cluster",
    )
    remove_parser.set_defaults(handler=_remove)

    delete_parser = subparsers.add_parser(
        "delete",
        help="Delete a repository context cluster",
    )
    delete_parser.add_argument(
        "cluster_id",
        type=int,
        help="Numeric cluster id from `diffuse cluster list`",
    )
    delete_parser.set_defaults(handler=_delete)
