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
    from src.api.app import app, get_store, rate_limit
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
    app.dependency_overrides[rate_limit] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def stored_client():
    """API backed by an in-memory MongoDB (mongomock)."""
    store = AuditStore(mongomock.MongoClient()["recon_api_test"])
    store.ensure_indexes()
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[rate_limit] = lambda: None
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


def _post(client, orders_text: str, payouts_text: str):
    return client.post("/reconcile", files={
        "orders": ("orders.csv", io.BytesIO(orders_text.encode()), "text/csv"),
        "settlements": ("payouts.csv", io.BytesIO(payouts_text.encode()), "text/csv"),
    })


ORDERS_HEADER = "order_id,gross_amount,expected_fee,net_expected,status,created_at\n"
PAYOUTS_HEADER = "payout_id,order_ref,settled_amount,fee_charged,settled_at,batch_id\n"


def test_escalation_without_a_recording_is_parked_as_needs_review(client):
    """An escalated record with no recorded LLM answer is NEEDS_REVIEW:
    the request succeeds, nothing is claimed, nothing is auto-cleared."""
    r = _post(client,
              ORDERS_HEADER + "ORD-NOCACHE,1000.00,20.00,980.00,captured,2026-08-19T20:00:00Z\n",
              PAYOUTS_HEADER + "PYT-NOCACHE,ORD-NOCACHE,850.00,20.00,2026-08-19T22:00:00Z,BATCH-2026-08-20\n")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["counts"]["needs_review"] == 1
    rec = body["records"][0]
    assert rec["final_category"] == "NEEDS_REVIEW"
    assert rec["llm_category"] is None and rec["guard_verdict"] is None
    assert "human review" in rec["guard_reason"]


def test_recording_for_different_numbers_is_not_replayed(client):
    """ORD-0006 has a recording, but for the reference batch's amounts.
    An upload with an ORD-0006 carrying other amounts must not inherit it."""
    r = _post(client,
              ORDERS_HEADER + "ORD-0006,5000.00,100.00,4900.00,captured,2026-08-20T12:00:00Z\n",
              PAYOUTS_HEADER + "PYT-0006,ORD-0006,4700.00,100.00,2026-08-20T15:00:00Z,BATCH-2026-08-20\n")
    assert r.status_code == 200, r.text
    assert r.json()["records"][0]["final_category"] == "NEEDS_REVIEW"


def test_records_carry_their_raw_rows(client):
    body = client.get("/reconcile/sample").json()
    rec = next(x for x in body["records"] if x["key"] == "ORD-0006")
    assert rec["order"]["gross_amount"] == "4823.00"
    assert rec["payouts"][0]["payout_id"] == "PYT-0006"
    assert rec["payouts"][0]["in_window"] is True


def test_generate_seed_42_reproduces_the_reference_batch(client):
    body = client.post("/reconcile/generate", params={"seed": 42}).json()
    c = body["counts"]
    assert (c["total_records"], c["matcher_matched"], c["guard_accepted"], c["needs_review"]) == (63, 51, 12, 0)
    assert body["seed"] == 42


def test_generate_other_seed_parks_escalations_for_review(client):
    body = client.post("/reconcile/generate", params={"seed": 7}).json()
    c = body["counts"]
    assert c["total_records"] > 0
    # New amounts: no recording matches, so no LLM verdict is invented.
    assert c["needs_review"] == c["matcher_escalated"]
    assert c["guard_accepted"] == c["guard_rejected"] == 0


def test_generate_rejects_negative_seed(client):
    assert client.post("/reconcile/generate", params={"seed": -1}).status_code == 422


@pytest.mark.parametrize("orders_text, fragment", [
    ("order_id,gross_amount\nORD-1,10.00\n", "missing column"),
    (ORDERS_HEADER + "ORD-1,abc,1.00,9.00,captured,2026-08-19T20:00:00Z\n", "Could not read a row"),
    (ORDERS_HEADER + "ORD-1,10.00,1.00,9.00,captured,yesterday\n", "Could not read a row"),
    ("   \n", "empty"),
])
def test_bad_csv_gets_a_readable_400(client, orders_text, fragment):
    r = _post(client, orders_text,
              PAYOUTS_HEADER + "PYT-1,ORD-1,9.00,1.00,2026-08-19T22:00:00Z,B\n")
    assert r.status_code == 400
    assert fragment in r.json()["detail"]


