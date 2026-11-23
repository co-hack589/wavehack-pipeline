"""教育リール第2弾: ネタ出しエージェント（social/）と、API のキューから作って投稿する流れ（edu/publish_edu.py）のテスト。
LLM・wavehack-api・Instagram はすべてモック（本番には何も送らない）。

  python3 -m unittest tests.test_social_ideas -v
"""
import copy
import datetime
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "social"))
sys.path.insert(0, str(ROOT / "edu"))
os.environ.setdefault("IG_USER_ID", "test")
os.environ.setdefault("IG_ACCESS_TOKEN", "test")
import edu_rules as R  # noqa: E402
import ideas_agent as A  # noqa: E402
import llm  # noqa: E402
import publish_edu as P  # noqa: E402
import social_api  # noqa: E402

JST = datetime.timezone(datetime.timedelta(hours=9))


def sample_script() -> dict:
    """第1弾 #08（公開済みの内容）を第2弾の形に直したもの（hook → bars → sum → next）。新しいネタは置かない。"""
    ep = copy.deepcopy(next(e for e in R.load_episodes() if e["no"] == 8))
    ep.pop("no")
    ep["scenes"] = [s for s in ep["scenes"] if s["kind"] != "cmp"]
    ep["scenes"][-1] = {"kind": "next", "say": R.END_SAY}
    ep["next"] = None
    return ep


SOURCES = [{"title": "気象庁 波浪の知識", "url": "https://www.data.jma.go.jp/kaiyou/db/wave/comment/elmknwl.html"}]


class RulesTests(unittest.TestCase):
    def test_season1_episodes_have_only_template_kinds_and_shapes(self):
        """既存の ep01〜10 は kind・キー・値の形・タグで引っかからない。引っかかるのは第2弾で変えた決まりだけ。"""
        season2_only = ("[字数]", "[締め]", "[出典]", "[構成] hook の q", "[構成] hook に big", "[構成] 場面の数")
        for ep in R.load_episodes():
            s = {k: v for k, v in ep.items() if k != "no"}
            errs = [e for e in R.validate(s) if not e.startswith(season2_only)]
            # next の list は第2弾では出さない（回の一覧は第1弾だけのもの）
            errs = [e for e in errs if "next に無いキー ['list']" not in e]
            self.assertEqual(errs, [], f"#{ep['no']:02d}")

    def test_char_limit_is_40_seconds_by_measured_rate(self):
        rates = {no: R._chars(no) / sec for no, sec in R.MEASURED.items()}
        print("\n1秒あたりの字数:", {no: round(r, 2) for no, r in rates.items()},
              f"上限 {R.MAX_SAY_CHARS}字 → {R.est_seconds(R.MAX_SAY_CHARS):.1f}秒")
        self.assertLessEqual(R.est_seconds(R.MAX_SAY_CHARS), 40)
        self.assertGreater(R.est_seconds(R.MAX_SAY_CHARS + 5), 40)  # 余裕を取りすぎていない
        over = {e["no"]: R.say_chars(e) for e in R.load_episodes() if R.say_chars(e) > R.MAX_SAY_CHARS}
        # 第1弾は 40〜56秒で作ったので #10 以外は上限を超える（意図どおり。第2弾から40秒以内にする）
        self.assertEqual(sorted(over), list(range(1, 10)))
        self.assertAlmostEqual(R.est_seconds(R.say_chars(next(e for e in R.load_episodes() if e["no"] == 10))), 31.0, places=1)

    def test_sample_script_passes(self):
        s = sample_script()
        self.assertEqual(R.validate(s), [])
        self.assertEqual(R.validate_sources(SOURCES), [])

    def test_violations_are_reported(self):
        s = sample_script()
        s["scenes"].insert(1, {"kind": "map", "say": "地図", "h": "x"})
        s["scenes"][2]["bars"] = [[1, "a", "b"], [2, "c"]]
        s["scenes"][0]["q"] = "<i>どれ</i>"
        s["source"] = "参考：一般的な知識"
        s["caption"] += " プロフィールのリンクから"
        s["scenes"][-2]["say"] += "あ" * 80
        errs = "\n".join(R.validate(s))
        for want in ("[kind] 場面2", "[形] 場面3.bars[1]", "<i>", "[構成] hook の q", "[出典] source に公的", "[宣伝] 「リンク」", "[字数]"):
            self.assertIn(want, errs)
        self.assertTrue(R.validate_sources([]))
        self.assertIn("公的機関", "\n".join(R.validate_sources([{"title": "blog", "url": "https://example.com/a"}])))
        self.assertIn("中継", "\n".join(R.validate_sources([{"title": "x", "url": "https://vertexaisearch.cloud.google.com/r/1"}])))

    def test_normalize_fixes_closing_and_fixed_values(self):
        s = sample_script()
        s["scenes"][-1] = {"kind": "next", "say": "次回もお楽しみに", "list": ["a"]}
        s["next"], s["series"], s["no"] = "x", "別", 3
        n = R.normalize(s)
        self.assertEqual((n["next"], n["series"], "no" in n), (None, R.SERIES, False))
        self.assertEqual(n["scenes"][-1], {"kind": "next", "say": R.END_SAY})


