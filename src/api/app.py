"""
FastAPI surface for the reconciliation agent.

Design choice — live matcher and guard, replay-only classifier.

The deterministic matcher and the validation guard execute in real time
on any batch. The LLM layer never calls a model from the API: it replays
committed recordings from evidence/llm_raw/, and only when the recording
was captured on exactly this record's data (the recorded prompt must
match byte for byte). An escalation with no such recording is reported
as NEEDS_REVIEW (parked for a human) instead of failing the request or
reusing an answer recorded for different numbers. So the public service
cannot run up a bill, cannot go rogue on demo day, and never auto-clears
money on a guess.

Audit store (optional). With DATABASE_URL (PostgreSQL) or MONGO_URL
(MongoDB) set, every run is saved and the response carries its run_id;
the /runs endpoints then query past runs. With neither set the pipeline
endpoints work the same and the /runs endpoints answer 503.

Public-demo protections: uploads are capped in size and rows, CSVs are
validated with a readable 400 error, and the pipeline endpoints are rate
limited per client with a token bucket.

Endpoints:
  GET  /                                  capability description
  GET  /health                            liveness (cheap; used to wake the server)
  GET  /reconcile/sample                  run the pipeline on the reference batch
  POST /reconcile/generate?seed=N         run it on a freshly generated batch
  POST /reconcile                         run it on uploaded CSVs
  GET  /sample/{orders|settlements}.csv   download the reference batch
  GET  /guard/cases                       hand-crafted lies and controls, judged live
  GET  /runs                              past runs, newest first
  GET  /runs/{run_id}                     one run's counts + category breakdown
  GET  /runs/{run_id}/decisions           a run's decisions (filter by category / exceptions)
  GET  /runs/{run_id}/decisions/{key}     full audit trail for one record
"""
from __future__ import annotations

import csv
import io
import os
import tempfile
import threading
import time
from decimal import InvalidOperation
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from src.classifier.classify import DEFAULT_EVIDENCE_DIR, replay_matching_recordings
from src.generator.generate import generate
from src.guard.cases import run_all_cases
from src.guard.verify import verify_batch
from src.matcher.match import load_orders, load_payouts, match_batch
from src.models import Order, Payout
from src.reporter.report import build_report
from src.store import store_from_env
from src.store.base import RunStore
from src.store.mongo_store import batch_fingerprint

SAMPLE_DIR = Path(os.environ.get("SAMPLE_DIR", "data"))  # /tmp on read-only hosts
SAMPLE_SEED = 42  # the seed the committed LLM recordings were captured on

MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", 2 * 1024 * 1024))
MAX_ROWS = int(os.environ.get("MAX_ROWS", 5000))
ORDER_COLUMNS = ("order_id", "gross_amount", "expected_fee", "net_expected", "status", "created_at")
PAYOUT_COLUMNS = ("payout_id", "order_ref", "settled_amount", "fee_charged", "settled_at", "batch_id")

# The generator keeps per-run module state, so two requests must not run it at once.
_generate_lock = threading.Lock()


# --------------------------------------------------------------------------
# Audit store
# --------------------------------------------------------------------------

_store: Optional[RunStore] = None
_store_loaded = False


def get_store() -> Optional[RunStore]:
    """The audit store, connected once on first use (None if no database is
    configured). Tests replace this dependency with an in-memory store."""
    global _store, _store_loaded
    if not _store_loaded:
        _store = store_from_env()
        _store_loaded = True
    return _store


def require_store(store: Optional[RunStore] = Depends(get_store)) -> RunStore:
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="Audit store not configured: set DATABASE_URL or MONGO_URL to enable /runs.")
    return store


# --------------------------------------------------------------------------
# Rate limiting (token bucket per client)
# --------------------------------------------------------------------------

