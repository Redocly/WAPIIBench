#!/usr/bin/env python3
"""Deliverable 4 — scoring driver: TypeScript artifacts -> per-task verdicts -> aggregate rate.

It runs the harness's OWN pipeline and does not reimplement any comparison:

    evaluation.execute(code_dir, node, test_data_file)
        -> sees {index}_code.ts, dispatches to sdk_repair_arm.execute_sdk_repair
        -> compiles candidate + client, prepends wapiibench/capture_shim.js, runs under node,
           writes {index}_config.json in the same shape mock.js produces
    evaluation.compare(test_data_file, code_dir, api)  -> results.json (per-key verdicts)
    evaluation.analyze(code_dir)                       -> per-task statistics + aggregates

The per-task verdict this driver reports is `results.json[index].statistics.sample_verdict`,
computed by `evaluation._analyze_sample`. This file adds exactly two things the harness does
not provide: a confidence interval, and one clearly-labelled diagnostic (see AUTH_DIAGNOSTIC).

WHAT IS SHIMMED, AND WHY (there are three things; none of them is comparison logic):

 1. `estimate/harness_import.py` installs placeholder modules for torch / langchain* /
    sentence_transformers if they are absent, because `evaluation.py` imports the generation
    stack at module scope even though the scoring path never touches it. In this environment
    all of them turned out to be installed, so nothing was actually stubbed — the mechanism
    is there so the driver still runs on a machine without a GPU stack.

 2. `code_dir` is passed to `evaluation.execute()` as an ABSOLUTE path. Required, not
    cosmetic: `sdk_repair_arm._compile_to_js` runs `npx tsc <ts_file>` with `cwd` set to the
    client directory, so a relative `code_dir` makes tsc look for the file under the client
    dir and every task fails with TS6053 "File not found" -> EXECUTION_ERROR. Worth fixing in
    the arm (resolve the path before the cwd switch); the driver just always passes absolute.

 3. `cwd` must be the repository root, because `execute_sdk_repair` opens
    "wapiibench/capture_shim.js" by relative path.

AUTH_DIAGNOSTIC — read this before quoting a number. Every expected config in this dataset
carries `Authorization: Bearer <token>`, and `Authorization` is NOT in
`evaluation.SPECIAL_KEYS` (only `Accept` and `Content-Type` are), so a request without it
scores MISSING_KEY and the task comes out `wrong`. The axios arms had the model write that
header by hand. The typed client does not: auth is the client's `auth()` mechanism, so the
captured request has no Authorization header unless the starter sets one — and the current
starter does not. Left alone, this pins the arm's measured correctness near zero for reasons
that have nothing to do with the model. The driver therefore reports BOTH:
    sample_verdict            — the harness's number, unmodified. THE HEADLINE.
    verdict_ignoring_auth     — a diagnostic recomputed here, ignoring a missing/incorrect
                                Authorization header. NOT a harness metric.
Fixing this properly is a decision for the arm's owner (set a token in the starter, or add
Authorization to SPECIAL_KEYS), not something a scoring driver should paper over.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "estimate"))

from harness_import import load_harness                              # noqa: E402

POPULATION = 395                      # N, for the finite-population correction
Z_95 = 1.959964
IGNORED_HEADERS = {"accept", "content-type"}          # evaluation.SPECIAL_KEYS
AUTH_HEADERS = {"authorization"}


# --------------------------------------------------------------------------------------- #
# statistics
# --------------------------------------------------------------------------------------- #

def wilson_interval(successes: int, n: int, population: int | None = POPULATION,
                    z: float = Z_95) -> dict[str, float]:
    """Wilson score interval, optionally with the finite-population correction.

    Wilson rather than the plain Wald interval because n is small and p may sit near 0 or 1,
    where Wald's interval leaves [0, 1] and under-covers badly. The FPC shrinks the
    half-width by sqrt((N - n) / (N - 1)) — the same correction that justified n = 78.
    """
    if n == 0:
        return {"p": 0.0, "low": 0.0, "high": 1.0, "half_width": 0.5,
                "fpc": 1.0, "method": "wilson+fpc", "n": 0, "successes": 0}
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    fpc = 1.0
    if population and n < population:
        fpc = math.sqrt((population - n) / (population - 1))
        half *= fpc
    return {"p": p, "low": max(0.0, center - half), "high": min(1.0, center + half),
            "half_width": half, "fpc": fpc, "method": "wilson+fpc",
            "n": n, "successes": successes}


# --------------------------------------------------------------------------------------- #
# the AUTH diagnostic (clearly separated from the harness verdict)
# --------------------------------------------------------------------------------------- #

def verdict_ignoring_auth(sample: dict, evaluation) -> str:
    """Recompute a sample verdict while ignoring the Authorization header. DIAGNOSTIC ONLY.

    Mirrors `evaluation._analyze_sample`'s decision rule (endpoint correct AND every counted
    argument correct -> correct; illegal endpoint or any illegal key -> illegal; else wrong)
    but skips `headers.Authorization` in addition to the Accept/Content-Type keys the harness
    already skips. It deliberately reuses evaluation.Verdict rather than string literals.
    """
    if "ERROR" in sample:
        return "nonexecutable"
    verdicts = evaluation.Verdict
    url_ok = sample["url"]["verdict"] == verdicts.CORRECT
    method_ok = sample["method"]["verdict"] == verdicts.CORRECT
    illegal_endpoint = verdicts.NONEXISTENT_ENDPOINT in (
        sample["url"]["verdict"], sample["method"]["verdict"])

    all_correct, any_illegal = True, False
    for field_key in evaluation.FIELD_KEYS:
        for key, entry in (sample.get(field_key) or {}).items():
            low = str(key).lower()
            if field_key == "headers" and (low in IGNORED_HEADERS or low in AUTH_HEADERS):
                continue
            if entry["verdict"] == verdicts.ILLEGAL_KEY:
                any_illegal = True
            if entry["verdict"] != verdicts.CORRECT:
                all_correct = False

    if illegal_endpoint or any_illegal:
        return "illegal"
    if url_ok and method_ok and all_correct:
        return "correct"
    return "wrong"


def _auth_only_failure(sample: dict, evaluation) -> bool:
    stats = sample.get("statistics") or {}
    return (stats.get("sample_verdict") != "correct"
            and verdict_ignoring_auth(sample, evaluation) == "correct")


# --------------------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------------------- #

def score_api(evaluation, api: str, work_root: str, node: str) -> dict:
    code_dir = os.path.abspath(os.path.join(work_root, api))
    test_data_file = os.path.join("data", "synthetic", api, "test_data_final.json")

    artifacts = sorted(f for f in os.listdir(code_dir) if f.endswith("_code.ts")) \
        if os.path.isdir(code_dir) else []
    if not artifacts:
        return {"api": api, "skipped": "no *_code.ts artifacts", "tasks": {}}

    # 1. execute (absolute code_dir; cwd is the repo root — see the module docstring).
    evaluation.execute(code_dir, node, test_data_file)
    # 2. compare — the harness's own _compare_configs / _add_path_params.
    evaluation.compare(test_data_file, code_dir, api)
    # 3. analyze — the harness's own _analyze_sample, keeping the per-key comparison.
    evaluation.analyze(code_dir, keep_comparison=True)

    with open(os.path.join(code_dir, "results.json"), "r") as file:
        results = json.load(file)
    aggregate = results.pop("statistics", None)

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
        }
    return {"api": api, "code_dir": code_dir, "artifacts": len(artifacts),
            "tasks": tasks, "harness_aggregate": aggregate}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apis", nargs="*", default=None,
                        help="default: every API with artifacts under --work-root")
    parser.add_argument("--work-root", default=os.path.join(REPO_ROOT, "estimate", "work"))
    parser.add_argument("--node", default="node")
    parser.add_argument("--population", type=int, default=POPULATION)
    parser.add_argument("--out", default=os.path.join(REPO_ROOT, "estimate", "results",
                                                      "verdicts.json"))
    args = parser.parse_args()

    os.chdir(REPO_ROOT)                       # capture_shim.js is opened by relative path
    evaluation, _arm, stubbed = load_harness()
    if stubbed:
        print(f"harness_import stubbed: {stubbed}")

    apis = args.apis or sorted(
        d for d in os.listdir(args.work_root)
        if os.path.isdir(os.path.join(args.work_root, d))
        and any(f.endswith("_code.ts") for f in os.listdir(os.path.join(args.work_root, d))))

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
                  + ("  [auth-only failure]" if task["auth_only_failure"] else ""))

    n = len(rows)
    correct = sum(1 for r in rows if r["sample_verdict"] == "correct")
    correct_ignoring_auth = sum(1 for r in rows if r["verdict_ignoring_auth"] == "correct")
    endpoint_correct = sum(1 for r in rows if r["endpoint_verdict"] == "correct")
    nonexecutable = sum(1 for r in rows if r["sample_verdict"] == "nonexecutable")

    summary = {
        "n_scored": n,
        "population": args.population,
        "counts": {
            "correct": correct,
            "wrong": sum(1 for r in rows if r["sample_verdict"] == "wrong"),
            "illegal": sum(1 for r in rows if r["sample_verdict"] == "illegal"),
            "nonexecutable": nonexecutable,
        },
        "correctness_rate": wilson_interval(correct, n, args.population),
        "endpoint_rate": wilson_interval(endpoint_correct, n, args.population),
        "DIAGNOSTIC_correctness_ignoring_auth_header":
            wilson_interval(correct_ignoring_auth, n, args.population),
        "auth_only_failures": sum(1 for r in rows if r["auth_only_failure"]),
    }

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as file:
        json.dump({"summary": summary, "tasks": rows,
                   "per_api": {a: r.get("harness_aggregate") for a, r in per_api.items()}},
                  file, indent=2, default=str)

    rate = summary["correctness_rate"]
    print("\n=== aggregate ===")
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
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
