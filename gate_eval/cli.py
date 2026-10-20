"""Command line: validate | sincerity | redteam | report.

Exit codes: 0 ok, 1 a ship-gate threshold was breached, 2 usage/config/
transport error or a resume mismatch.
"""
import argparse
import glob
import os
import sys

from .corpus import CorpusError, load_redteam, load_sincerity, validate_corpus
from .judge import JudgeTransportError, make_judge
from .runner import ResumeMismatch, read_results, results_path, run_gate
from .scoring import check_thresholds, format_table, score
from .spec import SpecError, canonical_sha256, load_gate, validate_gate

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JUDGES = ("llamacpp", "openai", "mock-always-pass", "mock-always-fail", "mock-keyword")


def _load_json(path):
    import json
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _gate_paths(gates_dir, wanted):
    paths = sorted(glob.glob(os.path.join(gates_dir, "*.json")))
    if wanted:
        names = [w.strip() for w in wanted.split(",") if w.strip()]
        by_id = {os.path.splitext(os.path.basename(p))[0]: p for p in paths}
        missing = [n for n in names if n not in by_id]
        if missing:
            raise SpecError(f"unknown gate(s): {', '.join(missing)} (have: {', '.join(by_id)})")
        paths = [by_id[n] for n in names]
    if not paths:
        raise SpecError(f"no gate specs in {gates_dir}")
    return paths


def cmd_validate(args):
    problems = 0
    for p in sorted(glob.glob(os.path.join(args.gates_dir, "*.json"))):
        errs = validate_gate(_load_json(p))
        problems += len(errs)
        print(f"{'ok ' if not errs else 'BAD'} {os.path.relpath(p, args.root)}")
        for e in errs:
            print(f"      {e}")
    for p in sorted(glob.glob(os.path.join(args.corpus_dir, "*", "*.json"))):
        obj = _load_json(p)
        errs = validate_corpus(obj)
        problems += len(errs)
        n = len(obj.get("items", [])) if isinstance(obj, dict) else 0
        print(f"{'ok ' if not errs else 'BAD'} {os.path.relpath(p, args.root)}  ({n} items)")
        for e in errs[:10]:
            print(f"      {e}")
    print(f"{problems} problem(s)")
    return 0 if problems == 0 else 2


def _judge_from_args(args):
    kind = "mock-always-fail" if args.dry_run else args.judge
    if not kind:
        raise ValueError("choose a --judge (or --dry-run)")
    judge = make_judge(kind, url=args.url, model=args.model, api_key_env=args.api_key_env,
                       temperature=args.temperature, max_tokens=args.max_tokens,
                       timeout=args.timeout, prompt_format=args.prompt_format) \
        if not kind.startswith("mock-") else make_judge(kind)
    model = args.model_name or ("dry-run" if args.dry_run else (args.model or judge.name))
    return judge, model


def _finish(summaries, args):
    breaches = check_thresholds(summaries, args.max_leaks, args.max_fn_rate)
    if args.max_leaks is not None or args.max_fn_rate is not None:
        if breaches:
            print("\nSHIP GATE: FAIL")
            for b in breaches:
                print(f"  - {b}")
            return 1
        print("\nSHIP GATE: PASS")
    return 0


def _progress(rec):
    bad = (rec["gold"] == "pass" and rec["verdict"] == "FAIL") or \
          (rec["gold"] == "fail" and rec["verdict"] == "PASS")
    sys.stdout.write("x" if bad else ".")
    sys.stdout.flush()


def cmd_run(args):
    judge, model = _judge_from_args(args)
    summaries = {}
    rt_items = rt_sha = None
    if args.command == "redteam":
        rt_items, rt_sha = load_redteam(args.corpus_dir)
        if args.categories:
            keep = {c.strip() for c in args.categories.split(",")}
            rt_items = [it for it in rt_items if it["bucket"] in keep]
    for gp in _gate_paths(args.gates_dir, args.gates):
        gate = load_gate(gp)
        gate_sha = canonical_sha256(gate)
        if args.command == "sincerity":
            items, corpus_sha = load_sincerity(args.corpus_dir, gate["id"])
        else:
            items, corpus_sha = rt_items, rt_sha
        if args.limit:
            items = items[:args.limit]
        out = results_path(args.results_dir, model, args.command, gate["id"])
        print(f"\n{args.command} :: {gate['id']}  model={model}  items={len(items)}"
              f"  corpus={corpus_sha[:12]}  gate={gate_sha[:12]}  -> {out}")
        recs = run_gate(items=items, gate=gate, judge=judge, out_path=out, kind=args.command,
                        model=model, corpus_sha=corpus_sha, gate_sha=gate_sha,
                        trials=args.trials, fresh=args.fresh, progress=_progress)
        print()
        ids = {it["id"] for it in items}
        recs = [r for r in recs if r["id"] in ids]
        s = score(recs)
        summaries[f"{args.command}:{gate['id']}"] = s
        texts = {it["id"]: it["text"] for it in items}
        print(format_table(f"{gate['id']}  ({args.command}, model={model})", s, texts))
    return _finish(summaries, args)


