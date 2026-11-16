"""ネタ出しエージェント: 教育リール「波予報の読み方」第2弾のネタと台本を週1回まとめて作り、wavehack-api に送る。

  python social/ideas_agent.py [--count 5] [--batch 2026-11-16] [--dry-run] [--out ideas.json]

流れ（money-reel/agents の ideas.py・write.py を移植）
  1. その週の batch が API に既にあれば何もしない（日曜夜〜月曜朝に複数回起動するため）
  2. ネタ案を --count の2倍出す（第1弾の題材・API の過去のネタと重ねない。投稿時期の季節に合うものを優先）
  3. 1本ずつ台本を書く → 機械検査（kind・キー・値の形・字数・出典・締め）→ 別の指示のレビュー（事実・出典との一致・字数・構成）
     → 指摘があれば直す（修正は2回まで）。合格したものだけ残す（--count 本に達したら止める）
     ネタごとにフックの型（HOOK_TYPES）を選ばせ、その型で1場面目を書かせる。AI っぽい言い回し（edu_rules.lint_script）は
     不合格にせずレビューに渡し、review_notes に「言い回しの注意」として載せる。取り込んだ外部素材は THIRD_PARTY_LICENSES.md
  4. POST /social/ideas/batch（409 batch_exists は正常終了）。Slack への投稿と承認は API 側
  合格が0本なら終了コード 1。--dry-run は API に送らず結果を標準出力（または --out）に出す

ネタ・台本は公開リポジトリに残さない（保存先は API）。実行ログには題と検査結果だけ出す。
環境変数: GEMINI_API_KEY / WAVEHACK_API_URL / WAVEHACK_CRON_SECRET / SOCIAL_IDEAS_COUNT（本数の既定）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import edu_rules as R  # noqa: E402
import llm  # noqa: E402
from social_api import SocialApi  # noqa: E402

JST = dt.timezone(dt.timedelta(hours=9))
KIND = "edu"

STANCE = ("サーフィンの波予報の読み方を、公的な資料（気象庁・NOAA など）に基づいて短く解説する人。"
          "Wave Hack（波予報サービス）のアカウントだが宣伝はしない（第1弾と同じ）。経験則は「目安」と明記する。出典を示せない主張は書かない。")
DIRECTIONS = """- 予報の読み方を深める: 風浪とうねりの見分け方、周期と波のサイズ、うねりが2方向から来る日、大潮・小潮と時間帯、など
- 判断の型: 小波の日にどこを見るか、台風うねりの安全な見方、季節ごとの読み方、など"""

# テンプレート（edu/template.html）の図の描き方。キーと値の形は既存回から機械的に集めたもの（R.allowed_kinds）が正
KIND_GUIDE = {
    "hook": "1場面目（0秒目から全部見える）。q=問いかけ（<br>で2〜3行、<b>で強調、？で終わる）、big=背景の大きな英数字（例 10s / SWELL / TIDE）",
    "sig": "有義波高の説明専用の図（15本の棒を高い順に並べ、上位1/3の平均線「有義波高 1.0m」が固定で出る）。h, sub",
    "max": "棒3本の倍率比較（単位は「倍」で固定。1.9 は「約2」と表示）。bars=[[倍率, ラベル]]",
    "cmp": "カード1〜3枚の比較。cards=[{label, value（短く）, note（<br>可）, hl（強調する1枚に true）}]",
    "waves": "波形2段のアニメ。rows=[{label, T=周期の秒数（波長と速さに反映）, note}]。h, sub",
    "arrows": "上が海・下が陸の図に矢印1〜3本。items=[{deg: 0=海から陸へ（正面）/90=海岸と平行/180=陸から海へ, label, good: true=青・false=橙・null=黒}]、landLabel=陸の名前",
    "scale": "4段階の目安表。rows=[[範囲, 名前, 説明, 濃さ0〜3]]、note=注記（※〜）",
    "curve": "潮位の曲線（0〜24時。形は固定で満潮 3時・15.4時ごろ、干潮 9.2時・21.6時ごろ）。curves=[{label, amp: 0.1〜1}] 1〜2本、marks=[[時刻, \"満潮\" か \"干潮\"]]",
    "isobar": "低気圧の等圧線パネル2枚。panels=[{label, note, gap: 線の間隔 20〜50（狭いほど混んだ等圧線）, hl}]",
    "travel": "沖の台風（storm）から海岸（coast）へうねりが届く図。label=図の見出し",
    "bars": "棒グラフ1〜3本。bars=[[値, ラベル, 棒の上の文字]]、colors=棒の色（#0877f9 系の青）",
    "check": "番号つきの順番リスト（7項目まで）。items",
    "sum": "まとめ3点（必須。ストーリーズ画像にも使う）。points",
    "next": f"締め（最後の場面）。キーは kind と say だけで、say は固定文「{R.END_SAY}」",
}


# フックの型（教育向けだけ）。instagram-agent-skill-ja（MIT）の skills/ig-reel/hooks.json から選んで、波予報の解説向けに書き直した
# （元の id を src に残す。出典は THIRD_PARTY_LICENSES.md）。煽り系（損した告白・暴露・他人の結果・期限・一番〜・常識の逆・盗んでいい）は入れない。
HOOK_TYPES = [
    {"name": "そのままの質問", "src": 16, "form": "見る人が実際に口にする・検索する形のままの質問を1つ投げる",
     "example": "予報の「1m」って、どの波の高さ？", "trap": "質問をきれいに整えすぎて、誰も実際には言わない言葉になる"},
    {"name": "よくある思い込みを正す", "src": "3・12 を元に教育向けに改変", "form": "よく言われる・思われがちな見方を1つ挙げ、公的資料ではどうかを示す",
     "example": "うねりの向きは「行く方向」だと思ってない？", "trap": "誰も思っていない思い込みを作る。出典で正せない話を「実は」で言い切る"},
    {"name": "間違った見方と正しい見方", "src": 7, "form": "ありがちな予報の見方を「惜しい」と示し、見る人のせいにせず正しい見方を渡す",
     "example": "波の高さだけ見てない？周期を見ないと、同じ1mでも別の波", "trap": "「間違ってます」だけで責める。正しい見方を同じ場面で渡さない"},
    {"name": "数＋一押し", "src": 11, "form": "見る所の数を言い、どれが一番効くかを予告する",
     "example": "小波の日に見る所は3つ。2つ目を見落としやすい", "trap": "数を盛る（4つを超える）。予告した一押しを最後まで言わない"},
    {"name": "呼びかけ・「〜なら、どう読む？」", "src": "10・14", "form": "具体的な状況や人に呼びかけ、その状況での読み方を問う",
     "example": "南うねり1.5m・北東の風5m/sなら、どう読む？", "trap": "状況が広すぎて誰にも当たらない（「上手くなりたいなら」は状況ではない）"},
    {"name": "統計の数字", "src": 23, "form": "公的資料の数字を1つ先に出して問う（出典は動画の中で言う）",
     "example": "100波に1回は、予報の約1.5倍の波が来る？", "trap": "出どころを言えない数字。動画内で出典（気象庁など）を言わないなら使わない"},
]
HOOK_NAMES = [h["name"] for h in HOOK_TYPES]


def hook_type(name: str | None) -> dict | None:
    return next((h for h in HOOK_TYPES if h["name"] == (name or "").strip()), None)


def hook_guide() -> str:
    return "\n".join(f"  - {h['name']}: {h['form']}。例「{h['example']}」。失敗の仕方: {h['trap']}" for h in HOOK_TYPES)


def min_hook_kinds(n: int) -> int:
    """n本のネタで使う型の種類の下限（5本なら3種類）。"""
    return min(len(HOOK_TYPES), max(1, -(-n * 3 // 5)))


def spread(cands: list[dict]) -> list[dict]:
    """型が偏らないよう、型ごとに順番に並べ直す（同じ型の中の順番は保つ）。"""
    groups: dict[str, list[dict]] = {}
    for c in cands:
        groups.setdefault(str(c.get("hook_type") or ""), []).append(c)
    out: list[dict] = []
    while any(groups.values()):
        for g in groups.values():
            if g:
                out.append(g.pop(0))
    return out


def today() -> dt.date:
    return dt.datetime.now(JST).date()


def default_batch(d: dt.date | None = None) -> str:
    """その週の月曜（JST）。日曜夜に起動したときは翌日の月曜の週として扱う。"""
    d = (d or today()) + dt.timedelta(days=1)
    return (d - dt.timedelta(days=d.weekday())).isoformat()


def season(d: dt.date) -> str:
    """投稿時期（d から約1か月）の季節の目安。題材の優先度づけに使う（事実の根拠にはしない）。"""
    m = (d + dt.timedelta(days=14)).month
    return {12: "冬（西高東低の冬型。北西の季節風で日本海側は荒れやすく、太平洋側は陸からの風になりやすい）",
            1: "冬（西高東低の冬型。北西の季節風。水温が低い）", 2: "冬の終わり（冬型と南岸低気圧が交互）",
            3: "春（低気圧が周期的に通り、風向きが変わりやすい。春一番）", 4: "春（移動性高気圧と低気圧が交互。風が変わりやすい）",
            5: "初夏（南寄りの風。梅雨入り前）", 6: "梅雨（梅雨前線、南寄りの風）", 7: "夏（梅雨明け、台風の発生が増える）",
            8: "夏（台風シーズン。遠くの台風のうねりが届く）", 9: "初秋（台風シーズンの最盛期）",
            10: "秋（台風シーズン終盤、北東の風が増える）", 11: "晩秋（北寄りの季節風の吹き始め、台風シーズン終盤）"}[m]


def norm_title(t: str) -> str:
    return re.sub(r"[\s・、。「」『』！？!?〜ー\-（）()]", "", R.visible(t or "")).lower()


# 実在を確認済みの公的資料（2026-11-16 確認）。検索が使えないとき、出典の URL はここからだけ選ばせる
KNOWN_SOURCES = """- 気象庁「波浪の基礎知識」https://www.data.jma.go.jp/kaiyou/db/wave/comment/elmknwl.html
- 気象庁「潮汐の知識」https://www.data.jma.go.jp/kaiyou/db/tide/knowledge/index.html
- 気象庁「潮汐とは」https://www.data.jma.go.jp/kaiyou/db/tide/knowledge/tide/choseki.html
- 気象庁「風の強さと吹き方」https://www.jma.go.jp/jma/kishou/know/yougo_hp/kazehyo.html
- 気象庁「予報用語（風）」https://www.jma.go.jp/jma/kishou/know/yougo_hp/kaze.html
- 気象庁「季節予報の利用と解説」https://www.jma.go.jp/jma/kishou/know/kisetsu_riyou/index.html
- 気象庁「台風について」https://www.jma.go.jp/jma/kishou/know/typhoon/index.html
- NOAA NCEP「GFS Products」https://www.nco.ncep.noaa.gov/pmb/products/gfs/"""


