# `estimate/` — blinded agent-as-generator estimate of the typed-SDK arm

Scaffolding for a rough, blinded correctness estimate of the `sdk-repair` arm using a Claude
subagent as the code generator, so the arm's correctness can be bounded before a GPU is
booked for open-weight models.

**Nothing in here modifies the arm.** `wapiibench/sdk_repair_arm.py`,
`wapiibench/capture_shim.js` and `wapiibench/evaluation.py` are read-only from this
directory: they are imported (`harness_import.py`, `build_clients.py`, `score_driver.py`) or
read as text (`emit_prompt.harness_starter()` parses `evaluation.py`'s AST for the starter, so
it can never go stale against a concurrently edited file).

## Files

| File | Role |
|---|---|
| `sampling_frame.py` | computes n, allocates it across strata, draws the sample. Seed `20260902`. |
| `sample.json` | the sample: 78 `{api, index}` identifiers, **no task text, no ground truth**. |
| `sample_meta.json` | the size arithmetic, strata sizes and allocation, and the seed. |
| `retrieval_standin.py` | BM25 stand-in for the paper's embedding retriever; builds the five-operation whitelists. |
| `whitelists.json` | per task: 5 operationIds, whether the ground truth was retrieved or substituted, its rank. |
| `build_clients.py` | `filter_spec` -> `generate-client` -> `tsconfig` per task, via the arm's own functions. |
| `emit_prompt.py` | **the blinded emitter.** Task text + starter + client path + artifact path. Nothing else. |
| `blinding_check.py` | leakage scan over the emitted material against ground truth. |
| `blinding_report.json` | its output. |
| `RUNNER_CONTRACT.md` | the per-task protocol and the ready-to-use prompt. |
| `harness_import.py` | imports `evaluation.py` without a GPU stack (a no-op where torch is installed). |
| `score_driver.py` | execute -> compare -> analyze via the harness, then rate + CI. |
| `results/verdicts.json` | per-task verdicts and the aggregate. |
| `dryrun/` | the three hand-written placeholder artifacts and the dry-run verdicts. |
| `work/` | generated per task: `{api}/{index:04d}_client/` and `{api}/{index}_code.ts`. |

## Sample size

Target +/-10 percentage points, 95% confidence, worst case p = 0.5, on N = 395:

```
n0 = z^2 p(1-p) / e^2 = 1.959964^2 * 0.25 / 0.10^2 = 96.0365
n  = n0 / (1 + (n0-1)/N) = 96.0365 / (1 + 95.0365/395) = 96.0365 / 1.240599 = 77.4114
n  = 78                                     (ceil)
```

Proportional allocation, largest-remainder: asana 33, slack 35, google_calendar_v3 7,
google_sheet_v4 3. That buys +/-10pp on the **overall** rate only; the strata are far too
small for per-API claims.

## Order of operations

```
python estimate/sampling_frame.py                       # sample.json, sample_meta.json
python estimate/retrieval_standin.py                    # whitelists.json (+ retrieval accuracy)
python estimate/build_clients.py                        # work/{api}/{index:04d}_client/
python estimate/blinding_check.py                       # blinding_report.json (fails on a hard leak)
python estimate/emit_prompt.py <api> <index> --strict    # per task -> one fresh agent
                                                         # agent writes work/{api}/{index}_code.ts
python estimate/score_driver.py                          # results/verdicts.json
```

## Declared deviations

1. **Retriever.** The paper's `rag.retriever` needs `all-MiniLM-L6-v2` plus a
   `sentence_transformers` CrossEncoder from huggingface.co, which network policy blocks here.
   `retrieval_standin.py` is a pure-Python BM25 over each operation's path, method,
   operationId, tags, summary, description, parameter names and request-body property names.
   Measured on the 78-task sample: **top-1 0.795, top-5 0.987** against the paper's reported
   0.757 / 0.952. Do not read that as "as good as the paper's retriever" — see the report's
   threats section; these tasks were generated from these specs, so lexical overlap is
   inflated.
2. **Whitelist width 5, ground truth guaranteed.** One task per operation in this dataset
   means a one-operation client would give endpoint choice away. Five matches `num_chunks=5`.
   The ground-truth operation is always in the whitelist; on the **1 of 78** tasks where the
   stand-in missed it, it was substituted in and the 4 top distractors kept. Recorded per
   task in `whitelists.json`.
3. **`matchStrategy`.** `sdk_repair_arm.filter_spec()` hardcodes `matchStrategy: "all"`,
   which is satisfiable only for a one-element whitelist; with five operationIds
   `generate-client` silently emits `OPERATIONS = {}`. `build_clients._patch_match_strategy`
   rewrites that one key to `"any"` in the config `filter_spec()` just wrote. The arm needs
   the same one-word fix.
4. **Absolute `code_dir`.** `sdk_repair_arm._compile_to_js` runs `npx tsc <ts_file>` with
   `cwd` set to the client dir, so a relative path fails with TS6053 and every task scores
   EXECUTION_ERROR. `score_driver.py` always passes an absolute path.
5. **`Authorization`.** Every expected config carries `Authorization: Bearer <token>` and
   `Authorization` is not in `evaluation.SPECIAL_KEYS`, so a captured request without it
   scores `MISSING_KEY` and the task comes out `wrong`. The typed client only sends it if
   something calls `client.auth.bearer(...)`, and the current `sdk-invocation` starter does
   not. `score_driver.py` reports the harness verdict as the headline and a separate
   `DIAGNOSTIC_correctness_ignoring_auth_header`. The real fix belongs in the arm.
