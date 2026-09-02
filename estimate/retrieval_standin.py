#!/usr/bin/env python3
"""Lexical retrieval stand-in for the paper's embedding retriever.

WHY A STAND-IN (declared deviation — see estimate/README.md and the report):
`wapiibench/rag/retriever.py` uses `langchain_huggingface.HuggingFaceEmbeddings`
("all-MiniLM-L6-v2") plus a `sentence_transformers.CrossEncoder` reranker. Both download
weights from huggingface.co, which is blocked by network policy in this environment, so the
paper's retriever CANNOT be reproduced here. This module is a pure-Python BM25 stand-in over
each operation's path, method, operationId, summary, description, parameter names and
request-body property names. It downloads nothing.

WHAT IT IS FOR: choosing the FIVE operations each task's typed client is generated from
(`num_chunks = 5` in the paper's RAG setting). Five, not one: the synthetic dataset has
exactly one task per operation, so a single-operation whitelist would hand the model endpoint
selection for free and make the url/method verdicts trivially correct. The four distractors
must be plausible-but-wrong, which is why they are retrieved rather than sampled at random.

GROUND-TRUTH GUARANTEE AND ITS COST: the whitelist ALWAYS contains the ground-truth
operation. If the retriever's top 5 already contains it, the whitelist is exactly that top 5.
If it does not, the whitelist is [ground truth] + the top 4 retrieved (a "substitution"), so
the task stays solvable and the estimate measures the ARM rather than the stand-in retriever.
Substitutions are counted and reported, because they are a favourable bias: on those tasks the
model is handed a candidate set a real end-to-end pipeline would not have produced.

Spec parsing here is raw PyYAML, not `openapi_utils.parse_spec` — the harness's parser
validates and fully resolves these 0.5-0.7 MB specs and takes minutes each. The
ground-truth operationId resolution done here is cross-checked against the harness's own
`find_path_in_spec` / `find_operation_in_path` by estimate/crosscheck_gt_ops.py.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import os
import re

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC_DIR = os.path.join(REPO_ROOT, "openapi", "real_world_specs")
DATASET_DIR = os.path.join(REPO_ROOT, "data", "synthetic")

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
WHITELIST_SIZE = 5

STOPWORDS = frozenset("""a an and are as at be by for from get in into is it its of on or that
the this to with will can should must does do if when what which use used using return
returns request response api endpoint http https true false null value values object
""".split())


# --------------------------------------------------------------------------------------- #
# spec -> operation records
# --------------------------------------------------------------------------------------- #

def _deref(node, root, depth: int = 0):
    """Resolve local $refs (one hop at a time, bounded)."""
    while isinstance(node, dict) and "$ref" in node and depth < 8:
        ref = node["$ref"]
        if not ref.startswith("#/"):
            return node
        target = root
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, dict) or part not in target:
                return node
            target = target[part]
        node, depth = target, depth + 1
    return node


def _body_property_names(operation, root) -> list[str]:
    body = _deref(operation.get("requestBody") or {}, root)
    names: list[str] = []
    for media in (body.get("content") or {}).values():
        schema = _deref((media or {}).get("schema") or {}, root)
        names.extend((schema.get("properties") or {}).keys())
        data = _deref((schema.get("properties") or {}).get("data") or {}, root)
        names.extend((data.get("properties") or {}).keys())
    return names


def load_operations(api: str) -> list[dict]:
    """Every operation in an API's spec, as a flat record list. Order is spec order."""
    with open(os.path.join(SPEC_DIR, f"{api}.yaml"), "r") as file:
        root = yaml.safe_load(file)

    servers = [s.get("url", "") for s in (root.get("servers") or [])]
    records: list[dict] = []
    for path, path_item in (root.get("paths") or {}).items():
        path_item = _deref(path_item or {}, root)
        shared_params = path_item.get("parameters") or []
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            params = [_deref(p, root) for p in (shared_params + (operation.get("parameters") or []))]
            records.append({
                "operation_id": operation.get("operationId"),
                "method": method,
                "path": path,
                "summary": operation.get("summary") or "",
                "description": operation.get("description") or "",
                "tags": operation.get("tags") or [],
                "param_names": [p.get("name") for p in params if isinstance(p, dict) and p.get("name")],
                "body_props": _body_property_names(operation, root),
                "servers": [s.get("url", "") for s in (operation.get("servers") or [])] or servers,
            })
    return records


