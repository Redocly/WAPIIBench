#!/usr/bin/env python3
"""Import `wapiibench/evaluation.py` for its SCORING functions without a GPU stack.

WHY A SHIM IS NEEDED — and exactly what it does, so the report can state it precisely.

`evaluation.py` imports, at module scope:
    from logits_processor import OpenApiDecoder     -> imports torch, transformers
    from model_utils import ModelWrapper            -> imports torch, transformers
    from rag.retriever import Retriever, ...        -> imports torch, langchain_chroma,
                                                       langchain_core, langchain_huggingface,
                                                       sentence_transformers
None of those are on the path to `compare()` / `_compare_configs()` / `_add_path_params()` /
`analyze()` / `_analyze_sample()`, which need only `openapi_parser`, `regex`, `strenum`,
`tqdm` and the stdlib. But Python evaluates the imports before it defines the functions, so
without torch on the machine `import evaluation` fails outright.

THE SHIM: this module installs permissive placeholder modules in `sys.modules` for
    torch, langchain_chroma, langchain_core (+ .documents, .documents.base),
    langchain_huggingface, sentence_transformers
BEFORE importing evaluation, but ONLY for names not already importable — a real installed
package always wins. Each placeholder returns a dummy class for any attribute, which is
enough to satisfy `from X import Y` at import time.

WHAT THE SHIM DOES NOT DO: it does not replace, patch or reimplement any scoring code.
`compare`, `_compare_configs`, `_add_path_params`, `analyze`, `_analyze_sample`, `execute`
and `sdk_repair_arm.execute_sdk_repair` all run as the harness defines them. If any code path
we exercise ever actually touched a stubbed module it would raise immediately rather than
return a wrong number; `assert_scoring_path_unstubbed()` checks the modules the scoring path
really resolved.

CONSEQUENCE FOR THE ESTIMATE: the generation half of the arm (ModelWrapper, the RAG
retriever, constrained decoding) is NOT importable here and is NOT what we are measuring.
We measure execution + comparison only, with an agent standing in for the model.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import types

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WAPIIBENCH_PKG = os.path.join(REPO_ROOT, "wapiibench")

STUBBED_MODULES = (
    "torch",
    "langchain_chroma",
    "langchain_core",
    "langchain_core.documents",
    "langchain_core.documents.base",
    "langchain_huggingface",
    "sentence_transformers",
)

# Modules the scoring path genuinely uses; assert_scoring_path_unstubbed() checks none of
# these ended up stubbed.
REQUIRED_REAL_MODULES = ("openapi_parser", "openapi_parser.specification",
                         "openapi_parser.enumeration", "regex", "strenum", "yaml")

_stubbed: list[str] = []


class _Any:
    """Stands in for any class or callable a stubbed module is asked for."""

    def __init__(self, *args, **kwargs):
        raise RuntimeError("estimate/harness_import.py stub was actually instantiated — the "
                           "code path under test is NOT scoring-only. Install the real "
                           "dependency instead of widening this shim.")

    def __call__(self, *args, **kwargs):
        raise RuntimeError("estimate/harness_import.py stub was actually called.")


class _StubModule(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _Any


def _installable(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is None
    except (ImportError, ValueError):
        return True


def install_stubs() -> list[str]:
    """Install placeholders for the GPU/RAG imports that are absent. Idempotent."""
    for name in STUBBED_MODULES:
        if name in sys.modules or not _installable(name.split(".")[0]):
            continue
        module = _StubModule(name)
        sys.modules[name] = module
        _stubbed.append(name)
        if "." in name:                        # attach to the parent so `from a.b import c` works
            parent_name, _, child = name.rpartition(".")
            parent = sys.modules.get(parent_name)
            if parent is not None:
                setattr(parent, child, module)
    return list(_stubbed)


def load_harness() -> tuple[object, object, list[str]]:
    """Return (evaluation module, sdk_repair_arm module, names that were stubbed)."""
    stubbed = install_stubs()
    if WAPIIBENCH_PKG not in sys.path:
        sys.path.insert(0, WAPIIBENCH_PKG)
    evaluation = importlib.import_module("evaluation")
    sdk_repair_arm = importlib.import_module("sdk_repair_arm")
    assert_scoring_path_unstubbed()
    return evaluation, sdk_repair_arm, stubbed


def assert_scoring_path_unstubbed() -> None:
    for name in REQUIRED_REAL_MODULES:
        module = sys.modules.get(name) or importlib.import_module(name)
        if isinstance(module, _StubModule):
            raise RuntimeError(f"{name} resolved to a stub; scoring would be meaningless")


if __name__ == "__main__":
    evaluation, arm, stubbed = load_harness()
    print(f"stubbed: {stubbed or '(nothing — all real deps present)'}")
    print(f"evaluation from {evaluation.__file__}")
    print(f"sdk_repair_arm from {arm.__file__}")
    print(f"Verdict values: {[str(v) for v in evaluation.Verdict]}")
    print(f"USE_ZOD = {getattr(arm, 'USE_ZOD', None)}")
