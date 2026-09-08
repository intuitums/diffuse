"""PostgreSQL coverage for `diffuse_relay.store`.

The relay store is the durable queue behind api.diffuse.website and is 850+
lines of raw SQL. Until now its only tests faked the cursor layer, so a
migration/query drift or a wire-contract change could land without anything
failing. These tests run the real store against the same disposable database
the server integration suite uses (the compose test image migrates the server
schema; the relay's schema is idempotent and applied here).

The signature assertion is deliberately the server's own
`delivery_poller._verify_event`: it pins that the envelopes the relay leases
are exactly the envelopes the self-hosted poller accepts, not merely that the
relay can re-verify its own HMAC.

Enrollment re-keys an installation's single live instance row, so every test
uses its own installation id; a shared one would rotate credentials out from
under an earlier test.
"""

import base64
import hashlib
import itertools
import json
import os
import uuid
from contextlib import closing

import psycopg2
import pytest
from diffuse.github import delivery_poller
from diffuse_relay import store as relay_store
from diffuse_relay.config import CREDENTIAL_KEK_VARIABLE
from diffuse_relay.sealed_secret import is_sealed, key_id_for, unseal

ROTATED_KEK = base64.urlsafe_b64encode(b"n" * 32).decode().rstrip("=")
_installation_ids = itertools.count(9_000_000)
_delivery_ids = itertools.count()
_RUN = uuid.uuid4().hex[:8]
# Installation ids reserved for probes that must never be enrolled by any test;
# enrolling one would turn the "unknown installation is dropped" probe into a
# queued event on a persistent database.
_UNENROLLED_INSTALLATION_OFFSET = 20_000_000


def _delivery_id(label: str, installation_id: int) -> str:
    """GitHub delivery ids are globally unique; mirror that, even across reruns."""
    return f"{label}-{installation_id}-{_RUN}-{next(_delivery_ids)}"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@pytest.fixture(scope="session", autouse=True)
def _apply_relay_schema():
    """The relay schema, once per run; idempotent, so the server migration may run first."""
    relay_store.initialize_schema()


@pytest.fixture(autouse=True)
def _relay_environment(monkeypatch: pytest.MonkeyPatch):
    """One pepper and one sealing KEK for every test, fixed and known."""
    monkeypatch.setenv(
        "DIFFUSE_GITHUB_INTEGRATION_TOKEN_PEPPER",
        _b64url(b"relay-test-pepper-0123456789abcdef"),
    )
    monkeypatch.setenv(CREDENTIAL_KEK_VARIABLE, _b64url(b"k" * 32))
    monkeypatch.delenv("DIFFUSE_GITHUB_INTEGRATION_CREDENTIAL_KEK_PREVIOUS", raising=False)


@pytest.fixture
def installation_id() -> int:
    return next(_installation_ids)


def _enrolled_instance(installation_id: int, *, display_name: str = "test-instance"):
    """Register an installation and enroll an instance through a connection code."""
    relay_store.record_verified_installation(
        installation_id, github_user_id=1000, github_login="octocat"
    )
    code = relay_store.create_connection_code(installation_id)
    credentials = relay_store.redeem_connection_code(code, display_name=display_name)
    assert credentials is not None and credentials.installation_id == installation_id
    return credentials


def _store_event(delivery_id: str, installation_id: int) -> bool:
    payload = {"installation": {"id": installation_id}, "ref": "refs/heads/main"}
    return relay_store.record_webhook_event(
        delivery_id=delivery_id,
        installation_id=installation_id,
        event_name="push",
        payload=payload,
        payload_sha256=hashlib.sha256(json.dumps(payload).encode()).hexdigest(),
    )


def _assert_poller_accepts(credentials, events) -> None:
    """The leased envelopes are byte-for-byte what the self-hosted poller accepts."""
    config = delivery_poller.DeliveryPollerConfiguration(
        url="https://api.diffuse.website",
        instance_token=credentials.instance_token,
        event_signing_key=credentials.delivery_signing_key,
        poll_seconds=15,
    )
    for event in events:
        delivery_poller._verify_event(
            config,
            delivery_id=event.delivery_id,
            event_name=event.event_name,
            payload=event.payload,
            signature=event.signature,
        )


def _stored_delivery_signing_key(instance_id: str) -> str:
    with closing(psycopg2.connect(os.environ["DATABASE_URL"])) as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT delivery_signing_key FROM self_hosted_instances WHERE id = %s::uuid",
            (instance_id,),
        )
        return str(cursor.fetchone()[0])


def test_webhook_ingest_pull_acknowledge_round_trip(installation_id):
    credentials = _enrolled_instance(installation_id)
    instance = relay_store.authenticate_instance(credentials.instance_token)
    assert instance is not None and instance.installation_id == installation_id

    # Events for unknown installations are dropped, known ones are stored once.
    assert (
        _store_event(
            _delivery_id("d-0", installation_id),
            installation_id + _UNENROLLED_INSTALLATION_OFFSET,
        )
        is False
    )
    queued = _delivery_id("d-1", installation_id)
    assert _store_event(queued, installation_id) is True
    assert _store_event(queued, installation_id) is False

    events = relay_store.pull_events(instance)
    assert [event.delivery_id for event in events] == [queued]
    assert events[0].event_name == "push"
    assert events[0].payload["ref"] == "refs/heads/main"
    _assert_poller_accepts(credentials, events)

    # The lease holds a delivery back until it is acknowledged.
    assert relay_store.pull_events(instance) == ()
    assert relay_store.acknowledge_event(instance, queued) is True
    assert relay_store.pull_events(instance) == ()
    assert relay_store.acknowledge_event(instance, "d-1") is False


