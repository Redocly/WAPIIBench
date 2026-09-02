# WAPIIBench — SDK+repair arm (`sdk-repair`)

A new generation arm that has the model write **TypeScript against a typed client** generated
from the API's OpenAPI spec by Redocly `generate-client`, then **repairs** the code against
`tsc` type errors over bounded retries until it type-checks. It is **model-agnostic** (plain
chat completions, no logit access), which is the contrast with WAPIIBench's constrained-
decoding (CD) arm — CD needs a HuggingFace `LogitsProcessor` and `ModelWrapper.run()` raises
`ValueError` for API models (`wapiibench/model_utils.py:108-109`), so CD cannot run on closed
models while `sdk-repair` can.

This document is the source of truth for the branch `sdk-repair-arm-rebased`. Line numbers
were verified against `redocly/wapiibench@main` (this fork's base). Everything about the CLI
was re-verified on **2026-09-02** against the **released** `@redocly/cli` **2.51.0** — PR #2885
merged on 2026-07-30 and the shipped command differs from the PR-branch spelling this arm was
originally written against. Sections that changed are marked **UPDATED 2026-09-02**.

## Files added / changed

| File | Change |
|------|--------|
| `wapiibench/sdk_repair_arm.py` | **new** — the arm: filter spec → generate-client → prompt → generate → tsc → repair loop → execute. Stdlib-only at import scope; heavy repo imports are lazy. |
| `wapiibench/capture_shim.js` | **new** — request capture at the `fetch` layer, injected AS the client's `fetch` option. Emits the same `{index}_config.json` shape as `mock.js`, plus a spec-driven coercion pass that puts the declared type back on captured query values (`coerceParamsFromSpecDeclaredTypes`) and an `_wapii_coercion` audit key. |
| `resources/sdk_code_generation_prompt.md` | **new** — SDK prompt template (the default one literally says "write a single call to Axios"). Adds `{{surface}}` and `{{error_feedback}}` placeholders. |
| `wapiibench/evaluation.py` | **edited** (4 additive edits) — `SETTINGS` += `'sdk-repair'`; `SETUPS` += `sdk-invocation`/`sdk-createclient`; early dispatch in `generate()`; `.ts` branch in `execute()`. |
| `wapiibench/sdk_repair_verify.py` | **new** — offline verification of the non-model half: hand-written ideal SDK answers pushed through `generate_client -> tsc -> capture -> compare -> analyze`, plus wrong-value, wrong-value-of-the-right-declared-type and invalid-value negative controls. No model involved. |
| `estimate/` | **new (a parallel agent's work, committed unchanged)** — the blinded agent-as-generator estimate: sampling frame, the BM25 retrieval stand-in and its precomputed whitelists, per-task client builder, blinded prompt emitter, leakage scan, scoring driver. `select_operation_ids` now reads `estimate/retrieval_standin.py` and `estimate/whitelists.json`. |

Scoring is **reused unchanged**: `compare()` / `_compare_configs()` / `_add_path_params()` /
`analyze()` / `analyze_all()` all consume the same `{index}_config.json`.

## How the request capture works (the load-bearing design decision)

The generated client's default `inline` runtime uses **web-standard `fetch`**, not axios, so
`mock.js` (axios-mock-adapter on the axios singleton) never fires. Instead of monkeypatching
global `fetch`, we inject a capture function **as the client's `fetch` option**. Verified in
released `@redocly/cli` 2.51.0, with paths and line numbers from the redocly-cli clone at HEAD `2566393` (the tree moved: there is no `packages/client-generator/src/runtime/` any more). The runtime is also inlined into the generated file, where the same expressions appear verbatim:

- `packages/client-generator/src/generators/typescript/runtime/send.ts:138`: `const doFetch = config.fetch ?? fetch;`
  then `doFetch(context.url, { ...fetchInit, method, headers, body: payload })`.
- `packages/client-generator/src/generators/typescript/runtime/types.ts:105`: `ClientConfig` has `fetch?: typeof fetch;`.
- The call shape received by our function: `url` is a string **with the query string already
  baked in**; `init.method` is a string; `init.headers` is a **plain `Record<string,string>`**
  (not a `Headers` instance); `init.body` is **already serialized** — a `JSON.stringify`'d
  string for JSON bodies (starts with `{`), or `URLSearchParams`/`FormData`/`Blob`/string.

`capture_shim.js` therefore normalizes exactly like `mock.js:6-88` (lowercase `method`, query
stripped into `params` with `''`→`true`, JSON/urlencoded/FormData body parsed into `data`,
headers → object) and returns a synthetic 200. It exposes the function as
`globalThis.__wapiiCaptureFetch` and the SDK starter wires it in with
`configure({ fetch: globalThis.__wapiiCaptureFetch })`. `configure()` sets it on the **default
client** used by both the flat free functions and the default `client` object, so a single
call routes every call style through capture. (A `createClient({ fetch })` variant starter is
also provided.)

`execute()` prepends `capture_shim.js` with the single `%s` placeholder replaced by the config
path — the exact analogue of `mock_code_template % config_log_file` (`evaluation.py`).

## Dependency: `redocly generate-client` (**UPDATED 2026-09-02** — merged and released)

PR #2885 merged **2026-07-30**. `generate-client` is now an **experimental** command in the
released CLI. Pinned for this arm:

```
npm install @redocly/cli@2.51.0 typescript@5.9.3 zod@4.5.4
```

`sdk_repair_arm.generate_client` shells out to:

```
redocly generate-client wapii_target \
    -o <client_dir>/client.ts \
    --config <client_dir>/redocly.yaml \
    --runtime inline --output-mode single \
    --generator typescript [--generator zod]
```

### What changed from the PR-branch spelling (this is what had to be fixed)

| PR-branch (what the arm was written against) | Released 2.51.0 | Consequence |
|---|---|---|
| `-o <directory>` | `-o <file>.ts` — **must end in `.ts`** | the arm now names the entrypoint itself; companion files land beside it (`--generator zod` -> `client.zod.ts`) |
| `--generators sdk zod` (variadic) | `--generator` **repeated once per generator** | `--generators` is not a flag any more |
| generator named `sdk` | generator named **`typescript`** | `--generator sdk` is not a valid generator name |
| flat free functions + `configure()` | operations live on the **`client` instance** (`client.<operationId>(args)`); `configure`/`use` are also re-exported (`export const { configure, use } = client;`) | the starter imports `client` so `client.auth.*` is reachable |
| `--runtime inline`, `--output-mode single` | unchanged (still the defaults) | passed explicitly anyway |

Other released-CLI facts the arm now depends on:

- **`filter-in` reaches the generator.** `generate-client` loads its input through `bundle()`,
  so a `filter-in` decorator on `operationId` in `redocly.yaml` applies with **no separate
  bundle step**. Verified: an Asana client generated with
  `filter-in: {property: operationId, value: [getAttachmentsForObject]}` contains exactly one
  entry in `OPERATIONS`.
- **`filter-in` prunes operations only.** Unreferenced `components.schemas` survive, so a
  one-operation Asana client is still ~165 kB of `client.ts` (207 exported types). Harmless
  for scoring, but the per-task `tsc` cost is set by the whole spec's type surface, not by the
  one operation — this dominates the arm's wall-clock time.
- **`REDOCLY_TELEMETRY=off` is mandatory for a benchmark run.** The CLI otherwise posts to
  `otel.cloud.redocly.com` on every invocation. Measured behind a proxy that denies the
  CONNECT: **~0.8 s per call with telemetry off vs. minutes per call with it on.** With one
  client generated per task this alone decides whether a 395-task run takes minutes or days.
  `generate_client()` sets it in the subprocess environment.
- **Method names are sanitized.** `.`, `-` and `/` in an `operationId` become `_`, so
  `calendar.calendars.insert` is `client.calendar_calendars_insert` and
  `users/get-by-username` is `client.users_get_by_username`.
- **Operations without an `operationId` cannot be filtered this way.** In this repo that is
  10 of the 28 real-world tasks (frankerfacez_v1, instagram, jsonplaceholder, npm_registry,
  telegram_bot_v5). Those need a different filter property.

## The zod question — **RE-VERIFIED 2026-09-02, and the PR-branch answer no longer holds**

The finding recorded on this branch (zod emits only standalone component schemas; nothing
validates the request; response-only validation in the example) was true of the PR branch and
is now **obsolete**. Read from the generated output of released `@redocly/cli` 2.51.0:

- `--generator zod` emits `<stem>.zod.ts` containing an **`operationSchemas` map keyed by
  operationId** with `{ request?: z.ZodType; response?: z.ZodType }` per operation, **plus a
  `zodValidation()` middleware factory** and a `ZodValidationError` class.
- The middleware is **inert until the consumer registers it**: `client.use(zodValidation())`.
  The generated client still validates nothing on its own.
- It uses `safeParse`. Defaults: **`request: true` → request validation THROWS**
  `ZodValidationError` *before any network call*; **`response: "warn"`** → response drift is
  reported through `onViolation` (default `console.warn`) and the call still succeeds.
- The arm now sets `USE_ZOD = True`, adds `--generator zod`, and the `sdk-invocation` starter
  registers `client.use(zodValidation())`. **Both defaults are kept**: requests fail loudly,
  responses only warn.

### The scope limit that matters (and that the arm's design did not anticipate)

`zodValidation().onRequest` validates **`context.body` and nothing else**:

```ts
onRequest(context) {
  if ((!request && !stripRequestBodies) || context.body === undefined) return;
  const schema = schemaIndex[context.operation.id]?.request;
  if (!schema) return;
  const result = schema.safeParse(context.body);
  if (!result.success) throw new ZodValidationError(/* ... */);
}
```

So, concretely:

- **Query, path, header and cookie parameter values are never validated by zod**, at any
  point. A wrong query value fails only at `tsc`, from the generated arg types.
- **Operations with no request body have no `request` entry at all.** Verified per operation:
  `getAttachmentsForObject`, `sheets.spreadsheets.get`, `admin_apps_approved_list`,
  `admin_apps_approve` and `users/get-by-username` all get `{ response }` only;
  `calendar.calendars.insert` and `createCustomField` (both `application/json`) get
  `{ request, response }`.
- **`application/x-www-form-urlencoded` bodies get no request schema either**, and the
  generated TypeScript types them as a bare `URLSearchParams` (e.g.
  `export type Admin_apps_approveBody = URLSearchParams;`) — no field names, no field types.
  In the synthetic dataset that is **90 of 395 tasks (22.8%)**, all Slack. For those tasks the
  typed-client premise of this arm gives the model *nothing*: no compile-time field checking
  and no runtime validation. 106 of 395 have `application/json` bodies and do get both.
- **Enums are emitted as TypeScript literal unions**, so `tsc` already rejects a bad enum
  value and zod is redundant there. The values zod catches that `tsc` cannot are the
  refinements TS cannot express — `z.number().int()`, string formats, `min`/`max` — or any
  value whose static type has been erased.

**Consequence for this arm:** zod is a *real* extra signal, but only on JSON request bodies,
and mostly only for refinement-level constraints. `tsc` remains the primary repair signal.

## How to run the arm

Install the toolchain (there is no dependency manifest in the repo; these are the exact
versions this branch was verified with):

```
npm install @redocly/cli@2.51.0 typescript@5.9.3 zod@4.5.4 axios axios-mock-adapter
pip install --upgrade 'transformers<5.0.0' 'tokenizers>=0.22.0,<=0.23.0' torch accelerate \
    numpy openapi3-parser pyyaml regex strenum tqdm \
    langchain langchain-huggingface langchain-chroma chromadb sentence-transformers
export REDOCLY_TELEMETRY=off      # generate_client() also sets this for its subprocess
```

`zod` is a real runtime dependency of the generated `*.zod.ts`, and the langchain/chromadb/
sentence-transformers group is **not optional** despite what the README says: `evaluation.py`
imports `rag.retriever` at module scope, so `import evaluation` fails without them even for a
run that uses no RAG. `pyyaml` is now load-bearing for the arm itself as well: both
`select_operation_ids` (through `estimate/retrieval_standin.py`) and
`param_types_from_spec()` read the spec with `yaml.safe_load` — lazily, so module-scope
imports stay stdlib-only.

Pair the SDK setup with the SDK setting:

```
python wapiibench/evaluation.py --settings sdk-repair --setups sdk-invocation \
    --models <model> --apis <api>
```

Note: because `SETUPS` now also contains `sdk-invocation`/`sdk-createclient`, a default
(no-`--setups`) run would try to pair them with every setting. Always scope `--setups` to the
SDK setups when `--settings sdk-repair`, and to `invocation`/`endpoint` otherwise. `generate()`
forces synchronous generation for this arm (the Batch API cannot carry the repair loop).

To verify the non-model half with **no model in the loop**:

```
PYTHONPATH=wapiibench python3 wapiibench/sdk_repair_verify.py \
    --redocly ./node_modules/.bin/redocly --node "$(command -v node)"
```

## Remaining blockers to a first run

1. ~~**Retrieval binding (`select_operation_ids`)**~~ — **DONE 2026-09-02, with a declared
   deviation.** `select_operation_ids(spec_file, task)` now returns a **five**-operation
   whitelist (five = the paper's RAG `num_chunks`; one operation would hand the model the
   endpoint, since the synthetic set has exactly one task per operation) and it is driven by
   the task's instruction TEXT only — never by `task['config']`, which is ground truth:
   filtering the client by the answer would hand the model the endpoint for free and
   invalidate the endpoint metrics. Two paths, in this order:
   * a **precomputed** whitelist from `estimate/whitelists.json` when one exists for the task
     (78-task stratified sample), matched on api + the task's position in its dataset file,
     joined on the task text — reused so a scored run reproduces the estimate's candidate
     sets exactly;
   * otherwise the retriever runs **live** and its top five is used.

   **DEVIATION FROM THE PAPER (do not report this as the paper's retriever).** The paper's
   `rag.retriever` uses `all-MiniLM-L6-v2` embeddings plus a `sentence_transformers`
   CrossEncoder reranker; those weights come from huggingface.co, which network policy blocks
   in this environment. `estimate/retrieval_standin.py` is a pure-Python **BM25** over each
   operation's path, method, operationId, tags, summary, description, parameter names and
   request-body property names. Measured on the 78-task sample: **top-1 0.795, top-5 0.987**
   vs. the paper's reported 0.757 / 0.952 — *inflated*, because these tasks were generated
   from these specs, so lexical overlap is unusually favourable to a lexical retriever.

   **The ground-truth guarantee, and where it stops.** `retrieval_standin.build_whitelists()`
   guarantees the ground-truth operation is present in a **precomputed** whitelist: where the
   stand-in's top five missed it (**1 of 78** tasks, slack #52, true rank 6) the ground truth
   was substituted in and the four best distractors kept. That behaviour is kept, and it is a
   *favourable bias* — on such a task the model gets a candidate set a real end-to-end
   pipeline would not have produced — so `select_operation_ids` logs every substitution at
   WARNING and `whitelists.json` records it per task (`ground_truth_substituted`). The **live**
   path cannot offer that guarantee, because checking it means reading the expected config; a
   live whitelist is the retriever's honest top five and may simply not contain the right
   operation, in which case the task is unsolvable and scores wrong. That asymmetry is logged
   too. Both facts are limitations of the estimate, stated here rather than hidden.
2. **Specs without `operationId`** — `filter-in` on `operationId` cannot select them
   (10 of 28 real-world tasks). Needs a different filter property for those APIs.
3. **API keys / GPU** — unchanged: OpenAI (GPT tiers), Google/OpenRouter (Gemini); Anthropic
   (Claude) still needs a new provider branch in `model_utils.py` (only `OpenAI`/
   `OpenRouter`/`HuggingFace` exist). Open-weight models and the CD head-to-head need the
   local HuggingFace path and a GPU.
4. **Real-world starter context** — `assemble_prompt` now drops `task['starter_code']` for
   this arm (it is axios-shaped JS and would otherwise override the SDK starter entirely, so
   no request could ever be captured). That means the SDK arm sees **less** surrounding
   context on the real-world dataset than the axios arms do. A fair head-to-head on
   `validation_data` needs that starter translated to SDK shape rather than dropped.

## Scoring: where the harness is unfair to SDK-shaped answers

`compare()`/`_compare_configs()`/`analyze()` are reused unchanged, which was the design goal.
Two consequences are **not** neutral between the axios arms and this one, and both were
measured on real captured output, not reasoned about:

### 1. Query parameter values arrive as strings while the expected values are typed — **FIXED 2026-09-02, spec-driven coercion in the shim**

`mock.js` reads `config.params` **off the axios config object**, and only folds the URL's
query string in when one is present. An axios answer therefore preserves the JavaScript type
the model wrote: `axios.get(url, { params: { limit: 50 } })` is logged as the **number** `50`.

A generated fetch client has no such object: it serializes every parameter into the URL, so
`capture_shim.js` could only recover **strings** — `"50"`. `_compare_configs` compares with
`actual_value == expected_value`, so a perfectly correct SDK answer scored
`Verdict.INCORRECT_VALUE`, and because `_analyze_sample` requires
`arguments_correct_value == arguments_all` for a `correct` sample verdict, **the whole task
scored wrong.**

Scope, counted over the datasets (per-API files, not the combined `all` file):

| dataset | tasks with a non-string query value | value types |
|---|---|---|
| synthetic (395) | **51 (12.9%)** | 43 int, 6 bool, 2 list, 1 float |
| real-world (28) | **2 (7.1%)** | 2 int (both `frankerfacez_v1 / page`) |

Non-string **`path_params`**: **0 of 395 and 0 of 28.** There is nothing to fix there, and
nothing is attempted — see "what is deliberately left alone" below.

#### The fix

The capture shim now puts the type back **using the type the OpenAPI description declares for
that parameter**:

* `sdk_repair_arm.param_types_from_spec(spec_file, operation_ids)` reads the spec's
  `parameters[].schema.type` (resolving local `$ref`s, merging path-level and
  operation-level parameters) and emits a table of declared query/path types plus each
  parameter's `style`/`explode`. Its only inputs are **a spec path and the operationId
  whitelist** — it cannot reach a task, let alone `task['config']`, and that is the point:
  coercing towards the *expected* value instead of towards the *spec* would reshape a wrong
  value into a matching one and the benchmark would be scoring itself.
* `write_param_types()` drops that table beside the generated client as
  `wapii_param_types.json`. It is written by `generate_sdk_repair()` at client-generation
  time, by `sdk_repair_verify.build_case_dir()`, and, as a fallback, by
  `execute_sdk_repair()` for a client built before this existed (when the task record names
  its API).
* `capture_shim.coerceParamsFromSpecDeclaredTypes()` runs after the existing mock.js-parity
  normalization. It matches the captured `method` + query-stripped URL against the table's
  `servers + path` templates (ties broken towards the most literal path, the same rule the
  Python side uses), then rewrites `config.params` per the declared types.

What it coerces, and nothing else:

| declared type | captured `"…"` becomes |
|---|---|
| `integer` | a number, only when the text matches `^[+-]?\d+$` and is a safe integer |
| `number` | a number, only when `Number()` of it is finite |
| `boolean` | `true` / `false`, for exactly the texts `true` and `false` |
| `array` | rebuilt from the **query string**, not from the params object — repeated keys for `explode` (the `form` default), else split on `,` / space / `\|` per `style` — then each item coerced by its declared item type |
| `string` | left exactly as captured |

**Left as the captured string, with the reason recorded** in the config's `_wapii_coercion`
block (`coerced` / `left_as_string` / `skipped`, a key outside `evaluation.FIELD_KEYS`, so
scoring ignores it):

* the parameter is not declared for the matched operation;
* the parameter has no `schema` at all (a `content`-negotiated parameter);
* the schema has no `type` keyword and no `oneOf`/`anyOf`/`allOf` whose branches agree on one
  type (a single type plus `null` **is** resolved, to that type);
* an OpenAPI 3.1 type union that is not "one type plus null";
* `type: object` — its wire form depends on `style`/`explode` in ways a captured string
  cannot be reversed through;
* the declared type is numeric or boolean but the captured text is not a literal of it
  (e.g. `limit=notanumber` stays `"notanumber"`);
* the value is already non-string because the mock.js empty-value rule turned `?flag` into
  boolean `true`;
* the captured method + URL matched no operation in the table, or the table is missing
  entirely (then **nothing** is coerced — the old all-strings behaviour, never a crash).

#### What is deliberately left alone

* **`evaluation.py` and the dataset.** The comparison still demands exact equality and the
  expected values keep their original types. The two rejected candidates —
  `str(expected) == str(actual)` in `_compare_configs`, or normalizing the dataset to strings
  — would rescore **every** arm, including the axios baselines the paper published.
* **Request bodies.** A JSON body is already parsed by `JSON.parse` and carries real types; a
  urlencoded body is string-typed on **both** sides of the comparison (mock.js parses it the
  same way for the axios arms), so coercing it would introduce a *new* asymmetry.
* **`path_params`.** `evaluation._add_path_params` re-derives that key by regexing the URL —
  for the expected config too — so both sides are strings by construction; writing a typed
  value there would be overwritten in the normal case and would compare against a string in
  the abnormal one. The declared path types and the values extracted from the URL *are*
  reported under `_wapii_coercion.path_values` with `applied: false`, so the omission is
  visible rather than silent.
* **The `X-Redocly-Client` header**, still suppressed by `clientHeader: false` in the starter
  (section 2 below), not by touching `SPECIAL_KEYS`.

#### Proven both ways (see the verification table below)

* The two known-good answers that used to fail now score `correct`:
  `google_sheet_v4 / sheets.spreadsheets.get` (`includeGridData` `"true"` -> `true`) and
  `slack / admin_apps_approved_list` (`limit` `"100"` -> `100`).
* Wrong values of the **right declared type** still score `wrong`: a wrong integer
  (`limit: 42` against an expected `100`), a flipped boolean (`includeGridData: false`
  against an expected `true`), and a wrong array item (`part: ['channel']` against an
  expected `snippet`). These are the new `wrong_typed_value` variants in
  `sdk_repair_verify.py`; if coercion ever turned one of them `correct` it would be hiding
  real errors, and the fix would be invalid.

### 2. The client's own header is scored as a model-authored argument

The generated client sends `X-Redocly-Client: redocly-client-generator` by default.
`SPECIAL_KEYS` covers only `Accept` and `Content-Type`, so that header reaches the
"parameters present that are not expected" loop in `_compare_configs` and is scored
`UNNECESSARY_KEY` or `ILLEGAL_KEY` — the latter forces `sample_verdict = 'illegal'` — on
**every task**. The arm suppresses it with `clientHeader: false` in the starter's
`configure()` call rather than touching `SPECIAL_KEYS`.

## Landmines that will cost you a day if you do not know them

All measured on 2026-09-02 in this repo, with `@redocly/cli` 2.51.0 and the Python stack
listed above.

### Specs that `parse_spec` cannot handle — this blocks SCORING, in every arm

`compare()` calls `openapi_utils.parse_spec` (prance `ResolvingParser`) on the API's spec, so
a spec that will not parse cannot be scored *at all* — this is not specific to `sdk-repair`.

| spec | `parse_spec` |
|---|---|
| `google_calendar_v3` | OK, 1.1 s |
| `slack` | OK, 5.1 s |
| `google_sheet_v4` | OK, 10.6 s |
| `etherscan_v1`, `frankerfacez_v1`, `google_maps_platform`, `instagram`, `jsonplaceholder`, `telegram_bot_v5`, `youtube_data_v3`, `zephyr_cloud_v2` | OK, 0.1–6.1 s |
| **`asana`** | **never completes** — ran >15 min and did not finish |
| **`github_v3`** | **ParserError** after 21 s: `Required list has not defined properties: ['encoding', 'conten…` |
| **`npm_registry`** | **ParserError** after 0.4 s: `Required list has not defined properties: ['upadted']` |

`asana` is **167 of the 395 synthetic tasks (42%)**, and `github_v3` is 8 of the 28 real-world
tasks. None of them can be scored until those specs are fixed or `parse_spec` is replaced.
Use `google_calendar_v3`, `google_sheet_v4` and `slack` for anything that has to produce
numbers today.

### `filter-in` and `matchStrategy` — a silent total-filter failure

`matchStrategy` MUST be `"any"` for an operationId whitelist. `"all"` requires the node to
match **every** listed value, which no single `operationId` can do for a whitelist longer than
one, so `filter-in` drops **every** operation — and `generate-client` still **exits 0 with no
warning**, emitting a full-size client whose `OPERATIONS = {}` and `Ops = Record<string,
never>`: nothing callable, no error anywhere. Measured on `google_calendar_v3` with a
five-operation whitelist:

| `matchStrategy` | operations in `OPERATIONS` | client.ts | exit |
|---|---|---|---|
| `any` | 5 (all of them) | 108 kB | 0 |
| `all` | **0** | 107,883 bytes | **0** |

A **one-element** whitelist happens to satisfy `"all"`, which is exactly why this hides: any
single-operation test passes and only multi-operation whitelists break. `filter_spec` now
emits `"any"`.

### `AUTH_SETUP_FROM_SPEC` — right for synthetic, unreliable for real-world

`sdk_repair_arm.AUTH_SETUP_FROM_SPEC` (default `True`) controls whether the starter carries
the `client.auth.bearer('<token>')` line derived from the operation's declared `security`.

- **Synthetic set: keep it True.** All 395 expected configs contain exactly
  `Authorization: "Bearer <token>"`, and `Authorization` is not in `SPECIAL_KEYS`, so without
  the line every task loses an argument and cannot score `correct`.
- **Real-world set: consider False.** It is wrong in *both* directions there, verified:
  `youtube_data_v3 / youtube.search.list` declares `Oauth2` (bearer) in its spec but the task
  authenticates via the `key` **query** parameter and expects **no** `Authorization` header —
  the line adds an unexpected header, and an `ILLEGAL_KEY` forces `sample_verdict = 'illegal'`;
  meanwhile `zephyr_cloud_v2 / createTestCycle` declares an `apiKey` scheme *named*
  `Authorization` (so the bearer-only rule emits nothing) yet its expected config **does**
  carry `Authorization: "Bearer <a real token>"` — `MISSING_KEY`.
  No rule that reads only the spec gets the real-world set right; real-world tokens are
  per-task values delivered through `definitions`, which is why the durable fix is translating
  each real-world task's own `starter_code` to SDK shape (blocker 4).

## Verification performed (2026-09-02) — no language model in the loop

Driver: `wapiibench/sdk_repair_verify.py`. For each case the ideal SDK invocation was
**hand-written** against the generated client and pushed through the arm's real
`generate_client -> tsc -> capture_shim -> compare -> analyze` path. Asana is absent because
its spec cannot be parsed for scoring (above).

**RE-RUN 2026-09-02 after the spec-driven coercion fix**, with the run before the fix kept for
comparison. The "before" column is the run recorded in the previous revision of this document
and reproduced byte-for-byte on the re-run; "after" is the same driver with
`coerceParamsFromSpecDeclaredTypes` active. Rows 15-17 are new negative controls added with
the fix (`wrong_typed_value`: a wrong value of the RIGHT declared type).

| # | API | operation | shape | variant | before | after |
|---|---|---|---|---|---|---|
| 1 | google_calendar_v3 | `calendar.calendars.insert` | POST, JSON body | correct | **correct** | **correct** |
| 2 | google_calendar_v3 | `calendar.calendars.insert` | POST, JSON body | wrong value | wrong | wrong |
| 3 | google_calendar_v3 | `calendar.calendars.get` | GET, path param | correct | **correct** | **correct** |
| 4 | google_sheet_v4 | `sheets.spreadsheets.create` | POST, JSON body | correct | **correct** | **correct** |
| 5 | google_sheet_v4 | `sheets.spreadsheets.create` | POST, JSON body | wrong value | wrong | wrong |
| 6 | google_sheet_v4 | `sheets.spreadsheets.create` | POST, JSON body | zod-invalid | nonexecutable | nonexecutable |
| 7 | google_sheet_v4 | `sheets.spreadsheets.get` | GET, path + bool query | correct | **wrong** (string bias) | **correct** ✅ |
| 8 | google_sheet_v4 | `sheets.spreadsheets.get` | GET, path + bool query | wrong value | wrong | wrong |
| 9 | slack | `admin_apps_approve` | POST, urlencoded body | correct | **correct** | **correct** |
| 10 | slack | `admin_apps_approve` | POST, urlencoded body | wrong value | wrong | wrong |
| 11 | slack | `admin_apps_approved_list` | GET, int query | correct | **wrong** (string bias) | **correct** ✅ |
| 12 | slack | `admin_apps_approved_list` | GET, int query | wrong value | wrong | wrong |
| 13 | youtube_data_v3 (REAL WORLD) | `youtube.search.list` | GET, query + injected variable | correct | **correct** | **correct** |
| 14 | youtube_data_v3 (REAL WORLD) | `youtube.search.list` | GET, query + injected variable | wrong value | wrong | wrong |
| 15 | google_sheet_v4 | `sheets.spreadsheets.get` | GET, bool query | **wrong value, right type** (`false` vs expected `true`) | (new) | wrong ✅ |
| 16 | slack | `admin_apps_approved_list` | GET, int query | **wrong value, right type** (`42` vs expected `100`) | (new) | wrong ✅ |
| 17 | youtube_data_v3 (REAL WORLD) | `youtube.search.list` | GET, array query | **wrong item, right type** (`['channel']` vs expected `snippet`) | (new) | wrong ✅ |

Captured values after the fix, for the rows that moved:

| # | captured `params` | `_wapii_coercion.coerced` | expected |
|---|---|---|---|
| 7 | `{"includeGridData": true}` | `includeGridData` boolean | `true` |
| 11 | `{"limit": 100, "team_id": "T12345678"}` | `limit` integer | `100` |
| 15 | `{"includeGridData": false}` | `includeGridData` boolean | `true` -> `INCORRECT_VALUE` |
| 16 | `{"limit": 42, "team_id": "T12345678"}` | `limit` integer | `100` -> `INCORRECT_VALUE` |
| 17 | `{"part": ["channel"], …}` | `part` array of string | `"snippet"` -> `INCORRECT_VALUE` |

Item by item:

- **Filtered client contains only the whitelisted operation** — 17/17. Every generated
  `OPERATIONS` map held exactly the one whitelisted operationId (the driver whitelists one
  operation per case; the five-operation whitelist that a real run uses is exercised
  separately, below).
- **Compiles under strict `tsc`** — 17/17 clean, including the zod-invalid case (which is the
  point: `rowCount: 10.5` is a valid `number` to TypeScript).
- **Shim captures the request and writes the config file** — 16/16 that reached the network
  layer. The 17th is the zod-invalid case, which correctly never got there.
- **Comparison scores a hand-written correct answer as correct** — **7 of 7 after the fix**
  (5 of 7 before it). The two that used to fail did so purely on the string bias:
  - #7 captured `params.includeGridData = "true"` where `true` (boolean) was expected;
  - #11 captured `params.limit = "100"` where `100` (integer) was expected.
  Everything else about both requests already matched — url, method, headers, other params.
  Both now capture the coerced type and score `correct`.
- **A deliberately wrong parameter value still scores incorrect** — 6/6 `wrong_value` variants
  scored `wrong` (never `correct`, never `illegal`), unchanged by the fix.
- **A wrong value of the RIGHT declared type still scores incorrect** — 3/3 (#15-17): a wrong
  integer, a flipped boolean and a wrong array item, each with `INCORRECT_VALUE` on exactly
  the coerced parameter and `CORRECT` on the rest. This is the control that matters: coercion
  restores the *type* and never the *correctness*. Had any of these turned `correct`, the fix
  would be hiding real errors and would have to be reverted.
- **No other verdict moved.** Of the 14 pre-existing case-variants, exactly the two named
  above changed, both from `wrong` to `correct`; the other 12 are identical before and after,
  including the real-world case whose `part` value is now captured as `["snippet"]` and still
  compares equal to the expected `"snippet"` (`_compare_configs` unwraps single-element lists
  on both sides for non-`data` fields).
- **An invalid value is rejected by zod request validation** — yes, #6. Both the valid and the
  invalid body typecheck cleanly; the invalid one throws before any network call:
  ```
  ZodValidationError: Request validation failed for operation "sheets.spreadsheets.create":
    sheets.0.properties.gridProperties.rowCount: Invalid input: expected int,
    received number (received 10.5)
  ```
  node exits 1, no config file is written, and the sample scores `nonexecutable`
  (`error_verdict: runtime_error`). NOTE: a zod rejection is therefore **indistinguishable in
  the results from any other runtime error** — the arm's own OPEN DECISION about a dedicated
  verdict applies here.

### Operation selection, end to end, without the expected config

`select_operation_ids` is bound (blocker 1 above), so the whole selection -> client ->
answer -> score path was run without any ground truth on the selection side. Two tasks,
neither in the 78-task precomputed sample, so both went through the **live** BM25 path:

| task | retrieved whitelist (5) | operations in the generated client | captured | verdict |
|---|---|---|---|---|
| `slack[1]` | `admin_apps_approved_list`, `admin_inviteRequests_approved_list`, `admin_apps_restricted_list`, `admin_apps_requests_list`, `admin_apps_approve` | all 5 | `limit` coerced to integer `100` | **correct** |
| `google_sheet_v4[1]` | `sheets.spreadsheets.get`, `sheets.spreadsheets.getByDataFilter`, `sheets.spreadsheets.batchUpdate`, `sheets.spreadsheets.values.batchGetByDataFilter`, `sheets.spreadsheets.values.batchClearByDataFilter` | all 5 | `includeGridData` coerced to boolean `true` | **correct** |

The whitelist came from `filter_spec()`'s default path (`operation_ids=None` ->
`select_operation_ids(spec_file, task['task'])`), the ideal answer was hand-written against
the resulting **five**-operation client, and scoring ran through the harness unchanged. This
also confirms the `matchStrategy: "any"` fix on a real multi-operation whitelist: all five
operations reached `OPERATIONS`. The precomputed path was exercised separately
(`google_calendar_v3[2]` -> the whitelist in `estimate/whitelists.json`; `slack[52]` -> the
one substituted whitelist, which logs its WARNING as designed).

**Still no language model.** Selection, generation of the client and scoring all run; the
*answer* was hand-written in both rows above.

### The typecheck-repair loop

Driven by a stub model that replays canned completions (`sdk_repair_verify.StubModel`), on
`calendar.calendars.insert` with a bad method name plus a wrong-typed argument:

- `run_tsc` returns `TS2551: Property 'calendar_calendars_insertt' does not exist on type
  'Client<Ops, ...>'. Did you mean 'calendar_calendars_insert'?` — tsc's spelling suggestion
  makes this a strong repair signal.
- **Repairs:** stub returns the fix on its second completion -> 2 model calls, `attempts=1`,
  0 final diagnostics, typechecks.
- **Exhausts the budget as designed:** stub never fixes it -> 4 model calls (attempt 0 plus
  `max_retries=3`), `attempts=3`, returns the final diagnostics with the non-typechecking code.
- **Only the first-order error is reported.** The wrong-typed argument produced no separate
  diagnostic, because tsc stops at the unresolved property. A candidate with two independent
  faults therefore needs at least two repair rounds. Checked individually against
  `sheets.spreadsheets.get`, tsc does catch each fault on its own: wrong-typed argument
  `TS2322`, missing required argument `TS2345`, invented field `TS2353`, wrong method name
  `TS2551`.

### Bugs found and fixed while verifying (all pre-existing on this branch)

1. **`run_tsc`/`_compile_to_js` passed repo-root-relative paths while running `cwd=<client
   dir>`** -> `TS6053: File ... not found` for the candidate itself. Every task would have
   looked like a repair-loop failure. Absolutized.
2. **`execute_sdk_repair` passed a relative `config_log_file` into the shim**, which also runs
   with `cwd=<client dir>` -> `fs.writeFileSync` threw, its `catch` threw again on the same
   bad path, node exited non-zero, and **every** task landed `EXECUTION_ERROR` regardless of
   the answer. Absolutized.
3. **`capture_shim.js` wrote `ERROR: 'execution_error'`** but `Verdict` is a `strenum.StrEnum`
   with `auto()`, so the value is the member NAME: `'EXECUTION_ERROR'`. The lowercase form
   makes `_analyze_sample` fall through its verdict chain and
   `raise AssertionError(f"Unexpected verdict ...")`, crashing `analyze()` for the whole run.
   Casing corrected. (The claim in this document that it was lowercase was wrong.)
4. **`globalThis.__wapiiCaptureFetch` did not typecheck** — `TS7017: Element implicitly has an
   'any' type because type 'typeof globalThis' has no index signature`. The starter, i.e. the
   part the model is told not to change, failed `tsc` on its own. `write_tsconfig` now emits
   `wapii_globals.d.ts` and both tsc calls pass it.
5. **Real-world tasks' injected variables did not typecheck** — `definitions` are prepended as
   JS at execute time, but a TS candidate has to typecheck first, so `username`/`searchTerm`
   were `TS2304: Cannot find name`. `write_task_globals` now emits an ambient declaration per
   task.
6. **`instantiate_prompt` prefers `task['starter_code']` over `SETUPS[setup]`**, and every
   real-world task has one — axios-shaped JS. The SDK starter would have been discarded
   entirely, so the capture fetch would never be injected and no real-world request could
   ever be captured. `assemble_prompt` drops it for this arm (see blocker 4 for the cost).
7. **The starter was not guaranteed to be in the executed code** — the model had to reproduce
   it. `_prepend_starter` splices it in when the completion omits the `./client` import,
   mirroring `evaluation.py:270-288`.
8. **`filter-in` used `matchStrategy: "all"`** — see the landmine above.

### Not verified — be aware

- **No model has ever run through this arm.** `select_operation_ids` is now bound, so
  `generate_sdk_repair` has no blocking gap left, but every answer scored so far was
  hand-written or came from a canned stub; a real `ModelWrapper` call has never been made in
  this arm (that needs an API key or a GPU — blocker 3).
- **No Asana, `github_v3` or `npm_registry` task was scored**, because their specs do not
  parse. That is 42% of the synthetic set.
- **Only 17 case-variants across 4 APIs were scored**, plus the 2 end-to-end selection
  tasks — not a full run. A full run may surface more.
- **The coercion was exercised on integer, boolean and array-of-string parameters only.**
  `number` (float) and array-of-integer/boolean paths are implemented and unit-tested at the
  JS level but no dataset task in the verified set uses them; the 1 float and 2 list expected
  values in the synthetic set were not among the scored cases.
- **`estimate/build_clients.py` does not call `write_param_types`**, so clients already built
  under `estimate/work/` carry no type table. `execute_sdk_repair` writes one at execute time
  only when the task record names its API (`task['api']`), which the per-API synthetic files
  do not; rebuild those clients, or add the one-line call, before reading coercion-sensitive
  numbers out of the estimate. Uncoerced runs are not silently wrong — the captured config's
  `_wapii_coercion.skipped` says so — but they are the old 12.9% handicap.
- **The BM25 retrieval stand-in is not the paper's retriever** (blocker 1). Any number
  produced with it must carry that deviation, and the one ground-truth substitution in the
  precomputed whitelists is a favourable bias.
- **`--args-style` was never exercised.** `--help` documents `flat` as the default while
  `ClientConfig` documents `grouped`; the emitted `Ops` is grouped (`{ path, query, body }`)
  and the arm uses what is emitted, but the flag itself is untested.
