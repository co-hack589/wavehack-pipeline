"""エリア全体（宮崎9ポイント集約）の Today's Topic コメント生成。

dashboard.json の各ポイントの当日スロットを時系列で集約し、
ルールベースで 1 行コメントを生成する。

設計方針:
- 風速は単純平均、風向は sin/cos の円形平均
- 風向の「オフショア / オンショア」判定は各ポイントの wind_label を多数決
  （ビーチ向きはポイントごとに違うため、生の風向だけでは判定できない）
- 強度: 弱 <3m/s / 中 3-7m/s / 強 >=7m/s
- パターン:
  all_strong_onshore     : 終日強オンショア
  shift_to_strong_onshore: 弱/中 → 強オンショア
  all_offshore_calm      : 終日穏やかなオフショア
  shift_to_offshore      : 強オン or クローズ → オフショア
  default                : 上記以外、best_time + 風向ラベルで生成
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Iterable


# ============================================================
# 風向 8方位ラベル
# ============================================================
_DIR_8 = [
    (22.5, "北"),
    (67.5, "北東"),
    (112.5, "東"),
    (157.5, "南東"),
    (202.5, "南"),
    (247.5, "南西"),
    (292.5, "西"),
    (337.5, "北西"),
]


def dir_label_8(deg: float | None) -> str:
    """0..360 の角度を 8方位の日本語ラベルに変換。"""
    if deg is None:
        return ""
    a = deg % 360.0
    for threshold, label in _DIR_8:
        if a < threshold:
            return label
    return "北"


# ============================================================
# 集約ヘルパー
# ============================================================
def _circular_mean_deg(degs: Iterable[float]) -> float | None:
    xs = [math.cos(math.radians(d)) for d in degs]
    ys = [math.sin(math.radians(d)) for d in degs]
    n = len(xs)
    if n == 0:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    if abs(mx) < 1e-9 and abs(my) < 1e-9:
        return None
    return (math.degrees(math.atan2(my, mx))) % 360.0


def _hhmm(timestamp_jst: str) -> str:
    try:
        return datetime.fromisoformat(timestamp_jst).strftime("%H:%M")
    except (ValueError, TypeError):
        return ""


def _hour_int(timestamp_jst: str) -> int | None:
    try:
        return datetime.fromisoformat(timestamp_jst).hour
    except (ValueError, TypeError):
        return None


# wind_label カテゴリ分け
_OFFSHORE_LABELS = {"オフショア", "サイドオフ"}
_ONSHORE_LABELS = {"オンショア", "サイドオン"}


def _wind_orientation(label: str) -> str:
    """wind_label を offshore/onshore/side の3カテゴリにまとめる。"""
    if label in _OFFSHORE_LABELS:
        return "offshore"
    if label in _ONSHORE_LABELS:
        return "onshore"
    return "side"


def _strength_category(wind_ms: float) -> str:
    if wind_ms < 3.0:
        return "weak"
    if wind_ms < 7.0:
        return "mid"
    return "strong"


def _strength_label_ja(cat: str) -> str:
    return {"weak": "弱", "mid": "中", "strong": "強"}.get(cat, "")


# ============================================================
# 当日スロット抽出と時間帯集約
# ============================================================
def _collect_today_slots(points: list[dict[str, Any]], target_date: str) -> list[dict[str, Any]]:
    """target_date(YYYY-MM-DD JST) かつ昼間 かつ is_extended=False のスロットを全 point 横断で集める。"""
    out: list[dict[str, Any]] = []
    for p in points:
        for s in p.get("slots", []) or []:
            ts = s.get("timestamp_jst", "")
            if ts[:10] != target_date:
                continue
            if not s.get("is_daylight"):
                continue
            if s.get("is_extended"):
                continue
            out.append(s)
    return out


def _aggregate_by_hour(slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同じ時刻(HH)の全 point スロットを 1 件にまとめる。"""
    buckets: dict[int, list[dict[str, Any]]] = {}
    for s in slots:
        h = _hour_int(s.get("timestamp_jst", ""))
        if h is None:
            continue
        buckets.setdefault(h, []).append(s)

    aggregated: list[dict[str, Any]] = []
    for h in sorted(buckets.keys()):
        items = buckets[h]
        winds = [it["wind_ms"] for it in items if it.get("wind_ms") is not None]
        wdirs = [it["wdir_deg"] for it in items if it.get("wdir_deg") is not None]
        comps = [it["composite"] for it in items if it.get("composite") is not None]
        labels = [it.get("wind_label") or "" for it in items]
        orientations = [_wind_orientation(lbl) for lbl in labels]
        # 多数決
        tally: dict[str, int] = {}
        for o in orientations:
            tally[o] = tally.get(o, 0) + 1
        majority = max(tally.items(), key=lambda kv: kv[1])[0] if tally else "side"

        avg_wind = sum(winds) / len(winds) if winds else 0.0
        mean_dir = _circular_mean_deg(wdirs)
        avg_comp = sum(comps) / len(comps) if comps else 0.0

        # 代表 timestamp_jst（最初の point の値で OK、HH:MM 抽出用）
        ts_repr = items[0].get("timestamp_jst", "")

        aggregated.append({
            "hour": h,
            "timestamp_jst": ts_repr,
            "wind_ms": avg_wind,
            "wdir_deg": mean_dir,
            "composite": avg_comp,
            "orientation": majority,
            "strength": _strength_category(avg_wind),
            "wind_dir_label_8": dir_label_8(mean_dir),
        })
    return aggregated


