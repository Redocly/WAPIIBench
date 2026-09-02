"""
sdk_repair_arm.py — the WAPIIBench "sdk-repair" generation arm.

This arm has the model write TypeScript against a *typed client* generated from the API's
OpenAPI spec by `redocly generate-client` (released @redocly/cli), then repairs the code
against `tsc` type errors until it type-checks (bounded retries). It is deliberately
*model-agnostic* — plain chat completions, no logit access — which is the contrast with
WAPIIBench's constrained-decoding (CD) arm, whose HuggingFace LogitsProcessor cannot run on
API models (model_utils.ModelWrapper.run raises ValueError at model_utils.py:108-109).

INTEGRATION CONTRACT (all line numbers verified against redocly/wapiibench@main, 2026-07):
  * generate()  dispatches here for setting == 'sdk-repair'          (evaluation.py:114)
  * execute()   dispatches here when the code dir holds *_code.ts    (evaluation.py:361)
  * SETTINGS gains 'sdk-repair'                                      (evaluation.py:90)
  * SETUPS gains the SDK_SETUPS starters                             (evaluation.py:74)
  * the same {index}_config.json shape is emitted by capture_shim.js, so compare()/
    _compare_configs()/analyze() are reused UNCHANGED.

IMPORT DISCIPLINE: only the standard library is imported at module scope, so
`import sdk_repair_arm` loads cleanly even where transformers/openapi_parser are absent.
Every binding to a heavy repo internal (ModelWrapper, instantiate_prompt, Verdict, the RAG
retriever) is imported lazily inside the function that needs it.

CLI STATUS (updated 2026-09-02): PR #2885 merged 2026-07-30. `generate-client` ships as an
experimental command in released `@redocly/cli` (pinned: 2.51.0). The flags CHANGED from the
PR-branch spelling this arm was written against — see generate_client() for the mapping. The
`config.fetch ?? fetch` capture seam SURVIVED unchanged, so capture_shim.js still works.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess

logging.basicConfig()
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# --------------------------------------------------------------------------------------- #
# Arm registration constants (the values that land in evaluation.py — see the README).
# --------------------------------------------------------------------------------------- #

SETTING_NAME = "sdk-repair"
# Chosen to avoid the SUBSTRING dispatch in generate() (evaluation.py:137-139):
#   use_spec = 'spec' in setting ; use_rag = 'rag' in setting ; use_cd = 'constrained' in setting
# "sdk-repair" contains none of 'spec' / 'rag' / 'constrained', so it triggers none of them.

# New SETUPS entries (evaluation.py:74). The existing 'invocation'/'endpoint' hard-code
# `const axios = require('axios'); axios.<method>(...)`, which is useless for a typed SDK.
# These starters instead (1) inject the capture function AS the client's fetch option and
# (2) leave the operation call for the model to complete.
#
# CAPTURE SEAM (re-verified against released @redocly/cli 2.51.0): the runtime resolves its
# fetch as `const doFetch = config.fetch ?? fetch;`
# (packages/client-generator/src/generators/typescript/runtime/send.ts:138) and ClientConfig has
# `fetch?: typeof fetch;` (packages/client-generator/src/generators/typescript/runtime/types.ts:105).
# (Paths/lines are from the redocly-cli clone at HEAD 2566393; the old
# packages/client-generator/src/runtime/ tree no longer exists.) `configure({ fetch })` sets it on the default
# client that BOTH the flat free functions AND the default `client` object use, so a single
# configure() call routes every call style through our capture. capture_shim.js exposes the
# function as `globalThis.__wapiiCaptureFetch` (it does NOT monkeypatch global fetch).
#
# The client module is imported from a FIXED relative path './client': generate_client()
# emits the client into the per-task work dir, alongside the candidate .ts, and normalizes
# the entrypoint to `client.ts` there (the observed generate-client examples already emit
# `client.ts`). Using a fixed path avoids threading a per-task specifier through
# instantiate_prompt's `.format()` (which fills only task/method/url — an unescaped
# `{client_import}` would raise KeyError). The `{{ ... }}` are escaped braces so that
# instantiate_prompt.format() renders them as literal JS `{ ... }`.
SDK_SETUPS = {
    "sdk-invocation": (
        "// {task}\n"
        "import {{ client }} from './client';\n"
        "import {{ zodValidation }} from './client.zod';\n"
        "client.configure({{ fetch: globalThis.__wapiiCaptureFetch, clientHeader: false }});\n"
        "client.use(zodValidation());\n"
        "{{auth_setup}}\n\n"
    ),
    # Alternative for per-instance code. `createClient` takes the same ClientConfig, so the
    # fetch/clientHeader options are identical; middleware is registered with `.use()`.
    "sdk-createclient": (
        "// {task}\n"
        "import {{ createClient, OPERATIONS }} from './client';\n"
        "import {{ zodValidation }} from './client.zod';\n"
        "const client = createClient(OPERATIONS, {{\n"
        "  fetch: globalThis.__wapiiCaptureFetch, clientHeader: false }});\n"
        "client.use(zodValidation());\n"
        "{{auth_setup}}\n\n"
    ),
}

# The `globalThis.__wapiiCaptureFetch` reference does not typecheck on its own: under
# `strict` (and even under `noImplicitAny` alone) tsc reports
#   TS7017: Element implicitly has an 'any' type because type 'typeof globalThis' has no
#           index signature
# so write_tsconfig() also emits this ambient declaration next to the candidate, and both
# run_tsc() and _compile_to_js() pass it to tsc.
GLOBALS_DTS_NAME = "wapii_globals.d.ts"
GLOBALS_DTS = """// Ambient declaration for the capture function that capture_shim.js installs on
// globalThis before the compiled candidate runs. See wapiibench/capture_shim.js.
declare global {
  // eslint-disable-next-line no-var
  var __wapiiCaptureFetch: typeof fetch;
}
export {};
"""

# The api alias the generated redocly.yaml registers the task's spec under. generate-client
# resolves a positional alias through the config's `apis:` block, which is what makes the
# `filter-in` decorator apply (generate-client loads its input via bundle()).
FILTER_API_ALIAS = "wapii_target"

# Prepended to the typed surface in the prompt. Everything here is a property of the
# generated client that is NOT visible in the `Ops`/`OPERATIONS` declarations we slice out.
CALL_CONVENTION = """// How to call this client (the setup lines above are already written for you):
//   client.<operationId>(args)         // `args` groups inputs by layer, see `Ops` below:
//                                      // { path?, query?, body?, headers?, cookies? }
//   client.auth.bearer('<token>')      // sets `Authorization: Bearer <token>`
//   client.auth.basic(user, password)  // sets `Authorization: Basic ...`
//   client.auth.apiKey(scheme, value)  // sets the apiKey scheme named in `security`
// An operationId that is not a valid identifier is exposed with `.`/`-`/`/` replaced by `_`
// (e.g. the operationId "calendar.calendars.insert" is the method
// `client.calendar_calendars_insert`). Each operation's `security` entry in OPERATIONS names
// the schemes it accepts."""