def cmd_report(args):
    summaries = {}
    for path in args.files:
        recs = read_results(path)
        if not recs:
            print(f"(no records in {path})")
            continue
        s = score(recs)
        summaries[os.path.basename(path)] = s
        r0 = recs[0]
        print(format_table(f"{os.path.basename(path)}  model={r0.get('model')}  "
                           f"corpus={str(r0.get('corpus_sha256'))[:12]}", s))
    return _finish(summaries, args)


def build_parser():
    ap = argparse.ArgumentParser(prog="python -m gate_eval",
                                 description="Calibrate an LLM-as-judge release gate.")
    ap.add_argument("--root", default=ROOT, help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="command", required=True)

    def thresholds(p):
        p.add_argument("--max-leaks", type=int, default=None,
                       help="ship gate: fail (exit 1) if total leaks exceed N")
        p.add_argument("--max-fn-rate", type=float, default=None,
                       help="ship gate: fail (exit 1) if any gate's FN rate exceeds X (0..1)")

    def dirs(p):
        p.add_argument("--gates-dir", default=None)
        p.add_argument("--corpus-dir", default=None)

    v = sub.add_parser("validate", help="schema-check every gate spec and corpus file")
    dirs(v)

    for name, hlp in (("sincerity", "run the gold-labeled sincerity corpus"),
                      ("redteam", "run the red-team suite (every item must FAIL)")):
        p = sub.add_parser(name, help=hlp)
        dirs(p)
        p.add_argument("--judge", choices=JUDGES)
        p.add_argument("--dry-run", action="store_true",
                       help="offline: use the always-fail mock judge, no network")
        p.add_argument("--url", help="judge server base URL")
        p.add_argument("--model", help="model field for OpenAI-compatible servers")
        p.add_argument("--model-name", help="label stamped on results (default: --model or judge)")
        p.add_argument("--api-key-env", default="OPENAI_API_KEY",
                       help="env var holding a bearer token for --judge openai")
        p.add_argument("--prompt-format", choices=("chatml", "plain"), default="chatml",
                       help="raw prompt layout for --judge llamacpp")
        p.add_argument("--temperature", type=float, default=0.0)
        p.add_argument("--max-tokens", type=int, default=64)
        p.add_argument("--timeout", type=float, default=120)
        p.add_argument("--gates", default="", help="comma list of gate ids (default: all)")
        p.add_argument("--trials", type=int, default=1,
                       help="judge each item N times; it counts as PASS if any trial passed")
        p.add_argument("--limit", type=int, default=0, help="first N items per gate (smoke test)")
        p.add_argument("--fresh", action="store_true", help="discard prior results instead of resuming")
        p.add_argument("--results-dir", default="results")
        if name == "redteam":
            p.add_argument("--categories", default="", help="comma list of red-team categories")
        thresholds(p)

    r = sub.add_parser("report", help="re-score existing results files (no model calls)")
    r.add_argument("files", nargs="+")
    thresholds(r)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    if hasattr(args, "gates_dir"):
        args.gates_dir = args.gates_dir or os.path.join(args.root, "gates")
        args.corpus_dir = args.corpus_dir or os.path.join(args.root, "corpus")
    try:
        if args.command == "validate":
            return cmd_validate(args)
        if args.command == "report":
            return cmd_report(args)
        if args.trials < 1:
            raise ValueError("--trials must be >= 1")
        return cmd_run(args)
    except (SpecError, CorpusError, ResumeMismatch, JudgeTransportError, ValueError) as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return 2
