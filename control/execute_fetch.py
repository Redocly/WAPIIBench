#!/usr/bin/env python3
"""CONTROL condition, execution — run a hand-written `fetch` answer and capture its request.

The control analogue of `sdk_repair_arm.execute_sdk_repair()` (which is itself the analogue of
`evaluation.execute()`). A SEPARATE executor rather than an edit to either: the treatment's
path must keep producing exactly the numbers it produced.

Why neither existing executor can run a control answer:

  * `evaluation.execute()`'s `.js` branch calls `_extract_axios_call()`, whose regex is
    `axios\\.[a-z]+\\(`. A plain `fetch(...)` answer matches nothing, so every control task
    would be written off as `Verdict.ABSENT_REQUEST` without ever running.
  * `execute_sdk_repair()` compiles TypeScript against a generated `./client` and injects the
    capture function as that client's `fetch` option. There is no client here.

What this does, per `{index}_code.js` in the code dir:

  1. prepend `wapiibench/capture_shim.js` — the TREATMENT'S FILE, unmodified — with the
     config path substituted into its single `%s` placeholder, exactly as
     `execute_sdk_repair()` does it and as `evaluation.execute()` does it for `mock.js`;
  2. append `control/capture_global_fetch.js`, whose entire content is one assignment that
     points `globalThis.fetch` at the shim's capture function (see that file: with the shim
     alone, a hand-written fetch call is NOT captured);
  3. prepend the task's `definitions` via `evaluation._create_variable_definitions()`, the
     harness's own helper, so real-world-style injected variables behave as in every other arm
     (the synthetic tasks have none);
  4. run the whole thing under node with `cwd` set to the task's `{index:04d}_spec/`
     directory, which is where `build_specs.py` wrote `wapii_param_types.json`. This mirrors
     the treatment, where `cwd` is the client dir holding the same file, so `capture_shim.js`
     finds it under its default relative name and the SAME coercion applies in both arms.

`compare()` and `analyze()` are then reused verbatim: the config file this writes is produced
by the treatment's shim, so it is the same shape by construction.

TWO DELIBERATE DIFFERENCES FROM `execute_sdk_repair()`, both recorded in control/README.md:

  A. THE ANSWER IS WRAPPED IN AN ASYNC IIFE. Node runs stdin as CommonJS, where top-level
     `await` is a SyntaxError. A correct request written as `await fetch(...)` would then be
     scored `nonexecutable` for a harness detail that has nothing to do with the model, and
     the control would be penalised where the treatment is not (a TS candidate's top-level
     await is caught by `tsc` and the treatment gets up to 3 repair rounds to fix it, which
     the control does not have). Wrapping is semantically inert for code that does not use
     top-level await, and the shim writes the config synchronously inside the fetch call, so
     nothing depends on the promise being awaited. This is a small advantage to the control
     and is disclosed as one.
  B. A RUN THAT PRODUCES NO CONFIG FILE IS RECORDED AS `EXECUTION_ERROR`, even when node
     exits 0. `execute_sdk_repair()` only writes the error file when the exit code is
     non-zero, so an answer that runs cleanly without issuing a request leaves no
     `{index}_config.json`, `compare()` never sees the task and it silently drops out of the
     denominator. An answer with no request is a wrong answer, not an absent one, so the
     control counts it. This is stricter on the control than the treatment's rule is on the
     treatment.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "wapiibench"))

SHIM = os.path.join(REPO_ROOT, "wapiibench", "capture_shim.js")
GLOBAL_PATCH = os.path.join(REPO_ROOT, "control", "capture_global_fetch.js")


def executable_code(answer_js: str, config_log_file: str,
                    variable_definitions: str = "") -> str:
    """The exact text handed to node. Kept as one function so the verification driver and the
    scoring driver cannot diverge."""
    with open(SHIM, "r") as file:
        shim = file.read() % config_log_file
    with open(GLOBAL_PATCH, "r") as file:
        patch = file.read()
    # See difference A in the module docstring for why the answer is wrapped.
    return (f"{shim}\n{patch}\n{variable_definitions}\n"
            f"(async () => {{\n{answer_js}\n}})();\n")


def run_answer(answer_js: str, spec_dir: str, config_log_file: str, node: str = "node",
               variable_definitions: str = "") -> subprocess.CompletedProcess:
    """Run one control answer with `cwd = spec_dir` so the shim finds the param-type table."""
    code = executable_code(answer_js, os.path.abspath(config_log_file), variable_definitions)
    return subprocess.run([node, "-"], input=code, text=True, capture_output=True,
                          cwd=os.path.abspath(spec_dir))


def execute_control(code_dir: str, node: str = "node",
                    test_data_file: str | None = None) -> list[dict]:
    """Execute every `{index}_code.js` in `code_dir`, writing `{index}_config.json` beside it.

    Signature mirrors `evaluation.execute(code_dir, node, test_data_file)`.
    """
    from evaluation import Verdict, _create_variable_definitions   # lazy heavy import

    if test_data_file is not None:
        with open(test_data_file, "r") as file:
            test_data = json.load(file)
    else:
        test_data = None

    outcomes = []
    with (open(os.path.join(code_dir, "execution.out"), "w") as out_file,
          open(os.path.join(code_dir, "execution.err"), "w") as err_file):
        for file_name in sorted(os.listdir(code_dir)):
            file_path = os.path.join(code_dir, file_name)
            root, ext = os.path.splitext(file_name)
            if not os.path.isfile(file_path) or ext != ".js" or not root.endswith("_code"):
                continue
            index = root.split(sep="_", maxsplit=1)[0]

            # ABSOLUTE: the shim runs under cwd=spec_dir, so a relative path would make
            # fs.writeFileSync target <spec_dir>/<code_dir>/... and every task would land
            # EXECUTION_ERROR regardless of the answer (the same bug the arm had).
            config_log_file = os.path.abspath(os.path.join(code_dir, f"{index}_config.json"))
            spec_dir = os.path.join(code_dir, f"{int(index):04d}_spec")

            def _fail(reason: str) -> None:
                with open(config_log_file, "w") as file:
                    json.dump({"ERROR": Verdict.EXECUTION_ERROR, "_detail": reason},
                              file, indent=2)

            out_file.write(f"\n### Now processing {file_name} ###\n")
            if not os.path.isdir(spec_dir):
                _fail(f"no filtered spec directory at {spec_dir}")
                outcomes.append({"index": int(index), "ok": False,
                                 "reason": "missing spec dir"})
                continue

            with open(file_path, "r") as file:
                answer_js = file.read()
            variable_definitions = "" if test_data is None \
                else _create_variable_definitions(test_data[int(index)])

            proc = run_answer(answer_js, spec_dir, config_log_file, node,
                              variable_definitions)
            out_file.write(proc.stdout)
            out_file.write(f"\nExit code: {proc.returncode}\n")
            err_file.write(f"\n### {file_name} ###\n{proc.stderr}")

            if not os.path.isfile(config_log_file):
                # Difference B in the module docstring: no captured request is a wrong
                # answer, not a task that disappears from the denominator.
                _fail(f"node exited {proc.returncode} and no request was captured")
                outcomes.append({"index": int(index), "ok": False,
                                 "reason": "no request captured",
                                 "returncode": proc.returncode})
            else:
                outcomes.append({"index": int(index), "ok": True,
                                 "returncode": proc.returncode})
    return outcomes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("code_dir")
    parser.add_argument("--node", default="node")
    parser.add_argument("--test-data-file", default=None)
    args = parser.parse_args()

    os.chdir(REPO_ROOT)          # capture_shim.js is read by a repo-relative path
    outcomes = execute_control(os.path.abspath(args.code_dir), args.node,
                               args.test_data_file)
    captured = sum(1 for o in outcomes if o["ok"])
    print(f"executed {len(outcomes)} answer(s); captured a request for {captured}")
    for outcome in outcomes:
        if not outcome["ok"]:
            print(f"  FAIL {outcome['index']}: {outcome['reason']}")


if __name__ == "__main__":
    main()