def facts_library(episodes: list[dict]) -> str:
    """確認済みの事実（運営者が確認して公開した第1弾の caption と出典）。検索が使えないとき、使ってよい事実はこれだけ。"""
    return "\n\n".join(f"■#{e['no']:02d} {e['title']}（{e.get('source', '')}）\n{e.get('caption', '')}" for e in episodes)


def examples(episodes: list[dict]) -> str:
    return "\n".join(json.dumps({k: v for k, v in e.items() if k != "no"}, ensure_ascii=False) for e in episodes)


# ---------------------------------------------------------------- 1. ネタ案
def ideas_prompt(n: int, episodes: list[dict], past: list[str], d: dt.date) -> str:
    fact = ("- 事実は Google 検索で公的な一次情報（気象庁・海上保安庁・NOAA など）を確かめられるものだけ。" if llm.SEARCH_OK else
            "- いまは検索が使えない。次の『確認済みの事実』と、気象庁・NOAA の公開資料で確実に確かめられることだけで書けるネタにする。\n"
            "確認済みの事実:\n" + facts_library(episodes)[:8000])
    return f"""あなたは Instagram リール「Wave Hack｜波予報の読み方」第2弾のネタ出し担当です。
立場: {STANCE}
今日は {d.isoformat()}。投稿はこの先1〜4週間（木曜以外の18時に1日1本、運営者が許可した回だけ）。
季節: {season(d)}。この時期に役立つ題材を半分以上にする。

方向:
{DIRECTIONS}

条件:
- 1本は約35秒（読み上げ {R.MAX_SAY_CHARS - len(R.END_SAY) - 30}〜{R.MAX_SAY_CHARS - len(R.END_SAY)}字＋締め）で言い切れる1テーマ。第1弾（下）より一歩深い内容。
- 第1弾の題と同じ内容は出さない。第1弾の題: {json.dumps([e['title'] for e in episodes], ensure_ascii=False)}
- これまでに出したネタ（許可されなかったものを含む）と重ねない: {json.dumps(past, ensure_ascii=False)}
- 図は既存の場面の種類だけで描ける題材にする（波形・矢印・潮位の曲線・等圧線・比較カード・棒グラフ・目安表・チェックリスト）。
{fact}
- 宣伝色は控えめ。リンクやプロフィールへの誘導はしない。
- ネタごとに、最初の問いかけ（hook）の型を次の一覧から1つ選び、hook_type にその名前をそのまま書く。hook はその型の形で書き、失敗の仕方に当てはまらないようにする。
  型が偏らないようにする: {n}本で{min_hook_kinds(n)}種類以上の型を使い、同じ型は{max(1, -(-n // min_hook_kinds(n)))}本まで。
フックの型:
{hook_guide()}

第1弾の原稿（題材・文体の手本）:
{facts_library(episodes)[:6000]}

JSON の配列（{n}本）だけで答える:
[{{"title": "画面のバッジに出す題（12字以内）", "pitch": "運営者向けの説明（何をどう見せ、なぜ今か。2文）", "hook_type": "フックの型の名前（一覧のどれか）",
  "hook": "最初の問いかけ", "angle": "使う場面の種類と見せ方", "facts_to_check": ["確かめる事実と、出典にする公的資料"]}}]"""


