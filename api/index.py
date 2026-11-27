"""Vercel entry point: serves the FastAPI app under /api.

vercel.json rewrites /api/* to this function. The request keeps its original
path (/api/reconcile/sample), so the app is told its root_path is /api and
Starlette strips the prefix before routing. Locally the app still runs at /
with `uvicorn src.api.app:app`.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # evidence/ and tests/fixtures are read with relative paths

# The deployment's file system is read-only apart from /tmp, and the reference
# CSVs are generated, not committed. Vercel sets the client IP in
# X-Forwarded-For, which the rate limiter needs to tell visitors apart.
if os.environ.get("VERCEL"):  # set by Vercel at runtime; tests import this file too
    os.environ.setdefault("SAMPLE_DIR", "/tmp/recon-sample")
    os.environ.setdefault("TRUST_PROXY_HEADERS", "1")

from src.api.app import app as _api  # noqa: E402

PREFIX = "/api"


async def app(scope, receive, send):
    if scope["type"] in ("http", "websocket"):
        path = scope.get("path", "")
        if path == PREFIX:
            path = PREFIX + "/"
        if path.startswith(PREFIX + "/"):
            scope = {**scope, "path": path, "root_path": PREFIX}
    await _api(scope, receive, send)
