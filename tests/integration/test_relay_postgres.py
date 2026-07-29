from __future__ import annotations

import json
import os
import uuid

import psycopg2

from service.oauth_store import upsert_user
from service.relay_store import (
    InstallationOwnershipError,
    acknowledge_delivery,
    authenticate_node,
    authorize_installation_user,
    create_pairing_code,
    exchange_pairing_code,
    lease_next_delivery,
    record_github_delivery,
    record_github_installation_event,
)


def _identifier() -> int:
    return int(uuid.uuid4().int % 8_000_000_000) + 1


def test_relay_pair_delivery_ack_and_installation_revocation():
    connection = psycopg2.connect(os.environ["POSTGRES_TEST_DATABASE_URL"])
    github_user_id = _identifier()
    account_id = _identifier()
    installation_id = _identifier()
    delivery_id = str(uuid.uuid4())
    try:
        with connection:
            user = upsert_user(
                connection,
                github_user_id=github_user_id,
                login=f"user-{github_user_id}",
                avatar_url=None,
            )
            created_payload = {
                "action": "created",
                "installation": {
                    "id": installation_id,
                    "account": {
                        "id": account_id,
                        "login": f"org-{account_id}",
                        "type": "Organization",
                    },
                },
                "sender": {
                    "id": github_user_id,
                    "login": f"user-{github_user_id}",
                },
            }
            record_github_installation_event(connection, payload=created_payload)
            assert (
                authorize_installation_user(
                    connection,
                    user_id=user.id,
                    github_installation_id=installation_id,
                )
                == f"org-{account_id}"
            )
            pairing_code = create_pairing_code(
                connection,
                user_id=user.id,
                github_installation_id=installation_id,
            )

        with connection:
            node, node_token = exchange_pairing_code(
                connection,
                code=pairing_code,
                node_name="integration-test-node",
            )
        with connection:
            assert authenticate_node(connection, token="x" * 43) is None
            authenticated = authenticate_node(connection, token=node_token)
            assert authenticated is not None
            assert authenticated.id == node.id
            assert authenticated.github_installation_id == installation_id

        body = json.dumps(
            {
                "installation": {"id": installation_id},
                "repository": {"full_name": "octo/example"},
            }
        ).encode()
        with connection:
            assert record_github_delivery(
                connection,
                github_installation_id=installation_id,
                provider_delivery_id=delivery_id,
                event_name="push",
                payload=body,
            )
            assert not record_github_delivery(
                connection,
                github_installation_id=installation_id,
                provider_delivery_id=delivery_id,
                event_name="push",
                payload=body,
            )
            delivery = lease_next_delivery(connection, node=node)
            assert delivery is not None
            assert delivery.payload == body
            assert acknowledge_delivery(
                connection,
                node=node,
                delivery_id=delivery.id,
            )

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT status, payload, delivered_at
                FROM relay_deliveries
                WHERE id = %s
                """,
                (delivery.id,),
            )
            status, payload, delivered_at = cursor.fetchone()
        assert status == "delivered"
        assert payload is None
        assert delivered_at is not None

        suspended_payload = {
            **created_payload,
            "action": "suspend",
        }
        with connection:
            record_github_installation_event(connection, payload=suspended_payload)
        with connection:
            assert authenticate_node(connection, token=node_token) is None

        unsuspended_payload = {
            **created_payload,
            "action": "unsuspend",
        }
        with connection:
            record_github_installation_event(connection, payload=unsuspended_payload)
        with connection:
            assert authenticate_node(connection, token=node_token) is not None

        deleted_payload = {
            **created_payload,
            "action": "deleted",
        }
        with connection:
            record_github_installation_event(connection, payload=deleted_payload)
        with connection:
            assert authenticate_node(connection, token=node_token) is None
        try:
            with connection:
                authorize_installation_user(
                    connection,
                    user_id=user.id,
                    github_installation_id=installation_id,
                )
        except InstallationOwnershipError:
            pass
        else:
            raise AssertionError("A revoked installation remained pairable")
    finally:
        connection.close()