def propose(n: int, episodes: list[dict], past: list[str], d: dt.date) -> list[dict]:
    try:
        got = llm.parse_json(llm.gemini(ideas_prompt(n * 2, episodes, past, d), search=True, temperature=0.9)[0])
    except (ValueError, SystemExit) as e:
        print("ネタ案を作れなかった:", str(e)[:200], flush=True)
        return []
    seen = {norm_title(t) for t in [e["title"] for e in episodes] + past}
    out = []
    for i in got if isinstance(got, list) else []:
        if not isinstance(i, dict):
            continue
        t = norm_title(i.get("title", ""))
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(i)
    return out


# ---------------------------------------------------------------- 2〜4. 台本 → 検査 → レビュー
def write_prompt(idea: dict, episodes: list[dict], kinds: dict, d: dt.date, feedback: str = "", draft: dict | None = None) -> str:
    if llm.SEARCH_OK:
        fact = "- 事実・数字は Google 検索で公的な一次情報（気象庁・海上保安庁・NOAA など）を確かめ、確かめられたことだけ書く。"
    else:
        fact = ("- Google 検索は使えない。使ってよい事実は、下の『確認済みの事実』と、出典に挙げる公的資料に確実に書いてあることだけ。"
                "確かでない数字は使わない。\n確認済みの事実:\n" + facts_library(episodes)[:8000])
    body = R.MAX_SAY_CHARS - len(R.END_SAY)
    guide = "\n".join(f"  - {k}: {KIND_GUIDE.get(k, '')}（キー {sorted(v)}）" for k, v in kinds.items())
    ht = hook_type(idea.get("hook_type"))
    if ht:
        hook_rule = (f"- 1場面目（q と say）はフックの型「{ht['name']}」で書く。形: {ht['form']}。例「{ht['example']}」（例の文は使い回さない）。"
                     f"失敗の仕方に当てはまらないこと: {ht['trap']}")
    else:
        hook_rule = "- 1場面目（q と say）は次のフックの型から1つ選んで、その形で書く（失敗の仕方に当てはまらないこと）:\n" + hook_guide()
    base = f"""あなたは Instagram リール「波予報の読み方」第2弾の台本担当です。
立場: {STANCE}
今日は {d.isoformat()}。

ネタ: {json.dumps(idea, ensure_ascii=False)}

ルール:
- 1場面目は hook。0秒目から画面に問いかけ（q）が出る。最初の1文（say）も問いかけにする。前置き・あいさつは書かない。
{hook_rule}
- 1場面目の say と q に入れないもの（入れると機械検査で不合格）: あいさつ、動画の前置き（「今日は〜を紹介します」「この動画では」）、
  注目のお願い（「ちょっと待って」「最後まで見て」）、ハッシュタグ、絵文字。弱い出だし（{'・'.join(R.HOOK_WEAK_OPENERS[:12])} など）で始めない。
- 1場面1アイデア。1つの場面で2つのことを言わない（言いたいことが2つあるなら場面を分ける）。
- 最後の sum の say では、1場面目の問いの言葉をそのまま使って、その問いに答える（問いを投げっぱなしにしない）。
- 使わない言い回し（AI っぽく聞こえる。say と caption で避ける）:
{R.style_brief()}
{fact}
- 経験則（「〜のことが多い」「〜しやすい」など公的資料に数字が無いもの）は「目安」と明記する。出典を示せない主張は書かない。
- 条件付きの話は条件を言う。例外を省いて「必ず」「全部」と言わない。安全に関わる話（台風・強いオフショア・離岸流）は控えめに、無理をしない方向で。
- 読み上げ（全場面の say の合計）は締めを除いて {body - 30}〜{body}字（締めを含めて {R.MAX_SAY_CHARS}字以内 ≒ 40秒。厳守）。1文は短く、話し言葉で。
- 画面の文字（q・h・label など）と読み上げの数字・言葉を一致させる。
- 場面は4〜6個。hook → 中身 → sum（まとめ3点）→ next（締め）。next の say は「{R.END_SAY}」で固定、キーは kind と say だけ。
- 使える場面の種類（テンプレートに無い図は作れない。キーもこれだけ）:
{guide}
- 画面の文字で使えるタグは <b> と <br> だけ。
- 宣伝・リンク・プロフィールへの誘導は書かない（「Wave Hack」「リンク」「プロフィール」「URL」を使わない）。
- script の形: {{"series": "{R.SERIES}", "title": 12字以内, "next": null, "source": "出典：気象庁「〜」ほか"（公的機関の資料名）, "caption": 動画の内容を補う文章（150字前後、改行可）, "scenes": [...]}}。no は書かない。
- sources: 台本の事実の根拠にした公的機関のページ（気象庁 jma.go.jp・NOAA noaa.gov など）を title と url（https://〜、元のページの URL）で1〜4件。{'' if llm.SEARCH_OK else '検索が使えないので、URL は次の実在確認済みの一覧からだけ選ぶ（一覧に無い URL を作らない）:' + chr(10) + KNOWN_SOURCES}

第1弾の原稿（形・文体・場面の使い方の手本。内容は繰り返さない）:
{examples(episodes)}

JSON だけで答える: {{"script": {{...}}, "sources": [{{"title": "...", "url": "https://..."}}]}}"""
    if draft:
        base += f"\n\n前回の原稿:\n{json.dumps(draft, ensure_ascii=False)}\n\n直すこと:\n{feedback}\n直した全体（script と sources）を JSON で返す。"
    return base


