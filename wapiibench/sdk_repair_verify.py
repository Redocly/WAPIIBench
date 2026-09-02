"""
sdk_repair_verify.py — offline verification of the NON-MODEL half of the `sdk-repair` arm.

Runs the arm's pipeline with NO language model anywhere in the loop: for each selected task
the ideal SDK invocation is HAND-WRITTEN below (what a perfect model would emit against the
generated client), then pushed through the arm's real
`generate_client -> tsc -> capture_shim -> compare -> analyze` path. The point is to find out
whether the harness scores a KNOWN-GOOD SDK answer as correct — if it does not, the arm is
measuring the wrong thing.

Each case also carries deliberately broken variants so the negative controls are checked too:
  * `wrong_value`   — a correct-shaped call with one wrong parameter value (must score wrong)
  * `invalid_value` — a value tsc accepts but the spec forbids (must be rejected by zod)
  * `bad_code`      — a bad method name + a wrong-typed argument (must fail `tsc`, feeding the
                      repair loop)

Run from the repository root:

    PYTHONPATH=wapiibench python3 wapiibench/sdk_repair_verify.py \\
        --redocly ./node_modules/.bin/redocly --node "$(command -v node)"
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

import sdk_repair_arm as arm

# PER-API files, not the combined "all" file: compare() keys on the POSITION of the task in
# whatever test_data_file it is given, and a task's `index` field is its position in the
# per-API file. Running per API is also how evaluation.py is invoked (`--apis <api>`).
def dataset_file(dataset: str, api: str) -> str:
    if dataset == "syn":
        return os.path.join("data", "synthetic", api, "test_data_final.json")
    return os.path.join("validation_data", "all", "validation_data_final.json")

# The shared prologue: exactly the 'sdk-invocation' starter from evaluation.SETUPS, with the
# `{task}`/brace escaping already resolved (the model would receive it pre-rendered).
STARTER = """import { client } from './client';
import { zodValidation } from './client.zod';
client.configure({ fetch: globalThis.__wapiiCaptureFetch, clientHeader: false });
client.use(zodValidation());
"""

# --------------------------------------------------------------------------------------- #
# The hand-written "ideal" answers. `pos` is the task's POSITION in the dataset file, which
# is the index compare() keys on (the per-task `index` field restarts per API).
# --------------------------------------------------------------------------------------- #

CASES = [
    # NOTE ON API CHOICE: Asana is deliberately absent. `evaluation.compare()` needs
    # `openapi_utils.parse_spec`, and asana.yaml does not finish parsing (>15 min, never
    # completed) — so no Asana task can be scored by this harness at all, in ANY arm. Measured
    # parse times for the specs used here: google_calendar_v3 1.1 s, slack 5.1 s,
    # google_sheet_v4 10.6 s. github_v3 and npm_registry FAIL to parse outright
    # (ParserError), which likewise blocks scoring for their real-world tasks.
    {
        "name": "google_calendar_v3/calendar.calendars.insert (POST, JSON body)",
        "dataset": "syn", "pos": 0, "api": "google_calendar_v3",
        "operation_ids": ["calendar.calendars.insert"],
        "correct": """client.calendar_calendars_insert({ body: {
  summary: 'Test Calendar', timeZone: 'America/Los_Angeles' } });
""",
        "wrong_value": """client.calendar_calendars_insert({ body: {
  summary: 'Test Calendar', timeZone: 'Europe/Zurich' } });
""",
        # A bad method name plus a wrong-typed argument, for the repair loop.
        "bad_code": """client.calendar_calendars_insertt({ body: { summary: 42 } });
""",
    },
    {
        "name": "google_calendar_v3/calendar.calendars.get (GET, path param)",
        "dataset": "syn", "pos": 2, "api": "google_calendar_v3",
        "operation_ids": ["calendar.calendars.get"],
        "correct": "client.calendar_calendars_get({ path: { calendarId: '<calendarId>' } });\n",
    },
    {
        "name": "google_sheet_v4/sheets.spreadsheets.get (GET, path param + bool query)",
        "dataset": "syn", "pos": 1, "api": "google_sheet_v4",
        "operation_ids": ["sheets.spreadsheets.get"],
        "correct": """client.sheets_spreadsheets_get({
  path: { spreadsheetId: '<spreadsheetId>' }, query: { includeGridData: true } });
