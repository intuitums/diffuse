"""Test-suite-wide setup that must run before any Diffuse module is imported.

`import litellm` calls `load_dotenv()` at module scope unless `LITELLM_MODE` is
set to something other than `DEV`, which silently merges a `.env` in the working
directory into `os.environ` for the whole process. Every Diffuse package reaches
litellm transitively, so without this the unit suite runs against whatever the
developer happens to have in `.env` rather than against the shipped defaults.

The guard keeps unit tests independent of an operator's local deployment
configuration, including provider credentials and host-specific values.

pytest imports this module before it imports any test module, so setting the
variable here happens before the first `import litellm`.
"""

from __future__ import annotations

import os

os.environ.setdefault("LITELLM_MODE", "PRODUCTION")