def review_prompt(script: dict, sources: list, hook: str | None = None, style_warnings: list[str] | None = None) -> str:
    fact = ("Google 検索で公的な一次情報を確かめること。sources の各ページに、台本の事実が実際に書かれているかも確かめる。" if llm.SEARCH_OK else
            "検索は使えない。台本の事実を1つずつ、確実に知っている公的資料の内容と照合し、確かでないものは重大とする。")
    ht = hook_type(hook)
    extra = ""
    if ht:
        extra += (f"\n7. フックの型「{ht['name']}」（{ht['form']}）になっているか。その型の失敗の仕方に当たっていないか: {ht['trap']}。"
                  "最後の sum の say が1場面目の問いの言葉を回収して答えているか。")
    else:
        extra += "\n7. 最後の sum の say が1場面目の問いの言葉を回収して答えているか。"
    if style_warnings:
        extra += ("\n8. 機械で見つけた AI っぽい言い回し（下）。読み上げで不自然なら軽微として直し方を書く。自然な使い方なら指摘しなくてよい:\n"
                  + "\n".join(f"   - {w}" for w in style_warnings))
    return f"""あなたは Instagram リール「波予報の読み方」の事実確認担当です。台本を書いた人とは別の立場で、厳しく確認してください。
{fact}

確認すること:
1. 事実の正確さ: 数字・用語の定義・現象の説明が公的資料（気象庁・NOAA など）と合っているか。
2. 出典との一致: script.source と sources が、台本の主張の根拠として妥当か（無関係なページ・存在しない資料名になっていないか）。
3. 経験則に「目安」と書いてあるか。出典を示せない主張が無いか。安全に関わる話で危ない行動を勧めていないか。
4. 字数と構成: 読み上げは締めを含めて {R.MAX_SAY_CHARS}字以内（いま {R.say_chars(script)}字）。1場面目が問いかけか。hook → 中身 → まとめ → 締め になっているか。
5. 画面の文字と読み上げ（say）、caption と動画が一致しているか。図の種類が内容に合っているか（例: 潮位の曲線の満潮・干潮の時刻）。
6. 読み上げとして自然な日本語か。宣伝色が強すぎないか。{extra}

台本:
{json.dumps(script, ensure_ascii=False, indent=1)}
sources:
{json.dumps(sources, ensure_ascii=False)}

JSON だけで答える:
{{"verdict": "pass か fix か reject", "issues": [{{"severity": "重大 か 軽微", "where": "場所", "problem": "問題", "fix": "直し方"}}],
  "checked_claims": ["確認した主な事実と、その根拠の出典"], "summary": "運営者向けの所見（2〜3文）"}}
reject は「事実として成り立たない・確かめられない」とき。軽微な指摘だけなら pass でよい。"""