# Placeholder values the auth setup lines use. `<token>` matches what the SDK prompt template
# tells the model to use and what the datasets' expected configs contain
# (`Authorization: "Bearer <token>"`).
AUTH_TOKEN_PLACEHOLDER = "<token>"

# Whether the starter supplies the auth call derived from the spec. TRUE is right for the
# SYNTHETIC dataset, where all 395 expected configs carry exactly `Authorization:
# "Bearer <token>"`. It is UNRELIABLE IN BOTH DIRECTIONS on the real-world dataset, verified:
#   * youtube_data_v3 / youtube.search.list declares `Oauth2` (bearer) in its spec, but the
#     task authenticates with the `key` QUERY parameter and its expected headers contain no
#     `Authorization` at all -> the starter's line ADDS an unexpected header
#     (UNNECESSARY_KEY/ILLEGAL_KEY, and an ILLEGAL_KEY forces sample_verdict 'illegal');
#   * zephyr_cloud_v2 / createTestCycle declares an `apiKey` scheme *named* `Authorization`
#     in the header, so the bearer-only rule emits nothing, yet the expected config does
#     carry `Authorization: "Bearer <a real token>"` -> MISSING_KEY.
# There is no rule that reads only the SPEC and gets the real-world set right; the real fix is
# translating each real-world task's own starter_code to SDK shape (blocker 4 in
# SDK_REPAIR_ARM.md). Until then, set this False for real-world runs.
AUTH_SETUP_FROM_SPEC = True


def auth_setup_for(client_dir: str) -> str:
    """Emit the `client.auth.*` line(s) the task's operation actually needs, or "".

    WHY THIS IS IN THE STARTER AND NOT LEFT TO THE MODEL: every expected config in both
    datasets that needs credentials carries `Authorization: "Bearer <token>"`, and
    `Authorization` is NOT in evaluation.SPECIAL_KEYS (only `Accept` and `Content-Type` are),
    so it is compared strictly and a missing one costs the task its `correct` verdict
    (`_analyze_sample` needs arguments_correct_value == arguments_all). The axios arms get
    this for free because the model writes the header literally into the axios config; a typed
    client sends it only if something calls the client's auth helper. Leaving that to the
    model puts measured correctness near zero for a reason that is not about the model, so the
    arm supplies it as part of its contract.

    GATED ON THE SPEC, NOT ON THE ANSWER: the line is derived from the generated
    `OPERATIONS[...].security` entry, i.e. from the API description — never from the task's
    expected config. That distinction matters. Operations that declare no security get NO auth
    line, which is what keeps the arm from bolting an unexpected `Authorization` header onto
    the unauthenticated real-world endpoints (GitHub's public user endpoints, JSONPlaceholder,
    the npm registry): there, an extra header would be scored UNNECESSARY_KEY/ILLEGAL_KEY and
    an ILLEGAL_KEY forces `sample_verdict = 'illegal'`.

    Only the FIRST security alternative is used — the generated runtime applies the first
    fully-configured alternative, and the SDK prompt tells the model to prefer OAuth2.

    LIMIT, worth knowing before reading real-world numbers: the placeholder is `<token>`,
    which is exactly what all 395 synthetic tasks' expected configs contain
    (`Authorization: "Bearer <token>"`). The REAL-WORLD tasks instead expect a concrete
    per-task token that arrives through `definitions` (e.g. `ZEPHYR_API_KEY`), so there the
    starter's line is a safe default the model is expected to OVERRIDE with its own
    `client.auth.bearer(<that variable>)` — a later call replaces the credential.
    """
    if not AUTH_SETUP_FROM_SPEC:
        return ""
    with open(os.path.join(client_dir, "client.ts"), "r") as file:
        text = file.read()
    block = _extract_declaration(text, "export const OPERATIONS = ") or ""
    match = re.search(r"security:\s*\[\s*\[(.*?)\]\s*[,\]]", block, re.DOTALL)
    if not match:
        return ""

    # BEARER ONLY, deliberately. `bearer` (and `basic`) write the standard `Authorization`
    # header, which is what every credential-bearing expected config in both datasets
    # contains. An `apiKey` scheme instead places a NAMED PARAMETER (query/header/cookie), so
    # emitting a placeholder for it would FABRICATE an argument — scored UNNECESSARY_KEY or
    # ILLEGAL_KEY, or overwriting a value the model would have got right (e.g. YouTube's
    # `key` query parameter, whose expected value is a real key, not a placeholder). apiKey
    # and basic are left to the model; CALL_CONVENTION documents both helpers.
    for _scheme, kind in re.findall(
            r'\{\s*scheme:\s*"([^"]+)"\s*,\s*kind:\s*"([^"]+)"', match.group(1)):
        if kind == "bearer":
            return f"client.auth.bearer('{AUTH_TOKEN_PLACEHOLDER}');"
    return ""


