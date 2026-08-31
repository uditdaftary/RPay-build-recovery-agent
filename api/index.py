"""Serverless entrypoint.

Vercel imports `app` from this module and drives it as an ASGI application. The file
exists only to put the repository root on the import path before `app.server` is loaded:
the function's working directory is the bundle root, but the package is imported as
`app.*` and `PROJECT_ROOT` is derived from `__file__`, so without this the templates and
`data/ledger.json` resolve one directory too deep.

Nothing else belongs here. Routing, auth and state all live in `app/server.py`, so the
deployed surface and the local one are the same object.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.server import app  # noqa: E402

__all__ = ["app"]
