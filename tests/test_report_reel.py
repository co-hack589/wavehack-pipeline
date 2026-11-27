"""波レポートのリールの台本・キャプション（report_reel/reel_script.py）のテスト。木崎浜の試作と同じ投稿で確かめる。

  python3 -m unittest tests.test_report_reel -v
TTS・API・Instagram には何も送らない。
"""
import copy
import datetime
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "reel"))
sys.path.insert(0, str(ROOT / "report_reel"))

import reel_script as S  # noqa: E402
from narration import to_speech  # noqa: E402

# GET /internal/report-reels/:id の形（木崎浜 2026-11-27 08:35 JST の投稿）
KIZAKI = {
    "reel": {"id": "8a3c1b9e-0000-4000-8000-000000000001", "status": "building", "build_count": 1, "caption": None, "video_key": None},
    "report": {
        "id": "f1709b6b-c968-4431-8470-a36964f1c576",
        "kind": "video",
        "media_url": "https://video.wavehack.surf/m/reports/2026-11/x/main",
        "thumb_url": None,
        "point_id": "kizaki",
        "point_name": "木崎浜",
        "pref": "宮崎",
        "area": None,
        "captured_at": "2026-11-26T23:35:00.000Z",
        "captured_at_jst": "2026-11-27T08:35:00+09:00",
        "captured_label": "11/27 8:35",
        "size": "waist",
        "size_label": "コシ",
        "quality": 3,
        "quality_label": "まあまあ",
        "crowd": "empty",
        "crowd_label": "空いてる",
        "terrain": None,
        "terrain_label": None,
        "comment": "木崎浜の放送塔前です。\nセットで腹ぐらい、満ちすぎてたるめです。",
        "forecast_score": 33,
    },
    "credit": {"mode": "none", "nickname": None, "line": "みんなの波レポートより"},
    "consent": {"ig_allowed": True, "ig_name_mode": "anonymous", "eligible": True, "reason": None},
    "slack": {"channel": "CREPORT", "thread_ts": "1700000000.000100"},
}
TODAY = datetime.date(2026, 11, 27)


def data(**over):
    d = copy.deepcopy(KIZAKI)
    d["report"].update(over)
    return d


class EpisodeTest(unittest.TestCase):
    def test_kizaki_script(self):
        ep = S.build_episode(KIZAKI, TODAY)
        says = [s["say"] for s in ep["scenes"]]
        self.assertEqual(says[0], "今朝8時35分の木崎浜。予報は33点でした。")
        self.assertEqual(says[1], "現地からの波レポートは、コシ、まあまあ。混み具合は空いてる。")
        self.assertEqual([s["kind"] for s in ep["scenes"]], ["open", "report", "compare", "crowd", "cta", "next"])
        self.assertEqual(says[-1], "保存して、次の海で投稿してね！")
        self.assertEqual(ep["title"], "木崎浜（宮崎）")
        self.assertEqual(ep["source"], "みんなの波レポートより")
        P = ep["post"]
        self.assertEqual((P["point"], P["place"], P["captured"], P["size"], P["quality"], P["crowd"], P["forecast"]),
                         ("木崎浜", "宮崎", "11/27 8:35", "コシ", "まあまあ", "空いてる", 33))
        self.assertFalse(P["forecast_hl"], "55 未満は azure で強調しない")
        self.assertEqual(P["comment"], "木崎浜の放送塔前です。<br>セットで腹ぐらい、満ちすぎてたるめです。")
        self.assertEqual(ep["scenes"][0]["h"], "今朝8:35の<b>木崎浜</b>")

    def test_readings(self):
        ep = S.build_episode(KIZAKI, TODAY)
        rd = ep["readings"]
        self.assertEqual(rd["8時35分"], "はちじさんじゅうごふん")
        self.assertEqual(rd["33点"], "さんじゅうさんてん")
        self.assertEqual(rd["木崎浜"], "きざきはま")
        self.assertEqual(to_speech(ep["scenes"][0]["say"], rd), "けさはちじさんじゅうごふんのきざきはま。予報はさんじゅうさんてんでした。")
        self.assertEqual(to_speech(ep["scenes"][-1]["say"], rd), "保存して、つぎのうみで投稿してね！")

    def test_deterministic(self):
        self.assertEqual(S.build_episode(KIZAKI, TODAY), S.build_episode(copy.deepcopy(KIZAKI), TODAY))
        self.assertEqual(S.build_caption(KIZAKI), S.build_caption(copy.deepcopy(KIZAKI)))

    def test_number_readings(self):
        self.assertEqual([S.kana_points(n) for n in (1, 8, 10, 18, 30, 41, 55, 68, 100)],
                         ["いってん", "はってん", "じゅってん", "じゅうはってん", "さんじゅってん", "よんじゅういってん",
                          "ごじゅうごてん", "ろくじゅうはってん", "ひゃくてん"])
        self.assertEqual([S.kana_hour(h) for h in (4, 6, 7, 9, 12)], ["よじ", "ろくじ", "しちじ", "くじ", "じゅうにじ"])
        self.assertEqual([S.kana_minute(m) for m in (1, 5, 10, 20, 35, 48)],
                         ["いっぷん", "ごふん", "じゅっぷん", "にじゅっぷん", "さんじゅうごふん", "よんじゅうはっぷん"])

    def test_other_days_and_missing_facts(self):
        ep = S.build_episode(data(forecast_score=None, crowd_label=None, comment=None), datetime.date(2026, 11, 29))
        self.assertEqual(ep["scenes"][0]["say"], "11月27日、8時35分の木崎浜。")
        self.assertNotIn("compare", [s["kind"] for s in ep["scenes"]], "予報が無ければ答え合わせの場面は出さない")
        self.assertEqual(ep["scenes"][1]["say"], "現地からの波レポートは、コシ、まあまあ。")
        self.assertIsNone(ep["post"]["comment"])
        self.assertEqual(S.build_episode(KIZAKI, datetime.date(2026, 11, 28))["scenes"][0]["say"],
                         "昨日8時35分の木崎浜。予報は33点でした。")

    def test_high_score_is_emphasized(self):
        ep = S.build_episode(data(forecast_score=60), TODAY)
        self.assertTrue(ep["post"]["forecast_hl"])
        self.assertTrue(S.build_episode(data(forecast_score=55), TODAY)["post"]["forecast_hl"])
        self.assertFalse(S.build_episode(data(forecast_score=54), TODAY)["post"]["forecast_hl"])

    def test_comment_is_escaped_and_truncated(self):
        long = "あ" * 45
        ep = S.build_episode(data(comment="<b>x</b> & y"), TODAY)
        self.assertEqual(ep["post"]["comment"], "&lt;b&gt;x&lt;/b&gt; &amp; y")
        self.assertEqual(S.short_comment(long), "あ" * 39 + "…")
        self.assertEqual(len(S.short_comment(long)), 40)
        self.assertEqual(S.build_episode(data(comment=long), TODAY)["post"]["comment"], "あ" * 39 + "…")


