"""Guard the operational claims the deployment docs make about the healthcheck.

An operator reads the README to decide what they still have to build. Telling
them a wedged worker is "restarted" when nothing in this repository restarts it
is worse than saying nothing: they stop looking, and the worker sits
`(unhealthy)` and idle until someone notices the queue.

`docker compose` (as opposed to Swarm, Kubernetes, or an autoheal sidecar) never
acts on a container's health status. `restart: unless-stopped` reacts to the
process exiting, which a wedged process never does.
"""

import re
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

# Files that describe the worker healthcheck to an operator.
HEALTHCHECK_DOCUMENTS = ("README.md", "docker-compose.yml", ".env.example")

# Phrasings that promise remediation Compose does not perform.
FALSE_REMEDIATION_CLAIMS = (
    "is therefore restarted rather than reported",
    "wedged container into a restart",
    "healthcheck restarts it",
    "healthcheck restarts the",
    "probe restarts it",
)


def _read(name: str) -> str:
    """Flatten a document so a claim still matches when it wraps across lines.

    Every one of these files wraps its prose, and two of them wrap it inside
    `#` comments, so a claim written on one line here can be split by a newline
    and a comment marker in the file.
    """
    text = (REPOSITORY_ROOT / name).read_text(encoding="utf-8").lower()
    return " ".join(re.sub(r"#", " ", text).split())


@pytest.mark.parametrize("document", HEALTHCHECK_DOCUMENTS)
def test_no_document_claims_compose_restarts_a_wedged_worker(document):
    text = _read(document)
    for claim in FALSE_REMEDIATION_CLAIMS:
        assert claim not in text, f"{document} promises a restart Compose never performs"


@pytest.mark.parametrize("document", HEALTHCHECK_DOCUMENTS)
def test_every_document_says_the_probe_only_surfaces_the_condition(document):
    text = _read(document)
    assert "unhealthy" in text, document
    # And names at least one thing an operator has to add to act on it.
    assert any(
        remedy in text
        for remedy in ("autoheal", "kubernetes", "swarm", "alert on the health")
    ), document
