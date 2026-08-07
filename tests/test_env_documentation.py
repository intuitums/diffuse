"""Every environment variable the code reads must be documented in `.env.example`.

`test_deploy_env_example.py` guards one direction of drift: a variable in
`.env.example` that never reached the customer bundle. It cannot catch the other
direction — a variable the code reads that was never written down *anywhere*.
Six had accumulated in that blind spot: `REVIEW_DIAGRAM_DIFF_CHARS`,
`REVIEW_DIAGRAM_CONTEXT_CHARS`, `REVIEW_DIAGRAM_MAX_OUTPUT_TOKENS`,
`GOOGLE_API_KEY`, `AZURE_API_KEY`, and `DIFFUSE_CLI_TRACEBACK`.

Three of those six are validated by `validate_worker_configuration`, so a
malformed value stops the worker at startup with a name the operator has never
seen and cannot look up. Two are provider credentials — an operator running
Azure OpenAI or Gemini had no way to learn which variable to set short of
reading `model_providers.py`.

The undocumented variables were reachable three different ways, so this collects
all three rather than grepping for one shape:

1. a literal `os.environ.get("NAME")` / `os.environ["NAME"]` anywhere in the
   shipped packages;
2. a name listed in `service.hosted.worker._CONFIGURATION_PROBES`, which reaches
   its variable through a `partial(...)` and so has no literal read site; and
3. a credential named by `service.model_providers._PROVIDER_TABLE`, which is
   data rather than code.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SOURCE_ENV = REPOSITORY_ROOT / ".env.example"

# The packages that ship in the runtime image. Tests and evals are excluded:
# they legitimately read variables that are not deployment configuration.
SHIPPED_PACKAGES = ("service", "indexer", "retriever", "repository_policy")

ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]*")

# Variables read by the code that are deliberately NOT operator configuration.
#
# These are Compose-wired internal addresses and identities, deliberately not
# installation knobs: changing one can collapse the runner isolation boundary.
# They belong in the sealed service definitions, not the operator env file.
NOT_OPERATOR_CONFIGURATION: frozenset[str] = frozenset(
    {
        "DIFFUSE_AGENT_EGRESS_PROXY_HOST",
        "DIFFUSE_AGENT_MODEL_HOST",
        "DIFFUSE_AGENT_RUNTIME",
        "DIFFUSE_AGENT_TOOL_UPSTREAM",
        "DIFFUSE_AGENT_TOOL_URL",
        "DIFFUSE_RUNNER_IMAGE_VERSION",
    }
)


def _declared() -> set[str]:
    """Every variable the file names, whether the assignment is live or commented.

    A commented assignment still documents the variable, which is all this test
    asks for. `REVIEW_MODEL` is the case that forces it: it ships as
    `#REVIEW_MODEL=anthropic/claude-sonnet-5` in both files so that `cp
    .env.example .env` cannot resurrect the code default that was deleted for
    guessing at a credential the operator never named. Matching live assignments
    only would report the most carefully documented variable in the file as
    undocumented. `test_deploy_env_example.py` reads both forms for the same
    reason; the `#` may be separated from the name because `# NAME=...` is the
    comment style used throughout both files.
    """

    return set(
        re.findall(r"^#?[ \t]*([A-Z][A-Z0-9_]*)=", SOURCE_ENV.read_text(), re.M)
    )


def _is_environ(node: ast.expr) -> bool:
    """Match `os.environ` and a bare `environ` imported from `os`."""
    return (isinstance(node, ast.Attribute) and node.attr == "environ") or (
        isinstance(node, ast.Name) and node.id == "environ"
    )


def _literal_reads(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        # os.environ.get("NAME") and os.getenv("NAME")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            target = node.func.value
            reads_env = (node.func.attr == "get" and _is_environ(target)) or (
                node.func.attr == "getenv"
                and isinstance(target, ast.Name)
                and target.id == "os"
            )
            if reads_env and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    names.add(first.value)
        # os.environ["NAME"]
        if isinstance(node, ast.Subscript) and _is_environ(node.value):
            index = node.slice
            if isinstance(index, ast.Constant) and isinstance(index.value, str):
                names.add(index.value)
    return {name for name in names if ENV_NAME.fullmatch(name)}


def _named_strings(tree: ast.AST, variable: str) -> set[str]:
    """Every env-shaped string literal inside a named module-level assignment."""
    names: set[str] = set()
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.Assign):
            targets = list(node.targets)
        if not any(isinstance(t, ast.Name) and t.id == variable for t in targets):
            continue
        if node.value is None:
            continue
        for child in ast.walk(node.value):
            if (
                isinstance(child, ast.Constant)
                and isinstance(child.value, str)
                and ENV_NAME.fullmatch(child.value)
            ):
                names.add(child.value)
    return names


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(), filename=str(path))


def environment_variables_read() -> dict[str, set[str]]:
    """Map each variable the shipped code reads to the files that reach it."""
    sources: dict[str, set[str]] = {}

    def record(names: set[str], origin: str) -> None:
        for name in names:
            sources.setdefault(name, set()).add(origin)

    for package in SHIPPED_PACKAGES:
        for path in sorted((REPOSITORY_ROOT / package).rglob("*.py")):
            relative = str(path.relative_to(REPOSITORY_ROOT))
            record(_literal_reads(_parse(path)), relative)

    worker = _parse(REPOSITORY_ROOT / "service" / "hosted" / "worker.py")
    record(
        _named_strings(worker, "_CONFIGURATION_PROBES"),
        "service/hosted/worker.py::_CONFIGURATION_PROBES",
    )

    providers = _parse(REPOSITORY_ROOT / "service" / "model_providers.py")
    record(
        _named_strings(providers, "_PROVIDER_TABLE"),
        "service/model_providers.py::_PROVIDER_TABLE",
    )
    return sources


def test_collector_finds_the_known_reads():
    """Guards the guard: a collector that silently matches nothing always passes."""
    found = environment_variables_read()
    for name in (
        "GITHUB_TOKEN",  # literal os.environ.get
        "DATABASE_URL",  # literal, in indexer/
        "REVIEW_DIAGRAM_DIFF_CHARS",  # reachable only through _CONFIGURATION_PROBES
        "AZURE_API_KEY",  # reachable only through _PROVIDER_TABLE
    ):
        assert name in found, (
            f"{name} is read by the code but the collector missed it, so this "
            "test is no longer checking what it claims to check."
        )


def test_declared_reads_commented_recommendations_but_still_misses_the_absent():
    """Guard the guard: widening the scan to commented lines must not blind it.

    `_declared` counts `#NAME=value` so that a deliberately-commented variable
    reads as documented. The failure mode that would introduce is a matcher so
    loose it finds every name, which would make the test above pass forever.
    """

    declared = _declared()
    assert "REVIEW_MODEL" in declared, (
        "REVIEW_MODEL ships commented out and must still read as documented."
    )
    assert "DIFFUSE_BIND_HOST" in declared, "A live assignment must still count."
    assert "DIFFUSE_NOT_A_REAL_VARIABLE" not in declared, (
        "A name that appears nowhere in .env.example must not read as documented."
    )
    # A name merely mentioned in prose, with no assignment, is not a declaration.
    # AZURE_API_BASE is the live example: the AZURE_API_KEY comment names it as
    # something LiteLLM reads from the ambient environment, and nothing assigns
    # it. A matcher that counted prose would report it as documented.
    assert "AZURE_API_BASE" not in declared, (
        "A name mentioned only in prose must not read as a declaration; "
        "otherwise any variable named in a comment silences this test."
    )


def test_every_variable_the_code_reads_is_documented():
    found = environment_variables_read()
    undocumented = sorted(set(found) - _declared() - NOT_OPERATOR_CONFIGURATION)
    detail = "\n".join(f"  {name}: {sorted(found[name])}" for name in undocumented)
    assert not undocumented, (
        "These environment variables are read by the shipped code but are "
        "documented in neither .env.example nor deploy/env.example, so an "
        "operator cannot discover them:\n"
        f"{detail}\n"
        "Document each in .env.example (test_deploy_env_example.py will then "
        "require it in deploy/env.example too), or add it to "
        "NOT_OPERATOR_CONFIGURATION with a reason."
    )
