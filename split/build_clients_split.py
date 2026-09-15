#!/usr/bin/env python3
"""Generate one filtered, FIVE-operation typed client per sampled task, in SPLIT layout.

Same 68 tasks, same five-operation whitelists (estimate/whitelists_parseable.json), same
filtered spec and same coercion table as the treatment. The ONLY difference from
estimate/build_clients.py is the pair of generate-client layout flags:

    --output-mode split   schema/data-model types move to a sibling `client.schemas.ts`
                          that the entry re-exports.
    --runtime module      the generic fetch runtime moves to `runtime/*.ts` beside the
                          client, instead of being inlined into `client.ts`.

WHY BOTH FLAGS. `--output-mode split` alone does NOT reduce what the agent must read: it
moves the DATA MODEL out and leaves the operation signatures AND the whole inlined runtime
behind in `client.ts` (measured on google_calendar_v3:0001: client.ts 16,079 -> of which
83% is generic runtime). `--runtime module` is the flag that removes the runtime. Together
they leave `client.ts` as operation signatures + argument types only -- 2,800 pretokens on
that task, 1.16x the control's OpenAPI document, against 10.57x for the treatment's
monolithic client.

`wapiibench/sdk_repair_arm.generate_client()` ALREADY takes `output_mode` and `runtime`
keyword arguments and forwards them to the CLI, so this needs no change to the arm.

Both flags are accepted by the pinned @redocly/cli 2.51.0 -- no version bump needed.

    REDOCLY_TELEMETRY=off python split/build_clients_split.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "wapiibench"))
sys.path.insert(0, os.path.join(REPO_ROOT, "estimate"))

DEFAULT_REDOCLY = "/home/claude/tools/node_modules/.bin/redocly"
DEFAULT_NODE_MODULES = "/home/claude/tools/node_modules"
# The 68 sampled tasks use the *parseable* frame -- the same file estimate/work was built
# from. estimate/whitelists.json is the 78-task superset; using it here would desync the
# condition from the treatment's sample.
DEFAULT_WHITELISTS = os.path.join(REPO_ROOT, "estimate", "whitelists_parseable.json")


def build(whitelist_file: str, work_root: str, redocly_bin: str, node_modules: str,
          output_mode: str = "split", runtime: str = "module",
          spec_source: str = "filter-in", only: list[str] | None = None) -> list[dict]:
    import sdk_repair_arm as arm                     # read-only import of the arm
    from build_clients import _patch_match_strategy  # read-only import of the treatment's shim

    with open(whitelist_file, "r") as file:
        whitelists = json.load(file)

    results = []
    for entry in whitelists["tasks"]:
        api, index = entry["api"], entry["index"]
        key = f"{api}:{index}"
        if only and key not in only:
            continue

        out_dir = os.path.join(work_root, api, f"{index:04d}_client")
        os.makedirs(out_dir, exist_ok=True)
        spec = os.path.join(REPO_ROOT, "openapi", "real_world_specs", f"{api}.yaml")

        if spec_source == "control-pruned":
            # VARIANT B. Feed generate-client the control's ALREADY-PRUNED five-operation
            # document (control/work/{api}/{index:04d}_spec/filtered_spec.yaml) instead of
            # the full spec plus a filter-in decorator.
            #
            # WHY. `filter-in` prunes OPERATIONS only; generate-client's type emitter then
            # walks the whole `components.schemas` block, so `client.schemas.ts` comes out
            # as the API's ENTIRE data model -- byte-identical across every task of an API,
            # 34 types on google_calendar_v3 where the five operations reach 2. The control
            # already prunes components to the transitive $ref closure of the surviving
            # operations (control/build_specs.py, "PRUNING IS A DELIBERATE ASYMMETRY, IN
            # THE CONTROL'S FAVOUR"), so reusing its output makes both conditions see the
            # SAME five-operation closure and nothing else.
            #
            # Verified on google_calendar_v3:0001: `client.ts` is BYTE-IDENTICAL to the
            # filter-in build (the operation signatures do not change), while
            # `client.schemas.ts` drops from 9,407 to 291 pretokens.
            pruned = os.path.join(REPO_ROOT, "control", "work", api,
                                  f"{index:04d}_spec", "filtered_spec.yaml")
            if not os.path.isfile(pruned):
                results.append({"api": api, "index": index, "ok": False,
                                "error": f"no control spec at {pruned}; "
                                         "run control/build_specs.py first"})
                print(f"FAIL {key}: missing {pruned}", flush=True)
                continue
            config = os.path.join(out_dir, "redocly.yaml")
            with open(config, "w") as file:
                json.dump({"apis": {arm.FILTER_API_ALIAS: {"root": pruned}}}, file, indent=2)
        else:
            config = arm.filter_spec(spec, api, task="",
                                     out_path=os.path.join(out_dir, "redocly.yaml"),
                                     operation_ids=entry["operation_ids"])
            _patch_match_strategy(config)   # matchStrategy all->any, + drop rank order
        try:
            client_entry, surface = arm.generate_client(
                config, out_dir, output_mode=output_mode, runtime=runtime,
                redocly_bin=redocly_bin)
        except Exception as error:                                  # noqa: BLE001
            results.append({"api": api, "index": index, "ok": False,
                            "error": str(error)[:400]})
            print(f"FAIL {key}: {str(error)[:200]}", flush=True)
            continue

        arm.write_tsconfig(out_dir)
        arm.write_task_globals(out_dir, {"definitions": None})
        arm.write_param_types(spec, out_dir, operation_ids=entry["operation_ids"])
        link = os.path.join(out_dir, "node_modules")
        if node_modules and not os.path.exists(link):
            os.symlink(node_modules, link)
        with open(os.path.join(out_dir, "_surface.txt"), "w") as file:
            file.write(surface)

        schemas = os.path.join(out_dir, "client.schemas.ts")
        results.append({"api": api, "index": index, "ok": True,
                        "client": client_entry,
                        "client_bytes": os.path.getsize(client_entry),
                        "schemas_bytes": os.path.getsize(schemas) if os.path.isfile(schemas) else None,
                        "has_schemas_file": os.path.isfile(schemas),
                        "has_runtime_dir": os.path.isdir(os.path.join(out_dir, "runtime")),
                        # Sorted, NOT the retriever rank order the whitelist stores:
                        # rank 1 is the ground truth on 57/68 sampled tasks, so recording
                        # the order here would commit the answer key by position.
                        "operations": sorted(entry["operation_ids"])})
        print(f"{key}: client.ts {os.path.getsize(client_entry)} B, "
              f"client.schemas.ts {results[-1]['schemas_bytes']} B", flush=True)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--whitelists", default=DEFAULT_WHITELISTS)
    parser.add_argument("--work-root", default=os.path.join(REPO_ROOT, "split", "work"))
    parser.add_argument("--redocly-bin", default=DEFAULT_REDOCLY)
    parser.add_argument("--node-modules", default=DEFAULT_NODE_MODULES)
    parser.add_argument("--output-mode", default="split", choices=["single", "split"])
    parser.add_argument("--runtime", default="module", choices=["inline", "module"])
    parser.add_argument("--spec-source", default="filter-in",
                        choices=["filter-in", "control-pruned"],
                        help="filter-in: full spec + the arm's filter-in decorator (variant "
                             "A, mirrors the treatment exactly). control-pruned: the "
                             "control's already-$ref-pruned five-operation document "
                             "(variant B, also prunes the data model).")
    parser.add_argument("--only", nargs="*", metavar="API:INDEX")
    parser.add_argument("--out", default=os.path.join(REPO_ROOT, "split", "build_report.json"))
    args = parser.parse_args()

    results = build(args.whitelists, args.work_root, args.redocly_bin, args.node_modules,
                    args.output_mode, args.runtime, args.spec_source, args.only)
    failed = [r for r in results if not r["ok"]]
    with open(args.out, "w") as file:
        json.dump({"output_mode": args.output_mode, "runtime": args.runtime,
                   "spec_source": args.spec_source,
                   "n": len(results), "failed": len(failed), "tasks": results}, file, indent=2)
    print(f"built {len(results) - len(failed)}/{len(results)}; failed: {len(failed)}")
    for r in failed:
        print(f"  FAIL {r['api']}:{r['index']} {r['error']}")


if __name__ == "__main__":
    main()
