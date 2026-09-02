#!/usr/bin/env python3
"""CONTROL condition — offline verification of the non-model half. No model in the loop.

Answers the three questions a reviewer will ask about the control's plumbing before they look
at any score:

  1. DOES THE TREATMENT'S CAPTURE SHIM SEE A HAND-WRITTEN `fetch` CALL?
     Run the identical answer twice: once with `wapiibench/capture_shim.js` alone (the
     treatment's wiring, which publishes `globalThis.__wapiiCaptureFetch` and patches nothing),
     once with `control/capture_global_fetch.js` appended. The first MUST capture nothing and
     the second MUST write the config — that is the evidence for the one-line adaptation, and
     it is re-measured here rather than asserted.

  2. DOES THE SPEC-DRIVEN VALUE COERCION APPLY IN THIS CONDITION?
     Two hand-written CORRECT fetch calls, one carrying a query INTEGER and one carrying a
     query BOOLEAN — the two cases that used to make a perfectly correct fetch-shaped answer
     score `INCORRECT_VALUE`, because the URL throws the type away and `_compare_configs`
     compares with `==`. Both must come out `correct`.

  3. DOES COERCION RESTORE THE TYPE WITHOUT RESTORING CORRECTNESS?
     The same two calls with a WRONG VALUE OF THE RIGHT DECLARED TYPE (an integer that is not
     the expected integer; the flipped boolean). Both must come out `wrong`. If coercion ever
     turned one of these `correct` it would be hiding real errors and the control would be
     invalid — this is the control that matters more than the two positives.

The answers here are GENERATED FROM THE EXPECTED CONFIG on purpose: this is a test of the
plumbing (capture -> coercion -> the harness's own compare/analyze), not a measurement of a
model, so the ideal answer is derived rather than guessed. That is the same choice
`wapiibench/sdk_repair_verify.py` makes for the treatment, where the ideal SDK invocation is
hand-written against the generated client. Nothing in the measurement path reads ground truth.

Scoring is the harness's own: `evaluation.compare()` then `evaluation.analyze()`, on config
files written by `wapiibench/capture_shim.js`. Only `evaluation.execute()` is replaced, by
`control/execute_fetch.py` (see its docstring for why neither existing executor can run a
plain-fetch answer).

    python control/verify_control.py            # -> control/verification.json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.parse

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "control"))
sys.path.insert(0, os.path.join(REPO_ROOT, "estimate"))
sys.path.insert(0, os.path.join(REPO_ROOT, "wapiibench"))

import build_specs                                                  # noqa: E402
import execute_fetch                                                # noqa: E402

DATASET_DIR = os.path.join(REPO_ROOT, "data", "synthetic")
SPEC_DIR = os.path.join(REPO_ROOT, "openapi", "real_world_specs")
DEFAULT_WORK = os.path.join(REPO_ROOT, "control", "verify_work")
DEFAULT_OUT = os.path.join(REPO_ROOT, "control", "verification.json")

# (api, index, which query parameter to damage in the negative control, damaged value)
CASES = [
    # In the operative 68-task sample. Expected `limit` is the integer 50.
    ("slack", 57, "limit", 42),
    # NOT in the sample (no sampled google_sheet_v4 task has a boolean query value); this is
    # the same case the treatment's own verification used for the boolean path.
    ("google_sheet_v4", 1, "includeGridData", False),
]


# --------------------------------------------------------------------------------------- #
# building the ideal (and the deliberately damaged) answer
# --------------------------------------------------------------------------------------- #

def _query_text(value: object) -> str:
    """A value as a URL query string writes it — what any hand-written fetch call produces."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    return str(value)


def fetch_answer(config: dict, damage: tuple[str, object] | None = None) -> str:
    """Render the plain-`fetch` answer a perfect generator would write for this config.

    Deliberately ordinary JavaScript: a template-free URL string with the query appended, a
    `method`, and a `headers` object. No capture hook, no helper, nothing the control's own
    tooling provides.
    """
    params = dict(config.get("params") or {})
    if damage is not None:
        key, value = damage
        assert key in params, f"{key!r} is not a query parameter of this task"
        params[key] = value

    url = config["url"]
    if params:
        query = "&".join(f"{urllib.parse.quote(str(k))}={urllib.parse.quote(_query_text(v))}"
                         for k, v in params.items())
        url = f"{url}?{query}"

    headers = {k: v for k, v in (config.get("headers") or {}).items()}
    lines = [f"const response = await fetch({json.dumps(url)}, {{",
             f"  method: {json.dumps(config['method'].upper())},",
             f"  headers: {json.dumps(headers)},"]
    if config.get("data"):
        lines.append(f"  body: JSON.stringify({json.dumps(config['data'])}),")
    lines.append("});")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------------------- #
