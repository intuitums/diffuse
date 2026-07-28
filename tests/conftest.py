"""Test-suite-wide setup that must run before any Diffuse module is imported.

`import litellm` calls `load_dotenv()` at module scope unless `LITELLM_MODE` is
set to something other than `DEV`, which silently merges a `.env` in the working
directory into `os.environ` for the whole process. Every Diffuse package reaches
litellm transitively, so without this the unit suite runs against whatever the
developer happens to have in `.env` rather than against the shipped defaults.

That is not hypothetical. `README.md` tells a developer to `cp .env.example .env`
before `docker compose up`, and `.env.example` sets
`DIFFUSE_MCP_ALLOWED_HOSTS=localhost:*,127.0.0.1:*,[::1]:*`. Having done exactly
that, `pytest` then fails in
`tests/test_mcp_server.py::test_mcp_initializes_lists_only_durable_tools_and_calls_one`
with `Invalid Host header: testserver` -- a failure in a file the developer never
touched, caused by a file the setup instructions told them to create.

pytest imports this module before it imports any test module, so setting the
variable here happens before the first `import litellm`.
"""

from __future__ import annotations

import os

os.environ.setdefault("LITELLM_MODE", "PRODUCTION")
