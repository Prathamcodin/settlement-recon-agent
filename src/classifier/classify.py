"""
LLM exception classifier — layer 2 of the reconciliation pipeline.

Consumes the deterministic matcher's escalations, sends each to an
LLM backend (see src.classifier.backend), and returns the raw + parsed
classification. Bounded and UNTRUSTED: no validation happens here.
Layer 3 (the guard, added in step 4) is responsible for rejecting
arithmetically-unsupported claims.

The concrete LLM is behind the LLMBackend Protocol — the classifier
never imports google-generativeai, anthropic, openai, or anything else
provider-specific. Swap the backend to swap the model.

Two operating modes:

  replay (default)
    Read a recorded response from evidence/llm_raw/<key>.json. Fail
    loudly if a response is missing. This is the mode used in tests,
    CI, and the demo — nobody burns API calls during a walk-through.

  record
    Call the live backend, write the verbatim response to
    evidence/llm_raw/<key>.json, and return the parsed classification.
    Run this once (whenever the batch or the prompt changes) to
    (re)build the evidence artifact.

Strict directory separation:

  evidence/llm_raw/    ← REAL model output, captured live, verbatim.
                          Owned by this module in record mode.
  tests/fixtures/      ← HAND-CRAFTED LLM-shaped JSON, used by the
                          step-4 guard tests. NEVER cross-populated
                          with evidence/llm_raw/.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.classifier.backend import LLMBackend
from src.classifier.prompt import SYSTEM, build_user_prompt
from src.models import MatchDecision, Order, Payout


MAX_TOKENS = 4000    # Gemini 3.x uses thinking tokens against this budget; keep it roomy.
DEFAULT_EVIDENCE_DIR = Path("evidence/llm_raw")


# --------------------------------------------------------------------------
# Escalation context: everything the LLM needs to classify one record
# --------------------------------------------------------------------------

@dataclass
class EscalationContext:
    decision: MatchDecision
    order: Optional[Order]                       # None for orphan payouts
    in_window_payouts: list[Payout] = field(default_factory=list)
    out_of_window_payouts: list[Payout] = field(default_factory=list)


def build_contexts(
    decisions: list[MatchDecision],
    orders: list[Order],
    payouts: list[Payout],
) -> list[EscalationContext]:
    orders_by_id = {o.order_id: o for o in orders}
    payouts_by_id = {p.payout_id: p for p in payouts}
    contexts: list[EscalationContext] = []
    for d in decisions:
        if d.status != "UNMATCHED_ESCALATE":
            continue
        contexts.append(EscalationContext(
            decision=d,
            order=orders_by_id.get(d.key),
            in_window_payouts=[payouts_by_id[pid] for pid in d.matched_payout_ids if pid in payouts_by_id],
            out_of_window_payouts=[payouts_by_id[pid] for pid in d.out_of_window_payout_ids if pid in payouts_by_id],
        ))
    return contexts


# --------------------------------------------------------------------------
# Response type
# --------------------------------------------------------------------------

@dataclass
class ClassifiedRecord:
    key: str
    matcher_strategy: str
    llm_category: str
    llm_reconciles: bool
    llm_reasoning: str
    llm_claim: dict
    llm_raw_response: str
    mode: str                                    # "replay" or "record"


# --------------------------------------------------------------------------
# JSON parser (tolerant of stray fences some models add)
# --------------------------------------------------------------------------

def parse_response(raw_text: str) -> dict:
    """
    Parse the model's response into a dict. We asked for strict JSON,
    and Gemini's response_mime_type=application/json usually delivers,
    but strip code fences defensively — any deeper failure should
    propagate: an unparseable response IS a data point about model
    reliability.
    """
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]                        # drop opening fence
        while lines and lines[-1].strip().startswith("```"):
            lines.pop()
        text = "\n".join(lines).strip()
    return json.loads(text)


# --------------------------------------------------------------------------
# Per-record classification (record or replay)
# --------------------------------------------------------------------------

def _record_path(evidence_dir: Path, key: str) -> Path:
    safe = key.replace("<", "_").replace(">", "_").replace(":", "_").replace("/", "_")
    return evidence_dir / f"{safe}.json"


def classify_one(
    ctx: EscalationContext,
    *,
    mode: str = "replay",
    backend: Optional[LLMBackend] = None,
    evidence_dir: Path = DEFAULT_EVIDENCE_DIR,
) -> ClassifiedRecord:
    if mode not in ("replay", "record"):
        raise ValueError(f"mode must be 'replay' or 'record', got {mode!r}")

    key = ctx.decision.key
    disk_path = _record_path(evidence_dir, key)

    if mode == "replay":
        if not disk_path.exists():
            raise FileNotFoundError(
                f"No recorded response for '{key}' at {disk_path}.\n"
                f"Re-run in --mode record (requires GEMINI_API_KEY) to capture it."
            )
        record = json.loads(disk_path.read_text(encoding="utf-8"))
        raw_text = record["response_text"]

    else:  # mode == "record"
        if backend is None:
            raise ValueError("record mode requires an LLM backend instance")
        user_prompt = build_user_prompt(
            order=ctx.order,
            in_pays=ctx.in_window_payouts,
            out_pays=ctx.out_of_window_payouts,
            strategy=ctx.decision.strategy,
            matcher_reason=ctx.decision.reason,
        )
        raw_text = backend.classify(user=user_prompt, max_tokens=MAX_TOKENS)
        disk_path.parent.mkdir(parents=True, exist_ok=True)
        disk_path.write_text(json.dumps({
            "key": key,
            "matcher_strategy": ctx.decision.strategy,
            "model": backend.name,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "prompt_system": backend.system_prompt,
            "prompt_user": user_prompt,
            "response_text": raw_text,
        }, indent=2, ensure_ascii=False), encoding="utf-8")

    parsed = parse_response(raw_text)
    return ClassifiedRecord(
        key=key,
        matcher_strategy=ctx.decision.strategy,
        llm_category=parsed.get("category", "UNKNOWN"),
        llm_reconciles=bool(parsed.get("reconciles", False)),
        llm_reasoning=parsed.get("reasoning", ""),
        llm_claim=parsed.get("claim", {}) or {},
        llm_raw_response=raw_text,
        mode=mode,
    )


def classify_escalations(
    decisions: list[MatchDecision],
    orders: list[Order],
    payouts: list[Payout],
    *,
    mode: str = "replay",
    backend: Optional[LLMBackend] = None,
    evidence_dir: Path = DEFAULT_EVIDENCE_DIR,
) -> list[ClassifiedRecord]:
    contexts = build_contexts(decisions, orders, payouts)
    return [
        classify_one(ctx, mode=mode, backend=backend, evidence_dir=evidence_dir)
        for ctx in contexts
    ]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _summarize(records: list[ClassifiedRecord]) -> str:
    from collections import Counter
    total = len(records)
    reconciled = sum(1 for r in records if r.llm_reconciles)
    by_cat = Counter(r.llm_category for r in records)
    lines = [
        "LLM classifier summary (no validation guard applied yet)",
        f"  total escalations   : {total}",
        f"  LLM says reconciles : {reconciled}",
        f"  LLM says exception  : {total - reconciled}",
        "  category breakdown:",
    ]
    for cat, count in sorted(by_cat.items(), key=lambda x: (-x[1], x[0])):
        lines.append(f"    {cat:32s} {count}")
    return "\n".join(lines)


def _build_backend() -> LLMBackend:
    """Wire up the default backend (Gemini 2.5 Flash) from environment."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("GEMINI_API_KEY not set (add to .env or export it)")
    from src.classifier.backend import GeminiBackend
    return GeminiBackend(api_key=api_key, system_prompt=SYSTEM)


def main() -> None:
    parser = argparse.ArgumentParser(description="Classify reconciliation exceptions via LLM.")
    parser.add_argument("--orders", type=Path, default=Path("data/orders_ledger.csv"))
    parser.add_argument("--payouts", type=Path, default=Path("data/settlement_file.csv"))
    parser.add_argument("--mode", choices=["replay", "record"], default="replay",
                        help="replay = read recorded responses; record = call live LLM and save.")
    parser.add_argument("--evidence-dir", type=Path, default=DEFAULT_EVIDENCE_DIR)
    args = parser.parse_args()

    from src.matcher import load_orders, load_payouts, match_batch

    orders = load_orders(args.orders)
    payouts = load_payouts(args.payouts)
    decisions = match_batch(orders, payouts)

    backend = None
    if args.mode == "record":
        backend = _build_backend()
        print(f"[record] backend={backend.name}, writing to {args.evidence_dir}/")

    records = classify_escalations(
        decisions, orders, payouts,
        mode=args.mode, backend=backend, evidence_dir=args.evidence_dir,
    )
    print(_summarize(records))


if __name__ == "__main__":
    main()