class TokenBucketLimiter:
    """`capacity` requests at once, refilled at `per_minute` per minute.

    In-process state: right for one instance (the demo runs one). Several
    instances would need the buckets in Redis, as in the author's
    high-concurrency-rate-limiter project.
    """

    def __init__(self, capacity: int, per_minute: float, max_clients: int = 10_000) -> None:
        self.capacity = capacity
        self.rate = per_minute / 60.0
        self.max_clients = max_clients
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, client: str, now: Optional[float] = None) -> tuple[bool, float]:
        """(allowed, seconds until the next token if not)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            tokens, last = self._buckets.get(client, (float(self.capacity), now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens >= 1:
                self._buckets[client] = (tokens - 1, now)
                self._evict_if_full(now)
                return True, 0.0
            self._buckets[client] = (tokens, now)
            return False, (1 - tokens) / self.rate

    def _evict_if_full(self, now: float) -> None:
        if len(self._buckets) <= self.max_clients:
            return
        # A bucket idle long enough to be full again carries no state.
        refill_time = self.capacity / self.rate
        self._buckets = {c: v for c, v in self._buckets.items() if now - v[1] < refill_time}


limiter = TokenBucketLimiter(
    capacity=int(os.environ.get("RATE_LIMIT_BURST", 20)),
    per_minute=float(os.environ.get("RATE_LIMIT_PER_MINUTE", 20)),
)
TRUST_PROXY_HEADERS = os.environ.get("TRUST_PROXY_HEADERS") == "1"


def client_id(request: Request) -> str:
    if TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def rate_limit(request: Request) -> None:
    allowed, retry_after = limiter.allow(client_id(request))
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Too many runs. Try again in {retry_after:.0f} s.",
            headers={"Retry-After": str(max(1, round(retry_after)))},
        )


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------

app = FastAPI(
    title="Settlement Reconciliation Agent",
    description=(
        "Matches merchant orders against aggregator settlements. Matcher and "
        "guard run live; the LLM classifier replays recordings captured on "
        "exactly the same data, and parks anything else for human review."
    ),
    version="0.6.0",
)

_origins = [o.strip() for o in os.environ.get(
    "ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/")
def root(store: Optional[RunStore] = Depends(get_store)):
    return {
        "name": "settlement-reconciliation-agent",
        "classifier_mode": ("replay (recordings used only when captured on identical data; "
                            "no live LLM calls; anything else is NEEDS_REVIEW)"),
        "audit_store": store.backend if store is not None else "disabled (set DATABASE_URL or MONGO_URL)",
        "limits": {"max_upload_bytes": MAX_UPLOAD_BYTES, "max_rows": MAX_ROWS},
        "endpoints": {
            "GET /": "this description",
            "GET /health": "liveness",
            "GET /reconcile/sample": "run pipeline on the reference batch",
            "POST /reconcile/generate": "run pipeline on a generated batch (?seed=)",
            "POST /reconcile": "run pipeline on uploaded CSVs (multipart: orders, settlements)",
            "GET /sample/{orders|settlements}.csv": "download the reference batch",
            "GET /guard/cases": "hand-crafted LLM lies and honest controls, judged live by the guard",
            "GET /runs": "past runs, newest first (?input_fingerprint= for re-runs of one input)",
            "GET /runs/{run_id}": "one run's counts and category breakdown",
            "GET /runs/{run_id}/decisions": "a run's decisions (?category=, ?exceptions_only=true)",
            "GET /runs/{run_id}/decisions/{key}": "full audit trail for one record",
        },
    }


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def _ensure_sample() -> None:
    with _generate_lock:
        if not (SAMPLE_DIR / "orders_ledger.csv").exists() or \
                not (SAMPLE_DIR / "settlement_file.csv").exists():
            generate(seed=SAMPLE_SEED, out_dir=SAMPLE_DIR)


@app.get("/reconcile/sample", dependencies=[Depends(rate_limit)])
def reconcile_sample(store: Optional[RunStore] = Depends(get_store)) -> dict:
    """Run the pipeline on the reference batch — no upload required."""
    _ensure_sample()
    orders = load_orders(SAMPLE_DIR / "orders_ledger.csv")
    payouts = load_payouts(SAMPLE_DIR / "settlement_file.csv")
    return _pipeline(orders, payouts, store=store, source="sample")


@app.post("/reconcile/generate", dependencies=[Depends(rate_limit)])
def reconcile_generate(
    seed: int = Query(..., ge=0, le=2**31 - 1, description="generator seed; 42 is the reference batch"),
    store: Optional[RunStore] = Depends(get_store),
) -> dict:
    """Generate a fresh 63-record batch from `seed` and reconcile it.

    Seed 42 reproduces the reference batch, whose escalations all have
    recordings. Any other seed produces new amounts, so its escalations
    come back NEEDS_REVIEW: the matcher and guard still run for real."""
    with tempfile.TemporaryDirectory() as td:
        with _generate_lock:
            generate(seed=seed, out_dir=Path(td))
        orders = load_orders(Path(td) / "orders_ledger.csv")
        payouts = load_payouts(Path(td) / "settlement_file.csv")
    report = _pipeline(orders, payouts, store=store, source=f"generated:seed={seed}")
    report["seed"] = seed
    return report


@app.post("/reconcile", dependencies=[Depends(rate_limit)])
async def reconcile(
    orders: UploadFile = File(..., description="orders_ledger CSV"),
    settlements: UploadFile = File(..., description="settlement_file CSV"),
    store: Optional[RunStore] = Depends(get_store),
) -> dict:
    """Run the pipeline on uploaded CSVs (same columns as /sample/*.csv)."""
    orders_list, payouts_list = await _read_uploads(orders, settlements)
    return _pipeline(orders_list, payouts_list, store=store, source="upload")


@app.get("/sample/{name}.csv")
def sample_csv(name: str) -> FileResponse:
    files = {"orders": "orders_ledger.csv", "settlements": "settlement_file.csv"}
    if name not in files:
        raise HTTPException(status_code=404, detail="Use orders.csv or settlements.csv")
    _ensure_sample()
    return FileResponse(SAMPLE_DIR / files[name], media_type="text/csv",
                        filename=files[name])


@app.get("/guard/cases")
def guard_cases() -> dict:
    cases = run_all_cases()
    adversarial = [c for c in cases if c["kind"] == "adversarial"]
    positive = [c for c in cases if c["kind"] == "positive"]
    return {
        "summary": {
            "adversarial_total": len(adversarial),
            "adversarial_rejected": sum(c["guard_verdict"] == "REJECT" for c in adversarial),
            "positive_total": len(positive),
            "positive_accepted": sum(c["guard_verdict"] == "ACCEPT" for c in positive),
        },
        "cases": cases,
    }


@app.get("/runs")
def list_runs(limit: int = Query(20, ge=1, le=200),
              input_fingerprint: Optional[str] = Query(
                  None, description="only runs of this exact input"),
              store: RunStore = Depends(require_store)) -> dict:
    return {"runs": store.list_runs(limit=limit, input_fingerprint=input_fingerprint)}


@app.get("/runs/{run_id}")
def get_run(run_id: str, store: RunStore = Depends(require_store)) -> dict:
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
    store: RunStore = Depends(require_store),
) -> dict:
    if store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id}")
    decisions = store.find_decisions(run_id, category=category,
                                     exceptions_only=exceptions_only,
                                     limit=limit, skip=skip)
    return {"run_id": run_id, "count": len(decisions), "decisions": decisions}


@app.get("/runs/{run_id}/decisions/{key}")
def get_decision(run_id: str, key: str, store: RunStore = Depends(require_store)) -> dict:
    decision = store.get_decision(run_id, key)
    if decision is None:
        raise HTTPException(status_code=404, detail=f"No decision {key} in run {run_id}")
    return decision


# --------------------------------------------------------------------------
# Upload handling
# --------------------------------------------------------------------------

async def _read_capped(upload: UploadFile, label: str) -> bytes:
    data = await upload.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"{label} file is larger than {MAX_UPLOAD_BYTES // 1024} KB.")
    if not data.strip():
        raise HTTPException(status_code=400, detail=f"{label} file is empty.")
    return data


def _check_csv(data: bytes, label: str, columns: tuple[str, ...]) -> None:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail=f"{label} file is not UTF-8 text.")
    reader = csv.reader(io.StringIO(text))
    header = next(reader, [])
    missing = [c for c in columns if c not in header]
    if missing:
        raise HTTPException(status_code=400,
                            detail=f"{label} CSV is missing column(s): {', '.join(missing)}. "
                                   f"Expected: {', '.join(columns)}.")
    rows = sum(1 for row in reader if any(cell.strip() for cell in row))
    if rows > MAX_ROWS:
        raise HTTPException(status_code=413,
                            detail=f"{label} CSV has {rows} rows; the limit is {MAX_ROWS}.")


async def _read_uploads(orders_upload: UploadFile,
                        settlements_upload: UploadFile) -> tuple[list[Order], list[Payout]]:
    orders_bytes = await _read_capped(orders_upload, "Orders")
    settlements_bytes = await _read_capped(settlements_upload, "Settlements")
    _check_csv(orders_bytes, "Orders", ORDER_COLUMNS)
    _check_csv(settlements_bytes, "Settlements", PAYOUT_COLUMNS)
    with tempfile.TemporaryDirectory() as td:
        opath = Path(td) / "orders.csv"
        spath = Path(td) / "settlements.csv"
        # utf-8-sig on read strips a BOM that spreadsheet exports often add.
        opath.write_text(orders_bytes.decode("utf-8-sig"), encoding="utf-8")
        spath.write_text(settlements_bytes.decode("utf-8-sig"), encoding="utf-8")
        try:
            return load_orders(opath), load_payouts(spath)
        except (InvalidOperation, ValueError, TypeError) as e:
            raise HTTPException(
                status_code=400,
                detail=f"Could not read a row: {e or type(e).__name__}. Amounts must be "
                       f"decimals like 1294.84 and times ISO 8601 like 2026-08-19T20:06:55Z.")


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------

def _pipeline(orders: list[Order], payouts: list[Payout], *,
              store: Optional[RunStore] = None, source: str = "api") -> dict:
    decisions = match_batch(orders, payouts)
    records, unclassified = replay_matching_recordings(
        decisions, orders, payouts, evidence_dir=DEFAULT_EVIDENCE_DIR)
    verdicts = verify_batch(records, decisions, orders, payouts)
    report = build_report(decisions, records, verdicts, unclassified=unclassified,
                          orders=orders, payouts=payouts)
    report["source"] = source
    if store is not None:
        fingerprint = batch_fingerprint(orders, payouts)
        # Re-running an input is allowed (every attempt is audited), but the
        # caller is told how many earlier runs saw exactly the same data.
        report["previous_runs_with_same_input"] = store.count_runs_with_input(fingerprint)
        report["input_fingerprint"] = fingerprint
        report["run_id"] = store.save_run(report, source=source, input_fingerprint=fingerprint)
    return report