DEFAULT_MAX_RETRIES = 3   # OPEN DECISION — repair budget (see EXPERIMENT_PLAN.md).
USE_ZOD = True            # zod REQUEST validation is on (see the note below).
# NOTE (verified against @redocly/cli 2.51.0 on 2026-09-02 — this REPLACES the PR-branch
# finding recorded in SDK_REPAIR_ARM.md, which no longer holds):
#   * `--generator zod` emits `<stem>.zod.ts` containing an `operationSchemas` map keyed by
#     operationId with `{ request?, response? }` zod schemas, PLUS a `zodValidation()`
#     middleware factory. Registering it (`use(zodValidation())`) is what activates it — the
#     generated client still validates nothing on its own.
#   * `zodValidation()` defaults: `request: true` -> request validation THROWS
#     (ZodValidationError) before any network call; `response: "warn"` -> response drift is
#     reported through `onViolation` (console.warn) and the call still succeeds. We keep both
#     defaults: requests must fail loudly, responses must not.
#   * SCOPE LIMIT (important, and NOT what the arm's design assumed): the middleware's
#     onRequest hook validates `context.body` ONLY. Query, path, header and cookie parameter
#     values are NOT validated by zod at any point, and operations without a request body
#     have no `request` entry in `operationSchemas` at all. So zod catches a bad *body field*
#     at runtime; a bad *query/path parameter value* is caught only by `tsc` at compile time
#     (the generated arg types), never at runtime.
# tsc type errors therefore remain the primary repair signal; zod adds a runtime signal for
# request BODIES only.


# --------------------------------------------------------------------------------------- #
# Step 1 — spec filtering
# --------------------------------------------------------------------------------------- #

def select_operation_ids(spec_file: str, task: str) -> list[str]:
    """Choose which operationId(s) the task's client should expose.

    THIS IS THE ARM'S RETRIEVAL STEP AND IS STILL UNBOUND. It must come from the RAG
    retriever (or another model-visible signal) — NOT from the task's expected config, which
    is ground truth and would leak the answer into the generated client.

    The offline verification harness passes `operation_ids` to filter_spec() explicitly, so
    the non-model half can be exercised without this binding.
    """
    # TODO(bind): reuse wapiibench.rag.retriever.Retriever to rank operations for `task`
    # and return the top-k operationIds.
    raise NotImplementedError(
        "TODO(bind): bind select_operation_ids to rag.retriever (must not read task['config'])")


def filter_spec(spec_file: str, api: str, task: str, out_path: str,
                operation_ids: list[str] | None = None) -> str:
    """Restrict the spec `generate-client` sees to `operation_ids`.

    Implemented as a generated Redocly config rather than a hand-pruned spec: the released
    `generate-client` loads its input through `bundle()`, so the `filter-in` decorator in
    `redocly.yaml` is applied before the generator runs and no separate `bundle` step is
    needed. `filter-in` on `operationId` drops every other operation from `paths`.

    CAVEATS, all verified against @redocly/cli 2.51.0:
      * `filter-in` prunes OPERATIONS ONLY. Unreferenced `components.schemas` survive, so the
        generated client still carries the API's whole type surface (e.g. Asana: 1 operation,
        ~165 kB of client.ts). Harmless for scoring, but it is not a small file.
      * It keys off `operationId`, so specs whose operations have no `operationId` cannot be
        filtered this way (in this repo: frankerfacez_v1, instagram, jsonplaceholder,
        npm_registry, telegram_bot_v5). Those need a different filter property.

    :param out_path: where to write the generated redocly config (a `.yaml` path).
    :return: path to the written config. The api alias inside it is FILTER_API_ALIAS.
    """
    if operation_ids is None:
        operation_ids = select_operation_ids(spec_file, task)
    if not operation_ids:
        raise ValueError(f"No operationIds selected for task {task!r} on api {api!r}")

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    config = {
        "apis": {
            FILTER_API_ALIAS: {
                "root": os.path.abspath(spec_file),
                "decorators": {
                    "filter-in": {
                        "property": "operationId",
                        "value": list(operation_ids),
                        # "any", NOT "all". `matchStrategy: all` requires the node to match
                        # EVERY listed value, which no single operationId can do for a
                        # whitelist of more than one -- filter-in then drops every operation
                        # and generate-client STILL EXITS 0, emitting a large client with
                        # `OPERATIONS = {}` and `Ops = Record<string, never>`: no callable
                        # operation and no error anywhere. A one-element whitelist happens to
                        # work under "all", which is exactly why this hid.
                        "matchStrategy": "any",
                    }
                },
            }
        }
    }
    # json is valid yaml, so we can emit the config without a yaml dependency at import time.
    with open(out_path, "w") as file:
        json.dump(config, file, indent=2)
    return out_path