class StyleAndHookTests(unittest.TestCase):
    """取り込んだ辞書（data/slop_ja.json）とフックの禁止事項（hookscore.py の考え方）。"""

    def test_slop_dictionary_hits(self):
        self.assertTrue(json.loads(R.SLOP.read_text(encoding="utf-8"))["_source"]["license"] == "MIT")
        hits = "\n".join(R.lint_style("周期は非常に重要なのは高さではなく周期です。いかがでしたか？この動画では潮を活用して解説します🚀"))
        for want in ("「非常に」", "「いかがでしたか」", "大切なのは〜ではなく", "動画の前置き", "絵文字"):
            self.assertIn(want, hits)
        self.assertEqual(R.lint_style("周期が長いほど、波は遠くまで届く。"), [])
        # 除外した項目（3つ並べ・保存/フォローのお願い）は当たらない
        self.assertEqual(R.lint_style("前日と当日の最新予報、そして現地の海で判断しよう。保存してね"), [])

    def test_lint_script_covers_say_and_caption_but_not_fixed_closing(self):
        s = sample_script()
        s["scenes"][1]["say"] += "非常に大事。"
        s["caption"] += "\n参考になれば幸いです。"
        got = R.lint_script(s)
        self.assertTrue(any(w.startswith("場面2: 「非常に」") for w in got))
        self.assertTrue(any(w.startswith("caption: 「参考になれば幸いです」") for w in got))
        self.assertFalse(any(w.startswith(f"場面{len(s['scenes'])}:") for w in got))
        self.assertEqual(R.validate(s), [])  # 言い回しは警告だけ（不合格にしない）

    def test_season1_style_hits_are_only_excluded_ones(self):
        for ep in R.load_episodes():
            self.assertEqual(R.lint_script(ep), [], f"#{ep['no']:02d}")

    def test_hook_dealbreakers_fail(self):
        bad = {"q": ["今日は<b>周期</b>を<br>紹介します？", "#波予報 周期って？", "周期って何？🌊", "ちょっと待って<br>周期って？"],
               "say": ["こんにちは、周期の話です。", "最後まで見てね、周期って何？", "えっと、周期って何？", "さて、周期とは？", "突然ですが、周期って何？",
                       "この動画では周期を解説します。"]}
        for key, texts in bad.items():
            for t in texts:
                s = sample_script()
                s["scenes"][0][key] = t
                errs = [e for e in R.validate(s) if e.startswith("[フック]")]
                self.assertTrue(errs, f"{key}: {t}")
                self.assertIn(f"場面1.{key}", errs[0])

    def test_hook_ok_and_season1_hooks_not_newly_failing(self):
        for t in ("同じ1mでも力が違うのはなぜ？", "予報は何日先まで信じていい？", "南うねり1.5mなら、どう読む？"):
            self.assertEqual(R.hook_problems(t, "x"), [], t)
        for ep in R.load_episodes():
            s = {k: v for k, v in ep.items() if k != "no"}
            self.assertEqual([e for e in R.validate(s) if e.startswith("[フック]")], [], f"#{ep['no']:02d}")
        self.assertEqual(R.validate(sample_script()), [])

    def test_hook_types_are_educational_and_spread(self):
        names = set(A.HOOK_NAMES)
        self.assertEqual(len(names), 6)
        for banned in ("損した告白", "中の人の暴露", "他人の結果", "期限", "「一番〜」", "常識の逆", "盗んでいい"):
            self.assertNotIn(banned, names)
        self.assertTrue(all(h["trap"] and h["form"] and h["example"] for h in A.HOOK_TYPES))
        self.assertEqual(A.min_hook_kinds(5), 3)
        p = A.ideas_prompt(5, R.load_episodes(), [], datetime.date(2026, 11, 15))
        self.assertIn('"hook_type"', p)
        self.assertIn("5本で3種類以上", p)
        cands = [{"title": "a", "hook_type": "統計の数字"}, {"title": "b", "hook_type": "統計の数字"}, {"title": "c", "hook_type": "数＋一押し"}]
        self.assertEqual([c["title"] for c in A.spread(cands)], ["a", "c", "b"])
        for h in A.HOOK_TYPES:  # 型の例そのものがフック検査で落ちない
            self.assertEqual(R.hook_problems(h["example"], "例"), [], h["name"])