# --------------------------------------------------------------------------------------- #
# ground-truth operation resolution (url + method -> operationId)
# --------------------------------------------------------------------------------------- #

def _path_regex(server: str, path: str) -> re.Pattern:
    pattern = re.escape(server.rstrip("/") + path)
    pattern = re.sub(r"\\\{[^}]*\\\}", r"[^/]+", pattern)
    return re.compile(rf"^{pattern}/?$")


def resolve_ground_truth(operations: list[dict], config: dict) -> str | None:
    """The operationId the expected config points at, or None if unresolvable.

    ONLY the scoring/whitelist side may call this. It reads ground truth. It must never be
    reachable from the prompt-emission path (see estimate/emit_prompt.py).
    """
    url = (config["url"] or "").split("?", 1)[0]
    method = (config["method"] or "").lower()
    matches: list[tuple[int, dict]] = []
    for record in operations:
        if record["method"] != method:
            continue
        for server in record["servers"]:
            if _path_regex(server, record["path"]).match(url):
                # Prefer the most literal path (fewest templated segments, longest literal).
                templated = record["path"].count("{")
                matches.append((templated * 1000 - len(record["path"]), record))
                break
    if not matches:
        return None
    matches.sort(key=lambda pair: pair[0])
    return matches[0][1]["operation_id"]


# --------------------------------------------------------------------------------------- #
# BM25
# --------------------------------------------------------------------------------------- #

