#!/usr/bin/env python3
"""Deliverable 2 — the BLINDED prompt-material emitter.

Given a task identifier (api + index) this emits, and only ever emits:
  1. the natural-language task instruction,
  2. the starter code the arm defines (evaluation.SETUPS['sdk-invocation']),
  3. the path to that task's FILTERED GENERATED CLIENT (5 operations),
  4. the path to the .ts artifact the generator must write.

STRUCTURAL BLINDING — this is the important part, not a promise but a property of the code:
this module reads the per-API dataset file `data/synthetic/{api}/test_data_final.json` and
immediately drops every key except `task`. `_load_task_text()` returns a STRING, not a dict,
so no ground-truth field is in scope anywhere downstream. There is no code path in this file
that can reach `config`, and `retrieval_standin.resolve_ground_truth` (the only ground-truth
reader in estimate/) is not imported here.

The generating agent is given the OUTPUT of this script and nothing else. It is not given the
dataset path, and per estimate/RUNNER_CONTRACT.md it is forbidden from reading the repo's
data/ directory at all. The blinding therefore has two layers: this emitter cannot leak, and
the contract forbids going around it. `--strict` additionally refuses to emit if the client
directory is missing, so an agent cannot be handed a task whose material is incomplete.

WHAT IS *DESIGNED* TO REACH THE MODEL, AND IS NOT A LEAK:
  * The task text. It necessarily quotes the literal values the request needs (ids, titles,
    limits) — otherwise the task is unsolvable. Expected and fine.
  * The five-operation typed client. Narrowing the API to 5 candidate operations is the
    arm's retrieval step and mirrors the paper's num_chunks=5 RAG setting. The model still
    has to pick one of five and name the parameters itself.
KNOWN RESIDUAL ADVANTAGE (reported, not hidden): the whitelist is guaranteed to contain the
ground-truth operation, so endpoint choice is 1-of-5 rather than 1-of-N. On the tasks where
the stand-in retriever missed, the ground truth was substituted in, which a real end-to-end
pipeline would not do. estimate/blinding_check.py measures the rest.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATASET_DIR = os.path.join(REPO_ROOT, "data", "synthetic")

EVALUATION_PY = os.path.join(REPO_ROOT, "wapiibench", "evaluation.py")
STARTER_SETUP = "sdk-invocation"


def harness_starter(setup: str = STARTER_SETUP) -> str:
    """Read `evaluation.SETUPS[setup]` out of evaluation.py as TEXT, via the AST.

    NOT a copy and NOT an import. Not a copy because the arm is under active development by
    another agent and a pasted starter silently goes stale (it already did once during this
    work: the starter gained the zod-validation middleware lines mid-session). Not an import
    because `import evaluation` pulls torch + transformers + langchain, none of which the
    prompt-emission path needs.

    evaluation.py stores the starter with doubled braces so `str.format()` renders literal JS
    braces; we undouble them exactly as `.format()` would.
    """
    with open(EVALUATION_PY, "r") as file:
        tree = ast.parse(file.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "SETUPS" for target in node.targets):
            setups = ast.literal_eval(node.value)
            if setup not in setups:
                raise KeyError(f"evaluation.SETUPS has no {setup!r}")
            return setups[setup].replace("{{", "{").replace("}}", "}")
    raise KeyError("no SETUPS assignment found in evaluation.py")


def _load_task_text(api: str, index: int) -> str:
    """Return ONLY the instruction string for a task. Nothing else leaves this function."""
    path = os.path.join(DATASET_DIR, api, "test_data_final.json")
    with open(path, "r") as file:
        tasks = json.load(file)
    if not 0 <= index < len(tasks):
        raise IndexError(f"{api} has {len(tasks)} tasks; index {index} is out of range")
    text = tasks[index]["task"]
    del tasks                      # ground truth goes out of scope immediately
    if not isinstance(text, str):
        raise TypeError("task instruction must be a string")
    return text


def emit(api: str, index: int, work_root: str, strict: bool = False) -> dict[str, object]:
    task_text = _load_task_text(api, index)
    client_dir = os.path.join(work_root, api, f"{index:04d}_client")
    artifact = os.path.join(work_root, api, f"{index}_code.ts")

    if strict and not os.path.isdir(client_dir):
        raise FileNotFoundError(
            f"no generated client at {client_dir}; run estimate/build_clients.py first")

    return {
        "api": api,
        "index": index,
        "task": task_text,
        "starter_code": harness_starter().replace("{task}", task_text),
        "starter_setup": STARTER_SETUP,
        "client_dir": client_dir,
        "client_entrypoint": os.path.join(client_dir, "client.ts"),
        "artifact_path": artifact,
        "attempts": 1,
        "repair_budget": "typecheck-repair loop only (see RUNNER_CONTRACT.md)",
    }


def render(material: dict[str, object]) -> str:
    """The exact text handed to one generator agent. Kept in one place so the contract file
    and the runner cannot drift apart."""
    return f"""\
# WAPIIBench SDK+repair task — {material['api']} #{material['index']}

## Task
{material['task']}

## Your artifact
Write TypeScript to exactly this path (overwrite if it exists):
    {material['artifact_path']}

## Starter code (begin your file with this, unchanged)
```typescript
{material['starter_code']}```

## The typed client
A generated TypeScript client for a FIVE-operation subset of this API is at:
    {material['client_dir']}
Its entrypoint is `client.ts`; import it as `./client` (the starter already does). Read
`client.ts` to discover the available operations, their argument shapes and their types.
Exactly one of those five operations is the right one for this task; the other four are
plausible distractors. Choose, and fill in every argument the task specifies.

## Rules
1. ONE attempt. Write the file once, then type-check it.
2. The ONLY iteration you may do is the typecheck-repair loop: run
   `npx tsc --noEmit --strict --target ES2020 --module CommonJS --moduleResolution node \\
        --esModuleInterop --skipLibCheck --lib ES2020,DOM <your file> <the *.d.ts in the client dir>`
   from inside the client directory, and fix ONLY the type errors it reports. Up to 3 repair
   rounds. Do not otherwise revise, second-guess or re-plan your call.
3. Do NOT read anything under `data/` in the WAPIIBench repo. Do NOT look for, infer or
   reconstruct the expected request. Do not read any other task's artifact or solution.
4. Do NOT run the code, do not call the real API, do not add mocks or a fetch override
   beyond the starter's line.
5. Your file must issue exactly one API call through the generated client, and must not
   hand-build a URL, use `fetch` directly, or use axios.
6. Report only: the operation you chose, and the tsc rounds you needed.
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("api")
    parser.add_argument("index", type=int)
    parser.add_argument("--work-root", default=os.path.join(REPO_ROOT, "estimate", "work"))
    parser.add_argument("--json", action="store_true", help="emit the material as JSON")
    parser.add_argument("--strict", action="store_true",
                        help="fail if the task's generated client is missing")
    args = parser.parse_args()

    material = emit(args.api, args.index, args.work_root, strict=args.strict)
    if args.json:
        json.dump(material, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render(material))


if __name__ == "__main__":
    main()
