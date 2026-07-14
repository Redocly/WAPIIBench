"""
sdk_repair_arm.py — the WAPIIBench "sdk-repair" generation arm.

This arm has the model write TypeScript against a *typed client* generated from the API's
OpenAPI spec by `redocly generate-client` (redocly-cli PR #2885), then repairs the code
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

PENDING (documented in the branch README, not solved here):
  * `redocly generate-client` is on the unmerged PR #2885 branch `feat/ts-client-gen`; the
    generate_client() step needs that CLI on PATH (build from the branch or `npm link`).
    Points that depend on the exact generated output are marked TODO(gen-client).
"""

from __future__ import annotations

import json
import os
import re
import subprocess

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
# CAPTURE SEAM (verified against redocly-cli@f5776cf): the generated runtime resolves its
# fetch as `const doFetch = config.fetch ?? fetch;` (runtime/send.ts) and ClientConfig has
# `fetch?: typeof fetch` (runtime/types.ts). `configure({ fetch })` sets it on the default
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
        "import {{ configure }} from './client';\n"
        "configure({{ fetch: globalThis.__wapiiCaptureFetch }});\n\n"
    ),
    # Alternative for per-instance code (createClient). TODO(gen-client): confirm the exact
    # createClient option name for the base URL against the generated module before use;
    # the fetch option name (`fetch`) is verified.
    "sdk-createclient": (
        "// {task}\n"
        "import {{ createClient }} from './client';\n"
        "const client = createClient({{ fetch: globalThis.__wapiiCaptureFetch }});\n\n"
    ),
}

DEFAULT_MAX_RETRIES = 3   # OPEN DECISION — repair budget (see EXPERIMENT_PLAN.md).
USE_ZOD = False           # OPEN DECISION — zod value-validation as an extra repair signal.
# NOTE (verified, redocly-cli@f5776cf): the generated SDK client performs NO runtime
# validation of the outbound request OR the response — send.ts serializes the body and never
# calls a schema. `--generators zod` (opt-in; default generator set is `sdk`) emits a
# SEPARATE `*.zod.ts` of component/model schemas that the CONSUMER calls; the shipped example
# (tests/e2e/generate-client/examples/zod/src/main.ts) only does RESPONSE validation
# (`MenuItemListSchema.parse(response)`). So zod gives no automatic OUTBOUND-request signal:
# using it as a repair signal would require hand-writing a validate-the-request-body call
# against the relevant component schema. tsc type errors remain the primary repair signal.


# --------------------------------------------------------------------------------------- #
# Step 1 — spec filtering
# --------------------------------------------------------------------------------------- #

def filter_spec(spec_file: str, api: str, task: str, out_path: str) -> str:
    """Filter the full OpenAPI spec down to the operation(s) relevant to `task`.

    Reuse target: the RAG retriever already scores/selects relevant endpoints. We reuse that
    selection and then emit a pruned spec (paths + transitively referenced components).

    :return: path to the filtered spec written at `out_path`.
    """
    # TODO(bind): reuse wapiibench.rag.retriever.Retriever to select relevant operations,
    # then prune the parsed spec to those paths + referenced $ref components and write YAML.
    raise NotImplementedError(
        "TODO(bind): reuse rag.retriever + openapi_utils to prune the spec for generate-client")


# --------------------------------------------------------------------------------------- #
# Step 2 — typed client generation (depends on the unmerged generate-client CLI)
# --------------------------------------------------------------------------------------- #

def generate_client(filtered_spec: str, client_dir: str,
                    output_mode: str = "single", runtime: str = "inline") -> tuple[str, str]:
    """Run `redocly generate-client` on the filtered spec.

    Verified CLI facts (redocly-cli PR #2885, branch feat/ts-client-gen):
      * command:  `redocly generate-client <spec> -o <outdir>`  (experimental)
      * default `--runtime inline` -> one self-contained file, zero deps, web-standard fetch
      * default `--output-mode single`; default generator set is `sdk`
      * `--generators sdk zod ...` opts extra emitters in (zod emits a separate *.zod.ts)
    See the branch README for how to obtain the CLI (it is not on a released redocly build).

    :return: (client_entry_path, surface_text). client_entry_path is normalized to
             `<client_dir>/client.ts` so the starter's `import ... from './client'` resolves.
    """
    os.makedirs(client_dir, exist_ok=True)
    cmd = ["redocly", "generate-client", filtered_spec, "-o", client_dir,
           "--runtime", runtime, "--output-mode", output_mode]
    generators = ["sdk"] + (["zod"] if USE_ZOD else [])
    cmd += ["--generators", *generators]
    subprocess.run(cmd, check=True, capture_output=True, text=True)

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


