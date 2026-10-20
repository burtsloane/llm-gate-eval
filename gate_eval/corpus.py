"""Corpus files: gold-labeled player inputs.

Schema "llm-gate-eval/corpus@1":

    {"schema": "llm-gate-eval/corpus@1",
     "kind": "sincerity" | "redteam",
     "gate": "<gate id>" (sincerity) | null (red-team applies to every gate),
     "items": [{"id", "bucket", "gold", "text", "notes"}, ...]}

gold is "pass" (a correct gate lets it through), "fail" (a correct gate
blocks it) or "grey" (defensible either way; reported, never scored).
Every red-team item is gold "fail".
"""
import glob
import json
import os

from .spec import canonical_sha256

CORPUS_SCHEMA = "llm-gate-eval/corpus@1"
GOLDS = ("pass", "fail", "grey")
ITEM_KEYS = ("id", "bucket", "gold", "text", "notes")

# Sincerity buckets and the gold label each must carry.
SINCERITY_BUCKETS = {
    "sincere_direct": "pass",
    "sincere_paraphrase": "pass",
    "sincere_oblique": "grey",
    "near_miss": "fail",
    "off_topic": "fail",
    "insincere_hollow": "fail",
    "adversarial_gaming": "fail",
}


class CorpusError(ValueError):
    pass


def validate_corpus(obj):
    """Return a list of problems (empty list = valid)."""
    errs = []
    if not isinstance(obj, dict):
        return ["corpus must be a JSON object"]
    if obj.get("schema") != CORPUS_SCHEMA:
        errs.append(f"schema must be {CORPUS_SCHEMA!r}")
    kind = obj.get("kind")
    if kind not in ("sincerity", "redteam"):
        errs.append("kind must be 'sincerity' or 'redteam'")
    if kind == "sincerity" and not (isinstance(obj.get("gate"), str) and obj["gate"]):
        errs.append("a sincerity corpus must name its gate")
    if kind == "redteam" and obj.get("gate") is not None:
        errs.append("a red-team corpus must have gate: null")
    items = obj.get("items")
    if not isinstance(items, list) or not items:
        return errs + ["items must be a non-empty list"]
    seen = set()
    for i, it in enumerate(items):
        where = f"items[{i}]"
        if not isinstance(it, dict):
            errs.append(f"{where} must be an object")
            continue
        if set(it) != set(ITEM_KEYS):
            errs.append(f"{where} keys must be exactly {ITEM_KEYS}, got {sorted(it)}")
        for k in ITEM_KEYS:
            if not isinstance(it.get(k), str):
                errs.append(f"{where}.{k} must be a string")
        if not (isinstance(it.get("text"), str) and it["text"].strip()):
            errs.append(f"{where}.text must be non-empty")
        iid = it.get("id")
        if iid in seen:
            errs.append(f"{where}: duplicate id {iid!r}")
        seen.add(iid)
        gold = it.get("gold")
        if gold not in GOLDS:
            errs.append(f"{where}.gold must be one of {GOLDS}")
        if kind == "sincerity":
            want = SINCERITY_BUCKETS.get(it.get("bucket"))
            if want is None:
                errs.append(f"{where}.bucket {it.get('bucket')!r} is not a sincerity bucket")
            elif gold != want:
                errs.append(f"{where}: bucket {it['bucket']} requires gold {want!r}")
        if kind == "redteam" and gold != "fail":
            errs.append(f"{where}: every red-team item must be gold 'fail'")
    return errs


def load_corpus(path):
    with open(path, encoding="utf-8") as f:
        obj = json.load(f)
    errs = validate_corpus(obj)
    if errs:
        raise CorpusError(f"{path}: " + "; ".join(errs[:5]))
    return obj


def load_sincerity(corpus_dir, gate_id):
    """Return (items, sha256) for one gate's sincerity corpus."""
    path = os.path.join(corpus_dir, "sincerity", f"{gate_id}.json")
    if not os.path.exists(path):
        raise CorpusError(f"no sincerity corpus for gate {gate_id!r} at {path}")
    obj = load_corpus(path)
    if obj["gate"] != gate_id:
        raise CorpusError(f"{path} declares gate {obj['gate']!r}, expected {gate_id!r}")
    return obj["items"], canonical_sha256(obj)


def load_redteam(corpus_dir):
    """Return (items, sha256) for the whole red-team suite (all category files)."""
    files = sorted(glob.glob(os.path.join(corpus_dir, "redteam", "*.json")))
    if not files:
        raise CorpusError(f"no red-team corpus files in {corpus_dir}/redteam")
    objs = [load_corpus(p) for p in files]
    items = [it for o in objs for it in o["items"]]
    ids = [it["id"] for it in items]
    if len(ids) != len(set(ids)):
        raise CorpusError("duplicate ids across red-team files")
    return items, canonical_sha256(objs)
