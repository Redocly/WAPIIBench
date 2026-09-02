#!/usr/bin/env python3
"""CONTROL condition, step 2 — the BLINDED prompt-material emitter.

The control arm's counterpart of `estimate/emit_prompt.py`, and deliberately a near-copy of
it: same structure, same blinding mechanism, same fields, three substitutions.

    estimate/emit_prompt.py (treatment)          control/emit_prompt_control.py (control)
    ------------------------------------------   ----------------------------------------
    starter = evaluation.SETUPS['sdk-invocation']  starter = fetch_starter.STARTER
      read out of evaluation.py via the AST         (no fetch-shaped SETUPS entry exists;
                                                    see control/fetch_starter.py)
    {auth_setup} <- sdk_repair_arm.auth_setup_for  {auth_setup} <- fetch_starter
      (generated client's OPERATIONS[].security)     .auth_headers_for (the SPEC's
                                                     securitySchemes; parity checked on all
                                                     68 tasks, control/auth_parity.json)
    client_dir  -> the generated 5-op client      spec_dir -> the filtered 5-op OpenAPI doc
    artifact    -> {index}_code.ts                artifact -> {index}_code.js
    repair loop -> tsc, <=3 rounds                NO repair loop (nothing to typecheck)

STRUCTURAL BLINDING — a property of the code, not a promise, identical to the treatment's:
`_load_task_text()` opens `data/synthetic/{api}/test_data_final.json`, takes `["task"]`,
`del`s the parsed list and returns a STRING. No ground-truth field is ever in scope
downstream. Nothing in this file can reach `config`; `estimate/retrieval_standin.py` (the
only ground-truth reader in the estimate/ tree) is not imported.

WHAT REACHES THE MODEL AND IS *NOT* A LEAK:
  * The task text, which necessarily quotes the literal values the request needs.
  * The five-operation filtered OpenAPI document. Narrowing to five candidates is the arm's
    retrieval step (num_chunks=5) and is held FIXED across conditions: the operationIds come
    from `estimate/whitelists_parseable.json`, the treatment's own whitelists. The document
    names all five operationIds, exactly as the treatment's `client.ts` does -- symmetric,
    and the model still has to pick one of five and name every parameter itself. The
    operations are emitted in SPEC order (build_specs.py), so the retriever's ranking, and
    with it the identity of the ground-truth operation, cannot leak through position.

KNOWN RESIDUAL ADVANTAGE, shared with the treatment and reported not hidden: the whitelist is
guaranteed to contain the ground-truth operation, so endpoint choice is 1-of-5 rather than
1-of-N, and on tasks where the stand-in retriever missed, the ground truth was substituted in.

HOW THE SPEC IS DELIVERED: by PATH, not inlined into the prompt. This mirrors the treatment,
which hands over `client_dir` and tells the agent to read `client.ts`. Inlining the spec while
the treatment's client stays a file would change the delivery channel as well as the content,
and would make the two prompts incomparable in length. Stated here because it is a choice.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATASET_DIR = os.path.join(REPO_ROOT, "data", "synthetic")
sys.path.insert(0, os.path.join(REPO_ROOT, "control"))

import fetch_starter                                                # noqa: E402

SPEC_SOURCE_DIR = os.path.join(REPO_ROOT, "openapi", "real_world_specs")
SPEC_NAME = "filtered_spec.yaml"
STARTER_SOURCE = "control/fetch_starter.py:STARTER"


def _auth_setup(api: str, operation_ids: list[str]) -> str:
    """Render the starter's `{auth_setup}` placeholder from the SPEC.

    NECESSARY, not cosmetic, for the same reason as in the treatment: every expected config
    in this dataset carries `Authorization: "Bearer <token>"`, `Authorization` is not in
    `evaluation.SPECIAL_KEYS`, and an answer without it scores MISSING_KEY. The treatment
    gets its line from `sdk_repair_arm.auth_setup_for()`; this gets the same decision from
    the spec, and `control/check_auth_parity.py` measured the two agreeing on all 68 tasks.

    STILL BLINDED: reads the spec's `security`/`securitySchemes` and the whitelist. Never a
    task, a dataset file or an expected config.
    """
    spec_file = os.path.join(SPEC_SOURCE_DIR, f"{api}.yaml")
    return fetch_starter.auth_headers_for(spec_file, operation_ids)


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


def _whitelisted_operations(api: str, index: int, whitelist_file: str) -> list[str]:
    """The five operationIds for this task, from the TREATMENT's whitelist file.

    Only ever used for the auth decision (which needs to know which operations' `security`
    to read). Not put in the material, not written to the prompt: the operator-only view
    stays in estimate/whitelists_parseable.json exactly as it does for the treatment.
    """
    with open(whitelist_file, "r") as file:
        tasks = json.load(file)["tasks"]
    for entry in tasks:
        if entry["api"] == api and entry["index"] == index:
            return list(entry["operation_ids"])
    raise KeyError(f"{api}:{index} is not in {whitelist_file}")


def emit(api: str, index: int, work_root: str, whitelist_file: str,
         strict: bool = False) -> dict[str, object]:
    task_text = _load_task_text(api, index)
    spec_dir = os.path.join(work_root, api, f"{index:04d}_spec")
    spec_file = os.path.join(spec_dir, SPEC_NAME)
    artifact = os.path.join(work_root, api, f"{index}_code.js")

    if strict and not os.path.isfile(spec_file):
        raise FileNotFoundError(
            f"no filtered spec at {spec_file}; run control/build_specs.py first")

    auth_setup = _auth_setup(api, _whitelisted_operations(api, index, whitelist_file))
    starter = fetch_starter.render_starter(task_text, auth_setup)
    return {
        "api": api,
        "index": index,
        "task": task_text,
        "starter_code": starter,
        "starter_source": STARTER_SOURCE,
        "spec_dir": spec_dir,
        "spec_file": spec_file,
        "artifact_path": artifact,
        "attempts": 1,
        "repair_budget": "none -- a raw fetch call has nothing to typecheck "
                         "(see control/RUNNER_CONTRACT.md)",
    }


def render(material: dict[str, object]) -> str:
    """The exact text handed to one generator agent. Kept in one place so the contract file
    and the runner cannot drift apart."""
    return f"""\
