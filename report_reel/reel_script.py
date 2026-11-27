"""波レポート → Instagram リールの台本とキャプション（事実だけから決まった形で作る。LLM は使わない）。

入力は wavehack-api の GET /internal/report-reels/:id の中身（report / credit / consent / slack）。
同じ入力と同じ「今日」なら毎回同じ台本・キャプションになる（作り直しても言い回しが変わらない）。

決まりごと
  - 事実だけ。盛らない（予報の点数・サイズ・良し悪し・混み具合・地形・撮影時刻だけを使う）
  - 予報スコアが 55 未満なら azure で強調しない（forecast_hl=False）
  - ひとことは投稿者本人の言葉（投稿時に NG ワード済み）。読み上げず、画面とキャプションに「」で引用、40 字まで
  - クレジットは「投稿：◯◯さん」（ニックネーム紹介の同意とニックネームがあるとき）か「みんなの波レポートより」
"""
from __future__ import annotations

import datetime
import html
import re

JST = datetime.timezone(datetime.timedelta(hours=9))
HL_MIN = 55  # これ以上の予報スコアだけ azure で強調する（デザイン規定）
COMMENT_MAX = 40

# TTS が誤読しやすいポイント名（narration.READINGS に無いもの）。画面・キャプションは漢字のまま
POINT_READINGS = {
    "木崎浜": "きざきはま",
    "青島": "あおしま",
    "曽山寺": "そざんじ",
    "金ヶ浜": "かねがはま",
    "伊勢ヶ浜": "いせがはま",
}
WORD_READINGS = {"今朝": "けさ", "次の海": "つぎのうみ", "頭オーバー": "あたまオーバー"}

_DIG = ["", "いち", "に", "さん", "よん", "ご", "ろく", "なな", "はち", "きゅう"]


def kana_number(n: int) -> str:
    """0〜999 の読み（三百・六百・八百などの音便込み）。"""
    if n == 0:
        return "ゼロ"
    h, t, o = n // 100, n // 10 % 10, n % 10
    out = ""
    if h:
        out += {1: "ひゃく", 3: "さんびゃく", 6: "ろっぴゃく", 8: "はっぴゃく"}.get(h, _DIG[h] + "ひゃく")
    if t:
        out += ("" if t == 1 else _DIG[t]) + "じゅう"
    if o:
        out += _DIG[o]
    return out


def kana_hour(h: int) -> str:
    return {4: "よじ", 7: "しちじ", 9: "くじ", 14: "じゅうよじ", 17: "じゅうしちじ", 19: "じゅうくじ"}.get(h, kana_number(h) + "じ") if h else "れいじ"


def kana_minute(m: int) -> str:
    t, o = m // 10, m % 10
    ones = {0: "", 1: "いっぷん", 2: "にふん", 3: "さんぷん", 4: "よんぷん", 5: "ごふん", 6: "ろっぷん",
            7: "ななふん", 8: "はっぷん", 9: "きゅうふん"}
    if o == 0:
        return ("" if t == 1 else _DIG[t]) + "じゅっぷん"
    return (("" if t == 1 else _DIG[t]) + "じゅう" if t else "") + ones[o]


def kana_points(n: int) -> str:
    """33 → さんじゅうさんてん（1 は いってん、8 は はってん、10 の倍数は じゅってん）"""
    if n == 100:
        return "ひゃくてん"
    o = n % 10
    if n and o == 0:
        return kana_number(n)[: -len("じゅう")] + "じゅってん"
    if o == 1:
        return kana_number(n - 1) + "いってん" if n > 1 else "いってん"
    if o == 8:
        return (kana_number(n - 8) if n > 8 else "") + "はってん"
    return kana_number(n) + "てん"


def _captured(data: dict) -> datetime.datetime:
    r = data["report"]
    iso = r.get("captured_at_jst") or r["captured_at"]
    return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(JST)


def short_comment(text: str | None, limit: int = COMMENT_MAX) -> str | None:
    """本人の言葉を 1 行・40 字までにする（超えたら 39 字 + …）。"""
    if not text:
        return None
    t = re.sub(r"\s+", " ", text.replace("\r", " ").replace("\n", " ")).strip()
    if not t:
        return None
    return t if len(t) <= limit else t[: limit - 1] + "…"


def comment_html(text: str | None) -> str | None:
    """画面用: 改行は保ち、HTML として安全にする（40 字までは short_comment と同じ数え方）。"""
    if not text:
        return None
    t = text.replace("\r", "").strip()
    if len(re.sub(r"\s+", "", t)) == 0:
        return None
    if len(t.replace("\n", "")) > COMMENT_MAX:
        t = short_comment(t) or ""
    return "<br>".join(html.escape(line) for line in t.split("\n") if line.strip())


def place_label(r: dict) -> str:
    return (r.get("pref") or r.get("area") or "").strip()