class BatchDateTests(unittest.TestCase):
    def test_monday_of_week_sunday_counts_as_next_week(self):
        d = datetime.date
        self.assertEqual(A.default_batch(d(2026, 11, 15)), "2026-11-16")  # 日曜の夜 → 翌日の週
        self.assertEqual(A.default_batch(d(2026, 11, 16)), "2026-11-16")
        self.assertEqual(A.default_batch(d(2026, 11, 21)), "2026-11-16")


class FakeLLM:
    """プロンプトの書き出しで役割を見分けて、用意した応答を順に返す。"""

    def __init__(self, ideas, writes, reviews):
        self.ideas, self.writes, self.reviews, self.prompts = list(ideas), list(writes), list(reviews), []

    def __call__(self, prompt, *, search=False, temperature=0.7, models=None):
        self.prompts.append(prompt)
        if "ネタ出し担当" in prompt:
            return json.dumps(self.ideas.pop(0), ensure_ascii=False), []
        if "台本担当" in prompt:
            return "```json\n" + json.dumps(self.writes.pop(0), ensure_ascii=False) + "\n```", ["https://vertexaisearch.cloud.google.com/x"]
        if "事実確認担当" in prompt:
            return json.dumps(self.reviews.pop(0), ensure_ascii=False), []
        return "2", []


class FakeIdeasApi:
    def __init__(self, listed=(), exists=False):
        self.listed, self.exists, self.posted = list(listed), exists, []

    def list_ideas(self, limit=200):
        return self.listed

    def post_batch(self, batch, ideas):
        self.posted.append((batch, ideas))
        return (not self.exists), ([] if self.exists else [f"id{i}" for i in range(len(ideas))])


PASS = {"verdict": "pass", "issues": [{"severity": "軽微", "where": "caption", "problem": "言い回し", "fix": "-"}],
        "checked_claims": ["GFS は1日4回（NOAA NCEP）"], "summary": "事実は出典と一致。"}


