#!/usr/bin/env python3
"""CONTROL condition, step 4 — does the control manifest point a generator at any answer?

The counterpart of `estimate/verify_manifest.py`, asking the same two questions of the
control's material. The per-task scan is not reimplemented: `estimate.verify_manifest`'s own
`check_task()` is CALLED, with the control's `spec_dir` substituted for the treatment's
`client_dir`, so every leak definition, boundary rule and by-design exemption is literally
the same code. Only the two things that are structurally different are written here:

A. PATH INTEGRITY.  Every `spec_file`, `spec_dir` and `prompt_file` exists, the spec parses
   as YAML and declares `paths` and `servers` (a spec that does not is not a usable API
   description, and the agent would have nothing to build a URL from -- the treatment's
   equivalent is "the client dir has a readable client.ts"), the per-task parameter-type
   table `wapii_param_types.json` is present (without it the control's coercion silently
   does nothing and the control is scored unfairly), and every `answer_path` does NOT yet
   exist, so a stale artifact cannot be scored as this run's output.

B. NO ANSWER IN REACH.  Every regular file a control generator can open -- the prompt file
   plus every file in the task's `{index:04d}_spec/` directory -- tested against that task's
   ground-truth config, which this script reads and the generator never does. The four
   checks are the treatment's, unchanged:
     1. expected values not stated in the task text (with spec-declared `enum` members split
        out as by-design; the filtered spec carries the API's enums for the same reason the
        generated client carries them as literal unions),
     2. which-of-the-five markers -- rank-order exposure, and asymmetric description of the
        ground truth versus the four distractors,
     3. the expected URL or expected config JSON appearing verbatim,
     4. a pointer to the dataset, whitelists, blinding report or results.

   The files scanned differ from the treatment's only in being the control's: the filtered
   OpenAPI document and the parameter-type table, instead of client.ts / client.zod.ts /
   _surface.txt / redocly.yaml / tsconfig.json and the same parameter-type table. That
   difference IS the condition.

The order aggregate is reported on `filtered_spec.yaml`, the file the control's prompt tells
the agent to read, where the treatment reported it on `client.ts`.

Output: control/manifest_verification_control.json. Exit status is non-zero on any finding.
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

# The treatment's own scan, called rather than copied.
import verify_manifest                                               # noqa: E402
from retrieval_standin import load_operations                         # noqa: E402

DEFAULT_MANIFEST = os.path.join(REPO_ROOT, "control", "task_manifest_control.json")
DEFAULT_WHITELISTS = os.path.join(REPO_ROOT, "estimate", "whitelists_parseable.json")
DEFAULT_OUT = os.path.join(REPO_ROOT, "control",
                           "manifest_verification_control.json")

PARAM_TYPES = "wapii_param_types.json"
SPEC_NAME = "filtered_spec.yaml"


def _as_treatment_row(row: dict) -> dict:
    """The control row in the shape `verify_manifest.check_task()` expects.

    `check_task()` reads `task_id`, `api`, `index`, `prompt_file` and `client_dir`, and walks
    every regular non-symlink file in `client_dir`. Pointing `client_dir` at the control's
    `spec_dir` makes it walk the filtered spec and the parameter-type table instead of the
    generated client -- which is exactly the set of files a control generator can open.
    """
    return {**row, "client_dir": row["spec_dir"]}


def _spec_problems(row: dict) -> list[str]:
    """A. the control's structural requirements on the per-task spec directory."""
    problems = []
    task_id = row["task_id"]
    for key in ("spec_file", "spec_dir", "prompt_file", "answer_path"):
        if not os.path.isabs(row[key]):
            problems.append(f"{task_id}: {key} is not absolute")
    if not os.path.isdir(row["spec_dir"]):
        problems.append(f"{task_id}: spec_dir missing")
        return problems
    if os.path.basename(row["spec_file"]) != SPEC_NAME:
        problems.append(f"{task_id}: spec_file is not {SPEC_NAME}")
    if not os.path.isfile(row["spec_file"]):
        problems.append(f"{task_id}: spec_file missing")
    else:
        try:
            with open(row["spec_file"], "r") as file:
                root = yaml.safe_load(file)
        except Exception as error:                                   # noqa: BLE001
            problems.append(f"{task_id}: spec_file does not parse as YAML ({error})")
            root = None
        if isinstance(root, dict):
            if not root.get("paths"):
                problems.append(f"{task_id}: spec_file declares no paths")
            if not root.get("servers"):
                problems.append(f"{task_id}: spec_file declares no servers "
                                f"(the agent has no base URL to build against)")
        elif root is not None:
            problems.append(f"{task_id}: spec_file is not a mapping")
    if not os.path.isfile(os.path.join(row["spec_dir"], PARAM_TYPES)):
        problems.append(f"{task_id}: spec_dir has no {PARAM_TYPES} "
                        f"(spec-driven coercion would silently do nothing)")
    if not os.path.isfile(row["prompt_file"]):
        problems.append(f"{task_id}: prompt_file missing")
    if os.path.exists(row["answer_path"]):
        problems.append(f"{task_id}: answer_path ALREADY EXISTS "
                        f"(a stale artifact would be scored)")
    if not os.path.isdir(os.path.dirname(row["answer_path"])):
        problems.append(f"{task_id}: answer_path's directory does not exist")
    if not row["answer_path"].endswith("_code.js"):
        problems.append(f"{task_id}: answer_path is not a *_code.js file "
                        f"(control/execute_fetch.py would not pick it up)")
    return problems


