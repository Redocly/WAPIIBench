#!/usr/bin/env python3
"""The CONTROL condition's starter code, and the spec-derived auth line that goes in it.

WHY THIS FILE EXISTS AND IS NOT A HARNESS `SETUPS` ENTRY. The treatment's starter is
`evaluation.SETUPS['sdk-invocation']`, and `estimate/emit_prompt.harness_starter()` reads it
live out of `evaluation.py` through the AST so the two can never drift. There is no
fetch-shaped entry in `evaluation.SETUPS` to read — the harness's own starters are
`axios.`-shaped — and `wapiibench/evaluation.py` belongs to the arm, not to the control. So
the control's starter is defined here instead.

THAT IS A REAL, IF SMALL, ASYMMETRY AND IS DISCLOSED (control/README.md): the treatment's
starter comes from the harness, the control's comes from the control. Its CONTENT is
constructed to mirror the treatment's line for line:

    treatment                                          control
    ------------------------------------------------   ------------------------------------
    // {task}                                          // {task}
    import { client } from './client';                 (nothing — no client to import)
    import { zodValidation } from './client.zod';      (nothing — no generated zod schemas)
    client.configure({ fetch: __wapiiCaptureFetch,     (nothing — capture is installed on
                       clientHeader: false });          globalThis.fetch by the executor,
                                                        invisibly to the answer)
    client.use(zodValidation());                       (nothing — no request validation)
    client.auth.bearer('<token>');                     const AUTH_HEADERS = { Authorization:
                                                         'Bearer <token>' };
    (blank line, the model continues)                  fetch(

The two ends of the auth row are NOT equivalent and that is the sharpest asymmetry in the
control (see `auth_headers_for`).

THE `fetch(` TAIL mirrors `evaluation.SETUPS['invocation']`, whose last characters are
`axios.` — the harness's own way of pinning the request mechanism in the starter rather than
in prose. It is what makes this a *plain fetch* condition rather than "write a request
somehow".
"""

from __future__ import annotations

import os
import re

AUTH_TOKEN_PLACEHOLDER = "<token>"          # == sdk_repair_arm.AUTH_TOKEN_PLACEHOLDER
AUTH_CONST_NAME = "AUTH_HEADERS"

# `{task}` and `{auth_setup}` are the same two placeholders the treatment's starter carries,
# rendered by the emitter exactly as `sdk_repair_arm.assemble_prompt()` renders them there.
STARTER = """\
// {task}
{auth_setup}
fetch(\
"""

# Scheme types that make the generated client emit `kind: "bearer"`, i.e. that end up writing
# a standard `Authorization` header. Verified against the generated clients of all 68 sampled
# tasks (control/auth_parity.json): `oauth2` (google_*: `Oauth2`/`Oauth2c`) and `http` with
# `scheme: bearer` (slack: `slackAuth`).
_BEARER_TYPES = ("oauth2", "openIdConnect")


def _resolve_scheme(root: dict, name: str) -> dict:
    scheme = ((root.get("components") or {}).get("securitySchemes") or {}).get(name)
    return scheme if isinstance(scheme, dict) else {}


def _is_bearer(scheme: dict) -> bool:
    kind = str(scheme.get("type") or "")
    if kind in _BEARER_TYPES:
        return True
    return kind == "http" and str(scheme.get("scheme") or "").lower() == "bearer"


def auth_headers_for(spec_file: str, operation_ids: list[str]) -> str:
    """The control's counterpart of `sdk_repair_arm.auth_setup_for()`, from the SPEC.

    GATED ON THE SPEC, NOT ON THE ANSWER, exactly as the treatment's version is: the decision
    reads the whitelisted operations' `security` requirements and the `securitySchemes` they
    name — never a task, a dataset file or an expected config. An operation that declares no
    security gets no auth constant, which is what stops the control bolting an unexpected
    `Authorization` header onto an unauthenticated endpoint (scored UNNECESSARY_KEY or
    ILLEGAL_KEY, and an ILLEGAL_KEY forces `sample_verdict = 'illegal'`).

    Mirrors the treatment's rule in three specifics, because a difference in any of them
    would change the score for reasons unrelated to the SDK:
      * BEARER ONLY. `apiKey` places a NAMED parameter and `basic` needs credentials, so
        emitting either would fabricate an argument. `sdk_repair_arm.auth_setup_for()` emits
        a line only for `kind == "bearer"`; so does this.
      * THE FIRST security requirement found over the whitelisted operations in SPEC ORDER,
        with the root-level `security` as the fallback default — `auth_setup_for()` regexes
        the FIRST `security: [[...]]` in the generated `OPERATIONS` map, which is spec order.
      * The placeholder is `<token>`, which is what all 395 synthetic expected configs
        contain (`Authorization: "Bearer <token>"`).

    `control/check_auth_parity.py` asserts this function agrees with
    `sdk_repair_arm.auth_setup_for()` on all 68 sampled tasks.

    :return: the starter line, or "" when no whitelisted operation declares bearer security.
    """
    import yaml

    with open(spec_file, "r") as file:
        root = yaml.safe_load(file)

    wanted = set(operation_ids)
    default = root.get("security")
    methods = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
    for _path, path_item in (root.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        for method in methods:
            operation = path_item.get(method)
            if not isinstance(operation, dict) or operation.get("operationId") not in wanted:
                continue
            requirements = operation.get("security")
            if requirements is None:
                requirements = default
            if not isinstance(requirements, list) or not requirements:
                continue
            first = requirements[0]
            if not isinstance(first, dict):
                continue
            for name in first:
                if _is_bearer(_resolve_scheme(root, name)):
                    return (f"const {AUTH_CONST_NAME} = "
                            f"{{ Authorization: 'Bearer {AUTH_TOKEN_PLACEHOLDER}' }};")
            return ""              # first requirement found, and it is not bearer
    return ""


def render_starter(task_text: str, auth_setup: str) -> str:
    """The starter as it reaches the generator agent."""
    return STARTER.replace("{task}", task_text).replace("{auth_setup}", auth_setup)


def has_auth_const(starter: str) -> bool:
    return bool(re.search(rf"\b{AUTH_CONST_NAME}\b", starter))
