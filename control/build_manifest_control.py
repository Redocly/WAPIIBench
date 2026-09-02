#!/usr/bin/env python3
"""CONTROL condition, step 2 — the task manifest the control's generator agents run from.

The counterpart of `estimate/build_manifest.py`, in the SAME format, one row per sampled
task, drawn from the SAME sample file (`estimate/sample_parseable.json`) so the two arms
cover exactly the same 68 tasks:

    task_id        "{api}:{index:04d}" -- byte-identical to the treatment's task_id for the
                   same task, which is what lets the two arms be compared task by task.
                   `index` is the task's position in data/synthetic/{api}/test_data_final.json
                   and is also the key evaluation.compare() looks a task up by.
    api            stratum / spec / dataset file
    index          the dataset index (int)
    spec_file      absolute path to the filtered five-operation OpenAPI document
                   (the treatment's row carries `client_dir` here instead)
    spec_dir       the directory holding it -- and holding wapii_param_types.json, which is
                   why control/execute_fetch.py runs node with cwd set to it
    prompt_file    absolute path to the blinded prompt, rendered by
                   emit_prompt_control.render()
    answer_path    absolute path the generating agent MUST write its JavaScript to

DELIBERATELY NOT IN HERE, exactly as in the treatment: the operation whitelist, the
ground-truth operationId, the retriever rank, whether the ground truth was substituted, and
every field of the expected config. A generator agent may be shown any row in full. The
operator-only material stays in estimate/whitelists_parseable.json.

Prompts are written to `control/prompts/{api}/`, OUTSIDE the per-API code dir, for the two
reasons the treatment gives: `control/execute_fetch.execute_control()` scans the code dir,
and the runner contract forbids an agent reading another task's prompt -- keeping prompts out
of the working directory means it cannot see a sibling prompt by accident.

    python control/build_manifest_control.py    # -> control/task_manifest_control.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "control"))

import emit_prompt_control                                          # noqa: E402

DEFAULT_SAMPLE = os.path.join(REPO_ROOT, "estimate", "sample_parseable.json")
DEFAULT_WHITELISTS = os.path.join(REPO_ROOT, "estimate", "whitelists_parseable.json")
DEFAULT_WORK = os.path.join(REPO_ROOT, "control", "work")
DEFAULT_PROMPTS = os.path.join(REPO_ROOT, "control", "prompts")
DEFAULT_OUT = os.path.join(REPO_ROOT, "control", "task_manifest_control.json")


def build(sample_file: str, work_root: str, prompt_root: str, whitelist_file: str) -> dict:
    with open(sample_file, "r") as file:
        sample = json.load(file)

    rows = []
    for task_id in sample["tasks"]:
        api, index = task_id["api"], task_id["index"]
        # --strict: refuse to manifest a task whose filtered spec was never built.
        material = emit_prompt_control.emit(api, index, work_root, whitelist_file,
                                            strict=True)
        prompt_dir = os.path.join(prompt_root, api)
        os.makedirs(prompt_dir, exist_ok=True)
        prompt_file = os.path.join(prompt_dir, f"{index}_prompt.md")
        with open(prompt_file, "w") as file:
            file.write(emit_prompt_control.render(material))
        rows.append({
            "task_id": f"{api}:{index:04d}",
            "api": api,
            "index": index,
            "spec_file": os.path.abspath(material["spec_file"]),
            "spec_dir": os.path.abspath(material["spec_dir"]),
            "prompt_file": os.path.abspath(prompt_file),
            "answer_path": os.path.abspath(material["artifact_path"]),
        })

    assert len({row["task_id"] for row in rows}) == len(rows), "duplicate task_id"
    return {
        "condition": "control-raw-openapi",
        "frame": sample.get("frame"),
        "seed": sample.get("seed"),
        "sample": os.path.relpath(sample_file, REPO_ROOT),
        "whitelist_size": 5,
        "n": len(rows),
        "contract": "control/RUNNER_CONTRACT_CONTROL.md",
        "treatment_manifest": "estimate/task_manifest.json",
        "note": "One fresh generator agent per row; one attempt; NO repair loop -- a raw "
                "fetch call has nothing to typecheck, which is an asymmetry favouring the "
                "treatment and is disclosed in the contract. This file contains no ground "
                "truth and no operation whitelist -- a row may be shown to a generator "
                "agent in full.",
        "tasks": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sample", default=DEFAULT_SAMPLE)
    parser.add_argument("--work-root", default=DEFAULT_WORK)
    parser.add_argument("--prompt-root", default=DEFAULT_PROMPTS)
    parser.add_argument("--whitelists", default=DEFAULT_WHITELISTS)
    parser.add_argument("--out", default=DEFAULT_OUT)
    args = parser.parse_args()

    manifest = build(args.sample, args.work_root, args.prompt_root, args.whitelists)
    with open(args.out, "w") as file:
        json.dump(manifest, file, indent=2)
    print(f"n = {manifest['n']}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
