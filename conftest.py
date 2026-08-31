"""Test-session defaults.

The unit suite is hermetic on purpose: it must not reach a network service, and it must
never touch whatever database the application happens to be configured with. Both became
live hazards once `app/store.py` started loading `.env` so that scripts could see
`DATABASE_URL` - from that point a populated `.env` silently routed every `audit.record`
in every test through a remote Postgres, which made the suite slow and, worse, pointed
destructive fixtures at production data.

`DATABASE_URL` is therefore cleared for the whole session. `test_store.py` opts back in
explicitly, against a throwaway database named by `STORE_TEST_DATABASE_URL`.
"""

import os

import pytest


@pytest.fixture(autouse=True, scope="session")
def _force_file_backend() -> None:
    os.environ.pop("DATABASE_URL", None)
