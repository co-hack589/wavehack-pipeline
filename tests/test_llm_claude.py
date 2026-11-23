"""social/llm.py の Claude 呼び出し（Messages API を urlopen ごとモック。本物の API には送らない）。

  python3 -m unittest tests.test_llm_claude -v
"""
import io
import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "social"))
import ideas_agent as A  # noqa: E402
import llm  # noqa: E402


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def ok(content, stop="end_turn"):
    return _Resp(json.dumps({"content": content, "stop_reason": stop}).encode())


def http_error(code, msg="err"):
    return urllib.error.HTTPError(llm.ANTHROPIC, code, msg, {}, io.BytesIO(json.dumps({"error": {"message": msg}}).encode()))


class FakeUrlopen:
    """用意した応答（dict の応答 or 例外）を順に返し、送った body を記録する。"""

    def __init__(self, *responses):
        self.responses, self.bodies, self.headers = list(responses), [], []

    def __call__(self, req, timeout=None):
        self.bodies.append(json.loads(req.data.decode()))
        self.headers.append(dict(req.header_items()))
        r = self.responses.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r


SEARCHED = [
    {"type": "thinking", "thinking": "", "signature": "s"},
    {"type": "text", "text": "調べます。"},
    {"type": "server_tool_use", "id": "srv1", "name": "web_search", "input": {"query": "気象庁 有義波高"}},
    {"type": "web_search_tool_result", "tool_use_id": "srv1", "content": [
        {"type": "web_search_result", "url": "https://www.data.jma.go.jp/kaiyou/db/wave/comment/elmknwl.html", "title": "波浪の基礎知識"},
        {"type": "web_search_result", "url": "https://www.jma.go.jp/x", "title": "x"}]},
    {"type": "text", "text": '{"a": 1', "citations": [
        {"type": "web_search_result_location", "url": "https://www.jma.go.jp/x", "cited_text": "..."}]},
    {"type": "text", "text": "}"},
]