def test_too_many_rows_is_413(client, monkeypatch):
    import src.api.app as api
    monkeypatch.setattr(api, "MAX_ROWS", 2)
    rows = "".join(f"ORD-{i},10.00,1.00,9.00,captured,2026-08-19T20:00:00Z\n" for i in range(3))
    r = _post(client, ORDERS_HEADER + rows, PAYOUTS_HEADER)
    assert r.status_code == 413


def test_oversized_upload_is_413(client, monkeypatch):
    import src.api.app as api
    monkeypatch.setattr(api, "MAX_UPLOAD_BYTES", 100)
    r = _post(client, ORDERS_HEADER + "x" * 200, PAYOUTS_HEADER)
    assert r.status_code == 413


def test_csv_with_byte_order_mark_is_accepted(client):
    orders = Path("data/orders_ledger.csv").read_text(encoding="utf-8")
    payouts = Path("data/settlement_file.csv").read_text(encoding="utf-8")
    r = _post(client, "﻿" + orders, payouts)
    assert r.status_code == 200, r.text
    assert r.json()["counts"]["total_records"] == 63


def test_sample_csv_downloads(client):
    r = client.get("/sample/orders.csv")
    assert r.status_code == 200
    assert r.text.startswith("order_id,")
    assert client.get("/sample/settlements.csv").text.startswith("payout_id,")
    assert client.get("/sample/other.csv").status_code == 404


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_guard_cases_endpoint_rejects_every_lie(client):
    body = client.get("/guard/cases").json()
    s = body["summary"]
    assert s["adversarial_rejected"] == s["adversarial_total"] == 6
    assert s["positive_accepted"] == s["positive_total"] == 4
    assert all(c["guard_correct"] for c in body["cases"])
    lie = next(c for c in body["cases"] if c["name"] == "adversarial_selfconsistent_fee_lie")
    assert any(not ch["passed"] for ch in lie["guard_checks"])


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------

def test_token_bucket_allows_burst_then_refills():
    from src.api.app import TokenBucketLimiter
    lim = TokenBucketLimiter(capacity=2, per_minute=60)   # 1 token per second
    assert lim.allow("a", now=0)[0] and lim.allow("a", now=0)[0]
    allowed, retry = lim.allow("a", now=0)
    assert not allowed and retry == pytest.approx(1.0)
    assert lim.allow("b", now=0)[0], "clients have separate buckets"
    assert lim.allow("a", now=1.0)[0], "one token back after one second"


def test_token_bucket_evicts_idle_clients():
    from src.api.app import TokenBucketLimiter
    lim = TokenBucketLimiter(capacity=1, per_minute=60, max_clients=2)
    for i, t in enumerate([0, 0, 10]):
        lim.allow(f"c{i}", now=t)
    assert set(lim._buckets) == {"c2"}


def test_pipeline_endpoints_answer_429_when_limited():
    import src.api.app as api
    app.dependency_overrides[get_store] = lambda: None
    saved = api.limiter
    api.limiter = api.TokenBucketLimiter(capacity=1, per_minute=1)
    try:
        c = TestClient(app)
        assert c.get("/reconcile/sample").status_code == 200
        r = c.get("/reconcile/sample")
        assert r.status_code == 429
        assert int(r.headers["Retry-After"]) >= 1
        assert c.get("/health").status_code == 200, "cheap endpoints are not limited"
    finally:
        api.limiter = saved
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------
# Audit store endpoints
# --------------------------------------------------------------------------

def test_runs_endpoints_answer_503_without_a_store(client):
    r = client.get("/runs")
    assert r.status_code == 503
    assert "DATABASE_URL" in r.json()["detail"]


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
