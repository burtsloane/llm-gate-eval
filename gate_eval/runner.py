"""Resumable evaluation loop.

Results are appended to a JSONL file, one record per corpus item, flushed as
they are written. Re-running skips ids already recorded. Every record carries
the model name and the SHA-256 of both the corpus and the gate spec; resuming
against a changed corpus or gate raises ResumeMismatch instead of silently
mixing results (use fresh=True to start over).
"""
import json
import os
import time

from .judge import parse_verdict
from .spec import render

RAW_KEEP = 300


class ResumeMismatch(RuntimeError):
    pass


def read_results(path):
    recs = []
    if not os.path.exists(path):
        return recs
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except ValueError:
                continue    # torn final line from an interrupted run
    return recs


def results_path(results_dir, model, kind, gate_id):
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in model)
    return os.path.join(results_dir, f"{safe}__{kind}__{gate_id}.jsonl")


def run_gate(*, items, gate, judge, out_path, kind, model, corpus_sha, gate_sha,
             trials=1, fresh=False, progress=None):
    """Judge every item not yet recorded. Returns all records for this file
    (prior + new). A judge transport error propagates after earlier records
    are safely on disk."""
    prior = [] if fresh else read_results(out_path)
    for r in prior:
        if r.get("corpus_sha256") != corpus_sha or r.get("gate_sha256") != gate_sha:
            raise ResumeMismatch(
                f"{out_path} was recorded against a different corpus or gate spec "
                f"(corpus {str(r.get('corpus_sha256'))[:12]} vs {corpus_sha[:12]}, "
                f"gate {str(r.get('gate_sha256'))[:12]} vs {gate_sha[:12]}); rerun with --fresh")
    done = {r["id"] for r in prior}
    todo = [it for it in items if it["id"] not in done]

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    mode = "w" if fresh else "a"
    if mode == "a" and os.path.exists(out_path) and os.path.getsize(out_path):
        with open(out_path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            needs_nl = f.read(1) != b"\n"
    else:
        needs_nl = False

    new = []
    with open(out_path, mode, encoding="utf-8") as fh:
        if needs_nl:
            fh.write("\n")
        for it in todo:
            passes = parse_failures = 0
            raw = ""
            for _ in range(trials):
                system, user = render(gate, it["text"])
                raw = judge.complete(system, user, gate)
                verdict, ok = parse_verdict(raw)
                parse_failures += 0 if ok else 1
                passes += verdict == "PASS"
            rec = {"id": it["id"], "kind": kind, "gate": gate["id"], "bucket": it["bucket"],
                   "gold": it["gold"], "verdict": "PASS" if passes else "FAIL",
                   "passes": passes, "trials": trials, "parse_failures": parse_failures,
                   "raw": (raw or "")[:RAW_KEEP], "model": model,
                   "corpus_sha256": corpus_sha, "gate_sha256": gate_sha, "ts": int(time.time())}
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            new.append(rec)
            if progress:
                progress(rec)
    return prior + new
