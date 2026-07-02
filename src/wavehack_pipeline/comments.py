"""ルールベース言語化コメント生成 (Phase B-1)。

scoring_rules.yaml の comments セクションのテンプレを使い、dashboard.json の
1スロット dict から人間向けの1行コメントを生成する。

設計方針:
- ランダム要素は (point_name + timestamp) を seed としたハッシュで決定論的に選ぶ
  → 同じスロットを再生成しても同じコメントになる
- プレースホルダ未提供時は無難な省略形（空文字 or デフォルト値）に置換
- 既存の ScoringRules クラスのアクセサ（label_for_direction_deg）を活用
- 標準ライブラリのみ使用（hashlib / random / datetime）
"""

from __future__ import annotations

import hashlib
import random
from datetime import datetime
from typing import Any

from wavehack_pipeline.scoring import ScoringRules, angle_diff


# ============================================================
# 風向きラベル（オフショア / サイドオフ / サイド / サイドオン / オンショア）
# ============================================================
# 風が「どこから吹いてくるか」とビーチ向きの相対関係:
#   - ビーチ向き（波が来る方向）の真反対 = オフショア（沖に吹く）が満点
#   - ビーチ向き = オンショア
#
# 角度差で5段階に分類:
#   diff(風向, オフショア方向) で
#     0..22.5    : オフショア
#    22.5..67.5  : サイドオフ
#    67.5..112.5 : サイド
#   112.5..157.5 : サイドオン
#   157.5..180   : オンショア
WIND_RELATIVE_LABELS = [
    (22.5, "オフショア"),
    (67.5, "サイドオフ"),
    (112.5, "サイド"),
    (157.5, "サイドオン"),
    (180.0, "オンショア"),
]


def wind_relative_label(wind_dir_deg: float | None, beach_dir_deg: float | None) -> str:
    """風向きとビーチ向きから 5段階の相対ラベルを返す。

    どちらか不明なら空文字。
    """
    if wind_dir_deg is None or beach_dir_deg is None:
        return ""
    offshore = (beach_dir_deg + 180.0) % 360.0
    diff = angle_diff(wind_dir_deg, offshore)
    for threshold, label in WIND_RELATIVE_LABELS:
        if diff <= threshold:
            return label
    return "オンショア"


# ============================================================
# 表示用フォーマッタ
# ============================================================
def _fmt_height(h: float | None) -> str:
    if h is None:
        return "-"
    return f"{h:.1f}"


def _fmt_period(p: float | None) -> str:
    if p is None:
        return "-"
    return f"{p:.1f}"


def _fmt_wind(w: float | None) -> str:
    if w is None:
        return "-"
    return f"{w:.1f}"


def _seeded_choice(seq: list[str], seed_str: str) -> str:
    """seed_str をハッシュして決定論的に1つ選ぶ。"""
    if not seq:
        return ""
    digest = hashlib.sha1(seed_str.encode("utf-8")).hexdigest()
    rng = random.Random(int(digest[:16], 16))
    return rng.choice(seq)


def _hhmm_jst(timestamp_jst: str) -> str:
    """ISO8601 文字列から HH:MM を返す。"""
    try:
        dt = datetime.fromisoformat(timestamp_jst)
        return dt.strftime("%H:%M")
    except (ValueError, TypeError):
        return ""


def _date_jst(timestamp_jst: str) -> str:
    """ISO8601 文字列から YYYY-MM-DD を返す。"""
    try:
        dt = datetime.fromisoformat(timestamp_jst)
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return ""


# ============================================================
# 1スロットコメント
# ============================================================
def format_slot_comment(
    slot: dict[str, Any],
    point_name: str,
    rules: ScoringRules,
) -> str:
    """1スロットの dashboard.json entry を受け取り、ランクに応じたテンプレを埋めて返す。"""
    rank = slot.get("rank", "fair")
    templates = rules.comments.get(rank, [])
    if not templates:
        return ""

    seed = f"{point_name}|{slot.get('timestamp_jst', '')}"
    template = _seeded_choice(templates, seed)

    # wind_label は slot にあれば使い、なければ計算
    wind_label = slot.get("wind_label")
    if not wind_label:
        beach_dir_deg = rules.beach_dir_for(point_name)
        wind_label = wind_relative_label(slot.get("wdir_deg"), beach_dir_deg)

    swell_dir_label = slot.get("swell_dir_label") or ""
    if not swell_dir_label and slot.get("swdir1_deg") is not None:
        swell_dir_label = rules.label_for_direction_deg(slot["swdir1_deg"])["ja"]

    fields: dict[str, str] = {
        "point_name": point_name,
        "swell_dir_label": swell_dir_label,
        "wind_label": wind_label,
        "wave_height_m": _fmt_height(slot.get("htsgw_m")),
        "period_s": _fmt_period(slot.get("perpw_s")),
        "wind_speed_ms": _fmt_wind(slot.get("wind_ms")),
    }

    try:
        return template.format(**fields)
    except KeyError:
        # テンプレに未対応のキーがあっても、シンプル版で復帰
        safe_fields = {k: fields.get(k, "") for k in fields}
        return template.format_map(_SafeDict(safe_fields))


