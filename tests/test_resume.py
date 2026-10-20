"""Resumable runs: skip recorded ids, survive torn lines, refuse mixed hashes."""
import json
import os
import tempfile
import unittest

from gate_eval.judge import JudgeTransportError, MockJudge
from gate_eval.runner import ResumeMismatch, read_results, run_gate

GATE = {"id": "g", "context": "c", "criterion": "k", "keywords": [],
        "judge_prompt": {"system": "{{context}} {{criterion}}", "user": "{{text}}"}}
ITEMS = [{"id": f"i{n}", "bucket": "near_miss", "gold": "fail", "text": f"t{n}", "notes": ""}
         for n in range(5)]


class FlakyJudge(MockJudge):
    """Fails with a transport error on the Nth call."""
    def __init__(self, die_on):
        super().__init__("always-fail")
        self.die_on = die_on

    def complete(self, system, user, gate=None):
        if self.calls + 1 == self.die_on:
            self.calls += 1
            raise JudgeTransportError("server went away")
        return super().complete(system, user, gate)


class TestResume(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "r.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def run_(self, judge, items=ITEMS, corpus="c1", gate="g1", fresh=False, trials=1):
        return run_gate(items=items, gate=GATE, judge=judge, out_path=self.out, kind="sincerity",
                        model="m", corpus_sha=corpus, gate_sha=gate, fresh=fresh, trials=trials)

    def test_second_run_skips_done(self):
        j1 = MockJudge("always-fail")
        self.run_(j1, items=ITEMS[:2])
        self.assertEqual(j1.calls, 2)
        j2 = MockJudge("always-fail")
        recs = self.run_(j2)
        self.assertEqual(j2.calls, 3)
        self.assertEqual([r["id"] for r in recs], [it["id"] for it in ITEMS])
        self.assertEqual(len(read_results(self.out)), 5)
        j3 = MockJudge("always-fail")
        self.run_(j3)
        self.assertEqual(j3.calls, 0)

    def test_records_are_stamped(self):
        rec = self.run_(MockJudge("always-pass"), items=ITEMS[:1])[0]
        for k in ("id", "gate", "bucket", "gold", "verdict", "model", "corpus_sha256",
                  "gate_sha256", "raw", "parse_failures", "passes", "trials", "ts"):
            self.assertIn(k, rec)
        self.assertEqual((rec["model"], rec["corpus_sha256"], rec["verdict"]), ("m", "c1", "PASS"))

    def test_transport_error_keeps_earlier_records(self):
        with self.assertRaises(JudgeTransportError):
            self.run_(FlakyJudge(die_on=4))
        self.assertEqual(len(read_results(self.out)), 3)
        j = MockJudge("always-fail")
        self.run_(j)
        self.assertEqual(j.calls, 2)

    def test_torn_last_line_is_tolerated_and_repaired(self):
        self.run_(MockJudge("always-fail"), items=ITEMS[:2])
        with open(self.out, "a") as f:
            f.write('{"id": "i2", "verd')            # interrupted mid-write
        j = MockJudge("always-fail")
        self.run_(j)
        self.assertEqual(j.calls, 3)                 # i2 was not counted as done
        with open(self.out) as f:
            good = [l for l in f if l.strip()]
        self.assertEqual(sum(1 for l in good if l.startswith('{"id": "i2", "verd') and
                             not l.rstrip().endswith("}")), 1)
        self.assertEqual(len(read_results(self.out)), 5)

    def test_changed_corpus_or_gate_refuses(self):
        self.run_(MockJudge("always-fail"), items=ITEMS[:2])
        with self.assertRaises(ResumeMismatch):
            self.run_(MockJudge("always-fail"), corpus="c2")
        with self.assertRaises(ResumeMismatch):
            self.run_(MockJudge("always-fail"), gate="g2")

    def test_fresh_overwrites(self):
        self.run_(MockJudge("always-fail"), items=ITEMS[:2])
        j = MockJudge("always-pass")
        recs = self.run_(j, corpus="c2", fresh=True)
        self.assertEqual(j.calls, 5)
        self.assertEqual(len(read_results(self.out)), 5)
        self.assertTrue(all(r["verdict"] == "PASS" and r["corpus_sha256"] == "c2" for r in recs))

    def test_trials_any_pass_counts_as_pass(self):
        class Alternating(MockJudge):
            def complete(self, s, u, g=None):
                self.calls += 1
                return "VERDICT: PASS" if self.calls % 2 == 0 else "garbage"
        rec = self.run_(Alternating("always-fail"), items=ITEMS[:1], trials=2)[0]
        self.assertEqual((rec["verdict"], rec["passes"], rec["parse_failures"]), ("PASS", 1, 1))


if __name__ == "__main__":
    unittest.main()
