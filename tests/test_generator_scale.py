"""
The generator's --scale option (used by scripts/scale_benchmark.py).
"""
from __future__ import annotations

import json
from pathlib import Path

from src.generator.generate import SCENARIO_MIX, generate


def _labels(out_dir: Path) -> list[dict]:
    return json.loads((out_dir / "ground_truth.json").read_text(encoding="utf-8"))["labels"]


def test_scale_multiplies_every_scenario(tmp_path):
    summary = generate(seed=3, out_dir=tmp_path, scale=4)
    assert summary["ground_truth_entries"] == 4 * sum(SCENARIO_MIX.values())
    assert summary["scenario_mix"] == {k: 4 * v for k, v in SCENARIO_MIX.items()}


def test_scaled_ground_truth_keys_are_unique(tmp_path):
    # Orphan refs get their own sequence when scaled, so they can't collide
    # with real order ids or with each other.
    generate(seed=3, out_dir=tmp_path, scale=200)      # 12,600 units, 200 orphans
    keys = [lab["key"] for lab in _labels(tmp_path)]
    assert len(keys) == len(set(keys))


def test_scale_one_is_the_reference_batch(tmp_path):
    generate(seed=42, out_dir=tmp_path, scale=1)
    reference = tmp_path / "ref"
    generate(seed=42, out_dir=reference)
    for name in ("orders_ledger.csv", "settlement_file.csv"):
        assert (tmp_path / name).read_bytes() == (reference / name).read_bytes()
