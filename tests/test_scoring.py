"""FN / leak / grey arithmetic and threshold checks."""
import unittest

from gate_eval.scoring import check_thresholds, format_table, score


def rec(i, bucket, gold, verdict, pf=0):
    return {"id": i, "bucket": bucket, "gold": gold, "verdict": verdict, "parse_failures": pf}


RECORDS = [
    rec("p1", "sincere_direct", "pass", "PASS"),
    rec("p2", "sincere_direct", "pass", "FAIL"),           # FN
    rec("p3", "sincere_paraphrase", "pass", "FAIL", 1),    # FN, unparseable
    rec("p4", "sincere_paraphrase", "pass", "PASS"),
    rec("g1", "sincere_oblique", "grey", "PASS"),           # grey: never scored
    rec("g2", "sincere_oblique", "grey", "FAIL"),
    rec("f1", "adversarial_gaming", "fail", "PASS"),        # leak
    rec("f2", "adversarial_gaming", "fail", "FAIL"),
    rec("f3", "insincere_hollow", "fail", "FAIL"),
    rec("f4", "off_topic", "fail", "FAIL"),
]


class TestScore(unittest.TestCase):
    def setUp(self):
        self.s = score(RECORDS)

    def test_totals(self):
        s = self.s
        self.assertEqual(s["n"], 10)
        self.assertEqual((s["fn"], s["n_gold_pass"]), (2, 4))
        self.assertAlmostEqual(s["fn_rate"], 0.5)
        self.assertEqual((s["leaks"], s["n_gold_fail"]), (1, 4))
        self.assertAlmostEqual(s["leak_rate"], 0.25)
        self.assertEqual(s["unparseable"], 1)

    def test_grey_reported_not_scored(self):
        s = self.s
        self.assertEqual((s["grey_passes"], s["grey_n"]), (1, 2))
        g = s["buckets"]["sincere_oblique"]
        self.assertEqual((g["fn_ids"], g["leak_ids"]), ([], []))
        all_grey_fail = score([rec("g", "sincere_oblique", "grey", "FAIL")])
        self.assertEqual((all_grey_fail["fn"], all_grey_fail["leaks"]), (0, 0))

    def test_per_bucket(self):
        b = self.s["buckets"]
        self.assertEqual(b["sincere_direct"]["fn_ids"], ["p2"])
        self.assertEqual(b["adversarial_gaming"]["leak_ids"], ["f1"])
        self.assertEqual((b["adversarial_gaming"]["passes"], b["adversarial_gaming"]["n"]), (1, 2))

    def test_duplicate_ids_counted_once(self):
        s = score(RECORDS + [rec("f1", "adversarial_gaming", "fail", "PASS")])
        self.assertEqual(s["leaks"], 1)

    def test_empty(self):
        s = score([])
        self.assertEqual((s["fn_rate"], s["leak_rate"]), (0.0, 0.0))

    def test_table_renders(self):
        t = format_table("x", self.s, {"p2": "bounced text"})
        self.assertIn("grey: not scored", t)
        self.assertIn("p2", t)
        self.assertIn("FN 2/4", t)


class TestThresholds(unittest.TestCase):
    def test_none_set_never_breaches(self):
        self.assertEqual(check_thresholds({"a": score(RECORDS)}), [])

    def test_leaks_summed_across_gates(self):
        sums = {"a": score(RECORDS), "b": score(RECORDS)}
        self.assertEqual(check_thresholds(sums, max_leaks=2), [])
        self.assertEqual(len(check_thresholds(sums, max_leaks=1)), 1)

    def test_fn_rate_per_gate(self):
        sums = {"a": score(RECORDS)}
        self.assertEqual(check_thresholds(sums, max_fn_rate=0.5), [])
        self.assertEqual(len(check_thresholds(sums, max_fn_rate=0.49)), 1)

    def test_redteam_has_no_fn_rate(self):
        rt = score([rec("r1", "spam_flood", "fail", "FAIL")])
        self.assertEqual(check_thresholds({"rt": rt}, max_fn_rate=0.0), [])


if __name__ == "__main__":
    unittest.main()
