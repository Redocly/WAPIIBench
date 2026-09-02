#!/usr/bin/env python3
"""CONTROL condition, step 4 — automated leakage scan over the control's prompt material.

The counterpart of `estimate/blinding_check.py`. It re-runs that script's three finding
classes against the control's emitted text, and adds the two checks the control needs and
the treatment did not.

RE-RUN FROM THE TREATMENT, UNCHANGED IN MEANING (the helpers are IMPORTED from
estimate/blinding_check.py rather than reimplemented, so the two arms cannot drift in what
counts as a leak):

  LEAK (must be zero)      the emitted text contains the expected URL, the expected endpoint
                           path, the ground-truth operationId, or a STRUCTURED expected
                           parameter name verbatim.
  EXPECTED (informational) an expected VALUE appears in the task text. Not a leak -- the task
                           must state its values or it is unsolvable.
  NOTE (judgement call)    a parameter name matches only after normalisation, is a common
                           English word, or occurs only inside a `<placeholder>`.

The scanned payload is `task + starter`, the same two task-specific pieces the treatment
scans. THE FILTERED SPEC IS NOT SCANNED FOR NAME/PATH LEAKS, for the same reason the
treatment does not scan `client.ts`: both documents describe all five candidate operations,
so both necessarily contain the ground truth's path and parameter names alongside four
distractors'. Naming the five operationIds is what the condition IS. What matters is that
nothing distinguishes the ground truth from the other four, which is the next check.

ADDED CHECK 1 -- POSITION AND ORDERING (the control-specific risk).
The treatment's `generate-client` emits `OPERATIONS` in spec order, so a reviewer never had
to ask whether the typed client betrayed the retriever's ranking. `control/build_specs.py`
makes the same choice, and this measures it instead of trusting it:
  * `emitted_order_is_spec_order` -- the emitted operationId sequence equals the FULL spec's
    order restricted to the whitelist. Spec order is a property of `{api}.yaml`, identical
    for every task drawn from that API, so it cannot carry task-specific information.
  * `emitted_order_equals_retriever_order` -- must be rare and must not be systematic; if the
    document were in rank order, position 0 would be the retriever's top hit.
  * `position_of_ground_truth` -- where the right operation sits among the five, 0-based, and
    the distribution over tasks. A degenerate distribution (always first, always last) would
    let a model guess without reading. Also compared against
    `retriever_rank_of_ground_truth`: agreement at chance level is the expected result.

ADDED CHECK 2 -- SURFACE SYMMETRY.
Confirms the treatment's `client.ts` also names all five operationIds, so "the control's
document names the ground-truth operationId" is not an asymmetry but a shared property.

ADDED CHECK 3 -- AGREEMENT WITH THE TREATMENT'S OWN REPORT.
Both arms scan `task + starter`, and the task text is shared, so every field of the
treatment's report should reproduce exactly. `vs_treatment` compares all nine shared finding
fields on all 68 tasks against `estimate/blinding_report_parseable.json`. A mismatch would
localise the difference to the starter, the only differing piece of the scanned payload.

GROUND TRUTH IS READ HERE, ON THE SCORING SIDE ONLY. This module imports
`retrieval_standin.resolve_ground_truth`, exactly as the treatment's checker does, and is
never on the generation path: `emit_prompt_control.py` does not import it and no prompt file
is produced from it.

    python control/blinding_check_control.py   # -> control/blinding_report_control.json
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "control"))
sys.path.insert(0, os.path.join(REPO_ROOT, "estimate"))

import yaml                                                          # noqa: E402

import emit_prompt_control                                           # noqa: E402
import fetch_starter                                                 # noqa: E402
# The treatment's own leak definitions, imported so the two arms cannot drift.
from blinding_check import (                                         # noqa: E402
    _expected_tokens, _is_structured, _normalise, _word_in, _word_in_outside_placeholders)
from retrieval_standin import load_operations                         # noqa: E402

DATASET_DIR = os.path.join(REPO_ROOT, "data", "synthetic")
SPEC_SOURCE_DIR = os.path.join(REPO_ROOT, "openapi", "real_world_specs")
HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

DEFAULT_SAMPLE = os.path.join(REPO_ROOT, "estimate", "sample_parseable.json")
DEFAULT_WHITELISTS = os.path.join(REPO_ROOT, "estimate", "whitelists_parseable.json")
DEFAULT_WORK = os.path.join(REPO_ROOT, "control", "work")
DEFAULT_CLIENT_ROOT = os.path.join(REPO_ROOT, "estimate", "work")
DEFAULT_OUT = os.path.join(REPO_ROOT, "control", "blinding_report_control.json")
DEFAULT_TREATMENT_REPORT = os.path.join(REPO_ROOT, "estimate",
                                        "blinding_report_parseable.json")

# The finding fields that MUST come out the same in both arms. Every one of them is computed
# from `task + starter`, and the task text is shared between conditions, so a difference here
# would mean the control's starter is leaking something the treatment's does not (or the
# reverse) -- which is exactly the failure this comparison exists to catch.
SHARED_FINDING_KEYS = (
    "leak_url", "leak_path_literal", "leak_path_template", "leak_operation_id",
    "leak_param_name_structured", "note_param_name_common_word",
    "note_param_name_normalised", "expected_value_in_task",
    "note_param_name_placeholder_only")


# --------------------------------------------------------------------------------------- #
# starter-shape check (the control's analogue of check_starter_is_live)
# --------------------------------------------------------------------------------------- #

def check_starter_shape() -> tuple[bool, str]:
    """The control's starter is defined in control/fetch_starter.py, not read out of the
    harness, so there is no live source to check for drift. What CAN be checked is that it is
    the condition it claims to be: no client, no SDK, no axios, and a `fetch(` tail.

    The auth half of the starter is checked separately and more strongly, by
    control/check_auth_parity.py, against the treatment's own auth_setup_for() on all 68
    tasks.
    """
    starter = fetch_starter.STARTER
    problems = []
    if not starter.rstrip().endswith("fetch("):
        problems.append(f"starter does not end in `fetch(`: {starter!r}")
    for banned in ("./client", "axios", "import ", "require("):
        if banned in starter:
            problems.append(f"starter mentions {banned!r}")
    if "{task}" not in starter or "{auth_setup}" not in starter:
        problems.append("starter lost a placeholder")
    if problems:
        return False, "; ".join(problems)
    return True, ("defined in control/fetch_starter.py (no harness SETUPS entry is "
                  "fetch-shaped); auth line parity checked in control/auth_parity.json: "
                  + repr(starter))


# --------------------------------------------------------------------------------------- #
# operation order, as it appears in the emitted document and in the full spec
# --------------------------------------------------------------------------------------- #

def _operation_order(root: dict) -> list[str]:
    """operationIds in document order: paths in order, methods in OpenAPI's own key order."""
    order = []
    for _path, path_item in (root.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if isinstance(operation, dict) and operation.get("operationId"):
                order.append(operation["operationId"])
    return order


def _load_yaml(path: str) -> dict:
    with open(path, "r") as file:
        return yaml.safe_load(file)


# --------------------------------------------------------------------------------------- #
# per-task scan
# --------------------------------------------------------------------------------------- #

def scan_task(api: str, index: int, work_root: str, whitelist_file: str,
              operations: list[dict], spec_order: list[str], client_root: str,
              whitelist_entry: dict) -> dict:
    material = emit_prompt_control.emit(api, index, work_root, whitelist_file)
    rendered = emit_prompt_control.render(material)
    # Only the task-specific parts. The rules text and the file paths are identical for
    # every task and cannot carry ground truth. Same payload definition as the treatment's.
    payload = f"{material['task']}\n{material['starter_code']}"

    with open(os.path.join(DATASET_DIR, api, "test_data_final.json"), "r") as file:
        config = json.load(file)[index]["config"]
    expected = _expected_tokens(config, operations)
    truth_id = (expected["operation_id"] or [None])[0]

    lower, norm_words = payload.lower(), set(_normalise(payload).split())
    hit = lambda needle: bool(needle) and _word_in(needle, lower)

    verbatim = [n for n in expected["param_names"]
                if _word_in_outside_placeholders(n, lower)]
    placeholder_only = [n for n in expected["param_names"] if hit(n) and n not in verbatim]

    # --- the emitted document's operation order ---
    emitted_order = _operation_order(_load_yaml(material["spec_file"]))
    whitelist = list(whitelist_entry["operation_ids"])
    spec_order_filtered = [o for o in spec_order if o in set(whitelist)]
    position = emitted_order.index(truth_id) if truth_id in emitted_order else None

    # --- surface symmetry: does the treatment's client.ts name all five too? ---
    client_ts = os.path.join(client_root, api, f"{index:04d}_client", "client.ts")
    client_names_all_five = None
    if os.path.isfile(client_ts):
        with open(client_ts, "r") as file:
            text = file.read()
        client_names_all_five = all(op in text for op in whitelist)

    findings = {
        "api": api, "index": index,
        "leak_url": [u for u in expected["url"] if hit(u)],
        "leak_path_literal": [p for p in expected["path_literal"]
                              if len(p or "") > 1 and hit(p)],
        "leak_path_template": [p for p in expected["path_template"]
                               if len(p or "") > 1 and hit(p)],
        "leak_operation_id": [o for o in expected["operation_id"] if hit(o)],
        "leak_param_name_structured": [n for n in verbatim if _is_structured(n)],
        "note_param_name_common_word": [n for n in verbatim if not _is_structured(n)],
        "note_param_name_normalised": [
            n for n in expected["param_names"]
            if n not in verbatim and all(w in norm_words for w in _normalise(n).split())],
        "expected_value_in_task": [v for v in expected["values"]
                                   if len(v) > 1 and v.lower() in lower],
        "note_param_name_placeholder_only": placeholder_only,
        "expected_param_names": expected["param_names"],
        "expected_values": expected["values"],
        "rendered_chars": len(rendered),
        # --- control-specific ---
        "operations_in_emitted_spec": emitted_order,
        "emitted_order_is_spec_order": emitted_order == spec_order_filtered,
        "emitted_order_equals_retriever_order": emitted_order == whitelist,
        "emitted_spec_has_exactly_the_whitelist":
            sorted(emitted_order) == sorted(whitelist),
        "position_of_ground_truth": position,
        "retriever_rank_of_ground_truth":
            whitelist_entry.get("retriever_rank_of_ground_truth"),
        "position_equals_retriever_rank":
            None if position is None else
            (position + 1) == whitelist_entry.get("retriever_rank_of_ground_truth"),
        "treatment_client_names_all_five": client_names_all_five,
    }
    findings["leaks"] = sum(len(findings[k]) for k in (
        "leak_url", "leak_path_literal", "leak_path_template",
        "leak_operation_id", "leak_param_name_structured"))
    return findings


def compare_with_treatment(rows: list[dict], treatment_report: str) -> dict:
    """Are the control's findings the SAME as the treatment's, task by task?

    The report is only meaningful next to the treatment's. `estimate/blinding_check.py`
    scanned `task + starter` too, and the task text is identical in both arms, so every
    shared finding field should match on every task. Any mismatch localises the difference to
    the starter -- the one piece of the scanned payload that differs between conditions.
    """
    if not os.path.isfile(treatment_report):
        return {"available": False, "report": treatment_report}
    with open(treatment_report, "r") as file:
        treatment = {(r["api"], r["index"]): r for r in json.load(file)["tasks"]}
    differences, unmatched = [], []
    for row in rows:
        other = treatment.get((row["api"], row["index"]))
        if other is None:
            unmatched.append(f"{row['api']}:{row['index']}")
            continue
        for key in SHARED_FINDING_KEYS:
            if row[key] != other.get(key):
                differences.append({"api": row["api"], "index": row["index"], "field": key,
                                    "treatment": other.get(key), "control": row[key]})
    return {"available": True,
            "report": os.path.relpath(treatment_report, REPO_ROOT),
            "compared": len(rows) - len(unmatched),
            "tasks_not_in_treatment_report": unmatched,
            "differing_fields": len(differences),
            "differences": differences,
            "identical": not differences and not unmatched}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sample", default=DEFAULT_SAMPLE)
    parser.add_argument("--whitelists", default=DEFAULT_WHITELISTS)
    parser.add_argument("--work-root", default=DEFAULT_WORK)
    parser.add_argument("--client-root", default=DEFAULT_CLIENT_ROOT)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--treatment-report", default=DEFAULT_TREATMENT_REPORT)
    args = parser.parse_args()

    ok, detail = check_starter_shape()
    print(f"starter: {'OK' if ok else 'FAIL'} - {detail}")

    with open(args.sample, "r") as file:
        sample = json.load(file)
    with open(args.whitelists, "r") as file:
        whitelists = {(e["api"], e["index"]): e for e in json.load(file)["tasks"]}

    ops_cache: dict[str, list[dict]] = {}
    order_cache: dict[str, list[str]] = {}
    rows = []
    for task_id in sample["tasks"]:
        api, index = task_id["api"], task_id["index"]
        if api not in ops_cache:
            ops_cache[api] = [r for r in load_operations(api) if r["operation_id"]]
            order_cache[api] = _operation_order(
                _load_yaml(os.path.join(SPEC_SOURCE_DIR, f"{api}.yaml")))
        rows.append(scan_task(api, index, args.work_root, args.whitelists,
                              ops_cache[api], order_cache[api], args.client_root,
                              whitelists[(api, index)]))

    leaking = [r for r in rows if r["leaks"]]
    positions = collections.Counter(r["position_of_ground_truth"] for r in rows)

    summary = {
        "starter_shape_ok": ok,
        "n": len(rows),
        "tasks_with_hard_leaks": len(leaking),
        "tasks_with_expected_value_overlap":
            sum(1 for r in rows if r["expected_value_in_task"]),
        "tasks_with_common_word_param_name_note":
            sum(1 for r in rows if r["note_param_name_common_word"]),
        "tasks_with_normalised_param_name_note":
            sum(1 for r in rows if r["note_param_name_normalised"]),
        "leak_kinds": {
            kind: sum(1 for r in rows if r[kind])
            for kind in ("leak_url", "leak_path_literal", "leak_path_template",
                         "leak_operation_id", "leak_param_name_structured")},
        "common_word_param_names_seen": sorted({
            n for r in rows for n in r["note_param_name_common_word"]}),
        "normalised_param_names_seen": sorted({
            n for r in rows for n in r["note_param_name_normalised"]}),
        "tasks_with_placeholder_only_param_name_note": sum(
            1 for r in rows if r["note_param_name_placeholder_only"]),
        "placeholder_only_param_names_seen": sorted({
            n for r in rows for n in r["note_param_name_placeholder_only"]}),
        # --- control-specific ---
        "ordering": {
            "emitted_order_is_spec_order_on":
                sum(1 for r in rows if r["emitted_order_is_spec_order"]),
            "emitted_spec_has_exactly_the_whitelist_on":
                sum(1 for r in rows if r["emitted_spec_has_exactly_the_whitelist"]),
            "emitted_order_equals_retriever_order_on":
                sum(1 for r in rows if r["emitted_order_equals_retriever_order"]),
            "position_equals_retriever_rank_on":
                sum(1 for r in rows if r["position_equals_retriever_rank"]),
            "position_of_ground_truth_histogram":
                {str(k): v for k, v in sorted(positions.items(),
                                              key=lambda kv: (kv[0] is None, kv[0]))},
            "ground_truth_not_found_in_emitted_spec_on":
                sum(1 for r in rows if r["position_of_ground_truth"] is None),
        },
        "surface_symmetry": {
            "treatment_client_names_all_five_on":
                sum(1 for r in rows if r["treatment_client_names_all_five"]),
            "treatment_client_missing_on":
                sum(1 for r in rows if r["treatment_client_names_all_five"] is None),
        },
        "vs_treatment": compare_with_treatment(rows, args.treatment_report),
    }
    # Blocking conditions: a hard leak, a document that is not in spec order, a document
    # that is not exactly the whitelist, or a ground truth missing from its own document.
    ordering = summary["ordering"]
    comparison = summary["vs_treatment"]
    # Exit-code semantics are the treatment's, plus the control's own two checks. Note that
    # `estimate/blinding_check.py` ALSO exits 1 on this population: two tasks
    # (google_calendar_v3 #15 `iCalUID`, #32 `summaryOverride`) have a structured expected
    # parameter name spelled out in the task text itself. That is a property of the DATASET,
    # identical in both arms -- `vs_treatment.identical` is what establishes that -- and not
    # something the control introduces. The exit code is advisory; the report is the finding.
    blocking = (not ok
                or bool(leaking)
                or ordering["emitted_order_is_spec_order_on"] != len(rows)
                or ordering["emitted_spec_has_exactly_the_whitelist_on"] != len(rows)
                or ordering["ground_truth_not_found_in_emitted_spec_on"] != 0
                or (comparison["available"] and not comparison["identical"]))

    with open(args.out, "w") as file:
        json.dump({"summary": summary, "tasks": rows}, file, indent=2)

    print(json.dumps(summary, indent=2))
    for row in leaking:
        print(f"  LEAK {row['api']}:{row['index']} -> " + json.dumps(
            {k: v for k, v in row.items() if k.startswith('leak_') and v}))
    print(f"wrote {args.out}")
    sys.exit(1 if blocking else 0)


if __name__ == "__main__":
    main()
