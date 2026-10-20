"""Every shipped gate spec and corpus file must validate."""
import glob
import json
import os
import unittest

from gate_eval.corpus import (SINCERITY_BUCKETS, load_redteam, load_sincerity,
                              validate_corpus)
from gate_eval.spec import render, validate_gate

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATES = sorted(glob.glob(os.path.join(ROOT, "gates", "*.json")))
CORPORA = sorted(glob.glob(os.path.join(ROOT, "corpus", "*", "*.json")))


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


class TestShippedFiles(unittest.TestCase):
    def test_files_exist(self):
        self.assertGreaterEqual(len(GATES), 1)
        self.assertGreaterEqual(len(CORPORA), 2)

    def test_every_gate_spec_valid(self):
        for p in GATES:
            with self.subTest(gate=os.path.basename(p)):
                self.assertEqual(validate_gate(load(p)), [])

    def test_every_corpus_file_valid(self):
        for p in CORPORA:
            with self.subTest(corpus=os.path.relpath(p, ROOT)):
                self.assertEqual(validate_corpus(load(p)), [])

    def test_every_gate_has_a_full_sincerity_corpus(self):
        for p in GATES:
            gid = load(p)["id"]
            items, sha = load_sincerity(os.path.join(ROOT, "corpus"), gid)
            self.assertEqual(len(sha), 64)
            self.assertEqual({it["bucket"] for it in items}, set(SINCERITY_BUCKETS))

    def test_ids_unique_across_all_files(self):
        ids = [it["id"] for p in CORPORA for it in load(p)["items"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_redteam_is_all_gold_fail(self):
        items, _ = load_redteam(os.path.join(ROOT, "corpus"))
        self.assertTrue(items)
        self.assertTrue(all(it["gold"] == "fail" for it in items))

    def test_every_item_renders_into_every_gate(self):
        texts = [it["text"] for p in CORPORA for it in load(p)["items"]]
        for p in GATES:
            spec = load(p)
            for t in texts:
                system, user = render(spec, t)
                self.assertNotIn("{{", system + user.replace(t, ""))
                self.assertEqual(user.count("</player_text>"), 1)


class TestValidatorCatchesProblems(unittest.TestCase):
    def good(self):
        return {"schema": "llm-gate-eval/corpus@1", "kind": "sincerity", "gate": "g",
                "items": [{"id": "a", "bucket": "sincere_direct", "gold": "pass",
                           "text": "x", "notes": ""}]}

    def test_good_is_valid(self):
        self.assertEqual(validate_corpus(self.good()), [])

    def test_wrong_gold_for_bucket(self):
        c = self.good()
        c["items"][0]["gold"] = "fail"
        self.assertTrue(validate_corpus(c))

    def test_extra_key_rejected(self):
        c = self.good()
        c["items"][0]["persona"] = "p"
        self.assertTrue(validate_corpus(c))

    def test_duplicate_id_rejected(self):
        c = self.good()
        c["items"].append(dict(c["items"][0]))
        self.assertTrue(any("duplicate" in e for e in validate_corpus(c)))

    def test_redteam_must_be_fail(self):
        c = {"schema": "llm-gate-eval/corpus@1", "kind": "redteam", "gate": None,
             "items": [{"id": "r", "bucket": "x", "gold": "pass", "text": "t", "notes": ""}]}
        self.assertTrue(validate_corpus(c))

    def test_gate_missing_placeholder(self):
        spec = load(GATES[0])
        spec["judge_prompt"] = {"system": "{{context}}", "user": "{{text}}"}
        self.assertTrue(any("criterion" in e for e in validate_gate(spec)))


if __name__ == "__main__":
    unittest.main()
