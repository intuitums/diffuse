"""Every relative link in the agent-facing markdown resolves to a real file.

Dead pointers teach agents (and humans) to distrust the docs; link existence
is mechanical, so it is enforced here rather than by prose convention.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LINK_PATTERN = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def _markdown_files():
    yield from REPO_ROOT.glob("*.md")
    yield from (REPO_ROOT / "docs").glob("*.md")


def test_markdown_links_resolve():
    missing = []
    for document in _markdown_files():
        for raw_target in LINK_PATTERN.findall(document.read_text(encoding="utf-8")):
            target = raw_target.split("#", 1)[0]
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            if not (document.parent / target).exists():
                missing.append(f"{document.relative_to(REPO_ROOT)} -> {raw_target}")
    assert not missing, "Dead markdown links:\n" + "\n".join(missing)