""",
        "wrong_value": """client.sheets_spreadsheets_get({
  path: { spreadsheetId: '<spreadsheetId>' }, query: { includeGridData: false } });
""",
    },
    {
        "name": "google_sheet_v4/sheets.spreadsheets.create (POST, JSON body, zod request schema)",
        "dataset": "syn", "pos": 0, "api": "google_sheet_v4",
        "operation_ids": ["sheets.spreadsheets.create"],
        "correct": """client.sheets_spreadsheets_create({ body: {
  properties: { title: 'My Spreadsheet' },
  sheets: [{ properties: { title: 'Sheet1',
    gridProperties: { rowCount: 10, columnCount: 5 } } }] } });
""",
        "wrong_value": """client.sheets_spreadsheets_create({ body: {
  properties: { title: 'Wrong Title' },
  sheets: [{ properties: { title: 'Sheet1',
    gridProperties: { rowCount: 10, columnCount: 5 } } }] } });
""",
        # `gridProperties.rowCount` is `number` in TypeScript but `z.number().int()` in the
        # generated zod request schema, so 10.5 TYPE-CHECKS and can only be caught at runtime
        # by zod. This is the one class of invalid value zod adds over tsc.
        "invalid_value": """client.sheets_spreadsheets_create({ body: {
  properties: { title: 'My Spreadsheet' },
  sheets: [{ properties: { title: 'Sheet1',
    gridProperties: { rowCount: 10.5, columnCount: 5 } } }] } });
""",
    },
    {
        "name": "slack/admin_apps_approve (POST, urlencoded body)",
        "dataset": "syn", "pos": 0, "api": "slack",
        "operation_ids": ["admin_apps_approve"],
        "correct": """client.admin_apps_approve({ body: new URLSearchParams({
  app_id: 'A12345678', request_id: 'R98765432', team_id: 'T87654321' }) });
""",
        "wrong_value": """client.admin_apps_approve({ body: new URLSearchParams({
  app_id: 'A00000000', request_id: 'R98765432', team_id: 'T87654321' }) });
""",
    },
    {
        "name": "slack/admin_apps_approved_list (GET, query params, int-valued limit)",
        "dataset": "syn", "pos": 1, "api": "slack",
        "operation_ids": ["admin_apps_approved_list"],
        "correct": "client.admin_apps_approved_list({ query: { team_id: 'T12345678', limit: 100 } });\n",
        "wrong_value": "client.admin_apps_approved_list({ query: { team_id: 'T00000000', limit: 100 } });\n",
    },
    {
        "name": "youtube_data_v3/youtube.search.list (REAL WORLD, query params + definitions)",
        "dataset": "rw", "pos": 24, "api": "youtube_data_v3",
        "operation_ids": ["youtube.search.list"],
        # `searchTerm` is injected by evaluation._create_variable_definitions at execute time
        # and declared for tsc by sdk_repair_arm.write_task_globals.
        #
        # auth_override: the spec declares `Oauth2` (bearer), so auth_setup_for would emit
        # `client.auth.bearer('<token>')` — but this task authenticates with the `key` QUERY
        # parameter and its expected headers carry NO `Authorization`. Leaving the line in
        # makes the ideal answer score 'illegal' for a reason that is purely an artifact of
        # the starter. This is the AUTH_SETUP_FROM_SPEC caveat, made concrete.
        "auth_override": "none",
        "correct": """client.youtube_search_list({ query: {
  part: ['snippet'], key: 'AIzaSyAPs3iCpnQcI6vMxCWR2JdZa1mcSTkemfU', q: searchTerm } });
""",
        "wrong_value": """client.youtube_search_list({ query: {
  part: ['snippet'], key: 'AIzaSyAPs3iCpnQcI6vMxCWR2JdZa1mcSTkemfU', q: 'not-the-term' } });
