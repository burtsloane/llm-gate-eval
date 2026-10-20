"""End-to-end CLI with offline judges: exit codes for CI ship gates."""
import contextlib
import io
import os
import tempfile
import unittest

from gate_eval.cli import main


def run(*argv):
    buf_out, buf_err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
        code = main(list(argv))
    return code, buf_out.getvalue() + buf_err.getvalue()


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.res = ["--results-dir", self.tmp.name]

    def tearDown(self):
        self.tmp.cleanup()

    def test_validate_shipped_tree(self):
        code, out = run("validate")
        self.assertEqual(code, 0, out)
        self.assertIn("0 problem(s)", out)

    def test_dry_run_no_thresholds_exits_0(self):
        code, out = run("sincerity", "--dry-run", *self.res)
        self.assertEqual(code, 0, out)
        self.assertIn("grey: not scored", out)

    def test_always_fail_passes_leak_gate_but_fails_fn_gate(self):
        code, _ = run("sincerity", "--dry-run", "--max-leaks", "0", *self.res)
        self.assertEqual(code, 0)
        code, out = run("sincerity", "--dry-run", "--max-fn-rate", "0.2", *self.res)
        self.assertEqual(code, 1)
        self.assertIn("SHIP GATE: FAIL", out)

    def test_always_pass_leaks(self):
        code, out = run("sincerity", "--judge", "mock-always-pass", "--max-leaks", "0", *self.res)
        self.assertEqual(code, 1)
        self.assertIn("leaks 96 > --max-leaks 0", out)       # 3 gates x 32 gold-fail items
        code, _ = run("report", *sorted(
            os.path.join(self.tmp.name, f) for f in os.listdir(self.tmp.name)
            if f.startswith("mock-always-pass")), "--max-leaks", "96")
        self.assertEqual(code, 0)

    def test_redteam_every_item_must_fail(self):
        code, out = run("redteam", "--dry-run", "--max-leaks", "0", "--gates",
                        "frankenstein_forgiveness", *self.res)
        self.assertEqual(code, 0, out)
        self.assertIn("leaks 0/80", out)
        code, out = run("redteam", "--judge", "mock-always-pass", "--max-leaks", "0",
                        "--categories", "spam_flood,prompt_extraction",
                        "--gates", "frankenstein_forgiveness", *self.res)
        self.assertEqual(code, 1)
        self.assertIn("leaks 16/16", out)

    def test_limit_and_resume_through_cli(self):
        run("sincerity", "--dry-run", "--limit", "3", "--gates", "frankenstein_my_nature", *self.res)
        code, out = run("sincerity", "--dry-run", "--gates", "frankenstein_my_nature", *self.res)
        self.assertEqual(code, 0)
        self.assertIn("items=56", out)
        self.assertIn("FN 16/16", out)

    def test_config_errors_exit_2(self):
        self.assertEqual(run("sincerity", "--dry-run", "--gates", "nope", *self.res)[0], 2)
        self.assertEqual(run("sincerity", "--judge", "llamacpp", *self.res)[0], 2)   # no --url
        self.assertEqual(run("sincerity", *self.res)[0], 2)                          # no judge

    def test_unreachable_server_exits_2(self):
        code, out = run("sincerity", "--judge", "llamacpp", "--url", "http://127.0.0.1:9",
                        "--timeout", "2", "--limit", "1", "--gates", "frankenstein_my_nature",
                        *self.res)
        self.assertEqual(code, 2, out)


if __name__ == "__main__":
    unittest.main()