# --------------------------------------------------------------------------------------- #
# Step 2 — typed client generation (released @redocly/cli `generate-client`, pinned 2.51.0)
# --------------------------------------------------------------------------------------- #

def generate_client(redocly_config: str, client_dir: str,
                    output_mode: str = "single", runtime: str = "inline",
                    redocly_bin: str | None = None) -> tuple[str, str]:
    """Run `redocly generate-client` on the filtered spec.

    FLAG MAPPING — released @redocly/cli 2.51.0 vs the PR-#2885 spelling this arm was
    originally written against (all confirmed from `redocly generate-client --help`):
      * `-o/--output` is an OUTPUT FILE PATH that MUST END IN `.ts`, not a directory. The
        companion files a generator emits land beside it (`--generator zod` writes
        `<stem>.zod.ts`), so we point it at `<client_dir>/client.ts` and get
        `client.ts` + `client.zod.ts` in `client_dir`.
      * `--generators sdk zod` (variadic, generator named `sdk`) is GONE. It is now
        `--generator`, repeated once per generator, and the TypeScript generator is named
        `typescript`, not `sdk`. `--generator sdk` is not a valid generator name.
      * `--runtime inline` / `--output-mode single` are unchanged and are the defaults; we
        still pass them explicitly so a future default change cannot move the arm.
      * the positional argument accepts an alias from the config's `apis:` block, which is
        how filter_spec()'s `filter-in` decorator reaches the generator.

    :param redocly_config: path to the config written by filter_spec().
    :return: (client_entry_path, surface_text).
    """
    os.makedirs(client_dir, exist_ok=True)
    entry = os.path.join(client_dir, "client.ts")
    cmd = [redocly_bin or "redocly", "generate-client", FILTER_API_ALIAS,
           "-o", entry,
           "--config", redocly_config,
           "--runtime", runtime, "--output-mode", output_mode]
    for generator in ["typescript"] + (["zod"] if USE_ZOD else []):
        cmd += ["--generator", generator]
    # REDOCLY_TELEMETRY=off is not optional for a benchmark run: the CLI otherwise posts to
    # otel.cloud.redocly.com on every invocation, and where that host is unreachable each
    # generate-client call blocks on the failing connection for tens of seconds (measured:
    # ~0.8 s per call with telemetry off vs. minutes per call with it on behind a proxy that
    # denies the CONNECT). With one client generated per task that alone decides whether a
    # 395-task run takes minutes or days.
    env = {**os.environ, "REDOCLY_TELEMETRY": "off"}
    subprocess.run(cmd, check=True, capture_output=True, text=True, env=env)

    entry = _normalize_client_entrypoint(client_dir)
    surface_text = _extract_surface(client_dir)
    return entry, surface_text


def _normalize_client_entrypoint(client_dir: str) -> str:
    """Ensure the generated client is importable as `./client` from `client_dir`.

    The observed generate-client examples emit `client.ts` directly (output-mode single). If
    a future/other layout emits a differently named entrypoint, write a `client.ts` barrel
    re-exporting it. TODO(gen-client): confirm the real entrypoint name once the CLI is built.
    """
    canonical = os.path.join(client_dir, "client.ts")
    if os.path.isfile(canonical):
        return canonical
    for name in ("index.ts",):
        candidate = os.path.join(client_dir, name)
        if os.path.isfile(candidate):
            with open(canonical, "w") as file:
                file.write(f"export * from './{os.path.splitext(name)[0]}';\n")
            return canonical
    ts_files = [f for f in os.listdir(client_dir)
                if f.endswith(".ts") and not f.endswith((".zod.ts", ".d.ts"))]
    if ts_files:
        stem = os.path.splitext(sorted(ts_files)[0])[0]
        with open(canonical, "w") as file:
            file.write(f"export * from './{stem}';\n")
        return canonical
    raise FileNotFoundError(f"No generated client entrypoint under {client_dir}")


