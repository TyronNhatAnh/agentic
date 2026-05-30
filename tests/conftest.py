"""Test-wide environment setup.

pytest imports conftest.py before collecting any test module, which is the only
hook that runs *before* the first `import agentic.*`. That matters because
`agentic.config` builds `settings = Settings()` as a module-level singleton on
first import — once built, later `os.environ` tweaks in individual test files are
ignored. Setting these here guarantees tests use a throwaway DB and an empty
service registry instead of the developer's real `agentic.db` / `services.json`.
"""

import os
import tempfile

os.environ.setdefault("AGENTIC_DB", tempfile.mktemp(suffix=".db"))
# Non-existent path → _load_service_seeds() returns [] (no seeding).
os.environ.setdefault("AGENTIC_SERVICES_JSON", tempfile.mktemp(suffix=".json"))
# Force legacy path unless a test opts in via monkeypatch. Process env wins over
# the developer's `.env` in pydantic-settings priority order.
os.environ["AGENTIC_USE_SDK"] = "false"
