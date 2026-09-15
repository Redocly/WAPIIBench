# `split/` — the types-only SDK condition

Third condition for the SDK+repair comparison, prompted by Roman Hotsiy's suggestion to run
the generator with `--output-mode split` so the agent reads a types file instead of a module
that is mostly fetch runtime.

**Read `RUNNER_CONTRACT.md` first.** The headline finding, before anything is run:

`--output-mode split` **alone does not do what it was expected to do.** It moves the
*data model* into `client.schemas.ts` and leaves the operation signatures **and the entire
inlined runtime** behind in `client.ts`. On `google_calendar_v3:0001` that file is still
16,079 pretokens, 83% of it generic runtime, and `client.schemas.ts` contains no operation
signature at all — no `Ops`, no `OPERATIONS`, no argument types. An agent restricted to
`client.schemas.ts` cannot know what to call.

`--runtime module` is the flag that removes the runtime. Pairing the two leaves `client.ts`
as operation signatures plus argument types — 2,800 pretokens on that task, against 25,420
for the treatment's monolith and 2,405 for the control's OpenAPI document.

Neither flag needs a change to `wapiibench/sdk_repair_arm.py`: `generate_client()` already
takes `output_mode` and `runtime` keyword arguments and forwards them to the CLI. Both are
accepted by the pinned `@redocly/cli` 2.51.0.

| file | role |
|---|---|
| `build_clients_split.py` | per-task client generation, `--output-mode split --runtime module`; `--spec-source` picks variant A or B |
| `emit_prompt_split.py` | the prompt; the treatment's emitter with one section replaced. `--check-preamble-constant` asserts the preamble is task-neutral |
| `build_manifest_split.py` | renders the 68 prompts and the manifest, from the treatment's own sample draw |
| `token_sizes_split.py` | sizes, measured with `control/token_sizes.py`'s own function so the numbers are comparable |
| `RUNNER_CONTRACT.md` | what differs, the two design decisions, the asymmetries |

Scoring is `estimate/score_driver.py --work-root split/work_pruned`. There is no scoring
code in this directory, by design.
