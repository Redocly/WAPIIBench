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
| `wapiibench/capture_shim.js` | **new** — request capture at the `fetch` layer, injected AS the client's `fetch` option. Emits the same `{index}_config.json` shape as `mock.js`. |
| `resources/sdk_code_generation_prompt.md` | **new** — SDK prompt template (the default one literally says "write a single call to Axios"). Adds `{{surface}}` and `{{error_feedback}}` placeholders. |
| `wapiibench/evaluation.py` | **edited** (4 additive edits) — `SETTINGS` += `'sdk-repair'`; `SETUPS` += `sdk-invocation`/`sdk-createclient`; early dispatch in `generate()`; `.ts` branch in `execute()`. |
| `wapiibench/sdk_repair_verify.py` | **new** — offline verification of the non-model half: hand-written ideal SDK answers pushed through `generate_client -> tsc -> capture -> compare -> analyze`, plus wrong-value and invalid-value negative controls. No model involved. |

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
run that uses no RAG.

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

1. **Retrieval binding (`select_operation_ids`)** — still `NotImplementedError`. The arm needs
   the whitelist of operationIds per task to come from `rag.retriever`, **not** from the
   task's expected config, which is ground truth: filtering the client by the answer would
   hand the model the endpoint for free and invalidate the endpoint metrics. This is the one
   remaining functional gap; `sdk_repair_verify.py` passes the whitelist explicitly so the
   rest of the pipeline can be exercised without it.
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

### 1. Query and path parameter values arrive as strings, and the expected values are typed

`mock.js` reads `config.params` **off the axios config object**, and only folds the URL's
query string in when one is present. An axios answer therefore preserves the JavaScript type
the model wrote: `axios.get(url, { params: { limit: 50 } })` is logged as the **number** `50`.

A generated fetch client has no such object: it serializes every parameter into the URL, so
`capture_shim.js` can only recover **strings** — `"50"`. `_compare_configs` compares with
`actual_value == expected_value`, so a perfectly correct SDK answer scores
`Verdict.INCORRECT_VALUE`, and because `_analyze_sample` requires
`arguments_correct_value == arguments_all` for a `correct` sample verdict, **the whole task
scores wrong.**

Scope, counted over the datasets:

| dataset | tasks with a non-string query/path value | value types |
|---|---|---|
| synthetic (395) | **51 (12.9%)** | 43 int, 6 bool, 2 list, 1 float |
| real-world (28) | **2 (7.1%)** | 2 int |

This is a **structural ~13% handicap on the synthetic set** that has nothing to do with model
quality. It is left unfixed here deliberately: the candidate fixes each have a cost, and the
choice belongs to whoever runs the comparison.
  * coercing captured strings back to numbers/booleans in `capture_shim.js` would make the SDK
    arm score a deliberately string-typed `"50"` as if the model had written `50`;
  * comparing loosely (`str(expected) == str(actual)`) in `_compare_configs` changes scoring
    for **every** arm, including all published results;
  * normalizing the dataset's expected values to strings likewise re-scores every arm.

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

| # | API | operation | shape | variant | verdict |
|---|---|---|---|---|---|
| 1 | google_calendar_v3 | `calendar.calendars.insert` | POST, JSON body | correct | **correct** |
| 2 | google_calendar_v3 | `calendar.calendars.insert` | POST, JSON body | wrong value | wrong |
| 3 | google_calendar_v3 | `calendar.calendars.get` | GET, path param | correct | **correct** |
| 4 | google_sheet_v4 | `sheets.spreadsheets.create` | POST, JSON body | correct | **correct** |
| 5 | google_sheet_v4 | `sheets.spreadsheets.create` | POST, JSON body | wrong value | wrong |
| 6 | google_sheet_v4 | `sheets.spreadsheets.create` | POST, JSON body | zod-invalid | nonexecutable |
| 7 | google_sheet_v4 | `sheets.spreadsheets.get` | GET, path + bool query | correct | **wrong — see below** |
| 8 | google_sheet_v4 | `sheets.spreadsheets.get` | GET, path + bool query | wrong value | wrong |
| 9 | slack | `admin_apps_approve` | POST, urlencoded body | correct | **correct** |
| 10 | slack | `admin_apps_approve` | POST, urlencoded body | wrong value | wrong |
| 11 | slack | `admin_apps_approved_list` | GET, int query | correct | **wrong — see below** |
| 12 | slack | `admin_apps_approved_list` | GET, int query | wrong value | wrong |
| 13 | youtube_data_v3 (REAL WORLD) | `youtube.search.list` | GET, query + injected variable | correct | **correct** |
| 14 | youtube_data_v3 (REAL WORLD) | `youtube.search.list` | GET, query + injected variable | wrong value | wrong |

Item by item:

- **Filtered client contains only the whitelisted operation** — 14/14. Every generated
  `OPERATIONS` map held exactly the one whitelisted operationId.
- **Compiles under strict `tsc`** — 14/14 clean, including the zod-invalid case (which is the
  point: `rowCount: 10.5` is a valid `number` to TypeScript).
- **Shim captures the request and writes the config file** — 13/13 that reached the network
  layer. The 14th is the zod-invalid case, which correctly never got there.
- **Comparison scores a hand-written correct answer as correct** — **5 of 7 correct answers
  scored `correct`; 2 scored `wrong`.** Both failures are the string-coercion bias, not the
  answer:
  - #7 captured `params.includeGridData = "true"`, expected `true` (boolean);
  - #11 captured `params.limit = "100"`, expected `100` (integer).
  Everything else about both requests matched — url, method, headers, other params. See
  "Scoring: where the harness is unfair to SDK-shaped answers" above; this is the 12.9% of
  synthetic tasks with a non-string query/path value, and it is a property of the harness, not
  of the arm or the model.
- **A deliberately wrong parameter value scores incorrect** — 7/7 wrong-value variants scored
  `wrong` (never `correct`, never `illegal`).
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

- **No model has ever run through this arm.** `select_operation_ids` is still
  `NotImplementedError`, so `generate_sdk_repair` cannot run end to end; everything above
  exercises the pipeline with hand-written answers and a stub model.
- **No Asana, `github_v3` or `npm_registry` task was scored**, because their specs do not
  parse. That is 42% of the synthetic set.
- **Only 14 case-variants across 4 APIs were scored**, not a full run, and the two failures
  found are the ones a small sample can find. A full run may surface more.
- **`--args-style` was never exercised.** `--help` documents `flat` as the default while
  `ClientConfig` documents `grouped`; the emitted `Ops` is grouped (`{ path, query, body }`)
  and the arm uses what is emitted, but the flag itself is untested.
