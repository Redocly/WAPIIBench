#!/usr/bin/env python3
"""Deliverable 1 — stratified sampling frame for the agent-as-generator estimate.

Draws a proportionally-allocated stratified sample of WAPIIBench synthetic tasks and writes
it out as TASK IDENTIFIERS ONLY (api + index). No ground truth is ever written here.

SAMPLE SIZE ARITHMETIC (recomputed at run time, printed, and recorded in sample_meta.json):

    Target: +/- 10 percentage points, 95% confidence, on a single proportion (the arm's
    per-task correctness rate). Worst case p = 0.5 (maximum variance).

    Infinite-population Wald n:
        n0 = z^2 * p * (1 - p) / e^2
           = 1.959964^2 * 0.5 * 0.5 / 0.10^2
           = 3.841459 * 0.25 / 0.01
           = 0.960365 / 0.01
           = 96.0365            -> 97 tasks if we stopped here

    Finite-population correction against N = 395 (we are sampling from a closed 395-task
    population without replacement, so the FPC is legitimate and not optional):
        n  = n0 / (1 + (n0 - 1) / N)
           = 96.0365 / (1 + 95.0365 / 395)
           = 96.0365 / (1 + 0.2406)
           = 96.0365 / 1.240599
           = 77.4111
        ceil -> n = 78

    So n = 78. Proportional allocation with largest-remainder rounding (see allocate()):
        asana              167/395 * 78 = 32.972 -> 33
        slack              174/395 * 78 = 34.365 -> 35   (2nd largest remainder)
        google_calendar_v3  37/395 * 78 =  7.306 ->  7
        google_sheet_v4     17/395 * 78 =  3.357 ->  3
                                                  ---
                                                    78

    NOTE ON WHAT THE +/-10pp BUYS: 78 tasks gives +/-10pp on the OVERALL rate only. The
    per-API strata (3 google_sheet_v4 tasks!) are far too small for per-API claims. This
    frame is deliberately NOT a per-API estimate.

Reproducibility: RANDOM_SEED below is fixed and recorded in both output files. Within each
stratum the draw is `random.Random(RANDOM_SEED).sample(sorted(indices), k)`, so the same seed
plus the same dataset file reproduces the identical 78 ids on any machine.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import os
import random

# --------------------------------------------------------------------------------------- #
# Fixed parameters — changing any of these changes the draw. Recorded in sample_meta.json.
# --------------------------------------------------------------------------------------- #
RANDOM_SEED = 20260902
Z_95 = 1.959964            # two-sided normal quantile for 95%
MARGIN = 0.10              # +/- 10 percentage points
P_WORST = 0.5              # worst-case proportion

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATASET = os.path.join(REPO_ROOT, "data", "synthetic", "all", "test_data_final.json")
OUT_SAMPLE = os.path.join(REPO_ROOT, "estimate", "sample.json")
OUT_META = os.path.join(REPO_ROOT, "estimate", "sample_meta.json")


def sample_size(population: int, z: float = Z_95, margin: float = MARGIN,
                p: float = P_WORST) -> dict[str, float | int]:
    """Wald sample size for a proportion, with the finite-population correction."""
    n0 = (z ** 2) * p * (1 - p) / (margin ** 2)
    n_fpc = n0 / (1 + (n0 - 1) / population)
    return {
        "z": z, "margin": margin, "p_worst_case": p, "N": population,
        "n0_infinite_population": n0,
        "n0_ceil": math.ceil(n0),
        "n_finite_population_corrected": n_fpc,
        "n": math.ceil(n_fpc),
    }


def allocate(strata_sizes: dict[str, int], n: int) -> dict[str, int]:
    """Proportional allocation with largest-remainder rounding.

    Largest-remainder (not naive round()) so the allocation sums to exactly n. Ties broken by
    descending stratum size then stratum name, so the result is deterministic.
    """
    total = sum(strata_sizes.values())
    exact = {api: size / total * n for api, size in strata_sizes.items()}
    floors = {api: math.floor(v) for api, v in exact.items()}
    remaining = n - sum(floors.values())
    order = sorted(strata_sizes,
                   key=lambda api: (-(exact[api] - floors[api]), -strata_sizes[api], api))
    for api in order[:remaining]:
        floors[api] += 1
    assert sum(floors.values()) == n, (floors, n)
    for api, k in floors.items():
        assert k <= strata_sizes[api], f"stratum {api} too small for {k}"
    return floors


def draw(dataset_file: str = DATASET, seed: int = RANDOM_SEED) -> dict[str, object]:
    with open(dataset_file, "r") as file:
        data = json.load(file)

    # `index` in the combined file is the task's position in data/synthetic/{api}/
    # test_data_final.json — that is the index evaluation.compare() uses to look a task up
    # (`test_data[int(index)]`), and therefore the only identifier worth carrying around.
    strata: dict[str, list[int]] = collections.defaultdict(list)
    for position, task in enumerate(data):
        strata[task["api"]].append(task["index"])
    for api, indices in strata.items():
        assert sorted(indices) == list(range(len(indices))), f"{api} indices are not 0..n-1"

    sizes = {api: len(indices) for api, indices in strata.items()}
    size_info = sample_size(len(data))
    n = size_info["n"]
    allocation = allocate(sizes, n)

    # One Random instance, strata visited in a fixed (sorted) order: deterministic.
    rng = random.Random(seed)
    drawn: list[dict[str, object]] = []
    for api in sorted(sizes):
        picks = sorted(rng.sample(sorted(strata[api]), allocation[api]))
        drawn.extend({"api": api, "index": idx} for idx in picks)

    return {
        "seed": seed,
        "sample_size_arithmetic": size_info,
        "population_by_api": sizes,
        "allocation_by_api": allocation,
        "n": n,
        "sample": drawn,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", default=DATASET)
    parser.add_argument("--seed", type=int, default=RANDOM_SEED)
    parser.add_argument("--out-sample", default=OUT_SAMPLE)
    parser.add_argument("--out-meta", default=OUT_META)
    args = parser.parse_args()

    result = draw(args.dataset, args.seed)

    # sample.json: identifiers ONLY. Deliberately no task text and no config.
    with open(args.out_sample, "w") as file:
        json.dump({"seed": result["seed"], "n": result["n"],
                   "dataset": os.path.relpath(args.dataset, REPO_ROOT),
                   "tasks": result["sample"]}, file, indent=2)
    with open(args.out_meta, "w") as file:
        json.dump({k: v for k, v in result.items() if k != "sample"}, file, indent=2)

    info = result["sample_size_arithmetic"]
    print(f"N = {info['N']}")
    print(f"n0 = z^2*p(1-p)/e^2 = {info['z']}^2 * {info['p_worst_case']}*"
          f"{1 - info['p_worst_case']} / {info['margin']}^2 = {info['n0_infinite_population']:.4f}")
    print(f"n  = n0 / (1 + (n0-1)/N) = {info['n0_infinite_population']:.4f} / "
          f"(1 + {info['n0_infinite_population'] - 1:.4f}/{info['N']}) = "
          f"{info['n_finite_population_corrected']:.4f}  -> ceil = {info['n']}")
    print(f"allocation: {result['allocation_by_api']} (sum {sum(result['allocation_by_api'].values())})")
    print(f"wrote {args.out_sample} and {args.out_meta}")


if __name__ == "__main__":
    main()