def mention_asymmetry(row: dict, whitelist: dict, truth: str | None) -> dict:
    """How often is the ground truth the MOST-mentioned of the five in its filtered spec?

    `check_task()`'s per-task `ground_truth_asymmetric` check fires whenever the ground truth
    is a unique extremum of the five mention counts. In the treatment that never happened,
    because `generate-client` writes each operation into `client.ts` once. In the control it
    can, because an OpenAPI document reuses `#/components/...` entries and those components
    are NAMED after whichever operation happened to define them first in the source spec --
    slack #27's `admin_inviteRequests_deny` `$ref`s
    `#/components/requestBodies/admin_inviteRequests_approve`, so `_approve` is mentioned
    four times and the other four ids once each.

    A per-task flag alone cannot say whether that is informative, so this is the aggregate
    that can: over all tasks where SOME id is a unique maximum, how often is it the ground
    truth? At or below the 1-in-5 chance rate, the mention count carries no signal about
    which of the five is the answer -- the component-naming asymmetry is a property of the
    source spec's authoring, not of the ground truth. Above it, the control would be
    leaking and `build_specs.py` would need to rename or inline shared components.
    """
    with open(row["spec_file"], "r") as file:
        text = file.read()
    ids = list(whitelist["operation_ids"])
    counts = {i: verify_manifest._token_count(i, text) for i in ids}
    top = max(counts.values())
    winners = [i for i, c in counts.items() if c == top]
    unique_max = winners[0] if len(winners) == 1 else None
    return {"task_id": row["task_id"], "counts": counts,
            "unique_max": unique_max,
            "unique_max_is_ground_truth": bool(unique_max) and unique_max == truth}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--whitelists", default=DEFAULT_WHITELISTS)
    parser.add_argument("--out", default=DEFAULT_OUT)
    args = parser.parse_args()

    with open(args.manifest, "r") as file:
        manifest = json.load(file)
    with open(args.whitelists, "r") as file:
        whitelists = {(e["api"], e["index"]): e for e in json.load(file)["tasks"]}

    # ---------------- A. path integrity ----------------
    path_problems = [p for row in manifest["tasks"] for p in _spec_problems(row)]
    print(f"A. path integrity: {len(manifest['tasks'])} rows, "
          f"{len(path_problems)} problem(s)")
    for problem in path_problems:
        print(f"   {problem}")

    # ---------------- B. no answer in reach ----------------
    cache: dict[str, list[dict]] = {}
    enums: dict[str, set[str]] = {}
    rows, kinds = [], collections.Counter()
    for row in manifest["tasks"]:
        api = row["api"]
        if api not in cache:
            cache[api] = [r for r in load_operations(api) if r["operation_id"]]
            enums[api] = verify_manifest.spec_enum_values(api)
        spec_order = [r["operation_id"] for r in cache[api]]
        result = verify_manifest.check_task(_as_treatment_row(row), cache[api],
                                            whitelists[(api, row["index"])],
                                            enums[api], spec_order)
        rows.append(result)
        for kind in ("value_not_in_task_text", "rank_order_exposed",
                     "ground_truth_asymmetric", "expected_url_verbatim",
                     "expected_config_verbatim", "forbidden_pointer"):
            kinds[kind] += len(result[kind])

    asymmetry = [mention_asymmetry(row, whitelists[(row["api"], row["index"])],
                                   verify_manifest.resolve_ground_truth(
                                       cache[row["api"]],
                                       json.load(open(os.path.join(
                                           REPO_ROOT, "data", "synthetic", row["api"],
                                           "test_data_final.json")))[row["index"]]["config"]))
                 for row in manifest["tasks"]]

    flagged = [r for r in rows if r["findings"]]
    files_scanned = sum(len(r["files_scanned"]) for r in rows)
    unresolved = [r["task_id"] for r in rows if not r["ground_truth_operation_resolved"]]

    # The aggregate that answers "can the agent tell which of the five it is?": how often the
    # ground truth is FIRST in the order a generator meets the five ids in the file the
    # prompt tells it to read. The treatment measured this on client.ts.
    spec_orders = [obs for result in rows for obs in result["order_observations"]
                   if obs["file"].endswith(SPEC_NAME)]
    gt_first = sum(1 for obs in spec_orders if obs["ground_truth_first"])
    unexplained = [obs for result in rows for obs in result["order_observations"]
                   if obs["explained_by"] is None]

    summary = {
        "condition": manifest.get("condition"),
        "n_tasks": len(rows),
        "files_scanned": files_scanned,
        "path_problems": path_problems,
        "tasks_with_findings": len(flagged),
        "findings_by_kind": dict(kinds),
        "tasks_with_spec_declared_enum_value": sum(
            1 for r in rows if r["value_is_spec_declared_enum"]),
        "spec_declared_enum_values_seen": sorted(
            {v for r in rows for v in r["value_is_spec_declared_enum"]}),
        "ground_truth_first_in_filtered_spec": {
            "hits": gt_first, "of": len(spec_orders),
            "rate": round(gt_first / len(spec_orders), 4) if spec_orders else None,
            "chance_baseline": 0.2},
        "file_orders_not_explained_by_sorted_or_spec_order": len(unexplained),
        "file_orders_not_explained_and_equal_to_rank_order": sum(
            1 for obs in unexplained if obs["equals_rank_order"]),
        "mention_asymmetry": {
            "note": "See mention_asymmetry(). A unique-maximum mention count arises from the "
                    "SOURCE spec naming shared #/components entries after whichever "
                    "operation defined them; the aggregate rate is what says whether it "
                    "identifies the ground truth.",
            "tasks_with_a_unique_max": sum(1 for a in asymmetry if a["unique_max"]),
            "unique_max_is_ground_truth":
                sum(1 for a in asymmetry if a["unique_max_is_ground_truth"]),
            "rate_given_a_unique_max": (
                round(sum(1 for a in asymmetry if a["unique_max_is_ground_truth"])
                      / sum(1 for a in asymmetry if a["unique_max"]), 4)
                if any(a["unique_max"] for a in asymmetry) else None),
            "chance_baseline": 0.2,
            # The decisive observation, stronger than the rate: if every unique maximum is
            # the SAME operationId across tasks, the count is a property of the spec (that
            # operation owns a shared component) and not of the answer -- it wins whether or
            # not it is the ground truth on that task.
            "unique_max_ids_seen": sorted({a["unique_max"] for a in asymmetry
                                           if a["unique_max"]}),
            "tasks": [a for a in asymmetry if a["unique_max"]],
        },
        "ground_truth_unresolved": unresolved,
        "clean": not path_problems and not flagged and not unresolved,
    }
    with open(args.out, "w") as file:
        json.dump({"summary": summary, "tasks": rows}, file, indent=2)

    print(f"B. no answer in reach: {files_scanned} file(s) scanned across {len(rows)} task(s)")
    print(json.dumps(summary["findings_by_kind"], indent=2))
    order = summary["ground_truth_first_in_filtered_spec"]
    print(f"   ground truth first in {SPEC_NAME} order: {order['hits']}/{order['of']} "
          f"= {order['rate']} (chance {order['chance_baseline']})")
    print(f"   file orders not explained by sorted/spec order: "
          f"{summary['file_orders_not_explained_by_sorted_or_spec_order']} "
          f"(of which equal to the retriever's rank order: "
          f"{summary['file_orders_not_explained_and_equal_to_rank_order']})")
    print(f"   by-design spec-declared enum values: "
          f"{summary['tasks_with_spec_declared_enum_value']} task(s) "
          f"{summary['spec_declared_enum_values_seen']}")
    asym = summary["mention_asymmetry"]
    print(f"   unique-max mention count identifies the ground truth on "
          f"{asym['unique_max_is_ground_truth']}/{asym['tasks_with_a_unique_max']} of the "
          f"tasks that have one = {asym['rate_given_a_unique_max']} "
          f"(chance {asym['chance_baseline']})")
    for result in flagged:
        print(f"   FINDING {result['task_id']}: " + json.dumps(
            {k: v for k, v in result.items() if k in kinds and v}))
    print(f"clean = {summary['clean']}")
    print(f"wrote {args.out}")
    sys.exit(0 if summary["clean"] else 1)


if __name__ == "__main__":
    main()