def build_episode(data: dict, today: datetime.date | None = None) -> dict:
    """テンプレート（report_reel/template.html）に入れる ep。scenes[].say が読み上げ、readings が読み。"""
    r, credit = data["report"], data["credit"]
    point = r.get("point_name") or r.get("point_id")
    cap = _captured(data)
    today = today or datetime.datetime.now(JST).date()
    score = r.get("forecast_score")
    score = int(round(score)) if isinstance(score, (int, float)) else None
    size, quality = r["size_label"], r["quality_label"]
    crowd, terrain = r.get("crowd_label"), r.get("terrain_label")

    if cap.date() == today:
        when_say = "今朝" if cap.hour < 11 else "今日"
        when_disp = when_say
    elif cap.date() == today - datetime.timedelta(days=1):
        when_say, when_disp = "昨日", "昨日"
    else:
        when_say = when_disp = f"{cap.month}月{cap.day}日"
    time_say = f"{cap.hour}時{cap.minute}分" if cap.minute else f"{cap.hour}時"
    time_disp = f"{cap.hour}:{cap.minute:02d}"
    readings = {**WORD_READINGS, time_say: kana_hour(cap.hour) + (kana_minute(cap.minute) if cap.minute else "")}
    if point in POINT_READINGS:
        readings[point] = POINT_READINGS[point]
    if score is not None:
        readings[f"{score}点"] = kana_points(score)
    if when_disp == "昨日":
        readings["昨日"] = "きのう"

    sep = "、" if when_say.endswith("日") and not when_say.startswith("今") and when_say != "昨日" else ""
    open_say = f"{when_say}{sep}{time_say}の{point}。" + (f"予報は{score}点でした。" if score is not None else "")
    report_say = f"現地からの波レポートは、{size}、{quality}。" + (f"混み具合は{crowd}。" if crowd else "") + (
        f"地形は{terrain}。" if terrain else "")

    scenes = [
        {"kind": "open", "video": True, "say": open_say, "h": f"{when_disp}{time_disp}の<b>{html.escape(point)}</b>"},
        {"kind": "report", "video": True, "say": report_say, "h": "現地からの<b>波レポート</b>"},
    ]
    if score is not None:
        scenes.append({"kind": "compare", "video": True, "say": "予報と、いまの海。答え合わせができるのが波レポートです。",
                       "h": "予報といまの海を<br><b>答え合わせ</b>"})
    scenes += [
        {"kind": "crowd", "video": True, "say": "みんなの声が集まるほど、予報の精度は上がっていきます。",
         "h": "みんなの声で<br><b>予報の精度アップ</b>"},
        {"kind": "cta", "say": "あなたの海も、波レポートで教えてください。", "h": "あなたの海も、<br><b>波レポート</b>で<br>教えてください。"},
        {"kind": "next", "say": "保存して、次の海で投稿してね！", "h": "保存して、<br><b>次の海で投稿</b>しよう",
         "follow": "フォローして波情報をチェック", "followSub": "Wave Hack 公式"},
    ]
    place = place_label(r)
    return {
        "badge": "現地",
        "series": "波レポート",
        "title": f"{point}（{place}）" if place else point,
        "source": credit["line"],
        "post": {
            "point": point,
            "place": place,
            "captured": f"{cap.month}/{cap.day} {time_disp}",
            "size": size,
            "quality": quality,
            "crowd": crowd,
            "terrain": terrain,
            "forecast": score,
            "forecast_hl": score is not None and score >= HL_MIN,
            "comment": comment_html(r.get("comment")),
            "kind": r["kind"],
        },
        "readings": readings,
        "scenes": scenes,
    }


def _tag(s: str | None) -> str | None:
    t = re.sub(r"[\s・･/／()（）#＃、。,.!?！？「」『』-]", "", s or "")
    return f"#{t}" if t else None


def build_caption(data: dict) -> str:
    r, credit = data["report"], data["credit"]
    point = r.get("point_name") or r.get("point_id")
    cap = _captured(data)
    place = place_label(r)
    score = r.get("forecast_score")
    score = int(round(score)) if isinstance(score, (int, float)) else None
    facts = [f"サイズ {r['size_label']}", r["quality_label"]]
    if r.get("crowd_label"):
        facts.append(r["crowd_label"])
    if r.get("terrain_label"):
        facts.append(f"地形 {r['terrain_label']}")
    comment = short_comment(r.get("comment"))
    pref_tag = _tag(f"{place}サーフィン") if place else None
    tags = ["#波レポート", "#波情報", _tag(point), pref_tag, _tag(r.get("area")) if r.get("area") not in (None, "", place, point) else None,
            "#サーフィン", "#surfjapan", "#wavehack"]
    seen, uniq = set(), []
    for t in tags:
        if t and t not in seen:
            seen.add(t)
            uniq.append(t)
    lines = [
        f"📍{point}{f'（{place}）' if place else ''}｜{cap.month}/{cap.day} {cap.hour}:{cap.minute:02d} の波レポート",
        "",
        "・".join(facts),
        f"「{comment}」" if comment else None,
        f"予報 {score}点 → 現地は「{r['quality_label']}」" if score is not None else None,
        credit["line"],
        "",
        "あなたの海の「いま」も、波レポートで教えてください。",
        "アプリはプロフィールのリンク（wavehack.surf）から。App Store で「Wave Hack」と検索。",
        "※ 投稿者が公式 SNS での紹介に同意した波レポートです。",
        "",
        " ".join(uniq),
    ]
    return "\n".join(l for l in lines if l is not None)