def _extract_surface(client_dir: str, max_chars: int = 12000) -> str:
    """Collect the typed surface (operation signatures + arg/result types) for the prompt.

    Slices the generated entrypoint rather than re-deriving anything: the `Ops` type is the
    generator's own per-operation `{args, result}` map, and `OPERATIONS` is the wire
    descriptor (method + path + params). Together they are what a model needs to write the
    call. The type aliases `Ops` references are then pulled in one level deep so argument
    field names/types are visible. Bounded by `max_chars` to keep prompt cost predictable.
    """
    entry = os.path.join(client_dir, "client.ts")
    with open(entry, "r") as file:
        text = file.read()

    # `Ops`/`OPERATIONS` describe the operations but NOT how to CALL them, and in particular
    # not how to supply credentials. Without this preamble the model cannot produce the
    # `Authorization` header that 3 of the 4 synthetic APIs' expected configs require, so
    # every one of those tasks would lose an argument for a reason the model cannot see.
    blocks: list[str] = [CALL_CONVENTION]
    ops_block = _extract_declaration(text, "export type Ops = ")
    operations_block = _extract_declaration(text, "export const OPERATIONS = ")
    for block in (operations_block, ops_block):
        if block:
            blocks.append(block)

    # Pull in the type aliases Ops names (GetFooQuery, GetFooResult, ...), one level deep.
    referenced: list[str] = []
    if ops_block:
        for name in dict.fromkeys(re.findall(r"\b([A-Z][A-Za-z0-9_]*)\b", ops_block)):
            if name in ("Ops",):
                continue
            block = _extract_declaration(text, f"export type {name} = ")
            if block:
                referenced.append(block)
    blocks.extend(referenced)

    surface = "\n\n".join(blocks)
    if len(surface) > max_chars:
        surface = surface[:max_chars] + "\n// ... surface truncated ...\n"
    return surface


def _extract_declaration(text: str, prefix: str) -> str | None:
    """Return the single top-level declaration starting with `prefix`, brace-balanced.

    The generated client is one flat file with declarations at column 0, so a declaration
    runs from `prefix` to the line whose closing brace returns the depth to 0 (or, for a
    brace-free alias, to the terminating `;`).
    """
    if text.startswith(prefix):
        start = 0
    else:
        start = text.find("\n" + prefix)
        if start == -1:
            return None
        start += 1
    depth = 0
    for i in range(start, len(text)):
        char = text[i]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        elif char == ";" and depth == 0:
            return text[start:i + 1]
    return text[start:]


# --------------------------------------------------------------------------------------- #
# Step 3 — prompt assembly
# --------------------------------------------------------------------------------------- #

def assemble_prompt(prompt_template: str, task: dict, setup: str, setting: str,
                    surface_text: str, model_name: str,
                    error_feedback: str | None = None,
                    auth_setup: str = "") -> tuple[str, str]:
    """Instantiate the SDK prompt, reusing evaluation.instantiate_prompt for placeholder
    filling so the system/user split for OpenAI/OpenRouter chat models keeps working.

    The SDK prompt template adds a `{{surface}}` placeholder (the typed client surface) and,
    on repair iterations, an `{{error_feedback}}` placeholder (the tsc errors from the prior
    attempt). Both are ESCAPED (double-braced) in the template so that instantiate_prompt's
    `.format()` renders them to literal `{surface}` / `{error_feedback}`; we then substitute
    the real values here with str.replace(). This ordering matters: tsc error text and typed
    surfaces routinely contain `{` `}` (object/type literals) which would crash `.format()`
    if substituted before it. `{starter_code}` still carries the SDK_SETUPS starter with the
    capture fetch injected. Returns (full_prompt, starter_code).
    """
    from evaluation import instantiate_prompt  # lazy: pulls heavy deps only when run

    # instantiate_prompt prefers task['starter_code'] over SETUPS[setup] whenever the task
    # carries one (evaluation.instantiate_prompt). EVERY real-world task does, and that
    # starter_code is axios-shaped JS (`var axios = require('axios'); ... return axios.`).
    # Handing that to the SDK arm would silently discard the SDK starter, so the model would
    # never receive the capture-fetch injection and no request could ever be captured. Drop
    # it for this arm; the SDK starter is the only usable prologue here.
    #
    # COST OF THIS, stated plainly: the real-world tasks' surrounding source context (the
    # enclosing function and its parameters) is part of task['starter_code'], so the SDK arm
    # sees LESS context on the real-world dataset than the axios arms do. The injected
    # variables themselves survive (write_task_globals declares them for tsc and
    # evaluation._create_variable_definitions defines them at execute time), but a head-to-
    # head against the axios arms on validation_data is NOT apples-to-apples until the
    # starter_code is translated to the SDK shape rather than dropped.
    if "starter_code" in task:
        task = {key: value for key, value in task.items() if key != "starter_code"}

    # setting has no 'spec'/'rag' substring, so instantiate_prompt uses spec="".
    prompt, starter_code = instantiate_prompt(prompt_template, task, setup, setting,
                                              spec="", model_name=model_name)
    # Substitute AFTER .format(); values may contain literal braces.
    prompt = prompt.replace("{surface}", surface_text)
    prompt = prompt.replace("{error_feedback}", error_feedback or "")
    # `{auth_setup}` lives inside the starter, so it has to be substituted in BOTH the prompt
    # (where the starter is embedded) and the returned starter_code (which repair_loop splices
    # back into the candidate).
    prompt = prompt.replace("{auth_setup}", auth_setup)
    starter_code = starter_code.replace("{auth_setup}", auth_setup)
    return prompt, starter_code


