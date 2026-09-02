#!/usr/bin/env python3
"""How big is the raw OpenAPI description the control reads, next to the typed client the
treatment reads?

This bears directly on a claim the SDK arm invites -- that a typed client is a more
token-efficient way to give a model an API than the OpenAPI description -- so it is measured
per task rather than asserted.

WHAT IS COMPARED. For each of the 68 sampled tasks, the artifact the PROMPT tells the agent
to read, and nothing else:
    control    control/work/{api}/{index:04d}_spec/filtered_spec.yaml
    treatment  estimate/work/{api}/{index:04d}_client/client.ts
`client.zod.ts` is measured separately as an upper bound: the treatment's starter imports it
for the request-validation middleware, but the treatment prompt only instructs the agent to
read `client.ts`.

HOW SIZE IS MEASURED, AND WHAT IS *NOT* VERIFIED HERE. This container has no network egress
to a tokenizer vocabulary (tiktoken's BPE file and huggingface.co both return 403 through the
agent proxy), so a true BPE token count could not be computed. Reported instead:
  * bytes -- exact, no proxy involved.
  * pretokens -- the number of pieces the GPT-4/cl100k *pretokenization* regex splits the
    text into, before any BPE merges. Every BPE token lies inside exactly one pretoken, so
    pretokens is a strict LOWER BOUND on the BPE token count, and for text this
    punctuation-dense the two run close. It is a deterministic, reproducible measure with no
    vocabulary file needed.
  * bytes_per_pretoken -- if this differs a lot between the two artifacts, the ratio of
    pretokens is not a safe stand-in for the ratio of tokens, and the comparison should be
    redone with a real tokenizer.
ANY TOKEN-EFFICIENCY CLAIM DRAWN FROM THIS FILE MUST SAY IT RESTS ON PRETOKENS, NOT TOKENS.

    python control/token_sizes.py      # -> control/token_sizes.json
"""

from __future__ import annotations

import argparse
import json
import os
import statistics

import regex

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_MANIFEST = os.path.join(REPO_ROOT, "control", "task_manifest_control.json")
DEFAULT_CLIENT_ROOT = os.path.join(REPO_ROOT, "estimate", "work")
DEFAULT_OUT = os.path.join(REPO_ROOT, "control", "token_sizes.json")

# The cl100k_base / GPT-4 pretokenization pattern, verbatim from tiktoken's registry.
CL100K_PRETOKEN = regex.compile(
    r"""'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}+|\p{N}{1,3}| ?[^\s\p{L}\p{N}]++[\r\n]*|\s*[\r\n]|\s+(?!\S)|\s+""")


def measure(path: str) -> dict | None:
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as file:
        text = file.read()
    pretokens = sum(1 for _ in CL100K_PRETOKEN.finditer(text))
    nbytes = len(text.encode("utf-8"))
    return {"path": path, "bytes": nbytes, "pretokens": pretokens,
            "bytes_per_pretoken": round(nbytes / pretokens, 3) if pretokens else None}


def _summary(values: list[float]) -> dict:
    values = sorted(values)
    return {"n": len(values),
            "min": values[0], "median": statistics.median(values), "max": values[-1],
            "mean": round(statistics.mean(values), 1)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--client-root", default=DEFAULT_CLIENT_ROOT)
    parser.add_argument("--out", default=DEFAULT_OUT)
    args = parser.parse_args()

    with open(args.manifest, "r") as file:
        manifest = json.load(file)

    rows, missing = [], []
    for entry in manifest["tasks"]:
        api, index = entry["api"], entry["index"]
        client_dir = os.path.join(args.client_root, api, f"{index:04d}_client")
        spec = measure(entry["spec_file"])
        client = measure(os.path.join(client_dir, "client.ts"))
        zod = measure(os.path.join(client_dir, "client.zod.ts"))
        if spec is None or client is None:
            missing.append(entry["task_id"])
            continue
        row = {"task_id": entry["task_id"], "api": api, "index": index,
               "control_spec": spec, "treatment_client": client,
               "treatment_client_zod": zod,
               "pretoken_ratio_client_over_spec":
                   round(client["pretokens"] / spec["pretokens"], 3),
               "byte_ratio_client_over_spec": round(client["bytes"] / spec["bytes"], 3)}
        rows.append(row)

    out = {
        "measure": "bytes (exact) and cl100k PRETOKENS (a strict lower bound on BPE tokens; "
                   "no tokenizer vocabulary was reachable from this container)",
        "compared": {"control": "control/work/{api}/{index:04d}_spec/filtered_spec.yaml",
                     "treatment": "estimate/work/{api}/{index:04d}_client/client.ts"},
        "n": len(rows),
        "missing": missing,
        "control_spec_pretokens": _summary([r["control_spec"]["pretokens"] for r in rows]),
        "treatment_client_pretokens":
            _summary([r["treatment_client"]["pretokens"] for r in rows]),
        "control_spec_bytes": _summary([r["control_spec"]["bytes"] for r in rows]),
        "treatment_client_bytes": _summary([r["treatment_client"]["bytes"] for r in rows]),
        "control_spec_bytes_per_pretoken":
            _summary([r["control_spec"]["bytes_per_pretoken"] for r in rows]),
        "treatment_client_bytes_per_pretoken":
            _summary([r["treatment_client"]["bytes_per_pretoken"] for r in rows]),
        "pretoken_ratio_client_over_spec":
            _summary([r["pretoken_ratio_client_over_spec"] for r in rows]),
        "tasks_where_client_is_smaller":
            sum(1 for r in rows if r["pretoken_ratio_client_over_spec"] < 1.0),
        "tasks": rows,
    }
    with open(args.out, "w") as file:
        json.dump(out, file, indent=2)
    for key in ("control_spec_pretokens", "treatment_client_pretokens",
                "control_spec_bytes", "treatment_client_bytes",
                "pretoken_ratio_client_over_spec"):
        print(f"{key}: {json.dumps(out[key])}")
    print(f"client smaller than spec on {out['tasks_where_client_is_smaller']}/{out['n']} tasks")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
