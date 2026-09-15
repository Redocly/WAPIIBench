#!/usr/bin/env python3
"""The SPLIT condition's task manifest, over the SAME 68 sampled tasks.

Identical in shape and provenance to estimate/build_manifest.py -- same
`estimate/sample_parseable.json` draw, same task_id scheme, same "no ground truth, no
operation whitelist in this file" property, so a row may be shown to a generator agent in
full. It differs only in pointing at `split/work` and rendering the split condition's
prompt (split/emit_prompt_split.py).

    REDOCLY_TELEMETRY=off python split/build_clients_split.py   # first
    python split/build_manifest_split.py                        # then
"""

from __future__ import annotations

import argparse
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "split"))

import emit_prompt_split                                             # noqa: E402

# The SAME draw the treatment was dispatched from -- not a re-sample.
DEFAULT_SAMPLE = os.path.join(REPO_ROOT, "estimate", "sample_parseable.json")
DEFAULT_WORK = os.path.join(REPO_ROOT, "split", "work")
DEFAULT_PROMPTS = os.path.join(REPO_ROOT, "split", "prompts")
DEFAULT_OUT = os.path.join(REPO_ROOT, "split", "task_manifest_split.json")


def build(sample_file: str, work_root: str, prompt_root: str) -> dict:
    with open(sample_file, "r") as file:
        sample = json.load(file)

    rows = []
    for task_id in sample["tasks"]:
        api, index = task_id["api"], task_id["index"]
        material = emit_prompt_split.emit(api, index, work_root, strict=True)
        prompt_dir = os.path.join(prompt_root, api)
        os.makedirs(prompt_dir, exist_ok=True)
        prompt_file = os.path.join(prompt_dir, f"{index}_prompt.md")
        with open(prompt_file, "w") as file:
            file.write(emit_prompt_split.render(material))
        rows.append({
            "task_id": f"{api}:{index:04d}",
            "api": api,
            "index": index,
            "client_dir": os.path.abspath(material["client_dir"]),
            "prompt_file": os.path.abspath(prompt_file),
            "answer_path": os.path.abspath(material["artifact_path"]),
        })

    assert len({row["task_id"] for row in rows}) == len(rows), "duplicate task_id"
    return {
        "condition": "split",
        "frame": sample.get("frame"),
        "seed": sample.get("seed"),
        "sample": os.path.relpath(sample_file, REPO_ROOT),
        "whitelist_size": 5,
        "n": len(rows),
        "contract": "split/RUNNER_CONTRACT.md",
        "generator_flags": ["--output-mode split", "--runtime module"],
        "note": "Same 68 tasks, same five-operation whitelists and same filtered specs as "
                "the treatment; the client is generated in split layout so the agent reads "
                "operation signatures without the inlined fetch runtime. One fresh "
                "generator agent per row; one attempt; the only permitted iteration is the "
                "tsc repair loop (<=3 rounds).",
        "tasks": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sample", default=DEFAULT_SAMPLE)
    parser.add_argument("--work-root", default=DEFAULT_WORK)
    parser.add_argument("--prompt-root", default=DEFAULT_PROMPTS)
    parser.add_argument("--out", default=DEFAULT_OUT)
    args = parser.parse_args()

    manifest = build(args.sample, args.work_root, args.prompt_root)
    with open(args.out, "w") as file:
        json.dump(manifest, file, indent=2)
    print(f"n = {manifest['n']}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
