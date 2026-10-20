"""Judge backends and fail-closed verdict parsing.

Backends (stdlib urllib only):
  llamacpp  POST <url>/completion            (llama-server, or any proxy speaking it)
  openai    POST <url>/v1/chat/completions   (any OpenAI-compatible server)
  mock-*    offline judges for tests and dry runs

A judge returns the model's raw text. parse_verdict() turns that into PASS or
FAIL; anything it cannot read unambiguously is FAIL (fail-closed), which is
how the gate must behave in production.

Transport errors are different: they raise JudgeTransportError and stop the
run. Silently scoring an outage as FAIL would understate leaks and overstate
false negatives; runs are resumable, so stopping costs nothing.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_THINK_OPEN = re.compile(r"<think>", re.IGNORECASE)
_VERDICT = re.compile(r"\bVERDICT\s*:\s*\**\s*(PASS|FAIL)\b", re.IGNORECASE)


class JudgeTransportError(RuntimeError):
    pass


def parse_verdict(raw):
    """Return (verdict, parsed_ok). verdict is "PASS" or "FAIL".

    PASS only when the reply contains one or more 'VERDICT: PASS' lines and no
    'VERDICT: FAIL'. Empty, missing, conflicting, or non-string replies are
    FAIL with parsed_ok=False. Reasoning inside <think>...</think> is ignored,
    and an unterminated <think> discards everything after it.
    """
    if not isinstance(raw, str):
        return "FAIL", False
    text = _THINK_BLOCK.sub(" ", raw)
    m = _THINK_OPEN.search(text)
    if m:
        text = text[:m.start()]
    found = {v.upper() for v in _VERDICT.findall(text)}
    if found == {"PASS"}:
        return "PASS", True
    if found == {"FAIL"}:
        return "FAIL", True
    return "FAIL", False


def chatml(system, user):
    """Single raw prompt for /completion. Text before the first <|im_start|> is
    the system turn, which is also how llm-proxy-kit's proxies read it."""
    return f"{system}\n<|im_start|>user\n{user}<|im_end|>\n<|im_start|>assistant\n"


def _post_json(url, body, headers, timeout, retries):
    data = json.dumps(body).encode("utf-8")
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace"), resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code} from {url}"
            e.close()
            if e.code < 500 and e.code != 429:
                break
        except (urllib.error.URLError, OSError) as e:
            last = f"{type(e).__name__}: {e} ({url})"
        if attempt < retries:
            time.sleep(min(2 ** attempt, 8))
    raise JudgeTransportError(last or f"request to {url} failed")


def _sse_content(body):
    """Join the 'content' fragments of a llama.cpp-style SSE stream."""
    parts = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            frag = json.loads(payload)
        except ValueError:
            continue
        if isinstance(frag, dict) and isinstance(frag.get("content"), str):
            parts.append(frag["content"])
    return "".join(parts)


class HttpJudge:
    def __init__(self, url, model=None, temperature=0.0, max_tokens=64, timeout=120,
                 retries=2, api_key=None, prompt_format="chatml"):
        self.url = url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.retries = retries
        self.api_key = api_key
        self.prompt_format = prompt_format


class LlamaCppJudge(HttpJudge):
    name = "llamacpp"

    def endpoint(self):
        return self.url if self.url.endswith("/completion") else self.url + "/completion"

    def complete(self, system, user, gate=None):
        prompt = chatml(system, user) if self.prompt_format == "chatml" else f"{system}\n\n{user}\n\n"
        body = {"prompt": prompt, "n_predict": self.max_tokens,
                "temperature": self.temperature, "stream": False}
        text, ctype = _post_json(self.endpoint(), body, {}, self.timeout, self.retries)
        # Some proxies always stream (SSE) even when asked not to.
        if "event-stream" in ctype or text.lstrip().startswith("data:"):
            return _sse_content(text)
        try:
            obj = json.loads(text)
        except ValueError:
            return text
        return obj.get("content", "") if isinstance(obj, dict) else ""


class OpenAIJudge(HttpJudge):
    name = "openai"

    def endpoint(self):
        if self.url.endswith("/chat/completions"):
            return self.url
        if self.url.endswith("/v1"):
            return self.url + "/chat/completions"
        return self.url + "/v1/chat/completions"

    def complete(self, system, user, gate=None):
        body = {"messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "temperature": self.temperature, "max_tokens": self.max_tokens}
        if self.model:
            body["model"] = self.model
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        text, _ = _post_json(self.endpoint(), body, headers, self.timeout, self.retries)
        try:
            obj = json.loads(text)
            return obj["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError):
            return ""


class MockJudge:
    """Offline judges.

    always-pass / always-fail: fixed verdicts (exercise scoring and thresholds).
    keyword: PASS if any of the gate's 'keywords' appears in the player text.
             This is the naive baseline an LLM gate is supposed to beat; run it
             to see how badly keyword matching leaks on the adversarial buckets.
    """

    def __init__(self, mode):
        if mode not in ("always-pass", "always-fail", "keyword"):
            raise ValueError(f"unknown mock mode {mode!r}")
        self.mode = mode
        self.name = f"mock-{mode}"
        self.calls = 0

    def complete(self, system, user, gate=None):
        self.calls += 1
        if self.mode == "always-pass":
            return "VERDICT: PASS"
        if self.mode == "always-fail":
            return "VERDICT: FAIL"
        start, end = user.find("<player_text>"), user.find("</player_text>")
        text = (user[start + 13:end] if start >= 0 and end > start else user).lower()
        words = (gate or {}).get("keywords", [])
        return "VERDICT: PASS" if any(w.lower() in text for w in words) else "VERDICT: FAIL"


def make_judge(kind, url=None, model=None, api_key_env=None, **kw):
    if kind.startswith("mock-"):
        return MockJudge(kind[5:])
    if not url:
        raise ValueError(f"--url is required for the {kind} judge")
    if kind == "llamacpp":
        return LlamaCppJudge(url, model=model, **kw)
    if kind == "openai":
        key = os.environ.get(api_key_env) if api_key_env else None
        return OpenAIJudge(url, model=model, api_key=key, **kw)
    raise ValueError(f"unknown judge {kind!r}")
