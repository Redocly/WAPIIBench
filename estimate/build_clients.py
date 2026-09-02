#!/usr/bin/env python3
"""Generate one filtered, FIVE-operation typed client per sampled task.

Reuses the arm's own steps by importing `wapiibench/sdk_repair_arm.py` READ-ONLY
(`filter_spec`, `generate_client`, `write_tsconfig`, `write_task_globals`). Nothing in
estimate/ writes to that file — a parallel agent owns it.

The operation whitelist comes from estimate/whitelists.json (the BM25 stand-in), NOT from
`sdk_repair_arm.select_operation_ids`, which is still `NotImplementedError`. That is the one
place this scaffolding substitutes for an unbound harness step, and it is the substitution
that keeps the estimate honest: a single-operation client would hand the model the endpoint.

Layout produced (per API, mirroring what evaluation.compare() expects):
    estimate/work/{api}/{index:04d}_client/    client.ts, tsconfig.json, *.d.ts, node_modules
    estimate/work/{api}/{index}_code.ts        <- written by the GENERATOR, not here
    estimate/work/{api}/{index}_config.json    <- written by the capture shim at execute time
"""

from __future__ import annotations

import argparse
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "wapiibench"))

DEFAULT_REDOCLY = "/home/claude/tools/node_modules/.bin/redocly"
DEFAULT_NODE_MODULES = "/home/claude/tools/node_modules"


def _patch_match_strategy(config_path: str) -> None:
    """SHIM (one key), and a BUG REPORT for whoever owns sdk_repair_arm.py.

    `sdk_repair_arm.filter_spec()` hardcodes `matchStrategy: "all"` in the `filter-in`
    decorator. Verified against @redocly/cli 2.51.0: "all" means the node must match EVERY
    listed value, so it is satisfiable only for a ONE-element whitelist. With five
    operationIds it filters everything out and `generate-client` emits a client whose
    `OPERATIONS` is `{}` and whose `Ops` is `Record<string, never>` — a 76-250 kB file of
    types with no callable operation. It exits 0, so the failure is silent.

    This rewrites that single key to "any" in the config filter_spec() just wrote. It does
    not touch sdk_repair_arm.py (a parallel agent owns that file). The arm needs the same
    one-word change before it can generate a multi-operation client.
    """
    with open(config_path, "r") as file:
        config = json.load(file)
    for api_config in config["apis"].values():
        api_config["decorators"]["filter-in"]["matchStrategy"] = "any"
    with open(config_path, "w") as file:
        json.dump(config, file, indent=2)


def build(whitelist_file: str, work_root: str, redocly_bin: str, node_modules: str,
          only: list[str] | None = None) -> list[dict]:
    import sdk_repair_arm as arm      # read-only import of the arm

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

        # filter_spec() is given operation_ids explicitly, so select_operation_ids() (the
        # unbound TODO(bind) stub) is never reached.
        config = arm.filter_spec(spec, api, task="", out_path=os.path.join(out_dir, "redocly.yaml"),
                                 operation_ids=entry["operation_ids"])
        _patch_match_strategy(config)
        try:
            client_entry, surface = arm.generate_client(config, out_dir, redocly_bin=redocly_bin)
        except Exception as error:                                  # noqa: BLE001
            results.append({"api": api, "index": index, "ok": False, "error": str(error)[:400]})
            continue

        arm.write_tsconfig(out_dir)
        # No `definitions` on synthetic tasks, but call it so the real-world set works too.
        arm.write_task_globals(out_dir, {"definitions": None})
        link = os.path.join(out_dir, "node_modules")
        if node_modules and not os.path.exists(link):
            os.symlink(node_modules, link)
        with open(os.path.join(out_dir, "_surface.txt"), "w") as file:
            file.write(surface)

        results.append({"api": api, "index": index, "ok": True,
                        "client": client_entry, "bytes": os.path.getsize(client_entry),
                        "operations": entry["operation_ids"]})
        print(f"{key}: {os.path.getsize(client_entry)} bytes, "
              f"{len(entry['operation_ids'])} ops", flush=True)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--whitelists", default=os.path.join(REPO_ROOT, "estimate", "whitelists.json"))
    parser.add_argument("--work-root", default=os.path.join(REPO_ROOT, "estimate", "work"))
    parser.add_argument("--redocly-bin", default=DEFAULT_REDOCLY)
    parser.add_argument("--node-modules", default=DEFAULT_NODE_MODULES)
    parser.add_argument("--only", nargs="*", metavar="API:INDEX",
                        help="build just these tasks (e.g. asana:4 slack:0)")
    args = parser.parse_args()

    results = build(args.whitelists, args.work_root, args.redocly_bin,
                    args.node_modules, args.only)
    failed = [r for r in results if not r["ok"]]
    print(f"built {len(results) - len(failed)}/{len(results)}; failed: {len(failed)}")
    for r in failed:
        print(f"  FAIL {r['api']}:{r['index']} {r['error']}")


if __name__ == "__main__":
    main()