# --------------------------------------------------------------------------------------- #
# Step 5 — tsc compile + error parsing
# --------------------------------------------------------------------------------------- #

# The compiler options shared by run_tsc() (repair signal) and _compile_to_js() (execute).
# They must match write_tsconfig(), because passing files explicitly makes tsc ignore
# tsconfig.json — keeping them in one constant is what stops the repair loop from type-
# checking under different rules than the code that actually runs.
_TSC_FLAGS = ("--strict", "--target", "ES2020", "--module", "CommonJS",
              "--moduleResolution", "node", "--esModuleInterop", "--skipLibCheck",
              "--lib", "ES2020,DOM")

# tsc diagnostic line, e.g. "0001_code.ts(12,5): error TS2345: Argument of type ..."
_TSC_ERROR_RE = re.compile(
    r"^(?P<file>.+?)\((?P<line>\d+),(?P<col>\d+)\):\s+error\s+(?P<code>TS\d+):\s+(?P<msg>.*)$")


def write_tsconfig(work_dir: str) -> str:
    """Emit a minimal tsconfig for tsc runs plus the ambient `__wapiiCaptureFetch`
    declaration the starter needs.

    `DOM` lib provides the fetch/Headers/Response types the generated inline runtime relies
    on. `GLOBALS_DTS` is required, not cosmetic: without it the starter's
    `globalThis.__wapiiCaptureFetch` fails with TS7017 under `strict`, which would make every
    candidate look like a repair-loop failure caused by the model.
    """
    cfg = {
        "compilerOptions": {
            "target": "ES2020", "module": "CommonJS", "moduleResolution": "node",
            "strict": True, "esModuleInterop": True, "skipLibCheck": True,
            "lib": ["ES2020", "DOM"],
        }
    }
    path = os.path.join(work_dir, "tsconfig.json")
    with open(path, "w") as file:
        json.dump(cfg, file, indent=2)
    with open(os.path.join(work_dir, GLOBALS_DTS_NAME), "w") as file:
        file.write(GLOBALS_DTS)
    return path


TASK_GLOBALS_DTS_NAME = "wapii_task_globals.d.ts"


def write_task_globals(work_dir: str, task: dict) -> str | None:
    """Declare the task's injected variables so a TypeScript candidate can reference them.

    WHY THIS IS NEEDED (a real gap between the axios arms and this one): real-world tasks
    carry a `definitions` dict, and execute() prepends it to the runnable script as JS
    (`const username = "5b086371";` — see evaluation._create_variable_definitions). The axios
    arms are raw JS, so that is enough. A TypeScript candidate, however, has to TYPECHECK
    before it ever runs, and at that point those names do not exist: referencing `username`
    fails with `TS2304: Cannot find name 'username'`. Without this file every real-world task
    would burn its whole repair budget on an error the model cannot fix.

    :return: path to the emitted declaration file, or None when the task injects nothing.
    """
    definitions = (task or {}).get("definitions")
    if not definitions:
        return None
    lines = ["// Ambient declarations for the variables evaluation._create_variable_definitions",
             "// prepends to the runnable script at execute time. Generated per task.",
             "declare global {"]
    for name, value in definitions.items():
        lines.append(f"  const {name}: {_dts_type(value)};")
    lines += ["}", "export {};", ""]
    path = os.path.join(work_dir, TASK_GLOBALS_DTS_NAME)
    with open(path, "w") as file:
        file.write("\n".join(lines))
    return path