class IdeasAgentTests(unittest.TestCase):
    def setUp(self):
        llm.SEARCH_OK = True

    def run_agent(self, fake, count=2, dry=True, api=None):
        with patch.object(llm, "generate", fake), patch.object(A.llm, "search_available", lambda: True):
            return A.run(count, "2026-11-16", dry, api, d=datetime.date(2026, 11, 15), url_check=False)

    def test_dry_run_flow_with_fix_and_review(self):
        good = sample_script()
        long = copy.deepcopy(good)
        long["scenes"][1]["say"] += "とても長い説明を足します。" * 8
        fake = FakeLLM(
            ideas=[[{"title": "周期", "pitch": "第1弾と同じ"},  # 第1弾と重なる → 除く
                    {"title": "予報の更新と見方", "pitch": "p1"}, {"title": "うねり2方向", "pitch": "p2"}, {"title": "予報の更新と見方 ", "pitch": "dup"},
                    {"title": "小波の日", "pitch": "p3"}]],
            writes=[{"script": long, "sources": SOURCES},  # 1本目 第1稿: 字数オーバー → 機械検査で差し戻し
                    {"script": good, "sources": SOURCES},  # 1本目 第2稿: 合格 → レビュー pass
                    {"script": good, "sources": []},        # 2本目: 出典なし ×3 → 不合格
                    {"script": good, "sources": []}, {"script": good, "sources": []}],
            reviews=[PASS])
        api = FakeIdeasApi(listed=[{"id": "x", "batch": "2026-11-09", "kind": "edu", "title": "うねり2方向", "status": "rejected"}])
        rc, ideas = self.run_agent(fake, api=api)
        self.assertEqual(rc, 0)
        self.assertEqual([i["title"] for i in ideas], ["予報の当たり外れ"])  # 題は台本の title
        self.assertEqual(ideas[0]["pitch"], "p1")
        self.assertIn("レビュー合格（第2稿", ideas[0]["review_notes"])
        self.assertIn("GFS は1日4回", ideas[0]["review_notes"])
        self.assertEqual(set(ideas[0]), {"kind", "title", "pitch", "script", "sources", "review_notes"})
        self.assertNotIn("no", ideas[0]["script"])
        self.assertEqual(api.posted, [])  # dry-run は送らない
        # 過去のネタ（API）と重なる「うねり2方向」は台本を書かない。差し戻しの理由は次の稿のプロンプトに入る
        writes = [p for p in fake.prompts if "台本担当" in p]
        self.assertTrue(all("うねり2方向" not in p.split("ルール:")[0] for p in writes))
        self.assertIn("[字数]", writes[1])

    def test_sends_batch_and_409_is_success(self):
        fake = FakeLLM(ideas=[[{"title": "a", "pitch": "p"}]], writes=[{"script": sample_script(), "sources": SOURCES}], reviews=[PASS])
        api = FakeIdeasApi(exists=True)
        rc, ideas = self.run_agent(fake, count=1, dry=False, api=api)
        self.assertEqual((rc, len(api.posted), api.posted[0][0]), (0, 1, "2026-11-16"))

    def test_existing_batch_skips_before_llm(self):
        fake = FakeLLM([], [], [])
        rc, ideas = self.run_agent(fake, dry=False, api=FakeIdeasApi(listed=[{"batch": "2026-11-16", "kind": "edu", "title": "t"}]))
        self.assertEqual((rc, ideas, fake.prompts), (0, [], []))

    def test_hook_type_reaches_write_and_review_and_style_notes(self):
        good = sample_script()
        good["caption"] += "\n参考になれば幸いです。"  # 言い回しの警告（不合格にはならない）
        fake = FakeLLM(ideas=[[{"title": "予報の更新", "pitch": "p", "hook_type": "そのままの質問"}]],
                       writes=[{"script": good, "sources": SOURCES}], reviews=[PASS])
        rc, ideas = self.run_agent(fake, count=1)
        self.assertEqual((rc, len(ideas)), (0, 1))
        write = next(p for p in fake.prompts if "台本担当" in p)
        self.assertIn("フックの型「そのままの質問」で書く", write)
        self.assertIn("1場面1アイデア", write)
        self.assertIn("1場面目の問いの言葉", write)
        self.assertIn("使わない言い回し", write)
        review = next(p for p in fake.prompts if "事実確認担当" in p)
        self.assertIn("フックの型「そのままの質問」", review)
        self.assertIn("参考になれば幸いです", review)
        notes = ideas[0]["review_notes"]
        self.assertIn("フックの型: そのままの質問", notes)
        self.assertIn("言い回しの注意: caption: 「参考になれば幸いです」", notes)
        self.assertEqual(set(ideas[0]), {"kind", "title", "pitch", "script", "sources", "review_notes"})

    def test_zero_passed_is_failure(self):
        rej = {"verdict": "reject", "issues": [{"severity": "重大", "where": "場面2", "problem": "出典に無い数字", "fix": "-"}]}
        fake = FakeLLM(ideas=[[{"title": "a", "pitch": "p"}]], writes=[{"script": sample_script(), "sources": SOURCES}], reviews=[rej])
        api = FakeIdeasApi()
        rc, ideas = self.run_agent(fake, count=1, dry=False, api=api)
        self.assertEqual((rc, ideas, api.posted), (1, [], []))


