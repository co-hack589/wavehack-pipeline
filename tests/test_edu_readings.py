"""教育リールの回ごとの読み（script.readings）のテスト。TTS（Gemini）はモック（本番には何も送らない）。

  python3 -m unittest tests.test_edu_readings -v
"""
import base64
import copy
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "reel"))
sys.path.insert(0, str(ROOT / "edu"))
sys.path.insert(0, str(ROOT / "social"))
os.environ.setdefault("IG_USER_ID", "test")
os.environ.setdefault("IG_ACCESS_TOKEN", "test")

# render_edu は動画用に playwright / imageio_ffmpeg を読み込む。音声化だけを試すので、無い環境では空の模造品で代える
for name in ("playwright", "playwright.sync_api", "imageio_ffmpeg"):
    if name not in sys.modules:
        try:
            __import__(name)
        except ImportError:
            mod = types.ModuleType(name)
            mod.sync_playwright = None
            mod.get_ffmpeg_exe = lambda: "ffmpeg"
            sys.modules[name] = mod

import edu_rules as R  # noqa: E402
import narration  # noqa: E402
import publish_edu as P  # noqa: E402
import render_edu  # noqa: E402

PCM = base64.b64encode(b"\0\0" * 2400).decode()  # 0.1 秒の無音


class FakeTTS:
    """narration.call の代わり。TTS に渡った文字列を記録する。"""

    def __init__(self):
        self.texts: list[str] = []

    def __call__(self, model, body):
        self.texts.append(body["contents"][0]["parts"][0]["text"])
        return {"candidates": [{"content": {"parts": [{"inlineData": {"data": PCM}}]}}]}


def ep(no: int, says: list[str], readings: dict | None = None) -> dict:
    e = {"no": no, "series": R.SERIES, "title": "t", "next": None, "source": "参考：気象庁", "caption": "c",
         "scenes": [{"kind": "body", "say": s} for s in says]}
    if readings is not None:
        e["readings"] = readings
    return e


class ReadingsTest(unittest.TestCase):
    def setUp(self):
        self.before = copy.deepcopy(narration.READINGS)
        self.tmp = Path(tempfile.mkdtemp())
        self.tts = FakeTTS()
        env = {k: v for k, v in os.environ.items() if k != "TTS_BACKEND"}
        self.patches = [patch.object(narration, "call", self.tts), patch.dict(os.environ, env, clear=True)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_to_speech_extra_is_per_call(self):
        self.assertEqual(narration.to_speech("離岸流と波高", {"離岸流": "りがんりゅう", "波高": "なみたか"}), "りがんりゅうとなみたか")
        self.assertEqual(narration.to_speech("離岸流と波高"), "離岸流とはこう")
        self.assertEqual(narration.READINGS, self.before)

    def test_readings_reach_tts_and_do_not_leak_to_next_episode(self):
        e1 = ep(11, ["離岸流に注意", "波高を見る"], {"離岸流": "りがんりゅう", "波高": "なみたか"})
        render_edu.narrate(e1, self.tmp / "ep11_voice")
        self.assertEqual(self.tts.texts, ["りがんりゅうに注意", "なみたかを見る"])

        self.tts.texts.clear()
        render_edu.narrate(ep(12, ["離岸流に注意", "波高を見る"]), self.tmp / "ep12_voice")
        self.assertEqual(self.tts.texts, ["離岸流に注意", "はこうを見る"])  # 別の回には残らない（既定の読みに戻る）
        self.assertEqual(narration.READINGS, self.before)

    def test_changed_readings_redo_cached_scene(self):
        out = self.tmp / "ep13_voice"
        render_edu.narrate(ep(13, ["離岸流に注意", "保存してね"]), out)
        self.tts.texts.clear()
        render_edu.narrate(ep(13, ["離岸流に注意", "保存してね"]), out)
        self.assertEqual(self.tts.texts, [])  # 原稿も読みも同じなら作り直さない
        render_edu.narrate(ep(13, ["離岸流に注意", "保存してね"], {"離岸流": "りがんりゅう"}), out)
        self.assertEqual(self.tts.texts, ["りがんりゅうに注意"])  # 読みが変わった場面だけ作り直す

    def test_validate_readings_shape(self):
        base = {k: v for k, v in ep(1, ["a"]).items() if k != "no"}
        ok = R.validate({**base, "readings": {"西高東低": "せいこうとうてい"}})
        self.assertFalse([e for e in ok if "readings" in e or "使えないキー" in e])
        for bad in ([["西高東低", "せいこうとうてい"]], {"西高東低": 1}, {"": "よみ"}, {"語": ""}):
            errs = R.validate({**base, "readings": bad})
            self.assertTrue(any("readings" in e for e in errs), bad)
        self.assertIn("readings", R.normalize({**base, "readings": {"語": "ご"}}))

    def test_publish_episode_passes_readings(self):
        script = {k: v for k, v in ep(1, ["a"], {"西高東低": "せいこうとうてい"}).items() if k != "no"}
        e = P.episode({"id": 1, "episode_no": 14, "script": script})
        self.assertEqual(e["readings"], {"西高東低": "せいこうとうてい"})
        self.assertEqual(e["no"], 14)


if __name__ == "__main__":
    unittest.main()