class ClaudeTests(unittest.TestCase):
    def setUp(self):
        llm.SEARCH_OK = True
        self.env = patch.dict(os.environ, {"ANTHROPIC_API_KEY": " sk-test \n"})
        self.env.start()
        self.sleep = patch.object(llm.time, "sleep", lambda s: None)
        self.sleep.start()

    def tearDown(self):
        self.env.stop()
        self.sleep.stop()
        llm.SEARCH_OK = True

    def test_request_shape_search_and_urls(self):
        fake = FakeUrlopen(ok(SEARCHED))
        with patch.object(llm.urllib.request, "urlopen", fake):
            text, urls = llm.claude("p", search=True, temperature=0.9)
        self.assertEqual(llm.parse_json(text), {"a": 1})  # 検索前の前置き（調べます。）は本文に入れない
        self.assertEqual(urls[0], "https://www.jma.go.jp/x")  # 引用が先
        self.assertIn("https://www.data.jma.go.jp/kaiyou/db/wave/comment/elmknwl.html", urls)
        b = fake.bodies[0]
        self.assertEqual(b["model"], "claude-sonnet-5-5")
        self.assertEqual(b["tools"], [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}])
        self.assertNotIn("temperature", b)  # Sonnet 5.5 / Opus 5.5 は既定以外の temperature を断る
        h = {k.lower(): v for k, v in fake.headers[0].items()}
        self.assertEqual(h["x-api-key"], "sk-test")  # 末尾の空白・改行を落とす
        self.assertEqual(h["anthropic-version"], "2023-06-01")

    def test_no_search_has_no_tools(self):
        fake = FakeUrlopen(ok([{"type": "text", "text": "[1]"}]))
        with patch.object(llm.urllib.request, "urlopen", fake):
            self.assertEqual(llm.claude("p", search=False), ("[1]", []))
        self.assertNotIn("tools", fake.bodies[0])

    def test_retry_529_5xx_timeout_then_ok(self):
        fake = FakeUrlopen(http_error(529, "overloaded"), TimeoutError("t"), ok([{"type": "text", "text": "ok"}]))
        with patch.object(llm.urllib.request, "urlopen", fake):
            self.assertEqual(llm.claude("p")[0], "ok")
        self.assertEqual([b["model"] for b in fake.bodies], ["claude-sonnet-5-5"] * 3)

    def test_falls_back_to_opus(self):
        fake = FakeUrlopen(http_error(500), http_error(503), http_error(529), ok([{"type": "text", "text": "ok"}]))
        with patch.object(llm.urllib.request, "urlopen", fake):
            self.assertEqual(llm.claude("p")[0], "ok")
        self.assertEqual([b["model"] for b in fake.bodies], ["claude-sonnet-5-5"] * 3 + ["claude-opus-5-5"])

    def test_404_and_refusal_go_to_next_model_then_fail(self):
        fake = FakeUrlopen(http_error(404, "model not found"), ok([], stop="refusal"))
        with patch.object(llm.urllib.request, "urlopen", fake), self.assertRaises(SystemExit):
            llm.claude("p")
        self.assertEqual([b["model"] for b in fake.bodies], ["claude-sonnet-5-5", "claude-opus-5-5"])

    def test_search_tool_unavailable_switches_to_known_sources(self):
        fake = FakeUrlopen(http_error(400, "web_search tool is not enabled for this organization"),
                           ok([{"type": "text", "text": "2"}]))
        with patch.object(llm.urllib.request, "urlopen", fake):
            self.assertEqual(llm.claude("p", search=True)[0], "2")
        self.assertFalse(llm.SEARCH_OK)
        self.assertIn("tools", fake.bodies[0])
        self.assertNotIn("tools", fake.bodies[1])
        # 検索が使えないと、台本の出典 URL は確認済みの一覧からだけ選ばせる（捏造防止）
        prompt = A.write_prompt({"title": "t"}, [], {}, A.today())
        self.assertIn("一覧に無い URL を作らない", prompt)
        self.assertIn("https://www.jma.go.jp/jma/kishou/know/typhoon/index.html", prompt)

    def test_pause_turn_is_continued(self):
        first = SEARCHED[:4]
        fake = FakeUrlopen(ok(first, stop="pause_turn"), ok([{"type": "text", "text": '{"b": 2}'}]))
        with patch.object(llm.urllib.request, "urlopen", fake):
            text, urls = llm.claude("p", search=True)
        self.assertEqual(llm.parse_json(text), {"b": 2})
        self.assertEqual(fake.bodies[1]["messages"][1], {"role": "assistant", "content": first})
        self.assertEqual(len(urls), 2)

    def test_401_stops(self):
        fake = FakeUrlopen(http_error(401, "invalid x-api-key"))
        with patch.object(llm.urllib.request, "urlopen", fake), self.assertRaises(SystemExit):
            llm.claude("p")
        self.assertEqual(len(fake.bodies), 1)


class ProviderTests(unittest.TestCase):
    def test_generate_uses_claude_when_key_present(self):
        calls = []
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}), \
                patch.object(llm, "claude", lambda p, **k: calls.append("claude") or ("c", [])), \
                patch.object(llm, "gemini", lambda p, **k: calls.append("gemini") or ("g", [])):
            self.assertEqual(llm.provider(), "claude")
            self.assertEqual(llm.generate("p", search=True)[0], "c")
        self.assertEqual(calls, ["claude"])

    def test_generate_falls_back_to_gemini_without_key(self):
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        env["ANTHROPIC_API_KEY"] = "  "  # Secrets が未設定だと空文字になる
        with patch.dict(os.environ, env, clear=True), \
                patch.object(llm, "claude", lambda p, **k: ("c", [])), \
                patch.object(llm, "gemini", lambda p, **k: ("g", [])):
            self.assertEqual(llm.provider(), "gemini")
            self.assertEqual(llm.generate("p")[0], "g")


if __name__ == "__main__":
    unittest.main()
