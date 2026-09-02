#!/usr/bin/env python3
"""Does the control's auth rule agree with the treatment's, task for task?

The treatment's starter carries `client.auth.bearer('<token>')` when
`sdk_repair_arm.auth_setup_for()` says the operation declares bearer security, read out of the
GENERATED CLIENT's `OPERATIONS[...].security` entry. The control has no generated client, so
`control/fetch_starter.auth_headers_for()` re-derives the same decision from the SPEC.

If the two rules disagreed on a task, that task's conditions would differ in whether the
`Authorization` header is available at all — and since `Authorization` is not in
`evaluation.SPECIAL_KEYS`, one condition would score MISSING_KEY where the other did not, for
a reason that has nothing to do with typed clients vs. raw OpenAPI. So agreement is checked
on every sampled task rather than argued for.

Requires the treatment's clients to be present under `estimate/work/` (rebuildable with
`python estimate/build_clients.py --whitelists estimate/whitelists_parseable.json`).

    python control/check_auth_parity.py     # -> control/auth_parity.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "control"))
sys.path.insert(0, os.path.join(REPO_ROOT, "wapiibench"))

import fetch_starter                                                # noqa: E402

SPEC_DIR = os.path.join(REPO_ROOT, "openapi", "real_world_specs")
DEFAULT_WHITELISTS = os.path.join(REPO_ROOT, "estimate", "whitelists_parseable.json")
DEFAULT_CLIENT_ROOT = os.path.join(REPO_ROOT, "estimate", "work")
DEFAULT_OUT = os.path.join(REPO_ROOT, "control", "auth_parity.json")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--whitelists", default=DEFAULT_WHITELISTS)
    parser.add_argument("--client-root", default=DEFAULT_CLIENT_ROOT)
    parser.add_argument("--out", default=DEFAULT_OUT)
    args = parser.parse_args()

    import sdk_repair_arm as arm

    with open(args.whitelists, "r") as file:
        whitelists = json.load(file)["tasks"]

    rows, disagreements, missing_clients = [], [], []
    for entry in whitelists:
        api, index = entry["api"], entry["index"]
        spec_file = os.path.join(SPEC_DIR, f"{api}.yaml")
        control_line = fetch_starter.auth_headers_for(spec_file, entry["operation_ids"])
        client_dir = os.path.join(args.client_root, api, f"{index:04d}_client")

        treatment_line = None
        if os.path.isfile(os.path.join(client_dir, "client.ts")):
            treatment_line = arm.auth_setup_for(client_dir)
        else:
            missing_clients.append(f"{api}:{index}")

        control_bearer = bool(control_line)
        treatment_bearer = None if treatment_line is None else bool(treatment_line)
        agree = treatment_bearer is None or control_bearer == treatment_bearer
        row = {"api": api, "index": index,
               "control_emits_auth": control_bearer,
               "treatment_emits_auth": treatment_bearer,
               "control_line": control_line,
               "treatment_line": treatment_line,
               "agree": agree}
        rows.append(row)
        if not agree:
            disagreements.append(row)

    summary = {
        "n": len(rows),
        "compared_against_a_generated_client": sum(
            1 for r in rows if r["treatment_emits_auth"] is not None),
        "clients_missing": missing_clients,
        "control_emits_auth": sum(1 for r in rows if r["control_emits_auth"]),
        "treatment_emits_auth": sum(1 for r in rows if r["treatment_emits_auth"]),
        "disagreements": len(disagreements),
        "clean": not disagreements and not missing_clients,
    }
    with open(args.out, "w") as file:
        json.dump({"summary": summary, "tasks": rows}, file, indent=2)
    print(json.dumps(summary, indent=2))
    for row in disagreements:
        print(f"  DISAGREE {row['api']}:{row['index']} "
              f"control={row['control_line']!r} treatment={row['treatment_line']!r}")
    print(f"wrote {args.out}")
    sys.exit(0 if summary["clean"] else 1)


if __name__ == "__main__":
    main()
