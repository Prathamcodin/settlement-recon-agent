"""
FastAPI surface for the reconciliation agent.

Design choice — replay-only classifier, live matcher and guard.

The deterministic matcher and the validation guard execute in real time
on any uploaded batch. The LLM classifier layer stays reproducible by
replaying from committed evidence/llm_raw/ recordings — the service
cannot call an external model, cannot go rogue on demo day, and cannot
generate new bills. If the uploaded batch contains escalations for
which no cached response exists, the endpoint returns HTTP 422 with
the missing key, which is the honest failure mode for a
replay-constrained service.

There is intentionally no ?mode=record on this endpoint. Recording
lives on the CLI (run.py --record-live) where a human is present to
approve API calls and where cache invalidation is a deliberate act.

Audit store (optional). When MONGO_URL is set, every reconciliation run
is saved to MongoDB (see src/store/mongo_store.py) and the response
carries its run_id; the /runs endpoints then query past runs. With no
MONGO_URL the pipeline endpoints behave exactly as before and the /runs
endpoints answer 503.

Endpoints:
  GET  /                                  health + capability description
  GET  /reconcile/sample                  run the pipeline on the committed data/ batch
  POST /reconcile                         run the pipeline on uploaded CSVs
  GET  /runs                              past runs, newest first
  GET  /runs/{run_id}                     one run's counts + category breakdown
  GET  /runs/{run_id}/decisions           a run's decisions (filter by category / exceptions)
  GET  /runs/{run_id}/decisions/{key}     full audit trail for one record
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile

from src.classifier.classify import DEFAULT_EVIDENCE_DIR, classify_escalations
from src.generator.generate import generate
from src.guard.verify import verify_batch
from src.matcher.match import load_orders, load_payouts, match_batch
from src.models import Order, Payout
from src.reporter.report import build_report
from src.store.mongo_store import AuditStore, batch_fingerprint, store_from_env

SAMPLE_DIR = Path("data")
SAMPLE_SEED = 42  # the seed the committed LLM recordings were captured on

_store: Optional[AuditStore] = None
_store_loaded = False


def get_store() -> Optional[AuditStore]:
    """The audit store, connected once on first use (None if MONGO_URL is unset).
    Tests replace this dependency with an in-memory store."""
    global _store, _store_loaded
    if not _store_loaded:
        _store = store_from_env()
        _store_loaded = True
    return _store


def require_store(store: Optional[AuditStore] = Depends(get_store)) -> AuditStore:
    if store is None:
        raise HTTPException(status_code=503,
                            detail="Audit store not configured: set MONGO_URL to enable /runs.")
    return store


app = FastAPI(
    title="Settlement Reconciliation Agent",
    description=(
        "Matches merchant orders against aggregator settlements. "
        "Matcher and guard run live; LLM classifier replays from "
        "committed recordings for reproducibility."
    ),
    version="0.5.0",
)


@app.get("/")
def root(store: Optional[AuditStore] = Depends(get_store)):
    return {
        "name": "settlement-reconciliation-agent",
        "classifier_mode": "replay (committed recordings only, no live LLM calls)",
        "audit_store": "mongodb" if store is not None else "disabled (set MONGO_URL)",
        "endpoints": {
            "GET /": "this description",
            "GET /reconcile/sample": "run pipeline on the committed data/ batch",
            "POST /reconcile": "run pipeline on uploaded CSVs (multipart: orders, settlements)",
            "GET /runs": "past runs, newest first (?input_fingerprint= for re-runs of one input)",
            "GET /runs/{run_id}": "one run's counts and category breakdown",
            "GET /runs/{run_id}/decisions": "a run's decisions (?category=, ?exceptions_only=true)",
            "GET /runs/{run_id}/decisions/{key}": "full audit trail for one record",
        },
    }


@app.get("/reconcile/sample")
def reconcile_sample(store: Optional[AuditStore] = Depends(get_store)) -> dict:
    """Run the pipeline on the committed data/ batch — no upload required."""
    orders_path = SAMPLE_DIR / "orders_ledger.csv"
    payouts_path = SAMPLE_DIR / "settlement_file.csv"
    if not orders_path.exists() or not payouts_path.exists():
        generate(seed=SAMPLE_SEED, out_dir=SAMPLE_DIR)
    orders = load_orders(orders_path)
    payouts = load_payouts(payouts_path)
    return _pipeline(orders, payouts, store=store, source="sample")


@app.post("/reconcile")
async def reconcile(
    orders: UploadFile = File(..., description="orders_ledger CSV"),
    settlements: UploadFile = File(..., description="settlement_file CSV"),
    store: Optional[AuditStore] = Depends(get_store),
) -> dict:
    """
    Run the pipeline on uploaded CSVs.

    Classifier is replay-only: if the uploaded batch escalates a record
    whose key has no cached LLM response, the endpoint returns 422 with
    the missing key. To add coverage, run `python run.py --record-live`
    against the same batch on the CLI, commit the new recordings, and
    retry.
    """
    orders_list, payouts_list = await _read_uploads(orders, settlements)
    try:
        return _pipeline(orders_list, payouts_list, store=store, source="upload")
    except FileNotFoundError as e:
        raise HTTPException(status_code=422, detail=f"Classifier replay miss: {e}")


@app.get("/runs")
def list_runs(limit: int = Query(20, ge=1, le=200),
              input_fingerprint: Optional[str] = Query(
                  None, description="only runs of this exact input"),
              store: AuditStore = Depends(require_store)) -> dict:
    return {"runs": store.list_runs(limit=limit, input_fingerprint=input_fingerprint)}


@app.get("/runs/{run_id}")
def get_run(run_id: str, store: AuditStore = Depends(require_store)) -> dict:
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}")
    run["category_counts"] = store.category_counts(run_id)
    return run


@app.get("/runs/{run_id}/decisions")
def list_decisions(
    run_id: str,
    category: Optional[str] = Query(None, description="e.g. GENUINE_MISMATCH"),
    exceptions_only: bool = Query(False, description="everything except MATCHED"),
    limit: int = Query(100, ge=1, le=1000),
    skip: int = Query(0, ge=0),
    store: AuditStore = Depends(require_store),
) -> dict:
    if store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}")
    decisions = store.find_decisions(run_id, category=category,
                                     exceptions_only=exceptions_only,
                                     limit=limit, skip=skip)
    return {"run_id": run_id, "count": len(decisions), "decisions": decisions}


@app.get("/runs/{run_id}/decisions/{key}")
def get_decision(run_id: str, key: str, store: AuditStore = Depends(require_store)) -> dict:
    decision = store.get_decision(run_id, key)
    if decision is None:
        raise HTTPException(status_code=404, detail=f"No decision {key} in run {run_id}")
    return decision


async def _read_uploads(orders_upload: UploadFile,
                        settlements_upload: UploadFile) -> tuple[list[Order], list[Payout]]:
    orders_bytes = await orders_upload.read()
    settlements_bytes = await settlements_upload.read()
    with tempfile.TemporaryDirectory() as td:
        odir = Path(td)
        opath = odir / "orders.csv"
        spath = odir / "settlements.csv"
        opath.write_bytes(orders_bytes)
        spath.write_bytes(settlements_bytes)
        return load_orders(opath), load_payouts(spath)


def _pipeline(orders: list[Order], payouts: list[Payout], *,
              store: Optional[AuditStore] = None, source: str = "api") -> dict:
    decisions = match_batch(orders, payouts)
    records = classify_escalations(
        decisions, orders, payouts,
        mode="replay", evidence_dir=DEFAULT_EVIDENCE_DIR,
    )
    verdicts = verify_batch(records, decisions, orders, payouts)
    report = build_report(decisions, records, verdicts)
    if store is not None:
        fingerprint = batch_fingerprint(orders, payouts)
        # Re-running an input is allowed (every attempt is audited), but the
        # caller is told how many earlier runs saw exactly the same data.
        report["previous_runs_with_same_input"] = store.count_runs_with_input(fingerprint)
        report["input_fingerprint"] = fingerprint
        report["run_id"] = store.save_run(report, source=source, input_fingerprint=fingerprint)
    return report
