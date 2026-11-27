"""The Vercel entry point serves the same app under /api."""
from fastapi.testclient import TestClient

from api.index import app as vercel_app


def client() -> TestClient:
    return TestClient(vercel_app)


def test_prefixed_routes_reach_the_app():
    c = client()
    assert c.get("/api/health").json() == {"status": "ok"}
    assert c.get("/api/guard/cases").status_code == 200


def test_bare_prefix_is_the_root_endpoint():
    c = client()
    assert c.get("/api").json() == c.get("/api/").json()
    assert c.get("/api/").status_code == 200


def test_openapi_knows_its_root_path():
    spec = client().get("/api/openapi.json").json()
    assert {"url": "/api"} in spec.get("servers", [])


def test_unknown_prefixed_route_is_a_404():
    assert client().get("/api/nope").status_code == 404
