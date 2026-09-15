# SPLIT condition — runner contract

Third condition for the SDK+repair comparison. Same 68 sampled tasks, same five-operation
whitelists, same capture/coercion/scoring path as the treatment (`estimate/`) and the
control (`control/`). It exists to answer one question the treatment could not:

> Does the typed SDK help, when the agent is given the API's **type surface** rather than a
> single module that is 52% generic fetch runtime?

## What differs from the treatment, and only this

| | treatment (`estimate/`) | split (this condition) |
|---|---|---|
| generator flags | `--output-mode single --runtime inline` | `--output-mode split --runtime module` |
| what the agent reads | `client.ts`, one 2,956-line module: API types **and** the whole fetch runtime | `client.ts` (operation signatures + argument types) and `client.schemas.ts` (data model) |
| prompt | `estimate/emit_prompt.py` | `split/emit_prompt_split.py` — the treatment's prompt with ONE section replaced and one rule added |
| repair budget | tsc loop, up to 3 rounds | **unchanged**, up to 3 rounds |
| sample, whitelists, starter, scoring | — | **all unchanged** |

The prompts are generated from the treatment's own emitter (`emit_prompt.py` imported
read-only), so the task text, the starter code, the artifact path and rules 1-6 are
identical by construction. `diff` on a rendered pair shows exactly two differences: the
`## The typed client` section, and the added rule 7.

## Two build variants

Both build 68/68. Choose one before the run; do not mix.

**Variant A — `--spec-source filter-in`** (default). Mirrors the treatment's input exactly:
full spec plus the arm's `filter-in` decorator. `filter-in` prunes *operations* only, so
generate-client's type emitter still walks the whole `components.schemas` block and
`client.schemas.ts` comes out as the API's **entire** data model — byte-identical across
every task of an API (34 types on google_calendar_v3, where the five operations reach 2).

**Variant B — `--spec-source control-pruned`** (recommended). Feeds generate-client the
control's already-`$ref`-pruned five-operation document. `client.ts` is **byte-identical**
to variant A (the operation signatures do not change); `client.schemas.ts` drops to only
the reachable schemas. This is the only way to make the two conditions see the same
five-operation closure, because the control already prunes components to the transitive
`$ref` closure and calls that out as a deliberate asymmetry in its own favour
(`control/build_specs.py`).

## Design decisions, stated rather than assumed

**The tsc repair loop stays ON, at 3 rounds.** The treatment had 3 rounds; the control had
none, because plain `fetch` has no types to check. Keeping 3 rounds makes
split-vs-treatment a clean single-variable comparison — the only thing that changes is how
the generated code is laid out across files. It does mean split-vs-control still carries the
repair-budget asymmetry, but that asymmetry is inherited from the treatment and is not what
this condition tests. Turning the loop off here would confound the one comparison this
condition is for.

**The agent is instructed not to read `runtime/`, not prevented.** Rule 7 names the two
files it may read and requires it to report every file it read. This is enforcement of the
same kind as rules 3, 4 and 5, which are also instructions — the condition has no sandbox.
Two things make it checkable rather than merely hoped for: the agent self-reports its
reading list, and the generator agents' transcripts can be audited afterwards for reads
under `runtime/`, the way `estimate/blinding_check.py` audits the treatment. **Report the
audit result alongside the score**; a task whose agent opened `runtime/create-client.ts`
has not run this condition. Note that `client.ts` is NOT forbidden — under split layout it
is the types file and the agent must read it. What is forbidden is the generic transport.

## Asymmetries this condition removes, and does not

Removes:
* the ~52% of the treatment's reading material that was generic fetch runtime, unrelated to
  the API under test;
* (variant B only) the unfiltered data model, so both conditions see the same five-operation
  `$ref` closure.

Does **not** remove:
* the retrieval advantage — the five-operation whitelist is guaranteed to contain the ground
  truth, so endpoint choice is 1-of-5 rather than 1-of-N, and on tasks where the stand-in
  retriever missed, the ground truth was substituted in. Same as treatment and control;
* the repair-budget asymmetry against the **control** (3 rounds vs none);
* the language asymmetry — TypeScript against a typed client vs. TypeScript against raw
  spec text. That is the intended contrast, not a defect;
* (variant A only) the whole-API data model in `client.schemas.ts`.

## Run order

```sh
REDOCLY_TELEMETRY=off python split/build_clients_split.py \
    --spec-source control-pruned --work-root split/work_pruned   # variant B
python split/build_manifest_split.py --work-root split/work_pruned
python split/emit_prompt_split.py --check-preamble-constant      # must print 1
# one fresh generator agent per manifest row; one attempt each
python estimate/score_driver.py --work-root split/work_pruned \
    --out split/results/results_split.json
```

`REDOCLY_TELEMETRY=off` is **not optional**: without it the CLI blocks on an unreachable
telemetry host and each client generation takes minutes instead of ~1 s.

Scoring reuses `estimate/score_driver.py` unchanged — it already parameterises
`--work-root` and `--out` — so the split condition is scored by the same code, with the
same statistics and the same `AUTH_DIAGNOSTIC`, as the treatment. No new scoring code.
