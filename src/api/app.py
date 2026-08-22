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

Endpoints:
  GET  /                     health + capability description
  GET  /reconcile/sample     run the pipeline on the committed data/ batch
  POST /reconcile            run the pipeline on uploaded CSVs
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile

from src.classifier.classify import DEFAULT_EVIDENCE_DIR, classify_escalations
from src.guard.verify import verify_batch
from src.matcher.match import load_orders, load_payouts, match_batch
from src.models import Order, Payout
from src.reporter.report import build_report


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
def root():
    return {
        "name": "settlement-reconciliation-agent",
        "classifier_mode": "replay (committed recordings only, no live LLM calls)",
        "endpoints": {
            "GET /": "this description",
            "GET /reconcile/sample": "run pipeline on the committed data/ batch",
            "POST /reconcile": "run pipeline on uploaded CSVs (multipart: orders, settlements)",
        },
    }


@app.get("/reconcile/sample")
def reconcile_sample() -> dict:
    """Run the pipeline on the committed data/ batch — no upload required."""
    orders = load_orders(Path("data/orders_ledger.csv"))
    payouts = load_payouts(Path("data/settlement_file.csv"))
    return _pipeline(orders, payouts)


@app.post("/reconcile")
async def reconcile(
    orders: UploadFile = File(..., description="orders_ledger CSV"),
    settlements: UploadFile = File(..., description="settlement_file CSV"),
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
        return _pipeline(orders_list, payouts_list)
    except FileNotFoundError as e:
        raise HTTPException(status_code=422, detail=f"Classifier replay miss: {e}")


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


def _pipeline(orders: list[Order], payouts: list[Payout]) -> dict:
    decisions = match_batch(orders, payouts)
    records = classify_escalations(
        decisions, orders, payouts,
        mode="replay", evidence_dir=DEFAULT_EVIDENCE_DIR,
    )
    verdicts = verify_batch(records, decisions, orders, payouts)
    return build_report(decisions, records, verdicts)
