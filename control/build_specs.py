#!/usr/bin/env python3
"""CONTROL condition, step 1 — one filtered FIVE-operation OpenAPI document per sampled task.

This is the control arm's counterpart to `estimate/build_clients.py`. The treatment gives the
model a generated TypeScript client filtered to five operations; the control gives it the RAW
OPENAPI DESCRIPTION of exactly the same five operations, and nothing else changes.

WHY NOT THE WHOLE SPEC. The treatment's model only ever saw five operations, because
`filter-in` on the retrieved whitelist pruned the rest before `generate-client` ran. Handing
the control the full API description would make the two conditions differ in TWO ways at once
(typed client vs. spec text, AND 5 operations vs. all of them), so the contrast would measure
retrieval, not the SDK. The five operationIds come from `estimate/whitelists_parseable.json`
— the SAME per-task whitelists the treatment used, not a fresh retrieval run.

HOW THE SPEC IS RENDERED (this is a confound, so it is stated exactly):
  * YAML, `yaml.safe_dump(sort_keys=False)`. YAML because that is how the harness itself
    renders a spec into a prompt (`evaluation.instantiate_prompt`, the `spec` setting, wraps
    `_get_full_spec()` in a ```yaml fence).
  * NOT bundled, because there is nothing to bundle: every spec under
    `openapi/real_world_specs/` is a single self-contained file with only local `#/...`
    references (asserted below). Internal `$ref`s are PRESERVED rather than inlined —
    inlining would explode recursive schemas and would not be a document any real pipeline
    would produce.
  * `paths` keeps only the path items that carry a whitelisted operation, and inside those
    only the whitelisted operations (plus the path item's own `parameters`/`servers`).
  * `components` IS PRUNED to the transitive `$ref` closure of what survives, plus the
    `securitySchemes` the surviving `security` blocks name. `info`, `openapi`, `servers` and
    the `tags` the surviving operations actually declare are kept.

    PRUNING IS A DELIBERATE ASYMMETRY, IN THE CONTROL'S FAVOUR, AND IT IS DISCLOSED.
    `filter-in` prunes operations only, so the treatment's `client.ts` still carries the
    API's entire type surface (76-272 kB per task). A control whose spec carried every
    unreferenced schema would be 10-100x larger than the information it contains, and a
    reviewer would rightly call it a strawman. Any doubt is resolved towards making the
    CONTROL stronger, because the hypothesis under test is that the typed SDK helps: a
    weakened control inflates the SDK's apparent contribution. `control/spec_sizes.json`
    records the pruned AND unpruned size of every task's document so the size confound can be
    read in both directions.

BLINDING (see control/blinding_check_control.py for the check): the five operations are
emitted in SPEC ORDER — the order they appear in `openapi/real_world_specs/{api}.yaml` —
never in retriever-rank order. Spec order is identical for every task drawn from the same
API, so it cannot carry task-specific information. This is the same property that makes
`generate-client`'s `OPERATIONS` order safe in the treatment.

The parameter-type table the capture shim coerces query values with is written into the SAME
per-task directory by `sdk_repair_arm.write_param_types()` — the arm's own function, called
with the same two inputs it gets in the treatment (the spec path and the operationId
whitelist) and no others. The control executor runs node with `cwd` set to this directory,
which is exactly how the treatment's client dir is reached, so `capture_shim.js` finds the
table under its default relative name with no change to the shim.

Layout produced (per API):
    control/work/{api}/{index:04d}_spec/filtered_spec.yaml     <- the model's API description
    control/work/{api}/{index:04d}_spec/wapii_param_types.json <- spec-declared param types
    control/work/{api}/{index}_code.js                         <- written by the GENERATOR
    control/work/{api}/{index}_config.json                     <- written by the capture shim
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "wapiibench"))

SPEC_DIR = os.path.join(REPO_ROOT, "openapi", "real_world_specs")
DEFAULT_WHITELISTS = os.path.join(REPO_ROOT, "estimate", "whitelists_parseable.json")
DEFAULT_WORK = os.path.join(REPO_ROOT, "control", "work")
DEFAULT_SIZES = os.path.join(REPO_ROOT, "control", "spec_sizes.json")

SPEC_NAME = "filtered_spec.yaml"
HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

# Kept verbatim from the source document when present.
ROOT_KEEP = ("openapi", "info", "servers")

_REF = re.compile(r"^#/components/([^/]+)/(.+)$")


def _external_refs(node: object, found: list[str] | None = None) -> list[str]:
    """Every `$ref` that is NOT a local `#/...` pointer. Must be empty for "no bundling"."""
    found = [] if found is None else found
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and not ref.startswith("#/"):
            found.append(ref)
        for value in node.values():
            _external_refs(value, found)
    elif isinstance(node, list):
        for item in node:
            _external_refs(item, found)
    return found


def _local_refs(node: object, found: set[tuple[str, str]] | None = None) -> set[tuple[str, str]]:
    """The `(component_type, name)` pairs a subtree references locally."""
    found = set() if found is None else found
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            match = _REF.match(ref)
            if match:
                found.add((match.group(1), match.group(2)))
        for value in node.values():
            _local_refs(value, found)
    elif isinstance(node, list):
        for item in node:
            _local_refs(item, found)
    return found


def _security_scheme_names(node: object, found: set[str] | None = None) -> set[str]:
    """Scheme names used by every `security` block in a subtree."""
    found = set() if found is None else found
    if isinstance(node, dict):
        requirements = node.get("security")
        if isinstance(requirements, list):
            for requirement in requirements:
                if isinstance(requirement, dict):
                    found.update(str(name) for name in requirement)
        for value in node.values():
            _security_scheme_names(value, found)
    elif isinstance(node, list):
        for item in node:
            _security_scheme_names(item, found)
    return found


def filter_spec_document(root: dict, operation_ids: list[str]) -> tuple[dict, dict, list[str]]:
    """Build the control's five-operation OpenAPI document.

    :return: (pruned document, unpruned document, the operationIds kept in SPEC ORDER)
    """
    wanted = set(operation_ids)
    kept_ids: list[str] = []
    paths: dict[str, dict] = {}

    for path, path_item in (root.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        selected = {method: operation for method, operation in path_item.items()
                    if method in HTTP_METHODS and isinstance(operation, dict)
                    and operation.get("operationId") in wanted}
        if not selected:
            continue
        entry: dict[str, object] = {}
        # Path-item-level members that apply to the operations we keep.
        for key in ("summary", "description", "servers", "parameters"):
            if key in path_item:
                entry[key] = copy.deepcopy(path_item[key])
        for method in HTTP_METHODS:                      # deterministic method order
            if method in selected:
                entry[method] = copy.deepcopy(selected[method])
                kept_ids.append(selected[method]["operationId"])
        paths[path] = entry

    missing = sorted(wanted - set(kept_ids))
    if missing:
        raise ValueError(f"operationIds not found in the spec: {missing}")

    document: dict[str, object] = {key: copy.deepcopy(root[key]) for key in ROOT_KEEP
                                   if key in root}
    document["paths"] = paths
    if isinstance(root.get("security"), list):
        document["security"] = copy.deepcopy(root["security"])

    # Only the tags the surviving operations actually declare.
    used_tags = {tag for entry in paths.values() for method in HTTP_METHODS
                 for tag in ((entry.get(method) or {}).get("tags") or [])
                 if isinstance(entry.get(method), dict)}
    if used_tags and isinstance(root.get("tags"), list):
        tags = [t for t in root["tags"] if isinstance(t, dict) and t.get("name") in used_tags]
        if tags:
            document["tags"] = copy.deepcopy(tags)

    components = root.get("components") or {}
    unpruned = dict(document)
    if components:
        unpruned["components"] = copy.deepcopy(components)

    # --- transitive $ref closure over components -------------------------------------
    needed: set[tuple[str, str]] = _local_refs(document)
    while True:
        grown = set(needed)
        for component_type, name in list(needed):
            target = (components.get(component_type) or {}).get(name)
            if target is not None:
                grown |= _local_refs(target)
        if grown == needed:
            break
        needed = grown

    # securitySchemes are named, not $ref'd.
    schemes = _security_scheme_names(document) | _security_scheme_names(
        {"security": root.get("security")})
    for name in schemes:
        if name in (components.get("securitySchemes") or {}):
            needed.add(("securitySchemes", name))

    pruned_components: dict[str, dict] = {}
    for component_type, name in sorted(needed):
        source = (components.get(component_type) or {}).get(name)
        if source is None:
            continue
        pruned_components.setdefault(component_type, {})[name] = copy.deepcopy(source)
    if pruned_components:
        document["components"] = {key: pruned_components[key]
                                  for key in sorted(pruned_components)}
    return document, unpruned, kept_ids


def build(whitelist_file: str, work_root: str, only: list[str] | None = None) -> list[dict]:
    import yaml
    import sdk_repair_arm as arm      # read-only import of the arm; stdlib-only at import

    with open(whitelist_file, "r") as file:
        whitelists = json.load(file)

    specs: dict[str, dict] = {}
    results = []
    for entry in whitelists["tasks"]:
        api, index = entry["api"], entry["index"]
        key = f"{api}:{index}"
        if only and key not in only:
            continue

        spec_file = os.path.join(SPEC_DIR, f"{api}.yaml")
        if api not in specs:
            with open(spec_file, "r") as file:
                specs[api] = yaml.safe_load(file)
            external = _external_refs(specs[api])
            assert not external, f"{api}: external $refs would need bundling: {external[:3]}"

        document, unpruned, kept_ids = filter_spec_document(
            specs[api], entry["operation_ids"])

        out_dir = os.path.join(work_root, api, f"{index:04d}_spec")
        os.makedirs(out_dir, exist_ok=True)
        spec_path = os.path.join(out_dir, SPEC_NAME)
        text = yaml.safe_dump(document, sort_keys=False, allow_unicode=True,
                              default_flow_style=False, width=100)
        with open(spec_path, "w") as file:
            file.write(text)
        unpruned_text = yaml.safe_dump(unpruned, sort_keys=False, allow_unicode=True,
                                       default_flow_style=False, width=100)

        # The parameter-type table, from the arm's own function: spec + whitelist, nothing
        # else. Lives beside the filtered spec because the control executor runs node with
        # cwd = this directory, which is where capture_shim.js looks for it by default.
        arm.write_param_types(spec_file, out_dir, operation_ids=entry["operation_ids"])

        results.append({
            "api": api, "index": index, "ok": True,
            "spec": os.path.abspath(spec_path),
            "spec_dir": os.path.abspath(out_dir),
            "operations_spec_order": kept_ids,
            "bytes_pruned": len(text.encode()),
            "bytes_unpruned_components": len(unpruned_text.encode()),
        })
        print(f"{key}: {len(text.encode())} bytes pruned "
              f"({len(unpruned_text.encode())} unpruned), {len(kept_ids)} ops", flush=True)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--whitelists", default=DEFAULT_WHITELISTS)
    parser.add_argument("--work-root", default=DEFAULT_WORK)
    parser.add_argument("--sizes-out", default=DEFAULT_SIZES)
    parser.add_argument("--only", nargs="*", metavar="API:INDEX")
    args = parser.parse_args()

    results = build(args.whitelists, args.work_root, args.only)
    if not args.only:
        with open(args.sizes_out, "w") as file:
            json.dump({"note": "byte sizes of the control's filtered spec per task; "
                               "bytes_unpruned_components is the same document with the "
                               "API's whole components block, for the size confound",
                       "spec_name": SPEC_NAME,
                       "tasks": results}, file, indent=2)
        print(f"wrote {args.sizes_out}")
    print(f"built {len(results)} filtered spec(s)")


if __name__ == "__main__":
    main()