def no_llm(*a, **k):
    raise AssertionError("--from-file では LLM を呼ばない")


class FromFileTests(unittest.TestCase):
    """--from-file: 人手の台本ファイルを機械検査にかけて送る（LLM は使わない）。"""

    def write(self, data) -> Path:
        import tempfile
        d = tempfile.mkdtemp()
        p = Path(d) / "ideas.json"
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return p

    def idea(self, script=None, **kw):
        i = {"kind": "edu", "title": "x", "pitch": "p", "script": script or sample_script(), "sources": SOURCES, "review_notes": "人手で確認"}
        i.update(kw)
        return i

    def run_file(self, path, dry=True, api=None):
        with patch.object(llm, "generate", no_llm):
            return A.run_from_file(path, "2026-11-23", dry, api, url_check=False)

    def test_manual_files_pass_all_checks(self):
        files = sorted((ROOT / "social" / "manual").glob("*.json"))
        self.assertTrue(files)
        for f in files:
            with self.subTest(file=f.name):
                batch, raw = A.load_file(f)
                self.assertEqual(datetime.date.fromisoformat(batch).weekday(), 0)
                ideas, errs = A.check_file_ideas(raw, R.allowed_kinds(), url_check=False)
                self.assertEqual(errs, [])
                season1 = {A.norm_title(e["title"]) for e in R.load_episodes()}
                for i in ideas:
                    self.assertEqual(set(i), set(A.IDEA_KEYS))
                    self.assertNotIn(A.norm_title(i["title"]), season1)
                    self.assertEqual(R.lint_script(i["script"]), [])
                    self.assertLessEqual(R.say_chars(i["script"]), R.MAX_SAY_CHARS)
                    self.assertEqual(i["script"]["scenes"][-1], {"kind": "next", "say": R.END_SAY})
                    for s in i["sources"]:  # 出典は確認済みの一覧からだけ
                        self.assertIn(s["url"], A.KNOWN_SOURCES)

    def test_dry_run_checks_and_keeps_notes_without_llm(self):
        p = self.write({"batch": "2026-11-23", "ideas": [self.idea()]})
        rc, ideas = self.run_file(p)
        self.assertEqual((rc, len(ideas)), (0, 1))
        self.assertEqual(set(ideas[0]), set(A.IDEA_KEYS))
        self.assertEqual(ideas[0]["title"], sample_script()["title"])  # 題は台本の title
        self.assertIn("機械検査 合格（ファイルから送信", ideas[0]["review_notes"])
        self.assertIn("人手で確認", ideas[0]["review_notes"])

    def test_any_failure_sends_nothing(self):
        bad = sample_script()
        bad["scenes"][1]["say"] += "とても長い説明を足します。" * 8
        p = self.write({"ideas": [self.idea(), self.idea(bad), self.idea(sources=[])]})
        api = FakeIdeasApi()
        rc, ideas = self.run_file(p, dry=False, api=api)
        self.assertEqual((rc, ideas, api.posted), (1, [], []))

    def test_sends_batch_and_skips_existing(self):
        p = self.write([self.idea()])  # ideas の配列だけのファイルも読める
        api = FakeIdeasApi()
        rc, ideas = self.run_file(p, dry=False, api=api)
        self.assertEqual((rc, len(api.posted), api.posted[0][0], len(api.posted[0][1])), (0, 1, "2026-11-23", 1))
        api2 = FakeIdeasApi(listed=[{"batch": "2026-11-23", "kind": "edu", "title": "t"}])
        rc, ideas = self.run_file(p, dry=False, api=api2)
        self.assertEqual((rc, ideas, api2.posted), (0, [], []))

    def test_duplicate_titles_fail(self):
        _, errs = A.check_file_ideas([self.idea(), self.idea()], R.allowed_kinds(), url_check=False)
        self.assertTrue(any("同じ題" in e for e in errs))

    def test_main_uses_file_batch_and_workflow_input(self):
        p = self.write({"batch": "2026-11-23", "ideas": [self.idea()]})
        out = Path(p.parent) / "out.json"
        env = {k: v for k, v in os.environ.items() if k not in ("WAVEHACK_API_URL", "WAVEHACK_CRON_SECRET")}
        with patch.object(llm, "generate", no_llm), patch.dict(os.environ, env, clear=True), \
                patch.object(sys, "argv", ["ideas_agent.py", "--from-file", str(p), "--dry-run", "--no-url-check", "--out", str(out)]):
            self.assertEqual(A.main(), 0)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["batch"], "2026-11-23")
        wf = (ROOT / ".github" / "workflows" / "social-ideas.yml").read_text(encoding="utf-8")
        self.assertIn("from_file:", wf)
        self.assertIn('--from-file "$FROM_FILE"', wf)


