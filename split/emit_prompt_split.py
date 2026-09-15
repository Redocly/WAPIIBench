#!/usr/bin/env python3
"""The SPLIT condition's blinded prompt-material emitter.

Reuses estimate/emit_prompt.py READ-ONLY for everything that is not the client-description
section: `harness_starter()` (reads evaluation.SETUPS via the AST), `_auth_setup()` (derives
the auth line from the client's own OPERATIONS[...].security) and `_load_task_text()` (the
structural blinding -- returns a STRING, so no ground-truth field is ever in scope). The
task text, the starter, the artifact path and every rule are therefore IDENTICAL to the
treatment's by construction, not by being kept in sync.

THE ONE DIFFERENCE is `render()`'s "## The typed client" section, replaced by PREAMBLE
below. Everything else -- rules 1-6, the tsc command, the one-attempt budget -- is copied
from emit_prompt.render() verbatim so the two prompts differ in exactly one place.

WHY A PREAMBLE IS NEEDED AT ALL. Under `--output-mode split` the generator moves the
API's DATA MODEL into `client.schemas.ts` and leaves the operation signatures behind in
`client.ts`; under `--runtime module` it additionally moves the generic transport into
`runtime/*.ts`. So the agent's reading list is two files, and which of the two a given
argument's type lives in depends on whether it is a parameter (client.ts) or a schema
alias (client.schemas.ts). The preamble says which file is which and which files carry no
API information at all. It does NOT tell the agent anything it could not derive from
`client.ts` itself -- `Ops` gives the argument shapes and the file's last line re-exports
the operation names -- it just saves the agent from having to open `runtime/create-client.ts`
to confirm the calling convention, which is exactly the generic-runtime reading the
condition exists to remove.

BLINDING: the preamble is a FIXED STRING. It is byte-identical across all 68 tasks (only
`client_dir`, which the treatment also interpolates, varies). It names no operation, no
parameter, no API and no task, so it cannot carry task-specific signal. Verify with
`--check-preamble-constant`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "estimate"))

import emit_prompt as treatment                                      # noqa: E402  read-only

STARTER_SETUP = treatment.STARTER_SETUP


# --------------------------------------------------------------------------------------- #
# The fixed preamble. Task-neutral, API-neutral, identical for all 68 tasks.
# --------------------------------------------------------------------------------------- #
PREAMBLE = """\
## The typed client
A generated TypeScript client for a FIVE-operation subset of this API is at:
    {client_dir}
It was generated in SPLIT layout, so its files divide by what they hold:

  * `client.ts` — THE API SURFACE, and the file to read. For each of the five operations
    it declares that operation's `Path`, `Query`, `Body` and `Result` types; the `Ops` map,
    whose keys are the operation names and whose entries give each operation's argument
    shape; and the `client` object the starter already imports.
  * `client.schemas.ts` — the API's DATA-MODEL types. `client.ts` re-exports them, and an
    operation's `Body` or `Result` type is frequently just an alias to one of them
    (`export type FooBody = Bar;`, where `Bar` is declared here). Read a type from this
    file when an alias in `client.ts` sends you to it; you do not need the rest of it.
  * `runtime/*.ts` and `client.zod.ts` — the generic transport (URL building, parameter
    serialisation, auth, retry, pagination, streaming) and the request validators. These
    are the same for every API and hold NO information about this API's operations.
    Do not read them.

Calling convention: each operation is a method on the imported `client`, named after its
operationId with every `.` replaced by `_`, and takes a single argument object whose
`path`, `query` and `body` members are the ones that operation's `Ops` entry declares:

    client.<operation_name>({{ path: {{ ... }}, query: {{ ... }}, body: {{ ... }} }});

Members that `Ops` marks optional with `?` may be omitted. The same operation names are
also re-exported from `client.ts` as standalone functions, if you prefer to call one
directly.

Exactly one of those five operations is the right one for this task; the other four are
plausible distractors. Choose, and fill in every argument the task specifies.\
"""


def emit(api: str, index: int, work_root: str, strict: bool = False) -> dict[str, object]:
    """Same material as the treatment's emit(), against the split work root."""
    material = treatment.emit(api, index, work_root, strict=strict)
    material["condition"] = "split"
    material["schemas_file"] = os.path.join(material["client_dir"], "client.schemas.ts")
    material["runtime_dir"] = os.path.join(material["client_dir"], "runtime")
    return material


def render(material: dict[str, object]) -> str:
    """The treatment's prompt with exactly one section swapped."""
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

{PREAMBLE.format(client_dir=material['client_dir'])}

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
7. Read ONLY `client.ts` and `client.schemas.ts` in the client directory. Do not open
   `runtime/`, `client.zod.ts`, `redocly.yaml`, `wapii_param_types.json` or any compiled
   `.js` beside them. Report every file you read.
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("api", nargs="?")
    parser.add_argument("index", nargs="?", type=int)
    parser.add_argument("--work-root", default=os.path.join(REPO_ROOT, "split", "work"))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--check-preamble-constant", action="store_true",
                        help="assert the rendered preamble differs across tasks only in "
                             "client_dir, then exit")
    args = parser.parse_args()

    if args.check_preamble_constant:
        manifest = json.load(open(os.path.join(REPO_ROOT, "split", "task_manifest_split.json")))
        seen = set()
        for row in manifest["tasks"]:
            seen.add(PREAMBLE.format(client_dir=row["client_dir"])
                     .replace(row["client_dir"], "<CLIENT_DIR>"))
        print(f"distinct preambles across {len(manifest['tasks'])} tasks "
              f"(client_dir masked): {len(seen)}")
        sys.exit(0 if len(seen) == 1 else 1)

    material = emit(args.api, args.index, args.work_root, strict=args.strict)
    if args.json:
        json.dump(material, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render(material))


if __name__ == "__main__":
    main()