# ============================================================
# パターン判定
# ============================================================
def _is_closed(h: dict[str, Any]) -> bool:
    return h["strength"] == "strong" and h["orientation"] == "onshore"


def _is_offshore_calm(h: dict[str, Any]) -> bool:
    return h["orientation"] == "offshore" and h["strength"] in {"weak", "mid"}


def _is_strong_onshore(h: dict[str, Any]) -> bool:
    return h["strength"] == "strong" and h["orientation"] == "onshore"


def _classify_pattern(hours: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """時刻別集約から当日パターンを判定する。

    Returns: (pattern_id, context_dict)
      context_dict は text 生成に使う追加情報（change_hour, best など）
    """
    n = len(hours)
    if n == 0:
        return ("no_data", {})

    # ベスト時刻（composite ピーク）
    best = max(hours, key=lambda h: h["composite"])
    ctx: dict[str, Any] = {"best": best}

    # all_strong_onshore: 7割以上が strong_onshore
    strong_on_count = sum(1 for h in hours if _is_strong_onshore(h))
    if strong_on_count >= max(1, int(n * 0.7)):
        return ("all_strong_onshore", ctx)

    # all_offshore_calm: 7割以上が offshore_calm
    offshore_calm_count = sum(1 for h in hours if _is_offshore_calm(h))
    if offshore_calm_count >= max(1, int(n * 0.7)):
        ctx["main_wind_dir"] = best["wind_dir_label_8"]
        return ("all_offshore_calm", ctx)

    # shift_to_strong_onshore: 前半に strong_onshore が無く、後半に出現
    half = n // 2 if n >= 2 else 1
    head, tail = hours[:half], hours[half:]
    head_has_strong_on = any(_is_strong_onshore(h) for h in head)
    tail_strong_on = [h for h in tail if _is_strong_onshore(h)]
    if not head_has_strong_on and tail_strong_on:
        change = tail_strong_on[0]
        ctx["change_hour"] = change
        ctx["change_dir"] = change["wind_dir_label_8"]
        return ("shift_to_strong_onshore", ctx)

    # shift_to_offshore: 前半に strong/onshore が多く、後半に offshore_calm
    head_bad = sum(1 for h in head if _is_strong_onshore(h) or h["orientation"] == "onshore")
    tail_offshore = [h for h in tail if _is_offshore_calm(h)]
    if head_bad >= max(1, len(head) // 2) and tail_offshore:
        change = tail_offshore[0]
        ctx["change_hour"] = change
        ctx["change_dir"] = change["wind_dir_label_8"]
        return ("shift_to_offshore", ctx)

    return ("default", ctx)


# ============================================================
# テンプレ展開
# ============================================================
def _format_text(pattern: str, ctx: dict[str, Any]) -> str:
    best = ctx.get("best") or {}

    if pattern == "no_data":
        return "本日のデータが揃っていません。最新の予報を待ってください。"

    if pattern == "all_strong_onshore":
        return "朝から風が強くクローズ気味。今日は微妙です。"

    if pattern == "all_offshore_calm":
        wd = ctx.get("main_wind_dir") or "西"
        return f"終日穏やかな{wd}オフショア。コンディション安定。"

    if pattern == "shift_to_strong_onshore":
        ch = ctx.get("change_hour") or {}
        t = _hhmm(ch.get("timestamp_jst", ""))
        wd = ctx.get("change_dir") or "東"
        return f"{t}ぐらいから強めの{wd}オンショアに変わるので早めに行きましょう。"

    if pattern == "shift_to_offshore":
        ch = ctx.get("change_hour") or {}
        t = _hhmm(ch.get("timestamp_jst", ""))
        wd = ctx.get("change_dir") or "西"
        return f"{t}から{wd}オフショアに変わり面が整う。午後が本命。"

    # default
    bt = _hhmm(best.get("timestamp_jst", ""))
    wd = best.get("wind_dir_label_8", "")
    orient = best.get("orientation", "side")
    orient_label = {"offshore": "オフショア", "onshore": "オンショア", "side": "サイド"}[orient]
    strength = _strength_label_ja(best.get("strength", "mid"))
    return f"{bt}前後がベスト。{wd}{orient_label}{strength}め。"


# ============================================================
# 公開関数
# ============================================================
def build_area_topic(
    points: list[dict[str, Any]],
    target_date: str | None = None,
) -> dict[str, Any]:
    """エリア全体の Today's Topic を生成する。

    Args:
      points: dashboard.json の "points" リスト
      target_date: "YYYY-MM-DD" (JST)。None なら全 point から取れる最新日付を採用

    Returns:
      {
        "text": str,
        "signature": str,
        "date": str (YYYY-MM-DD),
        "pattern": str,
        "best_time": str (HH:MM),
        "fallback": bool,  # 当日データが無く別日付を採用した場合 True
      }
    """
    # target_date 解決
    today = datetime.now().strftime("%Y-%m-%d")
    requested = target_date or today

    slots = _collect_today_slots(points, requested)
    fallback = False
    resolved_date = requested
    if not slots:
        # 全 point から取れる日付集合を作り、requested と異なる場合は最新日付に倒す
        dates: set[str] = set()
        for p in points:
            for s in p.get("slots", []) or []:
                if s.get("is_daylight") and not s.get("is_extended"):
                    d = (s.get("timestamp_jst") or "")[:10]
                    if d:
                        dates.add(d)
        if dates:
            # 「requested 以降の最初の日付」が無ければ「最大日付」
            future = sorted([d for d in dates if d >= requested])
            resolved_date = future[0] if future else max(dates)
            slots = _collect_today_slots(points, resolved_date)
            fallback = resolved_date != requested

    hours = _aggregate_by_hour(slots)
    pattern, ctx = _classify_pattern(hours)
    text = _format_text(pattern, ctx)

    best_time = ""
    if ctx.get("best"):
        best_time = _hhmm(ctx["best"].get("timestamp_jst", ""))

    return {
        "text": text,
        "signature": f"— wave hack editorial / 宮崎エリア {resolved_date}",
        "date": resolved_date,
        "pattern": pattern,
        "best_time": best_time,
        "fallback": fallback,
    }