def _dts_type(value: object) -> str:
    """Map a JSON definition value to a TypeScript type for the ambient declaration."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "unknown[]"
    if isinstance(value, dict):
        return "Record<string, unknown>"
    return "unknown"


def _dts_files(work_dir: str) -> list[str]:
    """Every ambient declaration file in `work_dir`.

    Both tsc invocations pass files explicitly (which makes tsc ignore tsconfig.json and thus
    its `include`), so the .d.ts files have to be listed on the command line or they are
    simply not part of the program.
    """
    return [os.path.join(work_dir, name)
            for name in sorted(os.listdir(work_dir)) if name.endswith(".d.ts")]


def run_tsc(ts_file: str, work_dir: str) -> list[dict]:
    """Type-check `ts_file` with --noEmit. Returns parsed diagnostics (empty == clean).

    Paths are absolutized because tsc runs with `cwd=work_dir` while callers hand us paths
    relative to the repository root — passing those through unchanged makes tsc report
    `TS6053: File ... not found` for the candidate itself, which the repair loop would read
    as an unfixable type error and burn the whole budget on.
    """
    ts_file = os.path.abspath(ts_file)
    work_dir = os.path.abspath(work_dir)
    # Explicit file arguments make tsc IGNORE tsconfig.json, so the compiler options that
    # write_tsconfig() records have to be repeated on the command line; the ambient globals
    # declaration must be passed too or the starter fails with TS7017.
    proc = subprocess.run(
        ["npx", "tsc", "--noEmit", "--pretty", "false", *_TSC_FLAGS,
         ts_file, *_dts_files(work_dir)],
        cwd=work_dir, capture_output=True, text=True)
    diagnostics = []
    for line in (proc.stdout + "\n" + proc.stderr).splitlines():
        match = _TSC_ERROR_RE.match(line.strip())
        if match:
            diagnostics.append(match.groupdict())
    return diagnostics


def format_errors_for_prompt(diagnostics: list[dict]) -> str:
    """Render diagnostics into compact feedback text for the next repair prompt."""
    lines = [f"{d['code']} (line {d['line']}): {d['msg']}" for d in diagnostics]
    return "The generated code did not type-check. Fix these errors:\n" + "\n".join(lines)


# --------------------------------------------------------------------------------------- #
# The arm entry points (mirror evaluation.generate()/execute()).
# --------------------------------------------------------------------------------------- #

def generate_sdk_repair(model_name: str, setup: str, setting: str, prompt_file: str,
                        test_data_file: str, output_dir: str, api: str | None = None,
                        num_outputs: int = 1, max_retries: int = DEFAULT_MAX_RETRIES,
                        **kwargs) -> None:
    """SDK+repair analogue of evaluation.generate() (evaluation.py:114).

    Per task: filter spec -> generate client -> generate TS -> repair-until-typechecks
    (bounded by max_retries) -> write {index:04d}_code.ts plus a {index:04d}_repair.json
    sidecar (retry count / typecheck status / final diagnostics / token totals) for the
    efficiency metrics in EXPERIMENT_PLAN.md.

    Batch API: the repair loop is multi-turn and synchronous, so this arm must run with
    openai_batch=False — OpenAI's async single-shot Batch API (model_utils.py:182-216) cannot
    carry a feedback loop. generate() forces this when it dispatches here (see the README).
    """
    from model_utils import ModelWrapper  # lazy heavy import
    from transformers import GenerationConfig

    os.makedirs(output_dir, exist_ok=True)
    with open(prompt_file, "r") as file:
        prompt_template = file.read()
    with open(test_data_file, "r") as file:
        test_data = json.load(file)

    model = ModelWrapper(model_name)
    generation_config = GenerationConfig(
        stop_strings="\n```\n",
        pad_token_id=model.tokenizer.eos_token_id if model.tokenizer is not None else None,
        **kwargs)

    for index, task in enumerate(test_data):
        task.setdefault("api", api)
        spec_file = os.path.join("openapi", "real_world_specs", f"{task['api']}.yaml")

        client_dir = os.path.join(output_dir, f"{index:04d}_client")
        redocly_config = filter_spec(spec_file, task["api"], task["task"],
                                     os.path.join(client_dir, "redocly.yaml"))
        _entry, surface = generate_client(redocly_config, client_dir)
        write_tsconfig(client_dir)
        write_task_globals(client_dir, task)
        auth_setup = auth_setup_for(client_dir)

        code, attempts, diagnostics, tokens = repair_loop(
            model, generation_config, prompt_template, task, setup, setting, surface,
            model_name, work_dir=client_dir, max_retries=max_retries,
            auth_setup=auth_setup)

        with open(os.path.join(output_dir, f"{index:04d}_code.ts"), "w") as file:
            file.write(code)
        with open(os.path.join(output_dir, f"{index:04d}_repair.json"), "w") as file:
            json.dump({
                "retries": attempts,
                "typechecks": len(diagnostics) == 0,
                "final_diagnostics": diagnostics,
                "tokens_total": tokens,
            }, file, indent=2)


def repair_loop(model, generation_config, prompt_template, task, setup, setting, surface,
                model_name, work_dir: str, max_retries: int, auth_setup: str = ""):
    """Generate -> tsc -> re-prompt until clean or budget hit.

    :return: (final_code, attempts_used, final_diagnostics, tokens_total)
    """
    error_feedback = None
    code = ""
    diagnostics: list[dict] = []
    tokens_total = None  # TODO(bind): sum ModelWrapper.run() usage per attempt if exposed.

    for attempt in range(max_retries + 1):        # attempt 0 is the first generation
        prompt, starter_code = assemble_prompt(
            prompt_template, task, setup, setting, surface, model_name, error_feedback,
            auth_setup=auth_setup)

        output_texts = model.run(prompt, generation_config=generation_config, batch=False)
        code = _prepend_starter(_strip_code_fence(output_texts[0]), starter_code)

        ts_file = os.path.join(work_dir, "candidate.ts")
        with open(ts_file, "w") as file:
            file.write(code)

        diagnostics = run_tsc(ts_file, work_dir)
        if not diagnostics:
            return code, attempt, [], tokens_total
        error_feedback = format_errors_for_prompt(diagnostics)

    # Budget exhausted. The final (non-typechecking) code is still handed to execute; it will
    # most likely land as an existing nonexecutable verdict via the error contract. OPEN
    # DECISION: a dedicated Verdict.REPAIR_BUDGET_EXHAUSTED vs folding into nonexecutable.
    return code, max_retries, diagnostics, tokens_total


def execute_sdk_repair(code_dir: str, node: str, test_data_file: str | None = None) -> None:
    """SDK+repair analogue of evaluation.execute() (evaluation.py:361).

    Difference from execute(): the artifact is TS, not a raw axios snippet, so there is no
    _extract_axios_call step (its regex `axios\\.[a-z]+\\(` at :435 would not match an SDK
    call). Also, the candidate imports the generated client via a relative `./client`, so it
    must be compiled and run INSIDE its per-task client dir (code_dir/{index:04d}_client/,
    where generate_client wrote client.ts). Per {index}_code.ts:
      1. copy the candidate into its client dir and compile it (+ the client) to JS with tsc,
      2. prepend capture_shim.js (fetch-option capture) with the config path substituted into
         its single `%s` placeholder — the analogue of prepending mock.js (evaluation.py:409),
      3. run under node with cwd = the client dir so `require('./client')` resolves; the shim
         writes {index}_config.json (into code_dir) in the EXISTING shape,
      4. compare()/analyze() are reused verbatim.
    """
    from evaluation import Verdict, _create_variable_definitions  # lazy heavy import

    with open(os.path.join("wapiibench", "capture_shim.js"), "r") as file:
        shim_template = file.read()

    if test_data_file is not None:
        with open(test_data_file, "r") as file:
            test_data = json.load(file)
    else:
        test_data = None

    files = sorted(os.listdir(code_dir))
    for file_name in files:
        file_path = os.path.join(code_dir, file_name)
        root, ext = os.path.splitext(file_name)
        if not os.path.isfile(file_path) or ext != ".ts" or not root.endswith("_code"):
            continue

        index, _ = root.split(sep="_", maxsplit=1)
        # ABSOLUTE, because the shim runs under `cwd=client_dir` (see the node call below)
        # while code_dir is relative to the repository root. With a relative path the shim's
        # fs.writeFileSync targets <client_dir>/<code_dir>/... , which does not exist: the
        # write throws, its except-branch throws again writing the same bad path, node exits
        # non-zero, and every task lands as EXECUTION_ERROR no matter how good the answer was.
        config_log_file = os.path.abspath(os.path.join(code_dir, f"{index}_config.json"))
        client_dir = os.path.join(code_dir, f"{int(index):04d}_client")

        def _fail():
            with open(config_log_file, "w") as file:
                json.dump({"ERROR": Verdict.EXECUTION_ERROR}, file, indent=2)

        if not os.path.isdir(client_dir):
            _fail()  # no generated client for this task -> nonexecutable
            continue

        # Place the candidate next to client.ts so `./client` resolves under tsc and node.
        exec_ts = os.path.join(client_dir, "_exec_code.ts")
        with open(file_path, "r") as src, open(exec_ts, "w") as dst:
            dst.write(src.read())

        compiled_js = _compile_to_js(exec_ts, client_dir)
        if compiled_js is None:
            _fail()
            continue

        shim = shim_template % config_log_file
        with open(compiled_js, "r") as file:
            body = file.read()
        variable_definitions = "" if test_data is None \
            else _create_variable_definitions(test_data[int(index)])
        executable_code = f"{shim}\n{variable_definitions}\n{body}"

        # cwd = client_dir so the compiled `require('./client')` resolves to client.js.
        proc = subprocess.run([node, "-"], input=executable_code, text=True,
                              capture_output=True, cwd=client_dir)
        if proc.returncode != 0 and not os.path.isfile(config_log_file):
            _fail()


# --------------------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------------------- #

def _strip_code_fence(text: str, lang: str = "typescript") -> str:
    """Extract a fenced code block if present, else return the trimmed text."""
    match = re.search(rf"```(?:{lang}|ts|typescript)?\s*(.*?)```", text, re.DOTALL)
    return match.group(1).strip() if match else text.strip()


def _prepend_starter(code: str, starter_code: str) -> str:
    """Make the starter part of the arm's CONTRACT rather than something the model must echo.

    The prompt shows the starter and says to continue after it, and the axios arms splice it
    back in explicitly (evaluation.py:270-288). Do the same here: without the starter the
    capture fetch is never injected, the auth helper is never called, and the task cannot be
    scored at all — a failure mode that says nothing about the model. If the model did echo
    the starter, leave its version alone.
    """
    marker = "from './client'"
    return code if marker in code else f"{starter_code.rstrip()}\n\n{code.lstrip()}"


def _compile_to_js(ts_file: str, out_dir: str) -> str | None:
    """Compile a candidate .ts (and its imported client) to JS. Returns the emitted path or
    None if compilation failed.

    Absolute paths for the same reason as run_tsc(): tsc runs with `cwd=out_dir`.
    """
    ts_file = os.path.abspath(ts_file)
    out_dir = os.path.abspath(out_dir)
    proc = subprocess.run(
        ["npx", "tsc", "--outDir", out_dir, *_TSC_FLAGS,
         ts_file, *_dts_files(out_dir)],
        cwd=out_dir, capture_output=True, text=True)
    if proc.returncode != 0:
        logger.warning("tsc failed for %s:\n%s", ts_file, proc.stdout + proc.stderr)
        return None
    return os.path.splitext(ts_file)[0] + ".js"
