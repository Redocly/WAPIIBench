# Per-task runner contract — blinded raw-OpenAPI CONTROL condition

One generator agent, one task, one attempt. This file is the protocol; nothing about a task
is left to the runner's judgement. It is written for the operator (whoever launches the
subagents) and for the generator agent itself, whose entire input is section 4.

It mirrors `estimate/RUNNER_CONTRACT.md` section for section on purpose, so the two
conditions can be compared at a glance. Where a section here differs from the treatment's,
the difference is the condition — or, in section 2, an asymmetry that is disclosed rather
than smoothed over.

The operative sample is the **same 68 tasks** the treatment uses,
`estimate/sample_parseable.json` — slack 52, google_calendar_v3 11, google_sheet_v4 5, drawn
over the N = 228 parseable synthetic APIs (see `estimate/README.md`, "Why the population
changed"). Dispatch from `control/task_manifest_control.json`, whose `task_id`s are
byte-identical to `estimate/task_manifest.json`'s; score with
`score_driver_control.py --population 228`.

---

## 1. What one generator agent receives

Exactly the output of:

```
python control/emit_prompt_control.py <api> <index> --strict
```

which is the text reproduced verbatim in section 4 with `<api>`, `<index>`, the task
instruction and the two paths filled in. It receives:

| Given | Source |
|---|---|
| the natural-language task instruction | `data/synthetic/{api}/test_data_final.json[index]["task"]` — the `task` field ONLY |
| the starter code | `control/fetch_starter.py:STARTER` — a plain `fetch(` tail, no client, no imports |
| a path to a filtered API **description** | `control/work/{api}/{index:04d}_spec/filtered_spec.yaml` — the **five-operation** OpenAPI document `build_specs.py` renders from the same whitelist the treatment's client was generated from |
| the path its artifact must be written to | `control/work/{api}/{index}_code.js` |
| — | no working directory to compile in, no `tsc` command line: there is nothing to compile |

It receives **nothing else**. In particular it is not given, and must not obtain:
the dataset file or its path, the expected request, the full unfiltered spec under
`openapi/`, the operation whitelist as a list, a generated client, another task's prompt,
another task's artifact, this contract's section 5, or any aggregate result. (The task's
spec directory also holds `wapii_param_types.json`, the spec-declared parameter-type table
`capture_shim.js` coerces query values with. It is derived from the same filtered spec by
the arm's own `sdk_repair_arm.write_param_types()`, so reading it reveals nothing
`filtered_spec.yaml` does not — it is the control's counterpart of `_surface.txt` sitting in
the treatment's client directory.)

The three substitutions against the treatment, and nothing else, are:

| treatment | control |
|---|---|
| a generated typed TypeScript client, read as `client.ts` | the filtered five-operation OpenAPI description, read as `filtered_spec.yaml` |
| `evaluation.SETUPS['sdk-invocation']` starter (imports `./client`, configures capture, `client.auth.bearer`) | `control/fetch_starter.py:STARTER` (a task comment, an optional `AUTH_HEADERS` constant, then `fetch(`) |
| answer is `{index}_code.ts`, type-checked | answer is `{index}_code.js`, a plain `fetch` call, **not** type-checked |

Both artifacts are delivered **by path**, not inlined into the prompt. Inlining the spec
while the treatment's client stayed a file would change the delivery channel as well as the
content and make the two prompts incomparable in length
(`control/emit_prompt_control.py`, "HOW THE SPEC IS DELIVERED").

### Why five operations, not one

For the treatment's reason, unchanged, and held fixed across the conditions. The synthetic
dataset has exactly one task per operation (slack 174 tasks / 174 operations, asana 167/167,
google_calendar_v3 37/37, google_sheet_v4 17/17; 393 distinct method+path pairs across 395
tasks). A one-operation description would hand the agent the endpoint, and the `url`/`method`
verdicts would be correct by construction. Five is the paper's retrieval width
(`num_chunks = 5`).

The five operationIds are **not** re-retrieved for the control: `build_specs.py` reads
`estimate/whitelists_parseable.json`, the treatment's own per-task whitelists, so the four
distractors are the same four operations in both conditions. Narrowing is the arm's
retrieval step, and the contrast under test is typed client vs. description text — nothing
else may move.

Operations are emitted in **spec order**, the order they appear in
`openapi/real_world_specs/{api}.yaml`, never in retriever-rank order: spec order is identical
for every task drawn from the same API, so it cannot carry task-specific information. This
is the same property that makes `generate-client`'s `OPERATIONS` order safe in the treatment,
and it is measured, not assumed — ground truth is first in the emitted order on **14 of 68**
tasks (20.6%, against the 20% chance baseline; `blinding_report_control.json`,
`manifest_verification_control.json`).

---

## 2. Attempts and the only permitted iteration

* **One attempt, and here that is the whole of it.** The agent writes `{index}_code.js` once.
  It does not draft alternatives, does not compare candidate operations by trial, and does
  not revise the call after writing it.
* **THERE IS NO REPAIR LOOP IN THE CONTROL, AND THE TREATMENT HAD THREE ROUNDS OF ONE. THIS
  IS AN ASYMMETRY THAT FAVOURS THE TREATMENT, AND ANY WRITE-UP THAT REPORTS A GAP BETWEEN THE
  TWO CONDITIONS MUST DISCLOSE IT.** A raw `fetch` call is untyped: there is nothing to
  type-check, no compiler to report an error, and therefore no error signal to repair
  against. The treatment gets `sdk_repair_arm.DEFAULT_MAX_RETRIES` = **3** rounds of `tsc`
  feedback and may edit its file to fix every error the compiler reports; the control writes
  its file blind and stops. Part of the treatment's measured advantage is therefore *the
  existence of a checker*, not the typed client's help in writing the call in the first
  place, and this design cannot separate the two. Do not describe the contrast as "typed SDK
  vs. OpenAPI description" without also saying "with a compile-repair loop vs. without one".
* **Nothing else counts as repair either.** Rewriting a call after re-reading the spec,
  switching operations on second thoughts, or "improving" argument values is out of contract
  and invalidates the task. Record it and report it rather than doing it quietly.
* **Do not execute.** The agent must not run the artifact, must not call a real API, and must
  not install, override or wrap `fetch` itself. Execution is the scoring driver's job
  (`control/score_driver_control.py` via `control/execute_fetch.py`), under the treatment's
  own capture shim plus the one-line global-`fetch` assignment in
  `control/capture_global_fetch.js`, which the answer must know nothing about.

There is no typecheck command. The treatment's section 2 spends its second half on `tsc`
flags, on why `*.d.ts` files are not optional, and on what a clean compile does and does not
prove. The control's counterpart is one sentence: **nothing checks a control answer before it
is scored.** The corresponding warning still applies in the other direction — a control
answer that *looks* well-formed has had no more validation than one that does not, so
plausibility is never evidence here either.

---

## 3. Blinding rules (binding on the agent)

1. **Never read `data/`** in the WAPIIBench repo — not the combined file, not a per-API file,
   not `data/generated/`, not `validation_data/`. The expected request lives there.
2. **Never read a spec** under `openapi/`. The filtered `filtered_spec.yaml` in the task's own
   spec directory is the only API description in scope; the full spec under
   `openapi/real_world_specs/` reintroduces every operation the retrieval step removed.
3. **Never read another task's** prompt, artifact, config, verdict or notes.
4. **Never search** for the answer: no grep for the endpoint, no web lookup of the API's
   docs, no reconstructing the expected URL from memory of the API.
5. **Do not read anything under `estimate/`.** That is the treatment's tree and it is
   operator-only from here: `whitelists_parseable.json` names the ground-truth operation
   outright, `blinding_report_parseable.json` and `manifest_verification.json` record per
   task whether the ground truth is first among the five, `work/{api}/{index:04d}_client/`
   is the other condition's material, and `results/` and `dryrun/` are outcomes.
6. **Do not read any of the operator-only files under `control/`.** The agent needs exactly
   two things: its own prompt file and its own `{index:04d}_spec/` directory. Off limits:
   * `verification.json` and `verify_work/` — **the sharpest hazard in this tree.** Both hold
     hand-written *correct* answers derived from the expected config, together with
     `expected_params`, for `slack #57` (which **is** one of the 68 sampled tasks) and
     `google_sheet_v4 #1`. Reading either hands over a finished answer.
   * `task_manifest_control.json` — another task's row is another task's material.
   * `blinding_report_control.json`, `manifest_verification_control.json` — per-task ordering,
     mention-asymmetry and leak findings, i.e. statements about where the answer sits.
   * `auth_parity.json` — per-task auth decisions across both conditions.
   * `spec_sizes.json`, `token_sizes.json` — per-task inventories of the other condition's
     artifacts.
   * `score_driver_smoke.json`, `results/` — outcomes.
   * `blinding_check_control.py`, `verify_manifest_control.py`, `verify_control.py`,
     `check_auth_parity.py` — the scoring-side scripts; they import
     `estimate/retrieval_standin.py`'s ground-truth reader and describe what a leak looks
     like.
   * `build_specs.py`, `build_manifest_control.py`, `emit_prompt_control.py`,
     `fetch_starter.py`, `execute_fetch.py`, `capture_global_fetch.js`, `token_sizes.py` —
     no ground truth in them, but no reason to read them either, and
     `execute_fetch.py` describes the capture wiring the answer must not touch.
   * section 5 of this file.
7. If the agent believes the task is ambiguous, it makes its best single choice and says so
   in its report. It does not ask for the answer, does not hedge by making two calls, and —
   since there is no repair round to fall back on — does not stall waiting for a checker.

The emitter is built so that rule 1 is not the only line of defence, by exactly the
treatment's mechanism: `control/emit_prompt_control.py`'s `_load_task_text()` reads the
dataset, keeps the `task` string, `del`s the parsed list and returns a `str`, so no
ground-truth field is in scope anywhere downstream, and the module never imports the only
ground-truth reader in the two trees (`estimate/retrieval_standin.resolve_ground_truth`).
`control/build_specs.py` and `control/fetch_starter.py` are gated on the spec and the
whitelist alone.

---

## 4. The prompt, ready to use

Everything between the rules below is what the generator agent gets.
`control/emit_prompt_control.py` renders it; `emit_prompt_control.render()` is the single
source of this text, so this file and the runner cannot drift.

**This block was generated, not transcribed.** It is the output of
`emit_prompt_control.render()` called with placeholder material, which is why `{api}`,
`{index}` and the two paths appear as placeholders while every fixed word is the emitter's
own. Reproduce it with:

```python
import sys; sys.path.insert(0, "control")
import emit_prompt_control as e
print(e.render({
    "api": "{api}", "index": "{index}",
    "task": "{the natural-language instruction, verbatim}",
    "artifact_path": "{work_root}/{api}/{index}_code.js",
    "starter_code": "// {the task instruction, as a comment}\n"
                    "const AUTH_HEADERS = { Authorization: 'Bearer <token>' };\n"
                    "fetch(",
    "spec_file": "{work_root}/{api}/{index:04d}_spec/filtered_spec.yaml",
}))
```

A real dispatch uses `python control/emit_prompt_control.py <api> <index> --strict`, which
differs from the block below only in those substituted values.

---

```
# WAPIIBench raw-OpenAPI task — {api} #{index}

## Task
{the natural-language instruction, verbatim}

## Your artifact
Write JavaScript to exactly this path (overwrite if it exists):
    {work_root}/{api}/{index}_code.js

## Starter code (begin your file with this, unchanged)
```javascript
// {the task instruction, as a comment}
const AUTH_HEADERS = { Authorization: 'Bearer <token>' };
fetch(
```

## The API description
The OpenAPI description of a FIVE-operation subset of this API is at:
    {work_root}/{api}/{index:04d}_spec/filtered_spec.yaml
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
   the path, and headers in `headers`. Use the `AUTH_HEADERS` constant
   from the starter for the `Authorization` header if the starter defines one.
6. Report only: the operation you chose.
```

---

The `const AUTH_HEADERS = ...` line is `control/fetch_starter.py:STARTER`'s `{auth_setup}`
placeholder, rendered by `emit_prompt_control._auth_setup()` from the **spec's**
`security` / `securitySchemes` — the treatment's line comes from
`sdk_repair_arm.auth_setup_for()` reading the generated client's own `OPERATIONS[...].security`
instead. It is absent for operations that declare no bearer security. The two rules were
checked against each other on all 68 sampled tasks and agreed on every one
(`control/check_auth_parity.py` -> `control/auth_parity.json`: 68/68, 0 disagreements).

---

## 5. The artifact, and what happens to it (operator only — not for the generator)

The artifact is a single JavaScript file at `control/work/{api}/{index}_code.js`. It must:

* begin with the starter, unchanged;
* issue **exactly one** request, as a single direct `fetch(...)` call;
* contain no generated client, no axios, no helper library, no wrapper function, and no
  `fetch` override or mock of its own;
* not be executed by the agent.

There is deliberately no "type-checks under the flags in section 2" line here. Nothing
validates a control artifact before scoring.

Scoring then runs, with exactly one stage replaced:

```
control.execute_fetch.execute_control(<abs path to control/work/{api}>, node,
                                      data/synthetic/{api}/test_data_final.json)
        -> prepends wapiibench/capture_shim.js (the TREATMENT'S FILE, unmodified),
           appends control/capture_global_fetch.js, wraps the answer in an async IIFE,
           runs it under node with cwd = {index:04d}_spec/, writes {index}_config.json
evaluation.compare(data/synthetic/{api}/test_data_final.json, <same dir>, api)   -> results.json
evaluation.analyze(<same dir>)                                                  -> statistics
```

`evaluation.execute()` cannot be used: its `.js` branch matches `axios\.[a-z]+\(`, so every
plain-`fetch` answer would score `ABSENT_REQUEST` without ever running. The two deliberate
differences between `execute_control()` and `sdk_repair_arm.execute_sdk_repair()` — the async
IIFE wrapper (a small advantage to the control) and counting a run that captures no request
as `EXECUTION_ERROR` rather than dropping it from the denominator (stricter on the control) —
are documented in `control/execute_fetch.py`'s docstring and in `control/README.md`.

`control/score_driver_control.py` does exactly that and then aggregates, importing
`wilson_interval`, `verdict_ignoring_auth` and `_auth_only_failure` from
`estimate/score_driver.py` rather than reimplementing them. Per-task verdict is
`results.json[index]["statistics"]["sample_verdict"]` — one of `correct`, `wrong`, `illegal`,
`nonexecutable` — the harness's own definition, not a reimplementation.

### Operator checklist per task

Dispatch is driven by `control/task_manifest_control.json` (built by
`control/build_manifest_control.py`, checked by `control/verify_manifest_control.py`). One row
per task; a row carries only the task id, api, dataset index, `spec_file`, `spec_dir`,
`prompt_file` and `answer_path`, so a row may be handed to a generator agent in full.

1. `python control/build_specs.py --only {api}:{index}` (once; writes the 5-operation
   `filtered_spec.yaml` and `wapii_param_types.json` into `{index:04d}_spec/`).
2. `python control/emit_prompt_control.py {api} {index} --strict` -> hand the output to one
   fresh agent.
3. Collect `control/work/{api}/{index}_code.js`; record the agent's reported operation. There
   is no repair-round count to record — that field exists only on the treatment's side, and
   its absence is the asymmetry in section 2.
4. Reject and re-run the task only for a **contract violation** (read `data/`, read
   `estimate/` or an operator-only file under `control/`, more than one request, used a
   client or axios, executed the code, revised the answer after writing it). Never re-run
   because the answer looked wrong — that is the measurement.
5. `python control/score_driver_control.py --apis {api} --population 228` when the batch is
   complete, then the whole run with
   `python control/score_driver_control.py --population 228`.
   The `--population` flag is the control's own default, but pass it explicitly: the
   treatment's `estimate/score_driver.py` still defaults to N = 395 and must be re-run with
   `--population 228` for the two intervals to be comparable.

### One agent per task

Each task gets a **fresh** agent with no memory of any other task. Two tasks on the same API
share nothing: not a conversation, not a scratch directory, not a summary of "how this API
works". Otherwise task *k* is scored on knowledge earned from tasks 1..*k*-1, which is not
what a single-shot completion does, and the sample stops being 68 independent draws. The rule
binds harder here than in the treatment: with no repair loop, a remembered spec detail from an
earlier task is the only feedback a control agent could accumulate, and it would accumulate
on one arm only.
