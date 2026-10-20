"""HTTP backends against a local stub server (no network beyond localhost)."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from gate_eval.judge import JudgeTransportError, LlamaCppJudge, OpenAIJudge, parse_verdict


class Stub(BaseHTTPRequestHandler):
    mode = "json"
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Stub.seen.append((self.path, body, self.headers.get("Authorization")))
        if Stub.mode == "500":
            self.send_response(500); self.end_headers(); return
        if self.path == "/completion" and Stub.mode == "sse":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for frag in ("VERD", "ICT: ", "PASS"):
                self.wfile.write(f"data: {json.dumps({'content': frag})}\n\n".encode())
            self.wfile.write(b'data: {"content": "", "stop": true}\n\n')
            return
        if self.path == "/completion":
            out = {"content": "VERDICT: FAIL"}
        elif self.path == "/v1/chat/completions":
            out = {"choices": [{"message": {"content": "VERDICT: PASS"}}]}
        else:
            self.send_response(404); self.end_headers(); return
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class TestHttpJudges(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        Stub.mode, Stub.seen = "json", []

    def test_llamacpp_json(self):
        raw = LlamaCppJudge(self.url, max_tokens=7).complete("SYS", "USR")
        self.assertEqual(parse_verdict(raw), ("FAIL", True))
        path, body, _ = Stub.seen[0]
        self.assertEqual(path, "/completion")
        self.assertEqual(body["n_predict"], 7)
        self.assertTrue(body["prompt"].startswith("SYS\n<|im_start|>user\nUSR<|im_end|>"))
        self.assertTrue(body["prompt"].endswith("<|im_start|>assistant\n"))

    def test_llamacpp_sse_stream_is_joined(self):
        Stub.mode = "sse"
        raw = LlamaCppJudge(self.url + "/completion").complete("s", "u")
        self.assertEqual(parse_verdict(raw), ("PASS", True))

    def test_openai_chat(self):
        import os
        os.environ["GATE_EVAL_TEST_KEY"] = "sk-test"
        from gate_eval.judge import make_judge
        j = make_judge("openai", url=self.url, model="m1", api_key_env="GATE_EVAL_TEST_KEY")
        self.assertEqual(parse_verdict(j.complete("s", "u")), ("PASS", True))
        path, body, auth = Stub.seen[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(body["model"], "m1")
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertEqual(auth, "Bearer sk-test")

    def test_openai_url_forms(self):
        for u in (self.url, self.url + "/v1", self.url + "/v1/chat/completions"):
            self.assertEqual(OpenAIJudge(u).endpoint(), self.url + "/v1/chat/completions")

    def test_server_error_raises_after_retries(self):
        Stub.mode = "500"
        with self.assertRaises(JudgeTransportError):
            LlamaCppJudge(self.url, retries=1).complete("s", "u")
        self.assertEqual(len(Stub.seen), 2)


if __name__ == "__main__":
    unittest.main()