class _SafeDict(dict):
    """str.format_map 用: 未定義キーを空文字に置換する辞書。"""

    def __missing__(self, key: str) -> str:
        return ""


# ============================================================
# 1日サマリ
# ============================================================
def format_daily_summary(
    point_name: str,
    daily_slots: list[dict[str, Any]],
    rules: ScoringRules,
) -> dict[str, Any] | None:
    """同日の slot list からベストスロットを選んで daily_summary テンプレで1行返す。

    Returns: {date, text, best_time, best_composite, is_extended} or None
    候補が空なら None。
    """
    if not daily_slots:
        return None

    # 参考値レンジ（is_extended=True）は候補から除外
    candidates = [s for s in daily_slots if not s.get("is_extended")]
    if not candidates:
        # その日が全部 extended のみの場合: ベストは無いが date は返す
        date_str = _date_jst(daily_slots[0].get("timestamp_jst", ""))
        return {
            "date": date_str,
            "text": "",
            "best_time": "",
            "best_composite": None,
            "is_extended": True,
        }

    best = max(candidates, key=lambda s: s.get("composite", 0.0))
    date_str = _date_jst(best.get("timestamp_jst", ""))
    best_time = _hhmm_jst(best.get("timestamp_jst", ""))

    # daily_summary テンプレ
    templates = rules.comments.get("daily_summary", [])
    if not templates:
        return {
            "date": date_str,
            "text": "",
            "best_time": best_time,
            "best_composite": best.get("composite"),
            "is_extended": False,
        }

    seed = f"{point_name}|daily|{date_str}"
    template = _seeded_choice(templates, seed)

    wind_label = best.get("wind_label")
    if not wind_label:
        beach_dir_deg = rules.beach_dir_for(point_name)
        wind_label = wind_relative_label(best.get("wdir_deg"), beach_dir_deg)

    best_swell_dir_label = best.get("swell_dir_label") or ""
    if not best_swell_dir_label and best.get("swdir1_deg") is not None:
        best_swell_dir_label = rules.label_for_direction_deg(best["swdir1_deg"])["ja"]

    fields: dict[str, str] = {
        "point_name": point_name,
        "date": date_str,
        "best_time": best_time,
        "best_swell_dir_label": best_swell_dir_label,
        "best_wave_height_m": _fmt_height(best.get("htsgw_m")),
        "best_wind_ms": _fmt_wind(best.get("wind_ms")),
        # daily_summary テンプレが slot コメントと共通プレースホルダを使う場合の保険
        "swell_dir_label": best_swell_dir_label,
        "wave_height_m": _fmt_height(best.get("htsgw_m")),
        "period_s": _fmt_period(best.get("perpw_s")),
        "wind_label": wind_label,
        "wind_speed_ms": _fmt_wind(best.get("wind_ms")),
    }

    text = template.format_map(_SafeDict(fields))
    return {
        "date": date_str,
        "text": text,
        "best_time": best_time,
        "best_composite": best.get("composite"),
        "is_extended": False,
    }


def group_slots_by_date_jst(slots: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """timestamp_jst の YYYY-MM-DD 部分でグルーピング。"""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for s in slots:
        ts = s.get("timestamp_jst", "")
        date_str = ts[:10] if len(ts) >= 10 else ""
        if not date_str:
            continue
        grouped.setdefault(date_str, []).append(s)
    return grouped


def build_daily_summaries(
    point_name: str,
    slots: list[dict[str, Any]],
    rules: ScoringRules,
) -> list[dict[str, Any]]:
    """全スロットを日付ごとにまとめて daily_summary を作る。"""
    grouped = group_slots_by_date_jst(slots)
    out: list[dict[str, Any]] = []
    for date_str in sorted(grouped.keys()):
        summary = format_daily_summary(point_name, grouped[date_str], rules)
        if summary is None:
            continue
        out.append(summary)
    return out