""",
    },
]

VARIANTS = ("correct", "wrong_value", "invalid_value")


class StubModel:
    """A ModelWrapper stand-in that replays canned completions. NO language model.

    `ModelWrapper.run()` returns a list of strings, and repair_loop strips a code fence off
    element 0, so that is all this needs to imitate.
    """

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.calls = 0

    def run(self, prompt, generation_config=None, batch=False):  # noqa: ARG002
        output = self.outputs[min(self.calls, len(self.outputs) - 1)]
        self.calls += 1
        return ["```typescript\n" + output + "```"]


def check_repair_loop(case: dict, root: str, redocly: str) -> dict:
    """Drive the real `repair_loop` with a stub model over a candidate that fails `tsc`.

    Two runs: one whose second completion is correct (must converge), and one that never
    fixes the code (must exhaust the budget and hand back the diagnostics).
    """
    code_dir = build_case_dir(case, "correct", root, redocly)
    client_dir = os.path.join(code_dir, f"{case['pos']:04d}_client")
    task = json.load(open(dataset_file(case["dataset"], case["api"])))[case["pos"]]
    # Per-API dataset files carry no `api` key; generate_sdk_repair supplies it with
    # task.setdefault("api", api) before assemble_prompt needs it for _get_api_name.
    task.setdefault("api", case["api"])
    surface = arm._extract_surface(client_dir)
    template = open(os.path.join("resources", "sdk_code_generation_prompt.md")).read()
    auth = arm.auth_setup_for(client_dir)
    starter = STARTER + (auth + "\n" if auth else "")
    bad = starter + "\n" + case["bad_code"]
    good = starter + "\n" + case["correct"]

    candidate = os.path.join(client_dir, "candidate.ts")
    with open(candidate, "w") as file:
        file.write(bad)
    diagnostics = arm.run_tsc(candidate, client_dir)

    result = {
        "bad_code_diagnostics": diagnostics,
        "error_feedback": arm.format_errors_for_prompt(diagnostics) if diagnostics else None,
    }
    for label, outputs in (("repairs_on_attempt_1", [bad, good]),
                           ("never_repairs", [bad])):
        model = StubModel(outputs)
        _code, attempts, final, _tokens = arm.repair_loop(
            model, None, template, task, "sdk-invocation", "sdk-repair", surface,
            "stub/model", work_dir=client_dir, max_retries=3, auth_setup=auth)
        result[label] = {"model_calls": model.calls, "attempts_used": attempts,
                         "typechecks": not final,
                         "final_diagnostic_codes": sorted({d["code"] for d in final})}
    return result


def build_case_dir(case: dict, variant: str, root: str, redocly: str) -> str | None:
    """Materialize one <code_dir> holding a single task's client + hand-written candidate."""
    code = case.get(variant)
    if code is None:
        return None
    task = json.load(open(dataset_file(case["dataset"], case["api"])))[case["pos"]]
    # The api MUST be in the directory name: `pos` is a per-API index, so
    # google_calendar_v3[0], google_sheet_v4[0] and slack[0] would otherwise all share one
    # directory and overwrite each other's client, candidate and results.json.
    code_dir = os.path.join(
        root, f"{case['dataset']}_{case['api']}_{case['pos']:04d}_{variant}")
    client_dir = os.path.join(code_dir, f"{case['pos']:04d}_client")
    os.makedirs(client_dir, exist_ok=True)

    config = arm.filter_spec(
        os.path.join("openapi", "real_world_specs", f"{case['api']}.yaml"),
        case["api"], task["task"], os.path.join(client_dir, "redocly.yaml"),
        operation_ids=case["operation_ids"])
    arm.generate_client(config, client_dir, redocly_bin=redocly)
    arm.write_tsconfig(client_dir)
    arm.write_task_globals(client_dir, task)

    node_modules = os.environ.get("WAPII_NODE_MODULES")
    if node_modules and not os.path.exists(os.path.join(client_dir, "node_modules")):
        os.symlink(node_modules, os.path.join(client_dir, "node_modules"))

    # The auth line comes from sdk_repair_arm.auth_setup_for, i.e. from the SPEC, exactly as
    # assemble_prompt() renders it into the starter at generation time. `auth_override` lets a
    # case suppress it (see the real-world case for why that is necessary there).
    auth = "" if case.get("auth_override") == "none" else arm.auth_setup_for(client_dir)
    starter = STARTER + (auth + "\n" if auth else "")
    with open(os.path.join(code_dir, f"{case['pos']:04d}_code.ts"), "w") as file:
        file.write(f"// {task['task']}\n{starter}\n{code}")
    return code_dir


