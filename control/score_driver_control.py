#!/usr/bin/env python3
"""CONTROL condition, scoring driver: hand-written `fetch` answers -> verdicts -> aggregate.

The control analogue of `estimate/score_driver.py`, and deliberately a thin one: everything
that turns a captured request into a number is IMPORTED from the treatment's driver rather
than rewritten here, so the two arms cannot drift in how they count.

    from score_driver import wilson_interval, verdict_ignoring_auth, _auth_only_failure

Exactly one stage differs between the arms, and it is the stage that has to:

    treatment   evaluation.execute(code_dir, node, test_data)   -> sdk_repair_arm (TS + client)
    control     execute_fetch.execute_control(code_dir, node, test_data)

`evaluation.execute()` cannot run a control answer at all: its `.js` branch looks for
`axios.<verb>(` and would score every plain-`fetch` answer `ABSENT_REQUEST` without running
it (see control/execute_fetch.py's docstring). Everything after execution is the harness's
own code, called with the same arguments in both arms:

    evaluation.compare(test_data_file, code_dir, api)   -> results.json
    evaluation.analyze(code_dir, keep_comparison=True)  -> per-task statistics

and the headline per-task verdict is `results.json[index]["statistics"]["sample_verdict"]`,
computed by `evaluation._analyze_sample`. Nothing in this file reimplements a comparison.

POPULATION — the default here is **228**, not the harness-wide 395.
The control is drawn over the same frame as the treatment's redrawn sample: the N = 228
synthetic APIs whose specs parse (`estimate/README.md`, "Why the population changed";
`control/task_manifest_control.json` records `frame = "parseable-apis-only"`, n = 68).
`estimate/score_driver.py` still defaults to 395 for backwards compatibility with the
numbers already recorded under it, so the treatment MUST be scored with
`--population 228` for the two arms' confidence intervals to be comparable. That asymmetry
in defaults is the reason this file states the number rather than inheriting it.

AUTH DIAGNOSTIC — the treatment's `verdict_ignoring_auth` is reported here too, unchanged
and still clearly labelled a diagnostic, so the same secondary number exists on both sides.
It is expected to be far less load-bearing in the control: a plain-`fetch` answer writes its
own headers, so the control CAN send `Authorization` (the starter's line does), whereas the
typed client's `auth()` mechanism produced captured requests without one. A large gap
between `correct` and the diagnostic in the CONTROL column would mean the control's answers
are dropping the starter's auth header, which is a finding about the answers, not plumbing.

Usage:

    python control/score_driver_control.py --population 228
    python control/score_driver_control.py --apis slack --population 228
"""

from __future__ import annotations

import argparse
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "estimate"))
sys.path.insert(0, os.path.join(REPO_ROOT, "control"))

import execute_fetch                                                    # noqa: E402
from harness_import import load_harness                                 # noqa: E402
from score_driver import (wilson_interval, verdict_ignoring_auth,       # noqa: E402
                          _auth_only_failure)

# N for the finite-population correction: the parseable-API frame both arms are drawn over.
POPULATION_PARSEABLE = 228