# WAPIIBench raw-OpenAPI task — {material['api']} #{material['index']}

## Task
{material['task']}

## Your artifact
Write JavaScript to exactly this path (overwrite if it exists):
    {material['artifact_path']}

## Starter code (begin your file with this, unchanged)
```javascript
{material['starter_code']}
```

## The API description
The OpenAPI description of a FIVE-operation subset of this API is at:
    {material['spec_file']}
Read it to discover the available operations, their paths, methods, parameters and
parameter types. Exactly one of those five operations is the right one for this task; the
other four are plausible distractors. Choose, and fill in every argument the task specifies.
The `servers` entry in that document gives the base URL to build the request against.

## Rules
1. ONE attempt. Write the file once. There is no repair loop and nothing to type-check:
   plain `fetch` is untyped, so no tool will tell you whether your call is well-formed. Do
   not revise, second-guess or re-plan your call after writing it.
2. Do NOT read anything under `data/` in the WAPIIBench repo. Do NOT look for, infer or
   reconstruct the expected request. Do not read any other task's artifact, prompt or
   solution. Do not read anything under `estimate/` or elsewhere in `control/`.
3. Do NOT run the code, do not call the real API, do not add mocks and do not override or
   wrap `fetch` yourself.
4. Your file must issue exactly one API call, as a single direct `fetch(...)` call. Do not
   use axios, do not use a generated client or SDK, do not use a helper library, and do not
   wrap the call in a function you then call.
5. Put query parameters in the URL, body parameters in the request body, path parameters in
   the path, and headers in `headers`. Use the `{fetch_starter.AUTH_CONST_NAME}` constant
   from the starter for the `Authorization` header if the starter defines one.
6. Report only: the operation you chose.
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("api")
    parser.add_argument("index", type=int)
    parser.add_argument("--work-root", default=os.path.join(REPO_ROOT, "control", "work"))
    parser.add_argument("--whitelists",
                        default=os.path.join(REPO_ROOT, "estimate",
                                             "whitelists_parseable.json"))
    parser.add_argument("--json", action="store_true", help="emit the material as JSON")
    parser.add_argument("--strict", action="store_true",
                        help="fail if the task's filtered spec is missing")
    args = parser.parse_args()

    material = emit(args.api, args.index, args.work_root, args.whitelists,
                    strict=args.strict)
    if args.json:
        json.dump(material, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render(material))


if __name__ == "__main__":
    main()
