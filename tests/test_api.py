"""
Smoke tests for the FastAPI surface.

Skipped if fastapi is not installed — run.py is the guaranteed-portable
entrypoint (stdlib only). The API is optional and requires
`pip install fastapi uvicorn`.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest

try:
    from fastapi.testclient import TestClient
    from src.api.app import app, get_store
    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False

import mongomock

from src.store.mongo_store import AuditStore


pytestmark = pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")


@pytest.fixture
def client():
    """API with no audit store, as when MONGO_URL is unset."""
    app.dependency_overrides[get_store] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def stored_client():
    """API backed by an in-memory MongoDB (mongomock)."""
    store = AuditStore(mongomock.MongoClient()["recon_api_test"])
    store.ensure_indexes()
    app.dependency_overrides[get_store] = lambda: store
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_root_advertises_replay_only(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "settlement-reconciliation-agent"
    assert "replay" in body["classifier_mode"].lower()
    assert "GET /reconcile/sample" in body["endpoints"]
    assert "POST /reconcile" in body["endpoints"]


def test_reconcile_sample_runs_full_pipeline_on_committed_batch(client):
    r = client.get("/reconcile/sample")
    assert r.status_code == 200, r.text
    body = r.json()
    c = body["counts"]
    # These are the numbers the committed batch produces; they should be
    # stable across runs because the classifier replays committed responses.
    assert c["total_records"] == 63
    assert c["matcher_matched"] == 51
    assert c["matcher_escalated"] == 12
    # Live-batch guard: everything must be accepted because the committed
    # LLM responses produced 0 false reconciliations.
    assert c["guard_rejected"] == 0
    assert c["guard_accepted"] == 12


def test_reconcile_post_accepts_uploaded_committed_csvs(client):
    orders_bytes = Path("data/orders_ledger.csv").read_bytes()
    settle_bytes = Path("data/settlement_file.csv").read_bytes()
    files = {
        "orders": ("orders.csv", io.BytesIO(orders_bytes), "text/csv"),
        "settlements": ("settlements.csv", io.BytesIO(settle_bytes), "text/csv"),
    }
    r = client.post("/reconcile", files=files)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["counts"]["total_records"] == 63


def test_reconcile_post_returns_422_on_replay_miss(client, tmp_path):
    """
    Upload an order whose id has no cached LLM response and force
    escalation. The endpoint should return 422 with the missing key.
    """
    orders_csv = tmp_path / "orders.csv"
    orders_csv.write_text(
        "order_id,gross_amount,expected_fee,net_expected,status,created_at\n"
        "ORD-NOCACHE,1000.00,20.00,980.00,captured,2026-08-19T20:00:00Z\n",
        encoding="utf-8",
    )
    # Payout that won't reconcile with the order -> escalates -> classifier
    # will look for evidence/llm_raw/ORD-NOCACHE.json and fail.
    payouts_csv = tmp_path / "payouts.csv"
    payouts_csv.write_text(
        "payout_id,order_ref,settled_amount,fee_charged,settled_at,batch_id\n"
        "PYT-NOCACHE,ORD-NOCACHE,850.00,20.00,2026-08-19T22:00:00Z,BATCH-2026-08-20\n",
        encoding="utf-8",
    )
    with orders_csv.open("rb") as of, payouts_csv.open("rb") as pf:
        r = client.post("/reconcile", files={
            "orders": ("orders.csv", of, "text/csv"),
            "settlements": ("payouts.csv", pf, "text/csv"),
        })
    assert r.status_code == 422
    assert "ORD-NOCACHE" in r.json()["detail"]


# --------------------------------------------------------------------------
# Audit store endpoints
# --------------------------------------------------------------------------

def test_runs_endpoints_answer_503_without_a_store(client):
    r = client.get("/runs")
    assert r.status_code == 503
    assert "MONGO_URL" in r.json()["detail"]


def test_reconcile_without_a_store_has_no_run_id(client):
    r = client.get("/reconcile/sample")
    assert r.status_code == 200
    assert "run_id" not in r.json()


def test_reconcile_saves_run_and_runs_endpoints_query_it(stored_client):
    r = stored_client.get("/reconcile/sample")
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]

    runs = stored_client.get("/runs").json()["runs"]
    assert [x["run_id"] for x in runs] == [run_id]

    run = stored_client.get(f"/runs/{run_id}").json()
    assert run["source"] == "sample"
    assert run["counts"]["total_records"] == 63
    assert sum(run["category_counts"].values()) == 63

    exc = stored_client.get(f"/runs/{run_id}/decisions",
                            params={"exceptions_only": "true"}).json()
    assert exc["count"] == 12
    assert all(d["final_category"] != "MATCHED" for d in exc["decisions"])

    one = stored_client.get(f"/runs/{run_id}/decisions/ORD-0006").json()
    assert one["key"] == "ORD-0006"
    assert one["guard_checks"]


def test_runs_endpoints_404_on_unknown_ids(stored_client):
    assert stored_client.get("/runs/nope").status_code == 404
    assert stored_client.get("/runs/nope/decisions").status_code == 404
    run_id = stored_client.get("/reconcile/sample").json()["run_id"]
    assert stored_client.get(f"/runs/{run_id}/decisions/ORD-9999").status_code == 404


def test_rerunning_the_same_input_reports_earlier_runs(stored_client):
    first = stored_client.get("/reconcile/sample").json()
    second = stored_client.get("/reconcile/sample").json()
    assert first["previous_runs_with_same_input"] == 0
    assert second["previous_runs_with_same_input"] == 1
    assert first["input_fingerprint"] == second["input_fingerprint"]
    assert first["run_id"] != second["run_id"]

    same = stored_client.get("/runs", params={"input_fingerprint": first["input_fingerprint"]}).json()
    assert {r["run_id"] for r in same["runs"]} == {first["run_id"], second["run_id"]}