def score_api(evaluation, api: str, work_root: str, node: str) -> dict:
    """Execute + score one API's control answers. Mirrors score_driver.score_api, except
    that execution goes through execute_fetch (see the module docstring)."""
    code_dir = os.path.abspath(os.path.join(work_root, api))
    test_data_file = os.path.join("data", "synthetic", api, "test_data_final.json")

    artifacts = sorted(f for f in os.listdir(code_dir)
                       if f.endswith("_code.js")) if os.path.isdir(code_dir) else []
    if not artifacts:
        return {"api": api, "skipped": "no *_code.js artifacts", "tasks": {}}

    # 1. execute — the control's own executor; absolute code_dir, cwd is the repo root.
    outcomes = execute_fetch.execute_control(code_dir, node, test_data_file)
    # 2. compare / 3. analyze — the harness's own, unmodified.
    evaluation.compare(test_data_file, code_dir, api)
    evaluation.analyze(code_dir, keep_comparison=True)

    with open(os.path.join(code_dir, "results.json"), "r") as file:
        results = json.load(file)
    aggregate = results.pop("statistics", None)

    captured = {str(o["index"]): o["ok"] for o in outcomes}
    tasks = {}
    for index, sample in results.items():
        stats = sample.get("statistics") or {}
        tasks[index] = {
            "sample_verdict": stats.get("sample_verdict"),
            "error_verdict": stats.get("error_verdict"),
            "url_verdict": stats.get("url_verdict"),
            "method_verdict": stats.get("method_verdict"),
            "endpoint_verdict": stats.get("endpoint_verdict"),
            "verdict_ignoring_auth": verdict_ignoring_auth(sample, evaluation),
            "auth_only_failure": _auth_only_failure(sample, evaluation),
            # Control-only: difference B in execute_fetch.py — an answer that ran cleanly but
            # issued no request is counted (as EXECUTION_ERROR), not dropped from the
            # denominator. This flag says which tasks that was.
            "request_captured": captured.get(str(index)),
        }
    return {"api": api, "code_dir": code_dir, "artifacts": len(artifacts),
            "tasks": tasks, "harness_aggregate": aggregate,
            "no_request_captured": [o["index"] for o in outcomes if not o["ok"]]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apis", nargs="*", default=None,
                        help="default: every API with *_code.js artifacts under --work-root")
    parser.add_argument("--work-root", default=os.path.join(REPO_ROOT, "control", "work"))
    parser.add_argument("--node", default="node")
    parser.add_argument("--population", type=int, default=POPULATION_PARSEABLE,
                        help="N for the FPC; 228 = the parseable-API frame (default)")
    parser.add_argument("--out", default=os.path.join(REPO_ROOT, "control", "results",
                                                      "verdicts_control.json"))
    args = parser.parse_args()

    os.chdir(REPO_ROOT)                  # capture_shim.js is opened by a relative path
    evaluation, _arm, stubbed = load_harness()
    if stubbed:
        print(f"harness_import stubbed: {stubbed}")

    apis = args.apis or sorted(
        d for d in os.listdir(args.work_root)
        if os.path.isdir(os.path.join(args.work_root, d))
        and any(f.endswith("_code.js") for f in os.listdir(os.path.join(args.work_root, d))))
    if not apis:
        print(f"no control answers (*_code.js) under {args.work_root}; nothing to score")
        return

    per_api, rows = {}, []
    for api in apis:
        print(f"--- {api} ---", flush=True)
        result = score_api(evaluation, api, args.work_root, args.node)
        per_api[api] = result
        for index, task in result["tasks"].items():
            rows.append({"api": api, "index": int(index), **task})
            print(f"  {api}:{index:>4}  {task['sample_verdict']:<13} "
                  f"endpoint={task['endpoint_verdict']}  "
                  f"ignoring_auth={task['verdict_ignoring_auth']}"
                  + ("  [auth-only failure]" if task["auth_only_failure"] else "")
                  + ("" if task["request_captured"] else "  [no request captured]"))

    n = len(rows)
    correct = sum(1 for r in rows if r["sample_verdict"] == "correct")
    correct_ignoring_auth = sum(1 for r in rows if r["verdict_ignoring_auth"] == "correct")
    endpoint_correct = sum(1 for r in rows if r["endpoint_verdict"] == "correct")

    summary = {
        "condition": "control-raw-openapi",
        "n_scored": n,
        "population": args.population,
        "counts": {
            "correct": correct,
            "wrong": sum(1 for r in rows if r["sample_verdict"] == "wrong"),
            "illegal": sum(1 for r in rows if r["sample_verdict"] == "illegal"),
            "nonexecutable": sum(1 for r in rows if r["sample_verdict"] == "nonexecutable"),
        },
        "correctness_rate": wilson_interval(correct, n, args.population),
        "endpoint_rate": wilson_interval(endpoint_correct, n, args.population),
        "DIAGNOSTIC_correctness_ignoring_auth_header":
            wilson_interval(correct_ignoring_auth, n, args.population),
        "auth_only_failures": sum(1 for r in rows if r["auth_only_failure"]),
        "no_request_captured": sum(1 for r in rows if not r["request_captured"]),
    }

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as file:
        json.dump({"summary": summary, "tasks": rows,
                   "per_api": {a: r.get("harness_aggregate") for a, r in per_api.items()}},
                  file, indent=2, default=str)

    rate = summary["correctness_rate"]
    print("\n=== aggregate (CONTROL: raw OpenAPI text + plain fetch) ===")
    print(f"scored {n} task(s); counts {summary['counts']}")
    print(f"correctness (harness sample_verdict) = {correct}/{n} = {rate['p']:.3f}")
    print(f"  95% Wilson CI with FPC(N={args.population}): "
          f"[{rate['low']:.3f}, {rate['high']:.3f}]  (half-width {rate['half_width']:.3f}, "
          f"fpc {rate['fpc']:.4f})")
    diag = summary["DIAGNOSTIC_correctness_ignoring_auth_header"]
    print(f"DIAGNOSTIC ignoring Authorization = {diag['successes']}/{n} = {diag['p']:.3f} "
          f"[{diag['low']:.3f}, {diag['high']:.3f}]   "
          f"({summary['auth_only_failures']} task(s) failed on Authorization alone)")
    print(f"endpoint correct = {endpoint_correct}/{n}")
    print(f"answers issuing no request = {summary['no_request_captured']}")
    print("compare against the treatment run with: "
          "python estimate/score_driver.py --population "
          f"{args.population}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