class CaptionTest(unittest.TestCase):
    def test_kizaki_caption(self):
        c = S.build_caption(KIZAKI)
        lines = c.split("\n")
        self.assertEqual(lines[0], "📍木崎浜（宮崎）｜11/27 8:35 の波レポート")
        self.assertIn("サイズ コシ・まあまあ・空いてる", c)
        self.assertIn("「木崎浜の放送塔前です。 セットで腹ぐらい、満ちすぎてたるめです。」", c)
        self.assertIn("予報 33点 → 現地は「まあまあ」", c)
        self.assertIn("みんなの波レポートより", c)
        self.assertIn("波レポートで教えてください", c)
        self.assertIn("プロフィールのリンク（wavehack.surf）", c)
        self.assertIn("App Store", c)
        tags = lines[-1].split()
        for t in ("#波レポート", "#木崎浜", "#宮崎サーフィン", "#wavehack"):
            self.assertIn(t, tags)
        self.assertEqual(len(tags), len(set(tags)))
        self.assertLessEqual(len(c), 2200)
        self.assertLessEqual(sum(1 for t in tags if t.startswith("#")), 30)

    def test_credit_nickname(self):
        d = copy.deepcopy(KIZAKI)
        d["credit"] = {"mode": "nickname", "nickname": "なみ", "line": "投稿：なみさん"}
        self.assertIn("投稿：なみさん", S.build_caption(d))
        self.assertEqual(S.build_episode(d, TODAY)["source"], "投稿：なみさん")

    def test_no_forecast_no_comment(self):
        c = S.build_caption(data(forecast_score=None, comment=None, terrain_label="いい"))
        self.assertNotIn("予報", c.split("\n\n")[1])
        self.assertNotIn("「」", c)
        self.assertIn("地形 いい", c)


if __name__ == "__main__":
    unittest.main()


class JobTest(unittest.TestCase):
    """report_reel/job.py の API 呼び出しの形と、同意が無いときに作らないこと（ネットワークには出ない）"""

    def setUp(self):
        import os
        os.environ.setdefault("WAVEHACK_API_URL", "http://api.test")
        os.environ.setdefault("WAVEHACK_CRON_SECRET", "test")
        sys.path.insert(0, str(ROOT / "social"))
        import job
        self.job = job
        self.calls = []
        api = job.ReelApi()
        api._req = lambda method, path, body=None, ok=(): (self.calls.append((method, path, body)) or (200, self.resp))
        self.api = api

    def test_paths(self):
        rid = KIZAKI["reel"]["id"]
        self.resp = KIZAKI
        self.assertEqual(self.api.get(rid)["report"]["point_name"], "木崎浜")
        self.api.ready(rid, self.job.video_key(rid), "cap")
        self.api.published(rid, "123", "https://www.instagram.com/reel/x/")
        self.api.failed(rid, "x" * 600)
        self.assertEqual([c[1] for c in self.calls], [f"/internal/report-reels/{rid}", f"/internal/report-reels/{rid}/ready",
                                                      f"/internal/report-reels/{rid}/published", f"/internal/report-reels/{rid}/failed"])
        self.assertEqual(self.calls[1][2], {"video_key": f"report-reels/{rid}.mp4", "caption": "cap"})
        self.assertEqual(len(self.calls[3][2]["error"]), 480)

    def test_no_consent_no_build(self):
        d = copy.deepcopy(KIZAKI)
        d["consent"] = {"eligible": False, "reason": "投稿者が Instagram での紹介に同意していません"}
        self.resp = d
        import types
        sys.modules.setdefault("reel_render", types.ModuleType("reel_render"))
        self.job.do_build(self.api, d["reel"]["id"], upload=False)
        self.assertEqual([c[0] + " " + c[1].rsplit("/", 1)[-1] for c in self.calls][-1], "POST failed")
        self.assertNotIn("ready", " ".join(c[1] for c in self.calls))

    def test_publish_only_when_publishing(self):
        d = copy.deepcopy(KIZAKI)
        d["reel"]["status"] = "skipped"
        self.resp = d
        self.assertEqual(self.job.do_publish(self.api, d["reel"]["id"]), "skipped")
        self.assertEqual(len(self.calls), 1)

    def test_rejects_bad_id(self):
        self.assertIsNone(self.job.UUID.match("abc; rm -rf /"))
