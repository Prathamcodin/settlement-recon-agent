"""
Held-out adversarial suite test.

Generates a batch with seed=1337 (distinct from the main seed=42) and runs
the ACTUAL validation guard (src/guard/verify.py, unchanged) on every
mechanically-generated smart lie plus every legitimate positive control.

If the guard fails on ANY adversarial case, this test fails loudly — that
is a real finding to investigate, not to work around by softening the
assertion. Similarly, a legit case being wrongly rejected is a real
finding about the guard being over-strict.

The test uses generous minimum-volume assertions (>=100 adversarial,
>=30 legit) so a future edit that accidentally shrinks the mix trips the
guardrail here first.
"""
from __future__ import annotations

from src.heldout import run_heldout_suite


def test_heldout_generates_meaningful_volume():
    results = run_heldout_suite(seed=1337)
    assert results["seed"] == 1337
    assert results["adversarial"]["total"] >= 100, (
        f"held-out suite should produce >=100 adversarial cases; got "
        f"{results['adversarial']['total']}"
    )
    assert results["legit"]["total"] >= 30, (
        f"held-out suite should produce >=30 legit cases; got "
        f"{results['legit']['total']}"
    )


def test_heldout_guard_rejects_all_smart_lies():
    results = run_heldout_suite(seed=1337)
    adv = results["adversarial"]
    if adv["false_positives"] != 0:
        # Surface the specific failures so a finding is visible without
        # needing to inspect the JSON artifact.
        wrong = [o for o in results["outcomes"]
                 if o["kind"] == "adversarial" and o["guard_verdict"] == "ACCEPTED"]
        details = "\n".join(
            f"  {o['case_id']} ({o['scenario']}/{o['lie_kind']}): "
            f"claim={o['llm_claim']} reason='{o['guard_reason'][:150]}'"
            for o in wrong[:10]
        )
        assert False, (
            f"Guard failed to reject {adv['false_positives']}/{adv['total']} "
            f"adversarial cases. This is a real finding, not a test bug — "
            f"the guard let a mechanically-generated smart lie through:\n{details}"
        )


def test_heldout_guard_accepts_all_legit_cases():
    results = run_heldout_suite(seed=1337)
    legit = results["legit"]
    if legit["false_rejections"] != 0:
        wrong = [o for o in results["outcomes"]
                 if o["kind"] == "legit" and o["guard_verdict"] == "REJECTED"]
        details = "\n".join(
            f"  {o['case_id']} ({o['scenario']}/{o['lie_kind']}): "
            f"claim={o['llm_claim']} reason='{o['guard_reason'][:150]}'"
            for o in wrong[:10]
        )
        assert False, (
            f"Guard falsely rejected {legit['false_rejections']}/{legit['total']} "
            f"legit cases — guard is over-strict on held-out data:\n{details}"
        )


def test_heldout_suite_is_reproducible():
    """Same seed twice -> same case ids and same guard verdicts."""
    r1 = run_heldout_suite(seed=1337)
    r2 = run_heldout_suite(seed=1337)
    ids1 = [o["case_id"] for o in r1["outcomes"]]
    ids2 = [o["case_id"] for o in r2["outcomes"]]
    assert ids1 == ids2
    v1 = [o["guard_verdict"] for o in r1["outcomes"]]
    v2 = [o["guard_verdict"] for o in r2["outcomes"]]
    assert v1 == v2


def test_guard_holds_across_seeds_at_larger_scale():
    """3 seeds x 4x the suite = 3,000 cases (scripts/stress_guard.py runs 50,000)."""
    for seed in (2024, 7, 31337):
        results = run_heldout_suite(seed=seed, scale=4)
        assert results["cases_total"] == 1000
        assert results["adversarial"]["false_positives"] == 0, f"seed {seed}: a lie got through"
        assert results["legit"]["false_rejections"] == 0, f"seed {seed}: a legit case was rejected"
