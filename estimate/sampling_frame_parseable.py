#!/usr/bin/env python3
"""Deliverable 1 (REDRAWN) — stratified sampling frame over the PARSEABLE synthetic APIs only.

This is a second frame, added ALONGSIDE `estimate/sampling_frame.py` rather than replacing
it, so the original 78-task draw over all 395 tasks stays reproducible and auditable.

WHY THE POPULATION CHANGED
--------------------------
Scoring a task requires `evaluation.compare()`, which calls
`openapi_utils.parse_spec(openapi/real_world_specs/{api}.yaml)` (evaluation.py:551) and then
`find_path_in_spec` / `find_operation_in_path` against the fully-resolved
`openapi_parser.Specification`. If the spec does not parse, the task cannot be scored AT ALL
— the arm's own generation path is unaffected (it reads the spec with `yaml.safe_load`), so
the failure is invisible until scoring time and then costs the whole stratum.

Measured with the harness's own parser (see `estimate/parse_support.json`):

    slack               parses
    google_calendar_v3  parses
    google_sheet_v4     parses
    asana               NEVER COMPLETES (no result inside the run budget)
    github_v3           ParserError
    npm_registry        ParserError

`asana` is 167 of the 395 synthetic tasks and 33 of the original 78-task sample. Keeping the
old frame and simply discarding the unscoreable draws would leave ~45 scored tasks, whose
95% half-width at p = 0.5 is ~+/-15pp with the FPC — the estimate would no longer answer the
question it was sized to answer. Redrawing over the parseable APIs restores the +/-10pp
target at the cost of a NARROWER population: this frame estimates the arm's correctness rate
on the three parseable synthetic APIs, not on all 395 synthetic tasks, and certainly not on
WAPIIBench as a whole. Report it that way.

SAMPLE SIZE ARITHMETIC (recomputed at run time, printed, and recorded in the meta file)
---------------------------------------------------------------------------------------
    Target: +/- 10 percentage points, 95% confidence, on a single proportion (the arm's
    per-task correctness rate). Worst case p = 0.5 (maximum variance).

    Population, parseable strata only:
        slack              174
        google_calendar_v3  37
        google_sheet_v4     17
                           ---
        N                  228

    Infinite-population Wald n:
        n0 = z^2 * p * (1 - p) / e^2
           = 1.959964^2 * 0.5 * 0.5 / 0.10^2
           = 3.84145880 * 0.25 / 0.01
           = 0.96036472 / 0.01
           = 96.036472

    Finite-population correction against N = 228 (a closed population sampled without
    replacement, so the FPC is legitimate; it is doing much more work here than at N = 395,
    which is the only reason a 228-task population still fits in 68 draws):
        n  = n0 / (1 + (n0 - 1) / N)
           = 96.036472 / (1 + 95.036472 / 228)
           = 96.036472 / (1 + 0.41682663)
           = 96.036472 / 1.41682663
           = 67.782797
        ceil -> n = 68

    Proportional allocation with largest-remainder rounding (see `allocate()`):
        slack              174/228 * 68 = 51.8947 -> 51 + 1 = 52   (largest remainder)
        google_calendar_v3  37/228 * 68 = 11.0351 -> 11
        google_sheet_v4     17/228 * 68 =  5.0702 ->  5
                                                     --
                                                     68

    WHAT THE +/-10pp BUYS: +/-10pp on the OVERALL rate across these three APIs, and nothing
    else. google_sheet_v4 contributes 5 tasks and google_calendar_v3 eleven; NO PER-API
    CLAIM IS SUPPORTABLE from this sample. A single-API rate from 5 draws has a 95%
    half-width near +/-40pp. Do not table per-API numbers from this frame.

CONVENTIONS (identical to sampling_frame.py, deliberately)
----------------------------------------------------------
* Fixed seed, recorded in both output files.
* Proportional allocation, largest-remainder rounding, deterministic tie-break.
* Output holds TASK IDENTIFIERS ONLY — `{api, index}`. No task text, no expected config.
* Reproducible: same seed + same dataset file -> the identical 68 ids on any machine.

`sampling_frame.allocate()` and `sampling_frame.sample_size()` are imported rather than
re-implemented, so the two frames cannot drift on the arithmetic that is shared.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sampling_frame import Z_95, MARGIN, P_WORST, allocate, sample_size   # noqa: E402

# --------------------------------------------------------------------------------------- #
# Fixed parameters — changing any of these changes the draw. Recorded in the meta file.
# --------------------------------------------------------------------------------------- #
RANDOM_SEED = 20260902          # same seed convention as sampling_frame.py

# The strata this frame samples. An API is IN if and only if the harness's own
# `openapi_utils.parse_spec` returns a Specification for its file, because a task whose spec
# does not parse cannot be scored by `evaluation.compare()`. Checked by
# estimate/check_parse_support.py, whose output is estimate/parse_support.json.
PARSEABLE_APIS = ("slack", "google_calendar_v3", "google_sheet_v4")
EXCLUDED_APIS = {
    "asana": "openapi_utils.parse_spec never completes within the run budget "
             "(167 synthetic tasks, therefore unscoreable)",
}

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATASET = os.path.join(REPO_ROOT, "data", "synthetic", "all", "test_data_final.json")
OUT_SAMPLE = os.path.join(REPO_ROOT, "estimate", "sample_parseable.json")
OUT_META = os.path.join(REPO_ROOT, "estimate", "sample_meta_parseable.json")


def draw(dataset_file: str = DATASET, seed: int = RANDOM_SEED,
         apis: tuple[str, ...] = PARSEABLE_APIS) -> dict[str, object]:
    import random

    with open(dataset_file, "r") as file:
        data = json.load(file)

    # `index` is the task's position in data/synthetic/{api}/test_data_final.json — the index
    # evaluation.compare() looks a task up by (`test_data[int(index)]`), and therefore the
    # only identifier worth carrying.
    all_strata: dict[str, list[int]] = collections.defaultdict(list)
    for task in data:
        all_strata[task["api"]].append(task["index"])
    for api, indices in all_strata.items():
        assert sorted(indices) == list(range(len(indices))), f"{api} indices are not 0..n-1"

    missing = [api for api in apis if api not in all_strata]
    assert not missing, f"requested strata absent from the dataset: {missing}"

    strata = {api: all_strata[api] for api in apis}
    sizes = {api: len(indices) for api, indices in strata.items()}
    population = sum(sizes.values())

    size_info = sample_size(population)
    n = size_info["n"]
    allocation = allocate(sizes, n)

    # One Random instance, strata visited in a fixed (sorted) order: deterministic.
    rng = random.Random(seed)
    drawn: list[dict[str, object]] = []
    for api in sorted(sizes):
        picks = sorted(rng.sample(sorted(strata[api]), allocation[api]))
        drawn.extend({"api": api, "index": idx} for idx in picks)

    exact = {api: sizes[api] / population * n for api in sizes}
    return {
        "seed": seed,
        "frame": "parseable-apis-only",
        "sample_size_arithmetic": size_info,
        "population_by_api": sizes,
        "population_total": population,
        "excluded_apis": {
            api: {"tasks": len(all_strata.get(api, [])), "reason": reason}
            for api, reason in EXCLUDED_APIS.items()},
        "dataset_population_total": len(data),
        "allocation_exact": {api: round(value, 6) for api, value in exact.items()},
        "allocation_by_api": allocation,
        "allocation_method": "proportional, largest-remainder rounding",
        "n": n,
        "supports_per_api_claims": False,
        "per_api_claim_note":
            "The strata are sized for an OVERALL rate across the three parseable APIs only. "
            "google_sheet_v4 contributes 5 draws and google_calendar_v3 eleven; a per-API "
            "proportion from those has a 95% half-width far wider than the +/-10pp this "
            "frame was sized for. No per-API claim is supportable.",
        "sample": drawn,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", default=DATASET)
    parser.add_argument("--seed", type=int, default=RANDOM_SEED)
    parser.add_argument("--apis", nargs="*", default=list(PARSEABLE_APIS))
    parser.add_argument("--out-sample", default=OUT_SAMPLE)
    parser.add_argument("--out-meta", default=OUT_META)
    args = parser.parse_args()

    result = draw(args.dataset, args.seed, tuple(args.apis))

    # sample_parseable.json: identifiers ONLY. Deliberately no task text and no config.
    with open(args.out_sample, "w") as file:
        json.dump({"seed": result["seed"], "frame": result["frame"], "n": result["n"],
                   "population": result["population_total"],
                   "dataset": os.path.relpath(args.dataset, REPO_ROOT),
                   "tasks": result["sample"]}, file, indent=2)
    with open(args.out_meta, "w") as file:
        json.dump({k: v for k, v in result.items() if k != "sample"}, file, indent=2)

    info = result["sample_size_arithmetic"]
    print(f"strata (parseable only): {result['population_by_api']}")
    excluded = ", ".join(f"{api}: {detail['tasks']} tasks"
                         for api, detail in result["excluded_apis"].items())
    print(f"excluded: {excluded}")
    print(f"N = {info['N']}  (was {result['dataset_population_total']} over all synthetic APIs)")
    print(f"n0 = z^2*p(1-p)/e^2 = {info['z']}^2 * {info['p_worst_case']}*"
          f"{1 - info['p_worst_case']} / {info['margin']}^2 = {info['n0_infinite_population']:.6f}")
    print(f"n  = n0 / (1 + (n0-1)/N) = {info['n0_infinite_population']:.6f} / "
          f"(1 + {info['n0_infinite_population'] - 1:.6f}/{info['N']}) = "
          f"{info['n_finite_population_corrected']:.6f}  -> ceil = {info['n']}")
    print(f"exact allocation: {result['allocation_exact']}")
    print(f"allocation: {result['allocation_by_api']} "
          f"(sum {sum(result['allocation_by_api'].values())})")
    print("per-API claims: NOT SUPPORTABLE (see sample_meta_parseable.json)")
    print(f"wrote {args.out_sample} and {args.out_meta}")


if __name__ == "__main__":
    main()
