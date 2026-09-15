#!/usr/bin/env python3
"""Token sizes for the SPLIT condition, measured exactly as control/token_sizes.py does.

Reuses control/token_sizes.measure() and its cl100k PRETOKEN regex unchanged, so every
number here is directly comparable to control/token_sizes.json. Pretokens are a strict
LOWER BOUND on BPE tokens (no tokenizer vocabulary is reachable from this container); any
claim drawn from this file must say it rests on pretokens, not tokens.

Compared, per task -- the artifact the prompt tells the agent to read, and nothing else:
    control    control/work/{api}/{index:04d}_spec/filtered_spec.yaml
    treatment  estimate/work/{api}/{index:04d}_client/client.ts   (monolithic, runtime inlined)
    split      split/work/{api}/{index:04d}_client/client.ts      (signatures + arg types)
    split+sch  the above plus client.schemas.ts (the API data model, needed only for bodies)

    python split/token_sizes_split.py      # -> split/token_sizes_split.json
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "control"))
from token_sizes import measure, _summary          # noqa: E402  (read-only reuse)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", default=os.path.join(REPO_ROOT, "control",
                                                           "task_manifest_control.json"))
    parser.add_argument("--out", default=os.path.join(REPO_ROOT, "split",
                                                      "token_sizes_split.json"))
    args = parser.parse_args()

    with open(args.manifest, "r") as file:
        manifest = json.load(file)

    rows, missing = [], []
    for entry in manifest["tasks"]:
        api, index = entry["api"], entry["index"]
        treat = os.path.join(REPO_ROOT, "estimate", "work", api, f"{index:04d}_client")
        split = os.path.join(REPO_ROOT, "split", "work", api, f"{index:04d}_client")
        spec = measure(entry["spec_file"])
        mono = measure(os.path.join(treat, "client.ts"))
        sigs = measure(os.path.join(split, "client.ts"))
        schemas = measure(os.path.join(split, "client.schemas.ts"))
        pruned_dir = os.path.join(REPO_ROOT, "split", "work_pruned", api, f"{index:04d}_client")
        p_sigs = measure(os.path.join(pruned_dir, "client.ts"))
        p_schemas = measure(os.path.join(pruned_dir, "client.schemas.ts"))
        if not all((spec, mono, sigs, schemas)):
            missing.append(entry["task_id"])
            continue
        both = sigs["pretokens"] + schemas["pretokens"]
        rows.append({"task_id": entry["task_id"], "api": api, "index": index,
                     "control_spec": spec, "treatment_monolith": mono,
                     "split_client": sigs, "split_schemas": schemas,
                     "split_client_plus_schemas_pretokens": both,
                     "ratio_monolith_over_spec":
                         round(mono["pretokens"] / spec["pretokens"], 3),
                     "ratio_split_client_over_spec":
                         round(sigs["pretokens"] / spec["pretokens"], 3),
                     "ratio_split_both_over_spec":
                         round(both / spec["pretokens"], 3),
                     "pruned_client": p_sigs, "pruned_schemas": p_schemas,
                     "pruned_both_pretokens":
                         (p_sigs["pretokens"] + p_schemas["pretokens"]) if p_sigs and p_schemas else None,
                     "ratio_pruned_both_over_spec":
                         round((p_sigs["pretokens"] + p_schemas["pretokens"]) / spec["pretokens"], 3)
                         if p_sigs and p_schemas else None})

    out = {
        "measure": "bytes (exact) and cl100k PRETOKENS (strict lower bound on BPE tokens)",
        "n": len(rows), "missing": missing,
        "control_spec_pretokens": _summary([r["control_spec"]["pretokens"] for r in rows]),
        "treatment_monolith_pretokens":
            _summary([r["treatment_monolith"]["pretokens"] for r in rows]),
        "split_client_pretokens": _summary([r["split_client"]["pretokens"] for r in rows]),
        "split_schemas_pretokens": _summary([r["split_schemas"]["pretokens"] for r in rows]),
        "split_client_plus_schemas_pretokens":
            _summary([r["split_client_plus_schemas_pretokens"] for r in rows]),
        "ratio_monolith_over_spec":
            _summary([r["ratio_monolith_over_spec"] for r in rows]),
        "ratio_split_client_over_spec":
            _summary([r["ratio_split_client_over_spec"] for r in rows]),
        "ratio_split_both_over_spec":
            _summary([r["ratio_split_both_over_spec"] for r in rows]),
        "pruned_schemas_pretokens":
            _summary([r["pruned_schemas"]["pretokens"] for r in rows
                      if r.get("pruned_schemas")]),
        "pruned_both_pretokens":
            _summary([r["pruned_both_pretokens"] for r in rows if r.get("pruned_both_pretokens")]),
        "ratio_pruned_both_over_spec":
            _summary([r["ratio_pruned_both_over_spec"] for r in rows
                      if r.get("ratio_pruned_both_over_spec")]),
        "tasks_where_split_client_is_smaller_than_spec":
            sum(1 for r in rows if r["ratio_split_client_over_spec"] < 1.0),
        "tasks": rows,
    }
    with open(args.out, "w") as file:
        json.dump(out, file, indent=2)
    for key in ("control_spec_pretokens", "treatment_monolith_pretokens",
                "split_client_pretokens", "split_schemas_pretokens",
                "split_client_plus_schemas_pretokens", "pruned_schemas_pretokens",
                "pruned_both_pretokens", "ratio_monolith_over_spec",
                "ratio_split_client_over_spec", "ratio_split_both_over_spec",
                "ratio_pruned_both_over_spec"):
        print(f"{key}: {json.dumps(out[key])}")
    print(f"split client.ts smaller than the spec on "
          f"{out['tasks_where_split_client_is_smaller_than_spec']}/{out['n']} tasks")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