def check_single_operation(client_dir: str, operation_ids: list[str]) -> tuple[bool, list[str]]:
    """Confirm `filter-in` left ONLY the whitelisted operation(s) in the generated client."""
    with open(os.path.join(client_dir, "client.ts")) as file:
        text = file.read()
    block = arm._extract_declaration(text, "export const OPERATIONS = ") or ""
    found = sorted(set(part.split('"')[0] for part in block.split('id: "')[1:]))
    return found == sorted(operation_ids), found


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--redocly", default="redocly")
    parser.add_argument("--node", default=os.path.join(
        os.environ.get("NVM_SYMLINK", os.path.expanduser("~/.nvm/versions/node/v24.16.0/bin")),
        "node"))
    parser.add_argument("--work-dir", default=os.path.join("data", "sdk_repair_verify"))
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    from evaluation import analyze, compare

    if not args.keep and os.path.isdir(args.work_dir):
        shutil.rmtree(args.work_dir)
    os.makedirs(args.work_dir, exist_ok=True)

    report: dict[str, dict] = {}
    for case in CASES:
        for variant in VARIANTS:
            code_dir = build_case_dir(case, variant, args.work_dir, args.redocly)
            if code_dir is None:
                continue
            key = f"{case['name']} :: {variant}"
            client_dir = os.path.join(code_dir, f"{case['pos']:04d}_client")
            single, found = check_single_operation(client_dir, case["operation_ids"])
            entry = {"only_whitelisted_operation": single, "operations_in_client": found}

            # Typecheck the candidate WHERE IT ACTUALLY COMPILES: `import ... from './client'`
            # is relative to the importing file, and the candidate lives in code_dir while
            # client.ts lives in code_dir/<pos>_client. execute_sdk_repair copies it in as
            # _exec_code.ts for exactly this reason; do the same here or every case reports a
            # spurious TS2307 "Cannot find module './client'".
            source = os.path.join(code_dir, f"{case['pos']:04d}_code.ts")
            candidate = os.path.join(client_dir, "_typecheck.ts")
            shutil.copyfile(source, candidate)
            entry["tsc_diagnostics"] = arm.run_tsc(candidate, client_dir)
            entry["typechecks"] = not entry["tsc_diagnostics"]
            os.remove(candidate)

            arm.execute_sdk_repair(code_dir, args.node,
                                   test_data_file=dataset_file(case["dataset"], case["api"]))
            config_file = os.path.join(code_dir, f"{case['pos']:04d}_config.json")
            entry["captured_config"] = json.load(open(config_file)) \
                if os.path.isfile(config_file) else None

            if entry["captured_config"] is not None:
                compare(dataset_file(case["dataset"], case["api"]), code_dir, api=case["api"])
                analyze(code_dir)
                results = json.load(open(os.path.join(code_dir, "results.json")))
                sample = results[f"{case['pos']:04d}"]
                entry["sample_verdict"] = sample["statistics"]["sample_verdict"]
                entry["comparison"] = {
                    k: v for k, v in sample.items() if k != "statistics"}
            report[key] = entry

    for case in CASES:
        if "bad_code" in case:
            report[f"{case['name']} :: REPAIR LOOP"] = check_repair_loop(
                case, args.work_dir, args.redocly)

    out = os.path.join(args.work_dir, "verification_report.json")
    with open(out, "w") as file:
        json.dump(report, file, indent=2, sort_keys=True, default=str)

    print(f"\n{'=' * 100}\nVERIFICATION SUMMARY\n{'=' * 100}")
    for key, entry in report.items():
        print(f"\n{key}")
        if "bad_code_diagnostics" in entry:
            for diagnostic in entry["bad_code_diagnostics"]:
                print(f"  bad code -> {diagnostic['code']}: {diagnostic['msg'][:150]}")
            for label in ("repairs_on_attempt_1", "never_repairs"):
                print(f"  {label}: {entry[label]}")
            continue
        print(f"  only whitelisted operation : {entry['only_whitelisted_operation']} "
              f"{entry['operations_in_client']}")
        print(f"  typechecks under strict tsc: {entry['typechecks']}")
        if not entry["typechecks"]:
            for diagnostic in entry["tsc_diagnostics"][:4]:
                print(f"      {diagnostic['code']}: {diagnostic['msg']}")
        print(f"  request captured           : {entry['captured_config'] is not None}")
        if entry["captured_config"] is not None:
            print(f"      {json.dumps(entry['captured_config'], sort_keys=True)}")
        print(f"  sample verdict             : {entry.get('sample_verdict', '(none)')}")
    print(f"\nFull report: {out}")


if __name__ == "__main__":
    main()
