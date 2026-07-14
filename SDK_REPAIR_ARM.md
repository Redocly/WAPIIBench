# WAPIIBench — SDK+repair arm (`sdk-repair`)

A new generation arm that has the model write **TypeScript against a typed client** generated
from the API's OpenAPI spec by Redocly `generate-client`, then **repairs** the code against
`tsc` type errors over bounded retries until it type-checks. It is **model-agnostic** (plain
chat completions, no logit access), which is the contrast with WAPIIBench's constrained-
decoding (CD) arm — CD needs a HuggingFace `LogitsProcessor` and `ModelWrapper.run()` raises
`ValueError` for API models (`wapiibench/model_utils.py:108-109`), so CD cannot run on closed
models while `sdk-repair` can.

This document is the source of truth for the branch `sdk-repair-arm`. Line numbers were
verified against `redocly/wapiibench@main` (this fork's base) and the Redocly generate-client
sources at `redocly-cli@f5776cf` (PR #2885, branch `feat/ts-client-gen`).

## Files added / changed

| File | Change |
|------|--------|
| `wapiibench/sdk_repair_arm.py` | **new** — the arm: filter spec → generate-client → prompt → generate → tsc → repair loop → execute. Stdlib-only at import scope; heavy repo imports are lazy. |
| `wapiibench/capture_shim.js` | **new** — request capture at the `fetch` layer, injected AS the client's `fetch` option. Emits the same `{index}_config.json` shape as `mock.js`. |
| `resources/sdk_code_generation_prompt.md` | **new** — SDK prompt template (the default one literally says "write a single call to Axios"). Adds `{{surface}}` and `{{error_feedback}}` placeholders. |
| `wapiibench/evaluation.py` | **edited** (4 additive edits) — `SETTINGS` += `'sdk-repair'`; `SETUPS` += `sdk-invocation`/`sdk-createclient`; early dispatch in `generate()`; `.ts` branch in `execute()`. |

Scoring is **reused unchanged**: `compare()` / `_compare_configs()` / `_add_path_params()` /
`analyze()` / `analyze_all()` all consume the same `{index}_config.json`.

## How the request capture works (the load-bearing design decision)

The generated client's default `inline` runtime uses **web-standard `fetch`**, not axios, so
`mock.js` (axios-mock-adapter on the axios singleton) never fires. Instead of monkeypatching
global `fetch`, we inject a capture function **as the client's `fetch` option**. Verified in
`redocly-cli@f5776cf`:

- `packages/client-generator/src/runtime/send.ts`: `const doFetch = config.fetch ?? fetch;`
  then `doFetch(context.url, { ...fetchInit, method, headers, body: payload })`.
- `packages/client-generator/src/runtime/types.ts`: `ClientConfig` has `fetch?: typeof fetch`.
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

## Dependency: `redocly generate-client` (PR #2885 — NOT merged)

The client-generation step (`sdk_repair_arm.generate_client`) shells out to:

```
redocly generate-client <filtered-spec> -o <client_dir> \
    --runtime inline --output-mode single --generators sdk [zod]
```

`generate-client` is an **experimental** command added by **redocly-cli PR #2885**
(`Redocly/redocly-cli`, branch `feat/ts-client-gen`, head `f5776cf`), which is **open and not
merged** (`mergeable_state: blocked` as of 2026-07-14). It is not on any released `redocly`
build, so a first run must obtain the CLI from the branch. Options, in order of preference:

1. **Build the CLI from the branch** (monorepo):
   ```
   git clone https://github.com/Redocly/redocly-cli && cd redocly-cli
   git checkout feat/ts-client-gen
   npm ci && npm run build      # or: npm run pack / nx build cli
   npm link                      # exposes `redocly` on PATH with generate-client
   ```
2. **`npm link` / local install** of the built `@redocly/cli` package into the WAPIIBench
   toolchain so `npx redocly generate-client` resolves the branch build.
3. If/when PR #2885 merges and ships, pin the released `@redocly/cli` version instead.

Verified CLI/output facts used by the arm (from the PR body and sources):
- default `--runtime inline` → one self-contained file, zero runtime deps, web-standard fetch;
- default `--output-mode single`; default generator set is `sdk`;
- add-on generators via `--generators` (e.g. `zod`, `tanstack-query`, `swr`, `mock`);
- exports **both** call styles — the `client` instance (grouped args + `configure`/`use`/`auth`)
  and flat free functions — plus `createClient` for per-instance use.

`TODO(gen-client)` markers in `sdk_repair_arm.py` flag the few spots that need confirmation
against real generated output once the CLI is available: the exact entrypoint filename
(examples emit `client.ts`; `_normalize_client_entrypoint` writes a `client.ts` barrel if the
name differs) and the typed-surface extraction (`_extract_surface`, still a `NotImplementedError`
stub, as is `filter_spec`).

## The zod question — VERIFIED (does the client validate the request or the response?)

Read from the committed zod example on the PR branch,
`tests/e2e/generate-client/examples/zod/` (`redocly-cli@f5776cf`):

- **Opt-in, not default.** zod is an add-on generator. The default generator set is `sdk`
  (per the PR body). The example enables it explicitly in `redocly.yaml`:
  `client.generators: [sdk, zod]` (CLI equivalent: `--generators sdk zod`).
- **The generated SDK client does NOT validate anything at runtime.** `src/api/client.ts`
  imports nothing from the zod file and calls no schema; the runtime serializes the body
  (`JSON.stringify(value)`) and parses the response by content type — no validation of the
  **outbound request** or the **response**. (Confirmed also in `runtime/send.ts`: no schema
  calls anywhere in the send path.)
- **zod emits standalone, consumer-driven schemas.** `--generators zod` produces a separate
  `client.zod.ts` of **component/model** schemas (e.g. `MenuItemListSchema`, `OrderSchema` —
  no `Request`/`Response`/`Body` suffixes). Nothing wires them in automatically.
- **The shipped example validates the RESPONSE only.** `src/api/.../main.ts` does exactly one
  validation call: `const parsed = MenuItemListSchema.parse(response);` — i.e. it validates
  the data returned by `listMenuItems()` after the call, **not** the outbound request.

**Consequence for this arm:** zod provides **no automatic outbound-request validation** signal.
Using zod as an extra repair signal would require hand-writing a `SomeSchema.parse(<request
body>)` against the relevant component schema in the executed snippet. Because component
schemas are not request-specific, this is doable but is extra scaffolding. **`tsc` type errors
remain the primary repair signal** (`USE_ZOD = False` by default in `sdk_repair_arm.py`).

## How to run the arm (once the CLI + keys/GPU are available)

The arm is registered so the CLI accepts it. Pair the SDK setup with the SDK setting, e.g.:

```
python wapiibench/evaluation.py --settings sdk-repair --setups sdk-invocation \
    --models <model> --apis <api>
```

Note: because `SETUPS` now also contains `sdk-invocation`/`sdk-createclient`, a default
(no-`--setups`) run would try to pair them with every setting. Always scope `--setups` to the
SDK setups when `--settings sdk-repair`, and to `invocation`/`endpoint` otherwise. `generate()`
forces synchronous generation for this arm (the Batch API cannot carry the repair loop).

## Remaining blockers to a first run

1. **`generate-client` binary** — PR #2885 unmerged; build the CLI from `feat/ts-client-gen`
   (above). Until then `generate_client()` cannot run.
2. **Stub steps** — `filter_spec()` and `_extract_surface()` are `NotImplementedError` (marked
   `TODO(bind)` / `TODO(gen-client)`): wire them to `rag.retriever` + the generated surface.
3. **TS toolchain** — `typescript`/`tsc` (and `npx`) on PATH; the repo has no `package.json`,
   so add the toolchain deps. `write_tsconfig()` emits a `tsconfig` per task (`lib: [ES2020,
   DOM]` for fetch/Headers/Response types). Node 22 is available; `fetch`/`Response` are global.
4. **API keys** — OpenAI (GPT tiers), Google/OpenRouter (Gemini). **Anthropic (Claude) needs a
   new provider branch** in `model_utils.py` (only `OpenAI`/`OpenRouter`/`HuggingFace` exist).
5. **GPU** — open-weight models (Qwen2.5-Coder / CodeLlama / Granite) run via the local
   HuggingFace path; CD (the head-to-head) requires that local path too.

## Verification performed on this branch (no full eval was run)

- `capture_shim.js`: exercised under Node 22 with a simulated `send.ts`-shape call
  (`doFetch(url, {method, headers, body: JSON.stringify(...)})`) — output matches the `mock.js`
  config shape (lowercase method, query→params with `''`→`true`, JSON body parsed, header
  casing restored). `node --check` clean. Single `%s` placeholder confirmed (printf-substitution
  compatible, like `mock.js`).
- `sdk_repair_arm.py`: `import sdk_repair_arm` succeeds with only the standard library present
  (transformers/openapi_parser absent here); pure helpers (`_strip_code_fence`, `run_tsc`
  diagnostic regex, `format_errors_for_prompt`, SETUP rendering) exercised.
- `evaluation.py`: `py_compile` clean; edits are additive.
- SDK prompt template: renders under the same `.format(spec, api, extra_instructions,
  starter_code)` call `instantiate_prompt` makes, with `{surface}`/`{error_feedback}` surviving
  as literals for post-`.format()` substitution (so brace-containing tsc text can't crash it).

**Not run:** the full pipeline (no `generate-client` binary, no API keys, no GPU).
