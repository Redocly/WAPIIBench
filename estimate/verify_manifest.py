#!/usr/bin/env python3
"""Deliverable 3 (verification half) — does the manifest point a generator at any answer?

Two independent questions, kept apart:

A. PATH INTEGRITY.  Every `client_dir` and `prompt_file` in the manifest exists (the client
   dir with a readable `client.ts`), and every `answer_path` does NOT yet exist — an
   answer_path that already has a file on it means a stale artifact would be scored as this
   run's output.

B. NO ANSWER IN REACH.  For every task, walk EVERY regular file a generator can reach —
   the prompt file, plus every file in the client directory (`node_modules` excluded: it is
   a symlink to the shared npm tree and contains no task material) — and test it against
   that task's ground-truth config, which this script reads and the generator never does.

   Four checks, because "the answer" is four different things here:

   1. EXPECTED VALUES NOT IN THE TASK TEXT.  The task instruction has to quote the ids,
      titles and limits the request needs or the task is unsolvable, so a value appearing in
      the prompt's task text is BY DESIGN. A value appearing in any generator-readable file
      while NOT appearing in the task text would be an answer the agent did not have to
      derive. Must be empty. (Values of length <= 1, and values that are substrings of the
      API's own server URL, are skipped as noise.)

      ONE CLASS IS SPLIT OUT AS BY-DESIGN: a value that the SPEC ITSELF declares in an
      `enum`. `generate-client` emits those as TypeScript literal unions, so they are in
      the client because the API description puts them there, and having `tsc` reject a
      wrong one is precisely the typed-client property this arm exists to measure (see
      SDK_REPAIR_ARM.md, "Enums are emitted as TypeScript literal unions"). Example:
      google_sheet_v4 #13's task says "interpreting the input as user entered values" and
      the expected value is the enum member `USER_ENTERED`; the agent still has to map the
      prose onto the member. Counted and listed under `value_is_spec_declared_enum`, never
      as a leak.

   2. WHICH-OF-THE-FIVE MARKERS.  The ground-truth operationId is in the client by design
      (1-of-5 choice is the arm's retrieval step). What must NOT be recoverable is WHICH of
      the five it is. Two ways it could leak, both checked:
        * ordering — the retriever ranks the ground truth first on most tasks, so any
          generator-readable file that lists the five in RANK order hands over the answer.
          A file is only flagged when its order matches the rank order AND is NOT
          explainable by a task-independent order: `sorted()` (what `build_clients` writes
          into `redocly.yaml`) or SPEC order (what `generate-client` emits into `client.ts`,
          and what `_surface.txt`, `client.zod.ts` and `wapii_param_types.json` inherit).
          Those two orders are the same for every task on an API, so they cannot carry
          task-specific information even when they coincide with the ranking. The aggregate
          check that actually matters is reported alongside: how often the ground truth is
          FIRST in the order a generator sees, against the 1-in-5 = 20% chance baseline.
        * asymmetry — the ground-truth operation must not be described more, less, or
          differently from the four distractors. Checked by counting occurrences of each of
          the five ids per file and requiring the ground truth not to be a unique extremum.
          Counted with WHOLE-TOKEN matching, because whitelisted operationIds are routinely
          substrings of one another (`admin_apps_approve` inside
          `admin_apps_approved_list`, `emoji_list` inside `admin_emoji_list`,
          `sheets.spreadsheets.values.batchUpdate` inside `...batchUpdateByDataFilter`);
          plain substring counting reports every such pair as a false asymmetry.

   3. GROUND-TRUTH CONFIG SHAPE.  No generator-readable file may contain the expected
      request's URL verbatim, nor the JSON serialisation of any part of the expected config.

   4. NO DATASET REACHABLE BY PATH.  No generator-readable file may mention
      `data/synthetic`, `test_data_final.json`, `validation_data`, or the whitelist /
      blinding / results file names — i.e. the manifest must not hand an agent a pointer to
      the answer even if it never contains one.

Output: estimate/manifest_verification.json. Exit status is non-zero on any finding.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "estimate"))

from retrieval_standin import load_operations, resolve_ground_truth   # noqa: E402

DATASET_DIR = os.path.join(REPO_ROOT, "data", "synthetic")
DEFAULT_MANIFEST = os.path.join(REPO_ROOT, "estimate", "task_manifest.json")
DEFAULT_WHITELISTS = os.path.join(REPO_ROOT, "estimate", "whitelists_parseable.json")
DEFAULT_OUT = os.path.join(REPO_ROOT, "estimate", "manifest_verification.json")

# Never opened as task material: a symlink into the shared npm tree.
SKIP_NAMES = {"node_modules"}
# Paths that would point an agent AT the answer even without containing it.
FORBIDDEN_POINTERS = ("data/synthetic", "test_data_final.json", "validation_data",
                      "whitelists.json", "whitelists_parseable.json",
                      "blinding_report", "results/verdicts.json", "manifest_verification")


def _token_count(needle: str, text: str) -> int:
    """Occurrences of `needle` as a whole identifier.

    Whitelisted operationIds are frequently prefixes of one another, so a plain
    `text.count(needle)` charges the shorter id for every occurrence of the longer one and
    manufactures an asymmetry that is not there. `.` is part of the boundary class because
    google_* operationIds are dotted (`sheets.spreadsheets.values.batchUpdate`).
    """
    return len(re.findall(rf"(?<![A-Za-z0-9_.]){re.escape(needle)}(?![A-Za-z0-9_.])", text))


def spec_enum_values(api: str) -> set[str]:
    """Every string the API description itself declares in an `enum`.

    Read from the spec with `yaml.safe_load` — no dataset, no expected config. Used only to
    classify a value as spec-declared rather than answer-derived.
    """
    import yaml

    with open(os.path.join(REPO_ROOT, "openapi", "real_world_specs", f"{api}.yaml")) as file:
        root = yaml.safe_load(file)
    found: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("enum"), list):
                found.update(str(v) for v in node["enum"] if isinstance(v, (str, int, float)))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(root)
    return found


def _first_seen_order(text: str, ids: list[str]) -> list[str]:
    """The five operationIds in the order a reader of `text` meets them."""
    seen: list[str] = []
    for match in re.finditer("|".join(re.escape(i) for i in sorted(ids, key=len, reverse=True)),
                             text):
        found = match.group(0)
        if found in ids and found not in seen:
            seen.append(found)
    return seen


def _leaf_values(value) -> list[str]:
    if isinstance(value, dict):
        return [v for item in value.values() for v in _leaf_values(item)]
    if isinstance(value, list):
        return [v for item in value for v in _leaf_values(item)]
    if isinstance(value, bool) or value is None:
        return []                       # booleans/nulls are not distinctive strings
    return [str(value)]


def readable_files(row: dict) -> list[str]:
    """Every regular file a generator agent can open for this task."""
    files = [row["prompt_file"]]
    for name in sorted(os.listdir(row["client_dir"])):
        if name in SKIP_NAMES:
            continue
        path = os.path.join(row["client_dir"], name)
        if os.path.isfile(path) and not os.path.islink(path):
            files.append(path)
    return files


def check_task(row: dict, operations: list[dict], whitelist: dict,
               enum_values: set[str], spec_order: list[str]) -> dict:
    api, index = row["api"], row["index"]
    with open(os.path.join(DATASET_DIR, api, "test_data_final.json"), "r") as file:
        task = json.load(file)[index]
    config, task_text = task["config"], task["task"]

    truth = resolve_ground_truth(operations, config)
    servers = {s for record in operations for s in record["servers"]}

    expected_values = set()
    for field in ("params", "data", "path_params"):
        expected_values.update(_leaf_values((config.get(field) or {})))
    # Values the task text already states are by design (the task must state them).
    task_lower = task_text.lower()
    unstated = sorted(v for v in expected_values
                      if len(v) > 1
                      and v.lower() not in task_lower
                      and not any(v in s for s in servers))

    # By design: values the SPEC declares in an enum (generate-client emits them as TS
    # literal unions, which is the typed-client property being measured, not a leak).
    by_design_enum = sorted(v for v in unstated if v in enum_values)
    unstated = [v for v in unstated if v not in enum_values]

    expected_url = (config.get("url") or "").split("?", 1)[0]
    config_json = json.dumps(config, sort_keys=True)
    ids = list(whitelist["operation_ids"])
    rank_order = ids                     # whitelists.json stores rank order
    # Orders that are the same for EVERY task on this API, and so cannot carry
    # task-specific information even when they happen to coincide with the ranking.
    uninformative_orders = [sorted(ids), [i for i in spec_order if i in ids]]
    order_observations: list[dict] = []
    findings: dict[str, list] = {
        "value_not_in_task_text": [], "rank_order_exposed": [],
        "ground_truth_asymmetric": [], "expected_url_verbatim": [],
        "expected_config_verbatim": [], "forbidden_pointer": [],
    }

    for path in readable_files(row):
        try:
            with open(path, "r", errors="replace") as file:
                text = file.read()
        except OSError as error:                                     # noqa: BLE001
            findings["forbidden_pointer"].append(f"{path}: unreadable ({error})")
            continue
        lower = text.lower()
        rel = os.path.relpath(path, REPO_ROOT)

        for value in unstated:
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(value.lower())}(?![A-Za-z0-9_])", lower):
                findings["value_not_in_task_text"].append({"file": rel, "value": value})
        if expected_url and expected_url.lower() in lower:
            findings["expected_url_verbatim"].append({"file": rel, "url": expected_url})
        if config_json and config_json in text:
            findings["expected_config_verbatim"].append(rel)
        for pointer in FORBIDDEN_POINTERS:
            if pointer in text:
                findings["forbidden_pointer"].append({"file": rel, "pointer": pointer})

        # --- which-of-the-five: ordering ---
        seen = _first_seen_order(text, ids)
        if len(seen) == len(ids):
            order_observations.append({
                "file": rel, "order": seen,
                "ground_truth_first": bool(truth) and seen[0] == truth,
                "equals_rank_order": seen == rank_order,
                "explained_by": ("sorted" if seen == uninformative_orders[0]
                                 else "spec_order" if seen == uninformative_orders[1]
                                 else None)})
            if (seen == rank_order and truth and rank_order[0] == truth
                    and seen not in uninformative_orders):
                findings["rank_order_exposed"].append({"file": rel, "order": seen})

        # --- which-of-the-five: asymmetry (whole-token counts; see _token_count) ---
        counts = {i: _token_count(i, text) for i in ids}
        if truth in counts and len(set(counts.values())) > 1:
            others = [c for i, c in counts.items() if i != truth]
            if others and (counts[truth] > max(others) or counts[truth] < min(others)):
                findings["ground_truth_asymmetric"].append({"file": rel, "counts": counts})

    return {
        "task_id": row["task_id"], "api": api, "index": index,
        "files_scanned": [os.path.relpath(p, REPO_ROOT) for p in readable_files(row)],
        "ground_truth_operation_resolved": bool(truth),
        "expected_values_total": len(expected_values),
        "expected_values_not_stated_in_task": unstated,
        "value_is_spec_declared_enum": by_design_enum,
        "order_observations": order_observations,
        **findings,
        "findings": sum(len(v) for v in findings.values()),
    }


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
    path_problems = []
    for row in manifest["tasks"]:
        for key in ("client_dir", "prompt_file", "answer_path"):
            if not os.path.isabs(row[key]):
                path_problems.append(f"{row['task_id']}: {key} is not absolute")
        if not os.path.isdir(row["client_dir"]):
            path_problems.append(f"{row['task_id']}: client_dir missing")
        elif not os.path.isfile(os.path.join(row["client_dir"], "client.ts")):
            path_problems.append(f"{row['task_id']}: client_dir has no client.ts")
        if not os.path.isfile(row["prompt_file"]):
            path_problems.append(f"{row['task_id']}: prompt_file missing")
        if os.path.exists(row["answer_path"]):
            path_problems.append(f"{row['task_id']}: answer_path ALREADY EXISTS "
                                 f"(a stale artifact would be scored)")
        if not os.path.isdir(os.path.dirname(row["answer_path"])):
            path_problems.append(f"{row['task_id']}: answer_path's directory does not exist")
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
            enums[api] = spec_enum_values(api)
        spec_order = [r["operation_id"] for r in cache[api]]
        result = check_task(row, cache[api], whitelists[(api, row["index"])],
                            enums[api], spec_order)
        rows.append(result)
        for kind in ("value_not_in_task_text", "rank_order_exposed",
                     "ground_truth_asymmetric", "expected_url_verbatim",
                     "expected_config_verbatim", "forbidden_pointer"):
            kinds[kind] += len(result[kind])

    flagged = [r for r in rows if r["findings"]]
    files_scanned = sum(len(r["files_scanned"]) for r in rows)
    unresolved = [r["task_id"] for r in rows if not r["ground_truth_operation_resolved"]]

    # The aggregate that actually answers "can the agent tell which of the five it is?":
    # how often is the ground truth FIRST in the order a generator meets the five ids,
    # against the 1-in-5 chance baseline. Measured on `client.ts`, the file the contract
    # tells the agent to read.
    client_orders = [obs for result in rows for obs in result["order_observations"]
                     if obs["file"].endswith("client.ts")]
    gt_first = sum(1 for obs in client_orders if obs["ground_truth_first"])
    unexplained = [obs for result in rows for obs in result["order_observations"]
                   if obs["explained_by"] is None]

    summary = {
        "n_tasks": len(rows),
        "files_scanned": files_scanned,
        "path_problems": path_problems,
        "tasks_with_findings": len(flagged),
        "findings_by_kind": dict(kinds),
        "tasks_with_spec_declared_enum_value": sum(
            1 for r in rows if r["value_is_spec_declared_enum"]),
        "spec_declared_enum_values_seen": sorted(
            {v for r in rows for v in r["value_is_spec_declared_enum"]}),
        "ground_truth_first_in_client_ts": {
            "hits": gt_first, "of": len(client_orders),
            "rate": round(gt_first / len(client_orders), 4) if client_orders else None,
            "chance_baseline": 0.2},
        # `unexplained` orders are the ones this script's spec-order reconstruction
        # (paths walked in YAML order) does not reproduce, because `generate-client`
        # groups OPERATIONS its own way. They are still task-INDEPENDENT: the same five
        # ids produce the same order whichever task drew them. What would matter is a file
        # order that equals the RETRIEVER's ranking, which is counted separately and must
        # be zero on tasks where the ranking begins with the ground truth.
        "file_orders_not_explained_by_sorted_or_spec_order": len(unexplained),
        "file_orders_not_explained_and_equal_to_rank_order": sum(
            1 for obs in unexplained if obs["equals_rank_order"]),
        "ground_truth_unresolved": unresolved,
        "clean": not path_problems and not flagged and not unresolved,
    }
    with open(args.out, "w") as file:
        json.dump({"summary": summary, "tasks": rows}, file, indent=2)

    print(f"B. no answer in reach: {files_scanned} file(s) scanned across {len(rows)} task(s)")
    print(json.dumps(summary["findings_by_kind"], indent=2))
    order = summary["ground_truth_first_in_client_ts"]
    print(f"   ground truth first in client.ts order: {order['hits']}/{order['of']} "
          f"= {order['rate']} (chance {order['chance_baseline']})")
    print(f"   file orders not explained by sorted/spec order: "
          f"{summary['file_orders_not_explained_by_sorted_or_spec_order']} "
          f"(of which equal to the retriever's rank order: "
          f"{summary['file_orders_not_explained_and_equal_to_rank_order']})")
    print(f"   by-design spec-declared enum values: "
          f"{summary['tasks_with_spec_declared_enum_value']} task(s) "
          f"{summary['spec_declared_enum_values_seen']}")
    for result in flagged:
        print(f"   FINDING {result['task_id']}: " + json.dumps(
            {k: v for k, v in result.items()
             if k in kinds and v}))
    print(f"clean = {summary['clean']}")
    print(f"wrote {args.out}")
    sys.exit(0 if summary["clean"] else 1)


if __name__ == "__main__":
    main()
