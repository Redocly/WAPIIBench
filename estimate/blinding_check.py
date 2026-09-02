#!/usr/bin/env python3
"""Deliverable 2 (verification half) — automated leakage scan over the emitted prompt material.

For every sampled task this renders the EXACT text estimate/emit_prompt.py hands a generator
agent, then checks it against that task's ground-truth config, which the generator never sees.

Three classes of finding, kept apart because they mean different things:

  LEAK (must be zero)      the emitted text contains the expected URL, the expected endpoint
                           path, or an expected parameter NAME verbatim. Any hit here means
                           the estimate is measuring copying, not integration.

  EXPECTED (informational) an expected VALUE (an id, a title, a limit) appears in the task
                           text. This is not a leak: the task has to state the values or it
                           is unsolvable. Counted so the report can say how much of the
                           answer is value-copying versus API knowledge.

  NOTE (judgement call)    an expected parameter name appears in the task text only after
                           normalisation (`team_id` vs "team ... ID", `opt_fields` vs
                           "optional fields"). Reported per task so a human can look.

It also asserts the starter code hardcoded in emit_prompt.py is byte-identical to
`evaluation.SETUPS['sdk-invocation']`, read as TEXT out of evaluation.py (no import, so no
torch/transformers needed) — otherwise the two could silently drift.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "estimate"))

import emit_prompt                                  # noqa: E402
from retrieval_standin import load_operations, resolve_ground_truth  # noqa: E402

DATASET_DIR = os.path.join(REPO_ROOT, "data", "synthetic")
# Header keys the harness itself treats as always-present boilerplate (evaluation.SPECIAL_KEYS)
# plus Authorization, which every task shares and which no starter has to guess.
IGNORED_HEADER_KEYS = {"accept", "content-type", "authorization"}


# --------------------------------------------------------------------------------------- #
# starter-code drift check
# --------------------------------------------------------------------------------------- #

def check_starter_is_live() -> tuple[bool, str]:
    """Confirm the emitter takes its starter from the LIVE evaluation.py, not a stale copy.

    emit_prompt.harness_starter() reads `evaluation.SETUPS['sdk-invocation']` out of
    evaluation.py via the AST on every call, so drift is structurally impossible. This
    re-reads it independently and prints what the generator will actually be handed — the
    arm is under active development and this starter changed once mid-session (it gained the
    zod-validation middleware lines).
    """
    try:
        starter = emit_prompt.harness_starter()
    except Exception as error:                                       # noqa: BLE001
        return False, f"cannot read the harness starter: {error}"
    if "__wapiiCaptureFetch" not in starter:
        return False, f"starter does not wire the capture shim: {starter!r}"
    return True, "read live from evaluation.SETUPS['sdk-invocation']: " + repr(starter)


# --------------------------------------------------------------------------------------- #
# ground-truth token extraction (scoring side only)
# --------------------------------------------------------------------------------------- #

def _expected_tokens(config: dict, operations: list[dict]) -> dict[str, list[str]]:
    url = (config.get("url") or "").split("?", 1)[0]
    names: list[str] = []
    values: list[str] = []
    for field in ("params", "data", "path_params"):
        for key, value in (config.get(field) or {}).items():
            names.append(str(key))
            values.extend(_leaf_values(value))
    for key in (config.get("headers") or {}):
        if str(key).lower() not in IGNORED_HEADER_KEYS:
            names.append(str(key))

    truth_id = resolve_ground_truth(operations, config)
    template = next((r["path"] for r in operations if r["operation_id"] == truth_id), None)
    literal = None
    for record in operations:
        if record["operation_id"] == truth_id:
            for server in record["servers"]:
                prefix = server.rstrip("/")
                if url.startswith(prefix):
                    literal = url[len(prefix):]
            break
    return {"url": [url], "path_template": [t for t in (template,) if t],
            "path_literal": [t for t in (literal,) if t],
            "param_names": sorted(set(names)), "values": sorted(set(values)),
            "operation_id": [truth_id] if truth_id else []}


def _leaf_values(value) -> list[str]:
    if isinstance(value, dict):
        return [v for item in value.values() for v in _leaf_values(item)]
    if isinstance(value, list):
        return [v for item in value for v in _leaf_values(item)]
    if isinstance(value, bool) or value is None:
        return []
    return [str(value)]


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower())


def _word_in(needle: str, haystack_lower: str) -> bool:
    """Whole-token containment. Substring matching produces nonsense here: the param name
    `id` matches "provide", `user` matches "users"."""
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(needle.lower())}(?![A-Za-z0-9_])",
                     haystack_lower) is not None


_PLACEHOLDER = re.compile(r"<[^<>]{1,64}>")


def _word_in_outside_placeholders(needle: str, haystack_lower: str) -> bool:
    """`_word_in`, ignoring text inside `<...>` placeholders.

    WHY: the synthetic dataset writes unresolved values as angle-bracket placeholders whose
    text is often the parameter's own name — task google_calendar_v3 #15 says `has iCalUID
    "<iCalUID>"`, and `<iCalUID>` is also the expected VALUE of the `iCalUID` field. Counting
    that as a name leak is wrong twice over: the task must state the value, and the "name"
    the model sees is the value it has to send. Occurrences OUTSIDE placeholders still count.
    """
    return _word_in(needle, _PLACEHOLDER.sub(" ", haystack_lower))


def _is_structured(name: str) -> bool:
    """True for a parameter name that prose could not produce by accident: it has an
    underscore/hyphen/dot/digit, or internal capitals (camelCase)."""
    return bool(re.search(r"[_\-.\d]", name)) or bool(re.search(r"[a-z][A-Z]", name))


def scan_task(api: str, index: int, work_root: str, operations: list[dict]) -> dict:
    material = emit_prompt.emit(api, index, work_root)
    rendered = emit_prompt.render(material)
    # Only the parts that carry task-specific semantics; the fixed rules text and the file
    # paths are identical for every task and cannot carry ground truth.
    payload = f"{material['task']}\n{material['starter_code']}"

    with open(os.path.join(DATASET_DIR, api, "test_data_final.json"), "r") as file:
        config = json.load(file)[index]["config"]
    expected = _expected_tokens(config, operations)

    lower, norm_words = payload.lower(), set(_normalise(payload).split())
    hit = lambda needle: bool(needle) and _word_in(needle, lower)

    verbatim = [n for n in expected["param_names"]
                if _word_in_outside_placeholders(n, lower)]
    placeholder_only = [n for n in expected["param_names"] if hit(n) and n not in verbatim]
    findings = {
        "api": api, "index": index,
        # Hard leaks: the answer's endpoint, verbatim.
        "leak_url": [u for u in expected["url"] if hit(u)],
        "leak_path_literal": [p for p in expected["path_literal"] if len(p or "") > 1 and hit(p)],
        "leak_path_template": [p for p in expected["path_template"] if len(p or "") > 1 and hit(p)],
        "leak_operation_id": [o for o in expected["operation_id"] if hit(o)],
        # Hard leak: a STRUCTURED parameter name (snake_case, camelCase, digits) verbatim.
        # These cannot be coincidences of English prose.
        "leak_param_name_structured": [n for n in verbatim if _is_structured(n)],
        # Soft: the parameter name is a single common English word that the task text has to
        # use anyway ("channel", "email", "summary"). Reported, not treated as a leak.
        "note_param_name_common_word": [n for n in verbatim if not _is_structured(n)],
        "note_param_name_normalised": [
            n for n in expected["param_names"]
            if n not in verbatim and all(w in norm_words for w in _normalise(n).split())],
        "expected_value_in_task": [v for v in expected["values"] if len(v) > 1 and v.lower() in lower],
        "note_param_name_placeholder_only": placeholder_only,
        "expected_param_names": expected["param_names"],
        "expected_values": expected["values"],
        "rendered_chars": len(rendered),
    }
    findings["leaks"] = sum(len(findings[k]) for k in (
        "leak_url", "leak_path_literal", "leak_path_template",
        "leak_operation_id", "leak_param_name_structured"))
    return findings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sample", default=os.path.join(REPO_ROOT, "estimate", "sample.json"))
    parser.add_argument("--work-root", default=os.path.join(REPO_ROOT, "estimate", "work"))
    parser.add_argument("--out", default=os.path.join(REPO_ROOT, "estimate", "blinding_report.json"))
    args = parser.parse_args()

    ok, detail = check_starter_is_live()
    print(f"starter source: {'OK' if ok else 'FAIL'} — {detail}")

    with open(args.sample, "r") as file:
        sample = json.load(file)

    cache: dict[str, list[dict]] = {}
    rows = []
    for task_id in sample["tasks"]:
        api = task_id["api"]
        if api not in cache:
            cache[api] = [r for r in load_operations(api) if r["operation_id"]]
        rows.append(scan_task(api, task_id["index"], args.work_root, cache[api]))

    leaking = [r for r in rows if r["leaks"]]
    value_overlap = [r for r in rows if r["expected_value_in_task"]]
    normalised = [r for r in rows if r["note_param_name_normalised"]]
    common_word = [r for r in rows if r["note_param_name_common_word"]]

    summary = {
        "starter_read_from_live_evaluation_py": ok,
        "n": len(rows),
        "tasks_with_hard_leaks": len(leaking),
        "tasks_with_expected_value_overlap": len(value_overlap),
        "tasks_with_common_word_param_name_note": len(common_word),
        "tasks_with_normalised_param_name_note": len(normalised),
        "leak_kinds": {
            kind: sum(1 for r in rows if r[kind])
            for kind in ("leak_url", "leak_path_literal", "leak_path_template",
                         "leak_operation_id", "leak_param_name_structured")},
        "common_word_param_names_seen": sorted({
            n for r in rows for n in r["note_param_name_common_word"]}),
        "normalised_param_names_seen": sorted({
            n for r in rows for n in r["note_param_name_normalised"]}),
        "tasks_with_placeholder_only_param_name_note": sum(
            1 for r in rows if r["note_param_name_placeholder_only"]),
        "placeholder_only_param_names_seen": sorted({
            n for r in rows for n in r["note_param_name_placeholder_only"]}),
    }
    with open(args.out, "w") as file:
        json.dump({"summary": summary, "tasks": rows}, file, indent=2)

    print(json.dumps(summary, indent=2))
    for row in leaking:
        print(f"  LEAK {row['api']}:{row['index']} -> " + json.dumps(
            {k: v for k, v in row.items() if k.startswith('leak_') and v}))
    print(f"wrote {args.out}")
    sys.exit(0 if (ok and not leaking) else 1)


if __name__ == "__main__":
    main()