def check_urls(sources: list, timeout: int = 20) -> tuple[list[str], list[str]]:
    """出典の URL が開けるか。404/410 は不合格、それ以外の失敗（接続できない等）は所見に書くだけ。"""
    errs, warns = [], []
    for s in sources if isinstance(sources, list) else []:
        url = s.get("url", "") if isinstance(s, dict) else ""
        if not url.startswith("https://"):
            continue
        for method in ("HEAD", "GET"):
            try:
                req = urllib.request.Request(url, method=method, headers={"User-Agent": "Mozilla/5.0 (compatible; source-check)"})
                with urllib.request.urlopen(req, timeout=timeout):
                    break
            except urllib.error.HTTPError as e:
                if e.code in (404, 410):
                    errs.append(f"[出典] {url} が見つからない（{e.code}）。実在するページの URL にする")
                    break
                if method == "GET":
                    warns.append(f"出典 {url} は確認できず（HTTP {e.code}）")
            except (urllib.error.URLError, TimeoutError, ValueError) as e:
                if method == "GET":
                    warns.append(f"出典 {url} は確認できず（{str(e)[:80]}）")
    return errs, warns


def write_one(idea: dict, episodes: list[dict], kinds: dict, d: dt.date, url_check: bool = True) -> dict:
    """1本書く。{ok, script, sources, review_notes, log} を返す。"""
    log: list[str] = []
    draft: dict | None = None
    feedback = ""
    for rnd in range(3):  # 初稿 + 修正2回
        text, ground = llm.gemini(write_prompt(idea, episodes, kinds, d, feedback, draft), search=True, temperature=0.6)
        try:
            got = llm.parse_json(text)
            script, sources = R.normalize(got["script"]), got.get("sources") or []
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            log.append(f"第{rnd + 1}稿: JSON にならなかった（{e}）")
            feedback, draft = "JSON の形 {\"script\": {...}, \"sources\": [...]} で返していなかった。JSON だけで返す。", None
            continue
        draft = {"script": script, "sources": sources}
        errs = R.validate(script, kinds) + R.validate_sources(sources)
        warns: list[str] = []
        if not errs and url_check:
            e2, warns = check_urls(sources)
            errs += e2
        log.append(f"第{rnd + 1}稿: 読み上げ {R.say_chars(script)}字（見積もり {R.est_seconds(R.say_chars(script)):.1f}秒）、機械検査 {'合格' if not errs else f'不合格 {len(errs)}件'}")
        if errs:
            log += [f"  - {e}" for e in errs]
            feedback = "\n".join(errs)
            continue
        style = R.lint_script(script)  # 警告だけ（不合格にはしない）。レビューに渡して判断させ、承認カードにも出す
        if style:
            log.append(f"  言い回しの注意 {len(style)}件")
        rv_text, _ = llm.gemini(review_prompt(script, sources, idea.get("hook_type"), style), search=True, temperature=0.1)
        try:
            rv = llm.parse_json(rv_text)
            issues = [i for i in rv.get("issues", []) if isinstance(i, dict)]
        except (ValueError, AttributeError):
            log.append("  レビューが JSON にならなかった")
            feedback = ""
            continue
        log.append(f"  レビュー: {rv.get('verdict')}（重大 {sum(i.get('severity') == '重大' for i in issues)}件・軽微 {sum(i.get('severity') != '重大' for i in issues)}件）")
        if rv.get("verdict") == "pass" and not any(i.get("severity") == "重大" for i in issues):
            notes = [f"レビュー合格（第{rnd + 1}稿、検索{'あり' if llm.SEARCH_OK else 'なし'}、読み上げ {R.say_chars(script)}字 ≒ {R.est_seconds(R.say_chars(script)):.0f}秒）"]
            if hook_type(idea.get("hook_type")):
                notes.append(f"フックの型: {idea['hook_type']}")
            if rv.get("summary"):
                notes.append(str(rv["summary"]))
            if style:
                notes.append("言い回しの注意: " + " / ".join(style))
            notes += [f"・軽微: {i.get('where')}: {i.get('problem')}" for i in issues]
            notes += [f"・確認: {c}" for c in rv.get("checked_claims", [])][:6]
            notes += [f"・{w}" for w in warns]
            return {"ok": True, "script": script, "sources": sources, "review_notes": "\n".join(notes), "log": log, "grounding": ground[:5]}
        if rv.get("verdict") == "reject":
            log.append("  レビューで却下（事実として成り立たない・確かめられない）")
            break
        feedback = "\n".join(f"{i.get('where')}: {i.get('problem')} → {i.get('fix')}" for i in issues)
    return {"ok": False, "script": (draft or {}).get("script"), "sources": (draft or {}).get("sources"), "review_notes": "", "log": log}


