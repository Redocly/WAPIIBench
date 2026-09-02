#!/usr/bin/env python3
"""Which synthetic-API specs the SCORING path can actually parse.

`evaluation.compare()` calls `openapi_utils.parse_spec(openapi/real_world_specs/{api}.yaml)`
(evaluation.py:551) and then resolves the expected URL against the parsed
`openapi_parser.Specification`. A spec that does not parse makes every task on that API
unscoreable, no matter how good the generated answer is. The arm's GENERATION path does not
notice, because it reads the spec with `yaml.safe_load` — so this failure only shows up at
scoring time, which is why it is checked up front and recorded.

This is the evidence behind `estimate/sampling_frame_parseable.PARSEABLE_APIS`. Output:
`estimate/parse_support.json`.

Each API is parsed in a SUBPROCESS with a wall-clock timeout, because a spec that neither
parses nor raises (asana) would otherwise hang the check. `parse_spec` caches per process,
so a subprocess also keeps one API's cache from masking another's cost.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC_DIR = os.path.join(REPO_ROOT, "openapi", "real_world_specs")
DEFAULT_APIS = ("slack", "google_calendar_v3", "google_sheet_v4", "asana",
                "github_v3", "npm_registry")
OUT = os.path.join(REPO_ROOT, "estimate", "parse_support.json")

CHILD = r"""
import json, logging, os, sys, traceback
logging.disable(logging.WARNING)
sys.path.insert(0, os.path.join(sys.argv[1], "wapiibench"))
from openapi_utils import parse_spec
try:
    spec = parse_spec(sys.argv[2])
    print(json.dumps({"ok": True, "paths": len(spec.paths)}))
except BaseException as error:                                       # noqa: BLE001
    print(json.dumps({"ok": False, "error": type(error).__name__,
                      "message": str(error)[:300],
                      "traceback_tail": traceback.format_exc()[-300:]}))
"""


def check(api: str, timeout: int) -> dict:
    spec = os.path.join(SPEC_DIR, f"{api}.yaml")
    if not os.path.isfile(spec):
        return {"api": api, "ok": False, "error": "FileNotFound", "spec": spec}
    started = time.time()
    try:
        done = subprocess.run([sys.executable, "-c", CHILD, REPO_ROOT, spec],
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"api": api, "ok": False, "error": "Timeout",
                "message": f"openapi_utils.parse_spec did not return within {timeout}s",
                "seconds": timeout, "spec_bytes": os.path.getsize(spec)}
    elapsed = round(time.time() - started, 1)
    line = (done.stdout or "").strip().splitlines()
    payload = json.loads(line[-1]) if line else {
        "ok": False, "error": "NoOutput", "message": (done.stderr or "")[-300:]}
    return {"api": api, "seconds": elapsed, "spec_bytes": os.path.getsize(spec), **payload}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apis", nargs="*", default=list(DEFAULT_APIS))
    parser.add_argument("--timeout", type=int, default=900,
                        help="per-API wall-clock budget in seconds (default 900)")
    parser.add_argument("--out", default=OUT)
    args = parser.parse_args()

    def flush(rows: list[dict]) -> dict:
        """Write what we have after EVERY API.

        Not belt-and-braces: parsing asana costs enough memory that the checker itself has
        been killed mid-run, and a checker that loses five good results to the sixth API's
        crash is useless. Written incrementally, so the file always reflects the APIs
        actually checked.
        """
        result = {"parser": "openapi_utils.parse_spec (openapi3-parser), as called by "
                            "evaluation.compare()",
                  "timeout_seconds": args.timeout,
                  "checked": [r["api"] for r in rows],
                  "parseable": sorted(r["api"] for r in rows if r.get("ok")),
                  "unparseable": sorted(r["api"] for r in rows if not r.get("ok")),
                  "detail": rows}
        with open(args.out, "w") as file:
            json.dump(result, file, indent=2)
        return result

    rows: list[dict] = []
    result = flush(rows)
    for api in args.apis:
        row = check(api, args.timeout)
        rows.append(row)
        result = flush(rows)
        status = "PARSES" if row.get("ok") else f"FAILS ({row.get('error')})"
        print(f"{api:<20} {status:<22} {row.get('seconds')}s", flush=True)

    print(f"parseable: {result['parseable']}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