# ---------------------------------------------------------------- wavehack-api クライアント（ローカルのモックサーバー）
class MockApi(BaseHTTPRequestHandler):
    state: dict = {}
    calls: list = []

    def log_message(self, *a):
        pass

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.calls.append(("GET", self.path, self.headers.get("authorization")))
        st = self.state
        if self.path.startswith("/social/ideas/next"):
            nxt = next((i for i in st["ideas"] if i["status"] == "approved"), None)
            return self._send(200, {"idea": nxt})
        if self.path.startswith("/social/ideas?"):
            return self._send(200, {"ideas": [{k: i[k] for k in ("id", "batch", "seq", "kind", "title", "status", "episode_no")} for i in st["ideas"]]})
        iid = self.path.rsplit("/", 1)[1]
        return self._send(200, {"idea": next(i for i in st["ideas"] if i["id"] == iid)})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        self.calls.append(("POST", self.path, body))
        st = self.state
        if self.path == "/social/ideas/batch":
            if body["batch"] in st["batches"]:
                return self._send(409, {"error": "batch_exists"})
            st["batches"].add(body["batch"])
            return self._send(200, {"ids": ["n1"]})
        iid, action = self.path.split("/")[3:5]
        idea = next(i for i in st["ideas"] if i["id"] == iid)
        if action == "preview":
            if idea["status"] != "approved":
                return self._send(409, {"error": "not_approved"})
            idea["status"] = "preview"
        elif action in ("posted", "failed"):
            idea["status"] = action
        return self._send(200, {"ok": True})


def idea(iid, status, no, title="予報の当たり外れ"):
    return {"id": iid, "batch": "2026-11-16", "seq": no, "kind": "edu", "title": title, "status": status, "episode_no": no,
            "script": sample_script()}


