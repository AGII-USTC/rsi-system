"""Independently check the reserved-seed, four-arm RSI payload from its rows."""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path


SOURCE = Path("/evidence/run-28ff6d55de0b3a7355bdcb6b/output/matched_adjudication_final.json")
METHODS = {"fixed_nominal", "diag_gain", "robust_cross_lag", "fast_continuous"}
CONDITIONS = {"invariant", "persistent", "transient"}
SEEDS = {401, 409, 419, 421}
STEPS = 96


def close(actual, expected):
    return math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-12)


def windows(condition):
    common = {"prefix": range(0, 32), "late": range(72, STEPS)}
    if condition == "invariant":
        return {**common, "change": None, "post_change": None, "shock": None, "post_shock": None}
    if condition == "persistent":
        return {**common, "change": range(32, STEPS), "post_change": range(32, STEPS),
                "shock": None, "post_shock": None}
    return {**common, "change": range(32, 60), "post_change": range(60, STEPS),
            "shock": range(44, 48), "post_shock": range(48, 60)}


def main():
    raw = SOURCE.read_bytes()
    data = json.loads(raw)
    assert set(data["methods"]) == METHODS
    assert set(data["conditions"]) == CONDITIONS
    assert set(data["seeds"]) == SEEDS
    assert set(data["final_seeds_not_run_locally"]) == SEEDS
    assert len(data["traces"]) == len(METHODS) * len(CONDITIONS) * len(SEEDS) * STEPS
    traces = {}
    targets = {}
    for row in data["traces"]:
        condition, seed, method, tick = row["condition"], row["seed"], row["method"], row["t"]
        assert condition in CONDITIONS and seed in SEEDS and method in METHODS and type(tick) is int
        assert 0 <= tick < STEPS
        key = (condition, seed, method, tick)
        assert key not in traces, f"duplicate trace {key}"
        traces[key] = row
        target = tuple(row["target"])
        shared = (condition, seed, tick)
        assert targets.setdefault(shared, target) == target, f"different target {shared}"
        squared = sum((a - b) ** 2 for a, b in zip(row["target"], row["prediction"]))
        assert close(squared, row["squared_error"]), f"bad squared error {key}"
        assert close(math.sqrt(squared), row["error_norm"]), f"bad error norm {key}"
    assert len(targets) == len(CONDITIONS) * len(SEEDS) * STEPS
    metrics = data["metrics"]
    assert set(metrics) == CONDITIONS
    aggregate = defaultdict(lambda: defaultdict(list))
    per_seed = {}
    for condition in CONDITIONS:
        assert {int(seed) for seed in metrics[condition]} == SEEDS
        for seed in SEEDS:
            assert set(metrics[condition][str(seed)]) == METHODS
            for method in METHODS:
                rows = [traces[(condition, seed, method, t)] for t in range(STEPS)]
                stored = metrics[condition][str(seed)][method]
                all_mse = sum(row["squared_error"] for row in rows) / STEPS
                assert close(math.sqrt(all_mse), stored["rmse"])
                for name, times in windows(condition).items():
                    actual = None if times is None else sum(rows[t]["squared_error"] for t in times) / len(times)
                    reported = stored["windows_mse"][name]
                    assert (actual is None and reported is None) or (
                        actual is not None and close(actual, reported)), (condition, seed, method, name)
                    if actual is not None:
                        aggregate[(condition, method)][name].append(actual)
                assert stored["post_shock_damage"] == stored["windows_mse"]["shock"]
                start = {"invariant": None, "persistent": 32, "transient": 60}[condition]
                recovery = None
                if start is not None:
                    for t in range(start, STEPS - 3 + 1):
                        if all(rows[i]["error_norm"] <= 0.05 for i in range(t, t + 3)):
                            recovery = t
                            break
                assert stored["recovery_consecutive_ticks"] == 3
                assert stored["recovery_eligible_start_t"] == start
                assert stored["recovery_first_t"] == recovery, (condition, seed, method, recovery)
                assert set(stored["fixed_clock_snapshots"]) == {"32", "48", "64", "80"}
                for key, snapshot in stored["fixed_clock_snapshots"].items():
                    capture = int(key)
                    sample = rows[capture + 1:capture + 9]
                    assert snapshot["capture_t"] == capture
                    assert snapshot["start_t"] == capture + 1 and snapshot["count"] == len(sample)
                    assert close(sum(row["squared_error"] for row in sample) / len(sample),
                                 snapshot["post_capture_mse"])
                assert stored["updates"] == rows[-1]["updates"] + (method != "fixed_nominal")
                assert stored["compute_flops"] >= rows[-1]["flops"]
                per_seed[(condition, seed, method)] = {
                    "rmse": stored["rmse"], "recovery_first_t": recovery,
                    "late_mse": stored["windows_mse"]["late"],
                    "shock_mse": stored["windows_mse"]["shock"],
                    "post_shock_mse": stored["windows_mse"]["post_shock"],
                }
    summary = {}
    for condition in sorted(CONDITIONS):
        summary[condition] = {}
        for method in sorted(METHODS):
            entries = [per_seed[(condition, seed, method)] for seed in sorted(SEEDS)]
            summary[condition][method] = {
                "mean_seed_rmse": sum(entry["rmse"] for entry in entries) / len(entries),
                "mean_seed_late_mse": sum(entry["late_mse"] for entry in entries) / len(entries),
                "recovery_first_t_by_seed": {str(seed): per_seed[(condition, seed, method)]["recovery_first_t"]
                                             for seed in sorted(SEEDS)},
                "mean_windows_mse": {name: sum(values) / len(values) for name, values in
                                     aggregate[(condition, method)].items()},
            }
    result = {"source_sha256": hashlib.sha256(raw).hexdigest(), "trace_count": len(traces),
              "shared_target_cells": len(targets), "checks": "passed", "summary": summary}
    Path("/work/reaggregation.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"source_sha256": result["source_sha256"], "trace_count": len(traces),
                      "shared_target_cells": len(targets), "checks": "passed"}))


if __name__ == "__main__":
    main()
