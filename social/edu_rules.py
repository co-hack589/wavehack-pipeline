"""教育リール「波予報の読み方」第2弾の台本の決まりと機械検査。

使える場面（kind）とキー・値の形は、既存の edu/episodes/ep01〜10.json から機械的に集める（allowed_kinds 方式）。
テンプレート（edu/template.html）に無い図は作れないため、ここに無い kind・キー・形は不合格にする。

台本（script）は ep*.json から no を除いた形: {series, title, next, source, caption, scenes[]}。no は投稿直前に API が採番する。
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

EPISODES = Path(__file__).resolve().parent.parent / "edu" / "episodes"
SERIES = "波予報の読み方"

# 読み上げの長さ。第1弾の実測（動画の長さ）と say の合計字数から、1秒あたりの字数を出す:
#   #02 255字 / 55.9秒 = 4.56字/秒、#10 139字 / 31.0秒 = 4.48字/秒（#06 は 234字・音声 44.6秒 → 動画 約50.6秒 = 4.62字/秒）
# 遅いほうの 4.48字/秒 で 40秒 = 179字。場面数による間（1場面 0.9秒）の差を見込んで 175字を上限にする（175 / 4.48 = 39.1秒）。
MEASURED = {2: 55.9, 10: 31.0}
MAX_SAY_CHARS = 175
MIN_SAY_CHARS = 100  # これより短いと中身が薄い（第1弾の最短は #10 の 139字）
# 締めは保存・フォローの呼びかけ（第1弾 #01〜09「フォローして、続きもチェックしてね！」・#10「保存して、次の海選びに使ってね！」に合わせる）。
# 次の回は承認順で決まり台本の時点では分からないので「次回は〇〇」は言わない。リンク・プロフィールへの誘導はしない。
END_SAY = "保存して、次の海選びに使ってね。フォローして、続きもチェックしてね！"

ALLOWED_TAGS = {"b", "br"}  # 第1弾で使っているタグ（テンプレートは innerHTML でそのまま差し込む）
TOP_KEYS = {"series", "title", "next", "source", "caption", "scenes"}
# 第2弾の締め（next）は回の一覧（list）を出さない。テンプレートは list が無いとき保存・フォローの画面にする
SEASON2_KEYS = {"next": {"kind", "say"}}
# 図の見た目が壊れない範囲（テンプレートの描き方から）
RANGES = {("arrows", "deg"): (0, 180), ("curve", "amp"): (0.1, 1), ("curve", "marks.0"): (0, 24),
          ("scale", "rows.3"): (0, 3), ("waves", "T"): (4, 20), ("isobar", "gap"): (20, 50)}
OFFICIAL_ORGS = ("気象庁", "海上保安庁", "国土交通省", "NOAA", "NWS", "NCEP", "WMO", "世界気象機関", "海洋研究開発機構", "JAMSTEC",
                 "国立天文台", "環境省", "水産庁", "防災科学技術研究所", "港湾空港技術研究所")
OFFICIAL_DOMAINS = (".go.jp", ".gov", "wmo.int", "jamstec.go.jp", "nao.ac.jp", "ecmwf.int", "unesco.org", "ioc.unesco.org")
BANNED = ("リンク", "プロフィール", "URL", "http", "DM", "Wave Hack", "WAVE HACK", "wavehack", "ウェーブハック")


def load_episodes() -> list[dict]:
    return [json.loads(f.read_text(encoding="utf-8")) for f in sorted(EPISODES.glob("ep*.json"))]


def visible(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)


def say_chars(script: dict) -> int:
    return sum(len(s.get("say", "")) for s in script.get("scenes", []))


def est_seconds(chars: int) -> float:
    """第1弾の遅いほうの読み上げ速さで、動画の長さを見積もる。"""
    rate = min(_chars(no) / sec for no, sec in MEASURED.items())
    return chars / rate


def _chars(no: int) -> int:
    return next(say_chars(e) for e in load_episodes() if e["no"] == no)


# ---------------------------------------------------------------- 値の形（既存回から集める）
def _merge(spec: dict, v) -> None:
    t = _type(v)
    spec.setdefault("types", set()).add(t)
    if t == "str":
        spec["str_max"] = max(spec.get("str_max", 0), len(visible(v)))
    elif t == "list":
        spec.setdefault("lens", set()).add(len(v))
        item = spec.setdefault("item", {})
        pos = spec.setdefault("pos", [])
        for i, x in enumerate(v):
            _merge(item, x)
            while len(pos) <= i:
                pos.append({})
            _merge(pos[i], x)
    elif t == "dict":
        keys = spec.setdefault("keys", {})
        for k, x in v.items():
            _merge(keys.setdefault(k, {}), x)


def _type(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "num"
    return {str: "str", list: "list", dict: "dict"}.get(type(v), type(v).__name__)


def allowed_kinds(episodes: list[dict] | None = None) -> dict[str, dict[str, dict]]:
    """kind → {キー → 値の形}。既存回で使われた kind・キーだけを集める（say と kind も含む）。"""
    kinds: dict[str, dict[str, dict]] = {}
    for ep in episodes or load_episodes():
        for s in ep["scenes"]:
            keys = kinds.setdefault(s["kind"], {})
            for k, v in s.items():
                _merge(keys.setdefault(k, {}), v)
    for kind, keep in SEASON2_KEYS.items():
        if kind in kinds:
            kinds[kind] = {k: v for k, v in kinds[kind].items() if k in keep}
    return kinds


def _check(spec: dict, v, where: str, kind: str, path: str, errs: list[str]) -> None:
    t = _type(v)
    types = spec.get("types", set())
    if t not in types:
        errs.append(f"[形] {where}: {t} は使えない（既存回は {sorted(types)}）")
        return
    rng = RANGES.get((kind, path))
    if t == "num" and rng and not rng[0] <= v <= rng[1]:
        errs.append(f"[形] {where}: {v} は範囲外（{rng[0]}〜{rng[1]}）")
    if t == "str":
        lim = max(spec["str_max"] + 3, math.ceil(spec["str_max"] * 1.3))  # 画面からはみ出さない長さ（既存回の最長の約1.3倍）
        if len(visible(v)) > lim:
            errs.append(f"[形] {where}: 画面の文字が長すぎる（{len(visible(v))}字、{lim}字以内）")
    elif t == "list":
        lens = spec.get("lens", {0})
        item_types = spec.get("item", {}).get("types", set())
        if len(item_types - {"list", "dict"}) > 1:  # [数, 文字, …] の組（bars・rows・marks）は位置ごとに確かめる
            if len(v) not in lens:
                errs.append(f"[形] {where}: 要素の数 {len(v)}（既存回は {sorted(lens)}）")
                return
            for i, x in enumerate(v):
                _check(spec["pos"][i], x, f"{where}[{i}]", kind, f"{path}.{i}", errs)
        else:
            if not v or len(v) > max(lens):
                errs.append(f"[形] {where}: 要素の数 {len(v)}（1〜{max(lens)} にする）")
            for i, x in enumerate(v):
                _check(spec["item"], x, f"{where}[{i}]", kind, path, errs)
    elif t == "dict":
        keys = spec.get("keys", {})
        extra = set(v) - set(keys)
        if extra:
            errs.append(f"[キー] {where}: 使えないキー {sorted(extra)}（使えるのは {sorted(keys)}）")
        for k, x in v.items():
            if k in keys:
                _check(keys[k], x, f"{where}.{k}", kind, k, errs)


# ---------------------------------------------------------------- 検査
def validate(script: dict, kinds: dict | None = None) -> list[str]:
    """機械的に確かめられることを検査する。問題の一覧（"[種類] 内容"）を返す。空なら合格。"""
    kinds = kinds or allowed_kinds()
    errs: list[str] = []
    extra = set(script) - TOP_KEYS - {"no"}
    if extra:
        errs.append(f"[キー] 台本に使えないキー {sorted(extra)}")
    for k in ("series", "title", "source", "caption", "scenes"):
        if not script.get(k):
            errs.append(f"[必須] {k} がありません")
    if script.get("series") and script["series"] != SERIES:
        errs.append(f"[必須] series は「{SERIES}」にする")
    if script.get("next") is not None:
        errs.append("[締め] next は null にする（次の回は承認順で決まるため台本では書かない）")
    title = visible(script.get("title") or "")
    if len(title) > 12:
        errs.append(f"[形] title が長い（{len(title)}字、12字以内。画面上部のバッジに出る）")
    sc = script.get("scenes") or []
    if sc and not 4 <= len(sc) <= 6:
        errs.append(f"[構成] 場面の数が {len(sc)}（4〜6 にする）")
    if sc:
        first, last = sc[0], sc[-1]
        if first.get("kind") != "hook":
            errs.append("[構成] 1場面目は hook（0秒目から問いかけを出す）")
        else:
            if "？" not in (first.get("q") or "") and "?" not in (first.get("q") or ""):
                errs.append("[構成] hook の q は問いかけ（？で終わる）にする")
            if not first.get("big"):
                errs.append("[構成] hook に big（背景の大きな英数字）を入れる（無いと既定の「1.0m」が出る）")
        if last.get("kind") != "next":
            errs.append("[締め] 最後の場面は next（保存・フォローの呼びかけ）")
        elif last.get("say") != END_SAY:
            errs.append(f"[締め] 最後の say は「{END_SAY}」にする")
        if not any(s.get("kind") == "sum" for s in sc):
            errs.append("[構成] sum（まとめ）の場面を入れる（ストーリーズ画像にも使う）")
    for i, s in enumerate(sc, 1):
        kind = s.get("kind")
        if kind not in kinds:
            errs.append(f"[kind] 場面{i}: 「{kind}」はテンプレートにありません（使えるのは {sorted(kinds)}）")
            continue
        extra = set(s) - set(kinds[kind])
        if extra:
            errs.append(f"[キー] 場面{i}: {kind} に無いキー {sorted(extra)}（使えるのは {sorted(kinds[kind])}）")
        if not s.get("say"):
            errs.append(f"[必須] 場面{i}: say がありません")
        for k, v in s.items():
            if k in kinds[kind] and k not in ("kind", "say"):
                _check(kinds[kind][k], v, f"場面{i}.{k}", kind, k, errs)
    blob = json.dumps(script, ensure_ascii=False)
    for tag in sorted(set(t.lower() for t in re.findall(r"</?([a-zA-Z]+)", blob)) - ALLOWED_TAGS):
        errs.append(f"[形] 使えないタグ <{tag}>（使えるのは <b> <br>）")
    if sc:
        n = say_chars(script)
        if n > MAX_SAY_CHARS:
            errs.append(f"[字数] 読み上げが {n}字（締めを含めて {MAX_SAY_CHARS}字以内 ≒ 40秒。見積もり {est_seconds(n):.1f}秒）")
        if n < MIN_SAY_CHARS:
            errs.append(f"[字数] 読み上げが {n}字で短すぎる（{MIN_SAY_CHARS}字以上）")
    src = script.get("source") or ""
    if src and not re.match(r"^(出典|参考)：", src):
        errs.append("[出典] source は「出典：」か「参考：」で始める")
    if src and not any(o in src for o in OFFICIAL_ORGS):
        errs.append(f"[出典] source に公的な出典（{'・'.join(OFFICIAL_ORGS[:4])} など）を書く")
    if len(script.get("caption") or "") > 400:
        errs.append(f"[字数] caption が {len(script['caption'])}字（400字以内）")
    for w in BANNED:
        if w in blob:
            errs.append(f"[宣伝] 「{w}」は書かない（リンク・プロフィールへの誘導や宣伝はしない）")
    return errs


def validate_sources(sources: list) -> list[str]:
    """ネタの出典（sources: [{title, url}]）を検査する。公的機関のページが1件以上必要。"""
    errs = []
    if not isinstance(sources, list) or not sources:
        return ["[出典] sources がありません（公的な出典を1件以上）"]
    official = 0
    for i, s in enumerate(sources, 1):
        if not isinstance(s, dict) or not s.get("title") or not str(s.get("url", "")).startswith("https://"):
            errs.append(f"[出典] sources[{i}] は {{title, url(https://…)}} にする")
            continue
        host = re.sub(r"^https://([^/]+).*$", r"\1", s["url"]).lower()
        if "vertexaisearch" in host or "google." in host:
            errs.append(f"[出典] sources[{i}] は検索の中継 URL ではなく元のページの URL にする")
        elif any(host.endswith(d) or host == d.lstrip(".") for d in OFFICIAL_DOMAINS):
            official += 1
    if not official:
        errs.append("[出典] 公的機関（気象庁・NOAA など）のページが sources にありません")
    return errs


def normalize(script: dict) -> dict:
    """決まった値をそろえる（series・next・締めの say）。LLM の出力に毎回かける。"""
    s = {k: v for k, v in script.items() if k in TOP_KEYS}
    s["series"], s["next"] = SERIES, None
    sc = s.get("scenes") or []
    if sc and isinstance(sc[-1], dict) and sc[-1].get("kind") == "next":
        sc[-1] = {"kind": "next", "say": END_SAY}
    return s
