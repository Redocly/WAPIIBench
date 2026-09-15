#!/usr/bin/env python3
"""Reproduce the dry run: build the fixture clients, score the hand-written placeholders.

The dry run is the treatment's PRE-FLIGHT EVIDENCE, not a measurement. Three hand-written
artifacts under `estimate/dryrun/` are pushed through the same
`build_clients -> evaluation.execute -> compare -> analyze -> score_driver` path a generator
agent's answer takes, to show the driver can actually produce each verdict it claims to
distinguish:

    A  slack:3               the RIGHT operation with the RIGHT arguments  -> `correct`
    B  google_sheet_v4:5     the RIGHT operation, well-typed, WRONG values -> not correct
    C  google_calendar_v3:2  a DISTRACTOR from the same five-op whitelist  -> wrong endpoint

EVERY FIXTURE TASK MUST BE OUTSIDE THE 68-TASK SAMPLE, and
`assert_fixtures_outside_sample()` below refuses to run if one is not. Placeholder A is a
deliberately, fully correct answer: operation, every argument name and every argument value.
Committed for a SAMPLED task, it would hand a generator agent the answer to a task all three
conditions are scored on -- which is exactly what happened to `slack:0000` (`admin.apps.approve`)
until this script replaced it with `slack:3` (`admin.apps.restrict`), the unsampled sibling
operation with the identical three-field `URLSearchParams` body. This mirrors
`control/verify_control.assert_cases_outside_sample()`, which makes the same refusal for the
control's derived answers, and `wapiibench/sdk_repair_verify.py`, which does it for the arm's
own hand-written invocation.

The guard lives HERE, in the producing script, because the fixtures had none: before this
file the dry run was a sequence of commands run by hand, so there was no place a future
contributor's run would trip over the precondition. `estimate/score_driver.py` is deliberately
NOT the home for it -- it is a score driver, shared with the recorded measurement, and it must
stay untouched.

    export REDOCLY_TELEMETRY=off                 # NOT optional -- see estimate/README.md
    python estimate/run_dryrun.py                # -> estimate/dryrun/verdicts.json

The work root (`estimate/dryrun_work/`, gitignored) is rebuilt from scratch on every run: a
client generated before the parameter-type table existed coerces nothing, and a stale one
would silently change the verdicts (deviation 6).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "estimate"))

DRYRUN_DIR = os.path.join(REPO_ROOT, "estimate", "dryrun")
DEFAULT_WORK = os.path.join(REPO_ROOT, "estimate", "dryrun_work")
DEFAULT_OUT = os.path.join(DRYRUN_DIR, "verdicts.json")
MANIFEST = os.path.join(REPO_ROOT, "estimate", "task_manifest.json")

# (api, index, fixture file under estimate/dryrun/, what the fixture is there to show)
#
# NOT ONE OF THESE MAY BE IN THE 68-TASK SAMPLE. See the module docstring.
FIXTURES = [
    ("slack", 3, "slack_3_code.ts",
     "placeholder A - fully correct call; proves a `correct` verdict is reachable"),
    ("google_sheet_v4", 5, "google_sheet_v4_5_code.ts",
     "placeholder B - right operation, well-typed, wrong values; tsc cannot catch this"),
    ("google_calendar_v3", 2, "google_calendar_v3_2_code.ts",
     "placeholder C - distractor from the same whitelist; endpoint verdict must catch it"),
]


def assert_fixtures_outside_sample(fixtures=FIXTURES, manifest_file: str = MANIFEST) -> list[str]:
    """Refuse to build or score a fixture for a task the conditions are measured on.

    A dry-run fixture is a hand-written answer committed to the repository, and placeholder A
    is a correct one. For a sampled task that is ground truth sitting inside the reach of a
    generator agent working in `estimate/`, so this is a hard precondition, not a warning.
    """
    with open(manifest_file, "r") as file:
        manifest = json.load(file)
    sampled = {(row["api"], row["index"]) for row in manifest["tasks"]}
    offenders = [f"{api}:{index}" for api, index, _file, _why in fixtures
                 if (api, index) in sampled]
    if offenders:
        raise SystemExit(
            "run_dryrun.py refuses to run: "
            f"{', '.join(offenders)} is in the measured sample ({manifest_file}). "
            "Dry-run fixtures must come from tasks no condition is scored on.")
    return [f"{api}:{index}" for api, index, _file, _why in fixtures]


def build_whitelists(fixtures, work_root: str) -> str:
    """The five-operation whitelist per fixture task, from the stand-in retriever.

    `estimate/whitelists_parseable.json` only covers the 68 sampled tasks, and by construction
    no fixture task is in it, so the whitelists are built live -- the same call
    `retrieval_standin.main()` makes for the sample, with the ground-truth operation guaranteed
    to be among the five. That guarantee is what makes placeholder C a fair distractor test.
    """
    import retrieval_standin as standin

    sample_file = os.path.join(work_root, "dryrun_sample.json")
    with open(sample_file, "w") as file:
        json.dump({"note": "dry-run fixtures only; not a sample of anything",
                   "tasks": [{"api": api, "index": index}
                             for api, index, _file, _why in fixtures]}, file, indent=2)

    whitelists = standin.build_whitelists(sample_file)
    whitelist_file = os.path.join(work_root, "dryrun_whitelists.json")
    with open(whitelist_file, "w") as file:
        json.dump(whitelists, file, indent=2)
    for entry in whitelists["tasks"]:
        print(f"{entry['api']}:{entry['index']} whitelist: {entry['operation_ids']}"
              + ("  [ground truth substituted]" if entry["ground_truth_substituted"] else ""))
    return whitelist_file


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--work-root", default=DEFAULT_WORK)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--node", default="node")
    parser.add_argument("--population", type=int, default=None,
                        help="passed through to score_driver.py; its default if omitted")
    parser.add_argument("--redocly-bin", default=None)
    parser.add_argument("--node-modules", default=None)
    args = parser.parse_args()

    os.chdir(REPO_ROOT)
    case_ids = assert_fixtures_outside_sample()
    print(f"dry-run fixtures outside the measured sample: {', '.join(case_ids)}")

    import build_clients

    shutil.rmtree(args.work_root, ignore_errors=True)
    os.makedirs(args.work_root, exist_ok=True)
    whitelist_file = build_whitelists(FIXTURES, args.work_root)

    built = build_clients.build(
        whitelist_file, args.work_root,
        args.redocly_bin or build_clients.DEFAULT_REDOCLY,
        args.node_modules or build_clients.DEFAULT_NODE_MODULES)
    failed = [r for r in built if not r["ok"]]
    if failed:
        raise SystemExit("client generation failed: "
                         + ", ".join(f"{r['api']}:{r['index']} {r['error']}" for r in failed))

    for api, index, fixture, why in FIXTURES:
        source = os.path.join(DRYRUN_DIR, fixture)
        if not os.path.isfile(source):
            raise SystemExit(f"missing dry-run fixture {source}")
        shutil.copyfile(source, os.path.join(args.work_root, api, f"{index}_code.ts"))
        print(f"staged {fixture} -> {api}/{index}_code.ts   ({why})")

    command = [sys.executable, os.path.join("estimate", "score_driver.py"),
               "--work-root", args.work_root, "--out", args.out, "--node", args.node]
    if args.population is not None:
        command += ["--population", str(args.population)]
    print("\n$ " + " ".join(command), flush=True)
    result = subprocess.run(command, cwd=REPO_ROOT,
                            env={**os.environ, "REDOCLY_TELEMETRY": "off"})
    if result.returncode != 0:
        raise SystemExit(f"score_driver.py exited {result.returncode}")

    with open(args.out, "r") as file:
        verdicts = json.load(file)
    by_task = {(row["api"], row["index"]): row["sample_verdict"] for row in verdicts["tasks"]}
    print("\n=== dry-run expectations ===")
    expected = {("slack", 3): "correct"}
    clean = True
    for api, index, _fixture, why in FIXTURES:
        got = by_task.get((api, index))
        want = expected.get((api, index))
        ok = got == want if want else got != "correct"
        clean &= bool(ok)
        print(f"  {api + ':' + str(index):<22} {str(got):<13} "
              f"{'OK' if ok else 'UNEXPECTED'}   ({why.split(';')[0]})")
    print(f"dry run {'passes' if clean else 'FAILS'}; wrote {args.out}")
    sys.exit(0 if clean else 1)


if __name__ == "__main__":
    main()