# question 1 — does the treatment's shim see a hand-written fetch call?
# --------------------------------------------------------------------------------------- #

def shim_interception_probe(spec_dir: str, answer_js: str, node: str,
                            scratch: str) -> dict:
    """Run the same answer with and without control/capture_global_fetch.js."""
    with open(os.path.join(REPO_ROOT, "wapiibench", "capture_shim.js"), "r") as file:
        shim_template = file.read()
    with open(os.path.join(REPO_ROOT, "control", "capture_global_fetch.js"), "r") as file:
        patch = file.read()

    results = {}
    for label, prologue in (("shim_only_treatment_wiring", ""),
                            ("shim_plus_control_global_patch", patch)):
        config_path = os.path.join(scratch, f"probe_{label}.json")
        if os.path.exists(config_path):
            os.remove(config_path)
        code = (f"{shim_template % config_path}\n{prologue}\n"
                f"(async () => {{\n{answer_js}\n}})();\n")
        proc = subprocess.run([node, "-"], input=code, text=True, capture_output=True,
                              cwd=spec_dir)
        captured = os.path.isfile(config_path)
        results[label] = {
            "returncode": proc.returncode,
            "config_written": captured,
            "captured_params": (json.load(open(config_path)).get("params")
                                if captured else None),
            "stderr_head": proc.stderr[:200],
        }
    results["verdict"] = (
        "adaptation required and effective"
        if (not results["shim_only_treatment_wiring"]["config_written"]
            and results["shim_plus_control_global_patch"]["config_written"])
        else "UNEXPECTED — see the two rows")
    return results


# --------------------------------------------------------------------------------------- #
# per-case pipeline
# --------------------------------------------------------------------------------------- #

def _whitelist_for(api: str, index: int) -> dict:
    """The task's five-operation whitelist.

    Prefers the operative precomputed whitelist so the verified case uses the SAME candidate
    set the measurement uses. Falls back to the BM25 stand-in's live top five with the ground
    truth substituted in when it missed — which is what `retrieval_standin.build_whitelists()`
    does, and which is acceptable here ONLY because this file scores hand-written answers and
    is not part of the measurement.
    """
    path = os.path.join(REPO_ROOT, "estimate", "whitelists_parseable.json")
    with open(path, "r") as file:
        for entry in json.load(file)["tasks"]:
            if entry["api"] == api and entry["index"] == index:
                return {"operation_ids": entry["operation_ids"], "source": "precomputed"}

    import retrieval_standin as standin
    with open(os.path.join(DATASET_DIR, api, "test_data_final.json"), "r") as file:
        task = json.load(file)[index]
    retriever = standin.Retriever(api)
    ranked = retriever.rank(task["task"])                     # blinded: task text only
    truth = standin.resolve_ground_truth(retriever.operations, task["config"])
    top = list(ranked[:5])
    substituted = truth not in top
    if substituted:
        top = [truth] + top[:4]
    return {"operation_ids": sorted(top), "source": "live BM25",
            "ground_truth_substituted": substituted}