def _extract_surface(client_dir: str) -> str:
    """Collect the typed surface (operation signatures + arg/response types) for the prompt.

    Overlaps with rag/typescript_spec_converter output. TODO(gen-client): slice the generated
    entrypoint to the relevant operations' signatures; keep it small to bound prompt tokens.
    """
    raise NotImplementedError(
        "TODO(gen-client): extract the typed surface from the generated client entrypoint")


# --------------------------------------------------------------------------------------- #
# Step 3 — prompt assembly
# --------------------------------------------------------------------------------------- #

def assemble_prompt(prompt_template: str, task: dict, setup: str, setting: str,
                    surface_text: str, model_name: str,
                    error_feedback: str | None = None) -> tuple[str, str]:
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

    # setting has no 'spec'/'rag' substring, so instantiate_prompt uses spec="".
    prompt, starter_code = instantiate_prompt(prompt_template, task, setup, setting,
                                              spec="", model_name=model_name)
    # Substitute AFTER .format(); values may contain literal braces.
    prompt = prompt.replace("{surface}", surface_text)
    prompt = prompt.replace("{error_feedback}", error_feedback or "")
    return prompt, starter_code


# --------------------------------------------------------------------------------------- #
# Step 5 — tsc compile + error parsing
# --------------------------------------------------------------------------------------- #

# tsc diagnostic line, e.g. "0001_code.ts(12,5): error TS2345: Argument of type ..."
_TSC_ERROR_RE = re.compile(
    r"^(?P<file>.+?)\((?P<line>\d+),(?P<col>\d+)\):\s+error\s+(?P<code>TS\d+):\s+(?P<msg>.*)$")


def write_tsconfig(work_dir: str) -> str:
    """Emit a minimal tsconfig for tsc runs. `DOM` lib provides fetch/Headers/Response types
    that the generated inline runtime relies on."""
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
    return path


def run_tsc(ts_file: str, work_dir: str) -> list[dict]:
    """Type-check `ts_file` with --noEmit. Returns parsed diagnostics (empty == clean)."""
    proc = subprocess.run(
        ["npx", "tsc", "--noEmit", "--pretty", "false", ts_file],
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
        filtered = filter_spec(spec_file, task["api"], task["task"],
                               os.path.join(client_dir, "filtered.yaml"))
        _entry, surface = generate_client(filtered, client_dir)
        write_tsconfig(client_dir)

        code, attempts, diagnostics, tokens = repair_loop(
            model, generation_config, prompt_template, task, setup, setting, surface,
            model_name, work_dir=client_dir, max_retries=max_retries)

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
                model_name, work_dir: str, max_retries: int):
    """Generate -> tsc -> re-prompt until clean or budget hit.

    :return: (final_code, attempts_used, final_diagnostics, tokens_total)
    """
    error_feedback = None
    code = ""
    diagnostics: list[dict] = []
    tokens_total = None  # TODO(bind): sum ModelWrapper.run() usage per attempt if exposed.

    for attempt in range(max_retries + 1):        # attempt 0 is the first generation
        prompt, starter_code = assemble_prompt(
            prompt_template, task, setup, setting, surface, model_name, error_feedback)

        output_texts = model.run(prompt, generation_config=generation_config, batch=False)
        code = _strip_code_fence(output_texts[0])

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
        config_log_file = os.path.join(code_dir, f"{index}_config.json")
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


def _compile_to_js(ts_file: str, out_dir: str) -> str | None:
    """Compile a candidate .ts (and its imported client) to JS. Returns the emitted path or
    None if compilation failed."""
    proc = subprocess.run(
        ["npx", "tsc", "--outDir", out_dir, "--module", "CommonJS", "--esModuleInterop",
         "--skipLibCheck", "--target", "ES2020", ts_file],
        cwd=out_dir, capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    return os.path.splitext(ts_file)[0] + ".js"