class FakeIG:
    v, user = "v26.0", "u"

    def __init__(self, media=()):
        self.media = list(media)

    def _req(self, method, url, data=None, headers=None):
        if "/media?" in url:
            return {"data": self.media}
        if "fields=permalink" in url:
            return {"permalink": "https://www.instagram.com/reel/abc/"}
        raise AssertionError(url)

    def recent_captions(self, limit=50):
        return [m["caption"] for m in self.media]


class EduFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), MockApi)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.api = social_api.SocialApi(f"http://127.0.0.1:{cls.srv.server_port}", "s3cret")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        MockApi.state = {"ideas": [], "batches": {"2026-11-09"}}
        MockApi.calls = []

    def ts(self, days=0):
        return (datetime.datetime.now(JST) - datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S%z")

    def test_client_contract(self):
        MockApi.state["ideas"] = [idea("a", "approved", 11)]
        self.assertEqual(self.api.post_batch("2026-11-16", []), (True, ["n1"]))
        self.assertEqual(self.api.post_batch("2026-11-09", []), (False, []))  # 409 batch_exists
        self.assertEqual(self.api.next_idea()["id"], "a")
        self.assertEqual(MockApi.calls[2][:3], ("GET", "/social/ideas/next?kind=edu", "Bearer s3cret"))
        self.assertTrue(self.api.preview("a", "https://x", "2026-11-16T18:00:00+09:00"))
        self.assertFalse(self.api.preview("a", "https://x", "2026-11-16T18:00:00+09:00"))  # 409（許可済み以外）
        self.api.posted("a", "m1", "https://p")
        self.assertEqual(MockApi.calls[-1], ("POST", "/social/ideas/a/posted", {"media_id": "m1", "permalink": "https://p"}))

    def test_next_builds_episode_json_with_number(self):
        MockApi.state["ideas"] = [idea("a", "approved", 11)]
        got = P.choose(self.api, FakeIG().media)
        ep = P.episode(got)
        self.assertEqual((ep["no"], ep["title"]), (11, "予報の当たり外れ"))
        self.assertTrue(P.caption(ep).startswith("🌊 波予報の読み方 #11｜予報の当たり外れ\n"))
        self.assertNotIn("フォローして", P.caption(ep))  # 締めの呼びかけは「保存して」の1回だけ
        self.assertEqual(ep["scenes"][-1]["say"], "保存して、次の海選びに使ってね！")
        self.assertNotIn("全10回", P.caption(ep))

    def test_nothing_when_no_approved_or_posted_today(self):
        self.assertIsNone(P.choose(self.api, []))  # 許可済みが無い → idea: null
        MockApi.state["ideas"] = [idea("a", "approved", 11)]
        media = [{"id": "1", "caption": "🌊 波予報の読み方 #10｜まとめ\n", "timestamp": self.ts()}]
        self.assertIsNone(P.choose(self.api, media))  # 今日すでに投稿済み
        media[0]["timestamp"] = self.ts(1)
        self.assertEqual(P.choose(self.api, media)["id"], "a")

    def test_leftover_preview_first_and_reconcile_posted(self):
        MockApi.state["ideas"] = [idea("old", "preview", 11, "前日に見送った回"), idea("a", "approved", 12)]
        self.assertEqual(P.choose(self.api, [])["id"], "old")
        media = [{"id": "m9", "caption": "🌊 波予報の読み方 #11｜予報の当たり外れ\n…", "timestamp": self.ts(1), "permalink": "https://p/9"}]
        self.assertEqual(P.choose(self.api, media)["id"], "a")  # 投稿済みだった → 記録してから次へ
        self.assertIn(("POST", "/social/ideas/old/posted", {"media_id": "m9", "permalink": "https://p/9"}), MockApi.calls)

    def test_preview_scheduled_at_and_held(self):
        MockApi.state["ideas"] = [idea("a", "approved", 11), idea("b", "held", 12)]
        ep = P.episode(MockApi.state["ideas"][0])
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "42", "GITHUB_REPOSITORY": "o/r", "EDU_PREVIEW_MODE": "run"}):
            self.assertTrue(P.do_preview(self.api, "a", ep, Path("x.mp4"), "2026-11-16"))
            self.assertFalse(P.do_preview(self.api, "b", ep, Path("x.mp4"), "2026-11-16"))  # 止められた
            self.assertTrue(P.do_preview(self.api, "a", ep, Path("x.mp4"), "2026-11-16"))  # 再予告が 409 でも preview のまま → 進める
        self.assertIn(("POST", "/social/ideas/a/preview", {"preview_url": "https://github.com/o/r/actions/runs/42",
                                                          "scheduled_at": "2026-11-16T18:00:00+09:00"}), MockApi.calls)

    def test_publish_skips_unless_preview(self):
        MockApi.state["ideas"] = [idea("a", "held", 11)]
        ep = P.episode(MockApi.state["ideas"][0])
        with patch.object(P, "publish", side_effect=AssertionError("投稿してはいけない")):
            self.assertEqual(P.do_publish(self.api, FakeIG(), "a", ep, Path("x.mp4"), 2500), "skipped")

    def test_publish_records_posted(self):
        MockApi.state["ideas"] = [idea("a", "preview", 11)]
        ep = P.episode(MockApi.state["ideas"][0])
        with patch.object(P, "publish", return_value="m1") as pub:
            self.assertEqual(P.do_publish(self.api, FakeIG(), "a", ep, Path("x.mp4"), 2500), "published")
        self.assertTrue(pub.call_args.args[2].startswith("🌊 波予報の読み方 #11｜"))
        self.assertEqual(MockApi.calls[-1], ("POST", "/social/ideas/a/posted", {"media_id": "m1", "permalink": "https://www.instagram.com/reel/abc/"}))
        self.assertEqual(MockApi.state["ideas"][0]["status"], "posted")

    def test_publish_failure_reports_failed(self):
        MockApi.state["ideas"] = [idea("a", "preview", 11)]
        ep = P.episode(MockApi.state["ideas"][0])
        with patch.object(P, "publish", side_effect=SystemExit("rupload 400 ProcessingFailedError")):
            with self.assertRaises(SystemExit):
                P.do_publish(self.api, FakeIG(), "a", ep, Path("x.mp4"), 2500)
        self.assertEqual(MockApi.calls[-1][1], "/social/ideas/a/failed")
        self.assertIn("ProcessingFailedError", MockApi.calls[-1][2]["reason"])

    def test_publish_already_on_instagram_is_recorded_not_reposted(self):
        MockApi.state["ideas"] = [idea("a", "preview", 11)]
        ep = P.episode(MockApi.state["ideas"][0])
        ig = FakeIG([{"id": "m5", "caption": P.caption(ep), "timestamp": self.ts(), "permalink": "https://p/5"}])
        with patch.object(P, "publish", side_effect=AssertionError("二重投稿")):
            self.assertEqual(P.do_publish(self.api, ig, "a", ep, Path("x.mp4"), 2500), "recorded")

    def test_upload_retry_waits(self):
        class IGFail(FakeIG):
            n = 0

            def _req(self, method, url, data=None, headers=None):
                IGFail.n += 1
                raise SystemExit("rupload 400 ProcessingFailedError")
        sleeps = []
        with patch.object(P.time, "sleep", sleeps.append), self.assertRaises(SystemExit):
            P.publish(IGFail(), Path(__file__), "🌊 波予報の読み方 #11｜x\n", 2500)
        self.assertEqual(sleeps, [120, 300, 900, 180])  # 4回試す（2分・5分・15分あけて）＋最後に3分待って公開を確かめる

    def test_season1_caption_unchanged(self):
        ep1 = next(e for e in R.load_episodes() if e["no"] == 1)
        self.assertIn("次回 #02 は「周期」", P.caption(ep1))
        self.assertIn("シリーズ全10回", P.caption(next(e for e in R.load_episodes() if e["no"] == 10)))


if __name__ == "__main__":
    unittest.main()