def run_case(evaluation, api: str, index: int, variant: str, answer_js: str,
             operation_ids: list[str], work_root: str, node: str) -> dict:
    import yaml

    code_dir = os.path.join(work_root, variant, api)
    spec_dir = os.path.join(code_dir, f"{index:04d}_spec")
    shutil.rmtree(code_dir, ignore_errors=True)
    os.makedirs(spec_dir, exist_ok=True)

    spec_file = os.path.join(SPEC_DIR, f"{api}.yaml")
    with open(spec_file, "r") as file:
        root = yaml.safe_load(file)
    document, _unpruned, kept = build_specs.filter_spec_document(root, operation_ids)
    with open(os.path.join(spec_dir, build_specs.SPEC_NAME), "w") as file:
        yaml.safe_dump(document, file, sort_keys=False, allow_unicode=True,
                       default_flow_style=False, width=100)

    import sdk_repair_arm as arm
    arm.write_param_types(spec_file, spec_dir, operation_ids=operation_ids)

    answer_path = os.path.join(code_dir, f"{index}_code.js")
    with open(answer_path, "w") as file:
        file.write(answer_js)

    test_data_file = os.path.join("data", "synthetic", api, "test_data_final.json")
    execute_fetch.execute_control(os.path.abspath(code_dir), node, test_data_file)
    evaluation.compare(test_data_file, os.path.abspath(code_dir), api)
    evaluation.analyze(os.path.abspath(code_dir), keep_comparison=True)

    with open(os.path.join(code_dir, "results.json"), "r") as file:
        results = json.load(file)
    results.pop("statistics", None)
    sample = results[str(index)]
    config_path = os.path.join(code_dir, f"{index}_config.json")
    with open(config_path, "r") as file:
        captured = json.load(file)

    return {
        "api": api, "index": index, "variant": variant,
        "operations_in_spec": kept,
        "answer": answer_js,
        "sample_verdict": (sample.get("statistics") or {}).get("sample_verdict"),
        "endpoint_verdict": (sample.get("statistics") or {}).get("endpoint_verdict"),
        "captured_params": captured.get("params"),
        "coerced": (captured.get("_wapii_coercion") or {}).get("coerced"),
        "left_as_string": (captured.get("_wapii_coercion") or {}).get("left_as_string"),
        "param_verdicts": {key: entry.get("verdict")
                           for key, entry in (sample.get("params") or {}).items()},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--work-root", default=DEFAULT_WORK)
    parser.add_argument("--node", default="node")
    parser.add_argument("--out", default=DEFAULT_OUT)
    args = parser.parse_args()

    os.chdir(REPO_ROOT)
    from harness_import import load_harness
    evaluation, _arm, stubbed = load_harness()
    if stubbed:
        print(f"harness_import stubbed: {stubbed}")

    os.makedirs(args.work_root, exist_ok=True)
    report: dict[str, object] = {"cases": [], "probe": None}

    for api, index, damaged_key, damaged_value in CASES:
        with open(os.path.join(DATASET_DIR, api, "test_data_final.json"), "r") as file:
            config = json.load(file)[index]["config"]
        whitelist = _whitelist_for(api, index)
        ids = whitelist["operation_ids"]

        correct = run_case(evaluation, api, index, "correct", fetch_answer(config), ids,
                           args.work_root, args.node)
        correct["whitelist_source"] = whitelist["source"]
        correct["expected_params"] = config.get("params")
        report["cases"].append(correct)
        print(f"{api}:{index} correct                -> {correct['sample_verdict']}  "
              f"captured {correct['captured_params']}")

        damaged = run_case(evaluation, api, index, "wrong_typed_value",
                           fetch_answer(config, damage=(damaged_key, damaged_value)),
                           ids, args.work_root, args.node)
        damaged["whitelist_source"] = whitelist["source"]
        damaged["damage"] = {damaged_key: damaged_value}
        damaged["expected_params"] = config.get("params")
        report["cases"].append(damaged)
        print(f"{api}:{index} wrong value/right type -> {damaged['sample_verdict']}  "
              f"captured {damaged['captured_params']}")

        if report["probe"] is None:
            report["probe"] = shim_interception_probe(
                os.path.join(args.work_root, "correct", api, f"{index:04d}_spec"),
                fetch_answer(config), args.node, args.work_root)
            print(f"shim interception probe: {report['probe']['verdict']}")

    positives = [c for c in report["cases"] if c["variant"] == "correct"]
    negatives = [c for c in report["cases"] if c["variant"] == "wrong_typed_value"]
    report["summary"] = {
        "correct_answers_scored_correct": sum(
            1 for c in positives if c["sample_verdict"] == "correct"),
        "correct_answers": len(positives),
        "wrong_typed_answers_scored_wrong": sum(
            1 for c in negatives if c["sample_verdict"] == "wrong"),
        "wrong_typed_answers": len(negatives),
        "shim_adaptation": (report["probe"] or {}).get("verdict"),
        "clean": (all(c["sample_verdict"] == "correct" for c in positives)
                  and all(c["sample_verdict"] == "wrong" for c in negatives)
                  and (report["probe"] or {}).get("verdict")
                  == "adaptation required and effective"),
    }
    with open(args.out, "w") as file:
        json.dump(report, file, indent=2)
    print(json.dumps(report["summary"], indent=2))
    print(f"wrote {args.out}")
    sys.exit(0 if report["summary"]["clean"] else 1)


if __name__ == "__main__":
    main()
