#!/usr/bin/env python3
"""Deliverable 3 — the task manifest the generator agents are dispatched from.

Writes ONE JSON file (`estimate/task_manifest.json`) with one row per sampled task:

    task_id        stable identifier, "{api}:{index:04d}". Stable because both halves come
                   from the dataset, not from a draw: `index` is the task's position in
                   data/synthetic/{api}/test_data_final.json, which is also the key
                   evaluation.compare() looks a task up by. Re-running the frame with a
                   different seed changes WHICH task_ids appear, never what one means.
    api            stratum / spec / dataset file
    index          the dataset index (int)
    client_dir     absolute path to the generated five-operation typed client
    prompt_file    absolute path to the blinded prompt, rendered by emit_prompt.render()
    answer_path    absolute path the generating agent MUST write its TypeScript to

It also RENDERS each blinded prompt to `prompt_file`, so the dispatcher hands an agent a
file path instead of re-running the emitter and risking a different rendering.

WHAT IS DELIBERATELY NOT IN HERE: the operation whitelist, the ground-truth operationId,
the retriever rank, whether the ground truth was substituted, and every field of the
expected config. A generator agent may be shown any row of this file in full. The
operator-only material stays in estimate/whitelists_parseable.json.

Prompts are written OUTSIDE the per-API code_dir (`estimate/prompts/{api}/` rather than
`estimate/work/{api}/`) for two reasons: `evaluation.execute()` scans the code_dir, and
RUNNER_CONTRACT.md forbids an agent from reading another task's prompt — keeping prompts out
of the directory the agent works in means it never sees a sibling prompt by accident.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "estimate"))

import emit_prompt                                                   # noqa: E402

DEFAULT_SAMPLE = os.path.join(REPO_ROOT, "estimate", "sample_parseable.json")
DEFAULT_WORK = os.path.join(REPO_ROOT, "estimate", "work")
DEFAULT_PROMPTS = os.path.join(REPO_ROOT, "estimate", "prompts")
DEFAULT_OUT = os.path.join(REPO_ROOT, "estimate", "task_manifest.json")


def build(sample_file: str, work_root: str, prompt_root: str) -> dict:
    with open(sample_file, "r") as file:
        sample = json.load(file)

    rows = []
    for task_id in sample["tasks"]:
        api, index = task_id["api"], task_id["index"]
        # --strict: refuse to put a task in the manifest whose client was never generated.
        material = emit_prompt.emit(api, index, work_root, strict=True)
        prompt_dir = os.path.join(prompt_root, api)
        os.makedirs(prompt_dir, exist_ok=True)
        prompt_file = os.path.join(prompt_dir, f"{index}_prompt.md")
        with open(prompt_file, "w") as file:
            file.write(emit_prompt.render(material))
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
        "frame": sample.get("frame"),
        "seed": sample.get("seed"),
        "sample": os.path.relpath(sample_file, REPO_ROOT),
        "whitelist_size": 5,
        "n": len(rows),
        "contract": "estimate/RUNNER_CONTRACT.md",
        "note": "One fresh generator agent per row; one attempt; the only permitted "
                "iteration is the tsc repair loop (<=3 rounds). This file contains no "
                "ground truth and no operation whitelist -- a row may be shown to a "
                "generator agent in full.",
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