def test_suspension_gates_ingress_but_not_delivery(installation_id):
    credentials = _enrolled_instance(installation_id)
    instance = relay_store.authenticate_instance(credentials.instance_token)
    assert instance is not None

    # A persistent database may hold earlier runs' never-acknowledged events for
    # this installation; drain them so the assertions below see only this run's.
    for stale in relay_store.pull_events(instance):
        relay_store.acknowledge_event(instance, stale.delivery_id)

    queued = _delivery_id("s-1", installation_id)
    assert _store_event(queued, installation_id) is True
    assert relay_store.set_installation_active(installation_id, active=False) is True
    assert _store_event(_delivery_id("s-2", installation_id), installation_id) is False
    # Already-queued events stay leasable; suspension only stops new ingress.
    assert [event.delivery_id for event in relay_store.pull_events(instance)] == [queued]

    assert relay_store.set_installation_active(installation_id, active=True) is True
    assert _store_event(_delivery_id("s-3", installation_id), installation_id) is True


def test_uninstall_revokes_the_instance_credential(installation_id):
    credentials = _enrolled_instance(installation_id)
    assert (
        relay_store.set_installation_active(installation_id, active=False, revoke_instances=True)
        is True
    )

    assert relay_store.authenticate_instance(credentials.instance_token) is None
    assert relay_store.instance_status(credentials.instance_token) is None


def test_disconnect_revokes_only_the_calling_instance(installation_id):
    # Distinct installations: re-enrolling the same one rotates the same row.
    first = _enrolled_instance(installation_id)
    second = _enrolled_instance(installation_id + 10_000_000)

    assert relay_store.revoke_instance(first.instance_token) is True
    assert relay_store.revoke_instance(first.instance_token) is False
    assert relay_store.authenticate_instance(first.instance_token) is None
    assert relay_store.authenticate_instance(second.instance_token) is not None


def test_credentials_are_never_stored_as_plaintext(installation_id):
    credentials = _enrolled_instance(installation_id)
    stored_key = _stored_delivery_signing_key(credentials.instance_id)

    assert credentials.delivery_signing_key not in stored_key
    assert is_sealed(stored_key)
    assert (
        unseal(
            stored_key,
            keks=(_b64url_decode(_b64url(b"k" * 32)),),
            aad=relay_store.DELIVERY_SIGNING_KEY_AAD,
        )
        == credentials.delivery_signing_key
    )


def test_kek_rotation_reseals_on_authentication(monkeypatch, installation_id):
    credentials = _enrolled_instance(installation_id)
    current_kek = _b64url(b"k" * 32)

    # Dual-read under the previous KEK, then the row moves to the rotated one.
    monkeypatch.setenv(CREDENTIAL_KEK_VARIABLE, ROTATED_KEK)
    monkeypatch.setenv("DIFFUSE_GITHUB_INTEGRATION_CREDENTIAL_KEK_PREVIOUS", current_kek)
    assert relay_store.authenticate_instance(credentials.instance_token) is not None
    stored = _stored_delivery_signing_key(credentials.instance_id)
    assert stored.split(".")[2] == key_id_for(_b64url_decode(ROTATED_KEK))
    assert (
        unseal(
            stored,
            keks=(_b64url_decode(ROTATED_KEK),),
            aad=relay_store.DELIVERY_SIGNING_KEY_AAD,
        )
        == credentials.delivery_signing_key
    )


def test_connect_session_claims_credentials_exactly_once(installation_id):
    session = relay_store.create_connect_session(display_name="self-hosted-box")

    assert relay_store.claim_connect_session(session.session_id, "wrong-secret").status == (
        "not_found"
    )
    claim = relay_store.claim_connect_session(session.session_id, session.poll_secret)
    assert claim.status == "pending" and claim.expires_in_seconds > 0

    # A browser flow that did not authorize this installation cannot complete.
    assert (
        relay_store.complete_connect_session(
            session.session_id,
            installation_id=installation_id,
            github_user_id=1000,
            github_login="octocat",
            allowed_installation_ids={installation_id + 500},
        )
        is None
    )

    completed = relay_store.complete_connect_session(
        session.session_id,
        installation_id=installation_id,
        github_user_id=1000,
        github_login="octocat",
        allowed_installation_ids={installation_id},
    )
    assert completed is not None and completed.instance_token

    # The CLI poller claims the credentials once; the second poll is 410.
    ready = relay_store.claim_connect_session(session.session_id, session.poll_secret)
    assert ready.status == "ready" and ready.credentials is not None
    assert ready.credentials.instance_token == completed.instance_token
    assert ready.credentials.delivery_signing_key == completed.delivery_signing_key
    assert relay_store.claim_connect_session(session.session_id, session.poll_secret).status == (
        "consumed"
    )