# ---------------------------------------------------------------- まとめ
def run(count: int, batch: str, dry: bool, api: SocialApi | None, d: dt.date | None = None, url_check: bool = True) -> tuple[int, list[dict]]:
    """(終了コード, 送るネタ) を返す。"""
    d = d or today()
    past: list[str] = []
    if api:
        listed = api.list_ideas(200)
        if not dry and any(i.get("batch") == batch for i in listed):
            print(f"batch {batch} は送信済み。何もしない")
            return 0, []
        past = [i.get("title", "") for i in listed if i.get("kind") == KIND]
    else:
        print("API に接続しない（dry-run）。過去のネタとの重複は第1弾とだけ確かめる")
    episodes = R.load_episodes()
    kinds = R.allowed_kinds(episodes)
    print("検索:", "使える" if llm.search_available() else "使えない（確認済みの事実の範囲で書く）", flush=True)
    cands = spread(propose(count, episodes, past, d))  # 合格した順に count 本で止まるので、型ごとに交互に書く
    print(f"ネタ案 {len(cands)}本:", " / ".join(f"{c.get('title', '')}（{c.get('hook_type') or '型なし'}）" for c in cands), flush=True)
    ideas = []
    for c in cands:
        if len(ideas) >= count:
            break
        r = write_one(c, episodes, kinds, d, url_check)
        print(f"■ {c.get('title')} → {'合格' if r['ok'] else '不合格'}", flush=True)
        for line in r["log"]:
            print("  " + line, flush=True)
        if r["ok"]:
            ideas.append({"kind": KIND, "title": r["script"]["title"], "pitch": str(c.get("pitch", "")), "script": r["script"],
                          "sources": r["sources"], "review_notes": r["review_notes"]})
    if not ideas:
        print("合格した台本が0本。送らない", flush=True)
        return 1, []
    if len(ideas) < count:
        print(f"合格は {len(ideas)}本（目標 {count}本）。ある分だけ送る", flush=True)
    if dry:
        return 0, ideas
    sent, ids = api.post_batch(batch, ideas)  # type: ignore[union-attr]
    print(f"batch {batch}: {len(ideas)}本を送った ids={ids}" if sent else f"batch {batch} は送信済み（409）。正常終了", flush=True)
    return 0, ideas


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--count", type=int, default=int(os.environ.get("SOCIAL_IDEAS_COUNT") or 5))
    ap.add_argument("--batch", help="その週の月曜（JST、YYYY-MM-DD）。既定は今週（日曜は翌日の週）")
    ap.add_argument("--dry-run", action="store_true", help="API に送らず結果を出す")
    ap.add_argument("--out", type=Path, help="dry-run の結果を書くファイル（無ければ標準出力）")
    ap.add_argument("--no-url-check", action="store_true", help="出典 URL が開けるかを確かめない")
    a = ap.parse_args()
    batch = a.batch or default_batch()
    dt.date.fromisoformat(batch)
    if dt.date.fromisoformat(batch).weekday() != 0:
        ap.error(f"--batch は月曜にする（{batch}）")
    api = SocialApi() if (not a.dry_run or (os.environ.get("WAVEHACK_API_URL") and os.environ.get("WAVEHACK_CRON_SECRET"))) else None
    rc, ideas = run(a.count, batch, a.dry_run, api, url_check=not a.no_url_check)
    if a.dry_run and ideas:
        out = json.dumps({"batch": batch, "ideas": ideas}, ensure_ascii=False, indent=1)
        if a.out:
            a.out.write_text(out + "\n", encoding="utf-8")
            print(f"{len(ideas)}本を {a.out} に書いた（API には送っていない）")
        else:
            print(out)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
