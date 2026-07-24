import pytest

from service.api_idempotency import (
    idempotency_key_sha256,
    validate_idempotency_key,
)


def test_idempotency_keys_are_bounded_and_hashed_before_storage():
    key = "review-trigger.client_retry-42"

    assert validate_idempotency_key(key) == key
    digest = idempotency_key_sha256(key)
    assert len(digest) == 64
    assert key not in digest

    for invalid in (
        "",
        " key",
        "key with spaces",
        "key\ninjection",
        "é",
        "a" * 201,
    ):
        with pytest.raises(ValueError, match="Idempotency-Key"):
            validate_idempotency_key(invalid)