_SPLIT = re.compile(r"[^A-Za-z0-9]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for chunk in _SPLIT.split(text or ""):
        if not chunk:
            continue
        for part in _CAMEL.split(chunk):
            part = part.lower()
            if len(part) < 2 or part in STOPWORDS:
                continue
            tokens.append(part[:-1] if len(part) > 3 and part.endswith("s") else part)
    return tokens


def operation_document(record: dict) -> str:
    """The text a retriever sees for one operation. No ground truth involved — this is derived
    from the spec alone and is identical for every task on the same API."""
    return " ".join([
        record["path"], record["method"], record["operation_id"] or "",
        " ".join(record["tags"]), record["summary"], record["description"][:2000],
        " ".join(record["param_names"]), " ".join(record["body_props"]),
    ])


class BM25:
    """Okapi BM25. Standard k1 = 1.5, b = 0.75."""

    def __init__(self, documents: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs = documents
        self.doc_len = [len(d) for d in documents]
        self.avgdl = (sum(self.doc_len) / len(documents)) if documents else 0.0
        self.tf = [collections.Counter(d) for d in documents]
        df: collections.Counter = collections.Counter()
        for doc in documents:
            df.update(set(doc))
        n = len(documents)
        self.idf = {term: math.log(1 + (n - freq + 0.5) / (freq + 0.5)) for term, freq in df.items()}

    def scores(self, query: list[str]) -> list[float]:
        out = [0.0] * len(self.docs)
        for term in query:
            idf = self.idf.get(term)
            if idf is None:
                continue
            for i, tf in enumerate(self.tf):
                freq = tf.get(term, 0)
                if not freq:
                    continue
                denom = freq + self.k1 * (1 - self.b + self.b * self.doc_len[i] / (self.avgdl or 1))
                out[i] += idf * freq * (self.k1 + 1) / denom
        return out


class Retriever:
    """Per-API BM25 index over operations."""

    def __init__(self, api: str):
        self.api = api
        self.operations = [r for r in load_operations(api) if r["operation_id"]]
        self.index = BM25([tokenize(operation_document(r)) for r in self.operations])

    def rank(self, task_text: str) -> list[str]:
        """operationIds, best first. Deterministic: ties broken by spec order."""
        scores = self.index.scores(tokenize(task_text))
        order = sorted(range(len(self.operations)), key=lambda i: (-scores[i], i))
        return [self.operations[i]["operation_id"] for i in order]


# --------------------------------------------------------------------------------------- #
# whitelist construction
# --------------------------------------------------------------------------------------- #

def build_whitelists(sample_file: str, size: int = WHITELIST_SIZE) -> dict:
    with open(sample_file, "r") as file:
        sample = json.load(file)

    by_api: dict[str, list[int]] = collections.defaultdict(list)
    for task_id in sample["tasks"]:
        by_api[task_id["api"]].append(task_id["index"])

    entries, stats = [], collections.Counter()
    for api in sorted(by_api):
        retriever = Retriever(api)
        with open(os.path.join(DATASET_DIR, api, "test_data_final.json"), "r") as file:
            tasks = json.load(file)
        for index in sorted(by_api[api]):
            task = tasks[index]
            ranked = retriever.rank(task["task"])            # blinded: task text only
            truth = resolve_ground_truth(retriever.operations, task["config"])  # scoring side

            top = ranked[:size]
            stats["total"] += 1
            if truth is None:
                stats["unresolved_ground_truth"] += 1
                whitelist, substituted = top, False
            else:
                if ranked and ranked[0] == truth:
                    stats["top1_hit"] += 1
                if truth in top:
                    stats["top5_hit"] += 1
                    whitelist, substituted = top, False
                else:
                    whitelist = [truth] + [op for op in ranked if op != truth][:size - 1]
                    substituted = True
                    stats["substituted"] += 1
            entries.append({
                "api": api, "index": index,
                "operation_ids": whitelist,
                "ground_truth_in_retrieved_top5": not substituted,
                "ground_truth_substituted": substituted,
                "retriever_rank_of_ground_truth":
                    (ranked.index(truth) + 1) if truth in ranked else None,
            })

    total = stats["total"] or 1
    return {
        "retriever": "bm25-lexical-standin",
        "note": "declared deviation from the paper's all-MiniLM-L6-v2 + CrossEncoder retriever;"
                " huggingface.co is blocked by network policy in this environment",
        "whitelist_size": size,
        "accuracy": {
            "n": stats["total"],
            "top1": stats["top1_hit"] / total,
            "top5": stats["top5_hit"] / total,
            "substituted": stats["substituted"],
            "substituted_fraction": stats["substituted"] / total,
            "unresolved_ground_truth": stats["unresolved_ground_truth"],
            "paper_reference": {"top1": 0.757, "top5": 0.952},
        },
        "tasks": entries,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sample", default=os.path.join(REPO_ROOT, "estimate", "sample.json"))
    parser.add_argument("--out", default=os.path.join(REPO_ROOT, "estimate", "whitelists.json"))
    parser.add_argument("--size", type=int, default=WHITELIST_SIZE)
    args = parser.parse_args()

    result = build_whitelists(args.sample, args.size)
    with open(args.out, "w") as file:
        json.dump(result, file, indent=2)
    acc = result["accuracy"]
    print(f"n = {acc['n']}")
    print(f"stand-in top-1 = {acc['top1']:.3f}  (paper: {acc['paper_reference']['top1']})")
    print(f"stand-in top-5 = {acc['top5']:.3f}  (paper: {acc['paper_reference']['top5']})")
    print(f"ground-truth substitutions = {acc['substituted']}/{acc['n']} "
          f"({acc['substituted_fraction']:.3f})")
    print(f"unresolved ground truth    = {acc['unresolved_ground_truth']}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
