"""朝のレコメンド/LINE通知の素地となる日次サマリ生成 (Phase B-2)。

dashboard.json から指定日の以下を CLI で出力する:
  - 9ポイント横断のベストスロット TOP N ランキング
  - 各ポイントの daily_summary（その日のひとこと）

出力形式: text / json / markdown
想定用途: LINE Bot, Slack 投稿, メモ貼り付け

実行例:
  uv run python scripts/generate_daily_summary.py
  uv run python scripts/generate_daily_summary.py --date 2026-05-12 --top 5
  uv run python scripts/generate_daily_summary.py --days 3 --format markdown
  uv run python scripts/generate_daily_summary.py --format json | jq .
  uv run python scripts/generate_daily_summary.py --time-range 04:00-09:00
  uv run python scripts/generate_daily_summary.py --morning --top 5
  uv run python scripts/generate_daily_summary.py --evening --format markdown
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from wavehack_pipeline.daylight import slot_is_daylight  # noqa: E402

WAVEHACK_OUTPUT = ROOT.parent.parent  # /Users/takeda.toi/wavehack/output
DASHBOARD_JSON = WAVEHACK_OUTPUT / "data" / "dashboard.json"

# rank -> 星表記（5段階）。スコア値の参考目安。
RANK_STARS: dict[str, str] = {
    "great": "★★★★★",
    "good": "★★★★",
    "fair": "★★★",
    "poor": "★★",
}

# 時間帯プリセット（朝マズメ・夕マズメ）
TIME_RANGE_PRESETS: dict[str, tuple[str, str]] = {
    "morning": ("04:00", "09:00"),
    "evening": ("15:00", "19:00"),
}


# ============================================================
# 入力・抽出ユーティリティ
# ============================================================
def load_dashboard(path: Path) -> dict[str, Any]:
    if not path.exists():
        print(f"ERROR: {path} not found. Run generate_dashboard.py first.", file=sys.stderr)
        sys.exit(1)
    return json.loads(path.read_text(encoding="utf-8"))


def parse_date_arg(s: str) -> str:
    """'YYYY-MM-DD' を検証して返す。"""
    try:
        return datetime.strptime(s, "%Y-%m-%d").date().isoformat()
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"invalid date: {s} ({e})") from None


def _parse_hhmm(s: str) -> tuple[int, str]:
    """'HH:MM' を分単位の整数と正規化文字列で返す。不正なら ValueError。"""
    if not isinstance(s, str) or len(s) != 5 or s[2] != ":":
        raise ValueError(f"invalid HH:MM: {s!r}")
    hh_str, mm_str = s[:2], s[3:]
    if not (hh_str.isdigit() and mm_str.isdigit()):
        raise ValueError(f"invalid HH:MM: {s!r}")
    hh, mm = int(hh_str), int(mm_str)
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"invalid HH:MM out of range: {s!r}")
    return hh * 60 + mm, f"{hh:02d}:{mm:02d}"


def parse_time_range_arg(s: str) -> tuple[str, str]:
    """'HH:MM-HH:MM' を検証し (start, end) の正規化文字列を返す。

    半開区間 [start, end)。24:00 跨ぎ（start >= end）は未対応でエラー。
    """
    if not isinstance(s, str) or "-" not in s:
        raise argparse.ArgumentTypeError(
            f"invalid --time-range {s!r}: expected 'HH:MM-HH:MM' (e.g. 04:00-09:00)"
        )
    parts = s.split("-")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(
            f"invalid --time-range {s!r}: expected 'HH:MM-HH:MM' (e.g. 04:00-09:00)"
        )
    start_raw, end_raw = parts[0].strip(), parts[1].strip()
    try:
        start_min, start_norm = _parse_hhmm(start_raw)
        end_min, end_norm = _parse_hhmm(end_raw)
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f"invalid --time-range {s!r}: {e} (expected 'HH:MM-HH:MM', 00:00-23:59)"
        ) from None
    if start_min >= end_min:
        raise argparse.ArgumentTypeError(
            f"invalid --time-range {s!r}: start must be earlier than end (24:00 跨ぎは未対応)"
        )
    return start_norm, end_norm


def _hhmm_to_minutes(hhmm_str: str) -> int | None:
    """'HH:MM' を分単位整数に。失敗時 None。"""
    if not hhmm_str or len(hhmm_str) < 5 or hhmm_str[2] != ":":
        return None
    hh_str, mm_str = hhmm_str[:2], hhmm_str[3:5]
    if not (hh_str.isdigit() and mm_str.isdigit()):
        return None
    return int(hh_str) * 60 + int(mm_str)


def slot_in_time_range(
    slot: dict[str, Any], time_range: tuple[str, str] | None
) -> bool:
    """スロットの timestamp_jst が time-range [start, end) 内か判定。

    time_range が None なら常に True。判定不能なら False（安全側）。
    """
    if time_range is None:
        return True
    ts = slot.get("timestamp_jst", "")
    minutes = _hhmm_to_minutes(hhmm(ts))
    if minutes is None:
        return False
    start_min, _ = _parse_hhmm(time_range[0])
    end_min, _ = _parse_hhmm(time_range[1])
    return start_min <= minutes < end_min


def slots_for_date(point: dict[str, Any], target_date: str) -> list[dict[str, Any]]:
    """指定日のスロットだけ抽出。timestamp_jst が YYYY-MM-DD で始まるもの。"""
    return [s for s in point["slots"] if s.get("timestamp_jst", "")[:10] == target_date]


def daily_summary_for_date(point: dict[str, Any], target_date: str) -> dict[str, Any] | None:
    """その日の daily_summary を取得。"""
    for ds in point.get("daily_summaries", []):
        if ds.get("date") == target_date:
            return ds
    return None


def hhmm(ts: str) -> str:
    return ts[11:16] if len(ts) >= 16 else ""


# ============================================================
# ランキング集計
# ============================================================
def _slot_is_daylight_with_point(s: dict[str, Any], point: dict[str, Any]) -> bool:
    """slot に is_daylight が埋まっていればそれを使い、無ければ point lat/lon
    で都度算出（後方互換: 旧 dashboard.json でも動くように）。"""
    if "is_daylight" in s:
        return bool(s["is_daylight"])
    lat = point.get("point_lat")
    lon = point.get("point_lon")
    if lat is not None and lon is not None:
        return slot_is_daylight(s, lat=lat, lon=lon)
    return slot_is_daylight(s)


def collect_ranking(
    dashboard: dict[str, Any],
    target_date: str,
    top_n: int,
    exclude_extended: bool,
    time_range: tuple[str, str] | None = None,
) -> list[dict[str, Any]]:
    """9ポイント横断でその日のベストスロット TOP N を返す。

    並び順: composite 降順 → timestamp_jst 昇順（早い時間優先）
    time_range が指定されたら [start, end) のスロットのみ採用。
    夜間スロット (日の出 -30min 〜 日の入り +30min の外) は常に除外。
    """
    rows: list[dict[str, Any]] = []
    for p in dashboard.get("points", []):
        name = p.get("point_name", "")
        for s in slots_for_date(p, target_date):
            if exclude_extended and s.get("is_extended"):
                continue
            if not slot_in_time_range(s, time_range):
                continue
            if not _slot_is_daylight_with_point(s, p):
                continue
            rows.append({
                "point_name": name,
                "timestamp_jst": s.get("timestamp_jst", ""),
                "time": hhmm(s.get("timestamp_jst", "")),
                "composite": s.get("composite", 0.0),
                "rank": s.get("rank", "fair"),
                "stars": RANK_STARS.get(s.get("rank", "fair"), ""),
                "wave_height_m": s.get("htsgw_m"),
                "period_s": s.get("perpw_s"),
                "wind_label": s.get("wind_label", ""),
                "wind_ms": s.get("wind_ms"),
                "swell_dir_label": s.get("swell_dir_label", ""),
                "tide_phase": s.get("tide_phase"),
                "is_extended": s.get("is_extended", False),
                "comment": s.get("comment", ""),
            })

    rows.sort(key=lambda r: (-float(r["composite"] or 0.0), r["timestamp_jst"]))
    return rows[:top_n]


def collect_point_summaries(
    dashboard: dict[str, Any],
    target_date: str,
    exclude_extended: bool,
) -> list[dict[str, Any]]:
    """ポイント名順に daily_summary（あれば）を集める。"""
    out: list[dict[str, Any]] = []
    for p in dashboard.get("points", []):
        name = p.get("point_name", "")
        ds = daily_summary_for_date(p, target_date)
        if ds is None:
            out.append({
                "point_name": name,
                "date": target_date,
                "text": "（その日のデータなし）",
                "best_time": "",
                "best_composite": None,
                "is_extended": False,
                "skipped": True,
            })
            continue
        if exclude_extended and ds.get("is_extended"):
            continue
        out.append({
            "point_name": name,
            "date": ds.get("date", target_date),
            "text": ds.get("text", ""),
            "best_time": ds.get("best_time", ""),
            "best_composite": ds.get("best_composite"),
            "is_extended": ds.get("is_extended", False),
            "skipped": False,
        })
    return out


def build_day_payload(
    dashboard: dict[str, Any],
    target_date: str,
    top_n: int,
    exclude_extended: bool,
    time_range: tuple[str, str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "date": target_date,
        "top_n": top_n,
        "exclude_extended": exclude_extended,
        "ranking": collect_ranking(
            dashboard, target_date, top_n, exclude_extended, time_range
        ),
        "point_summaries": collect_point_summaries(dashboard, target_date, exclude_extended),
    }
    if time_range is not None:
        payload["time_range"] = {"start": time_range[0], "end": time_range[1]}
    return payload


# ============================================================
# 整形（text）
# ============================================================
def _fmt_score(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{float(v) * 5:.1f}"  # composite (0-1) を 5段階スケールへ


def _fmt_height(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{float(v):.1f}m"


def _fmt_wind(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{float(v):.1f}m/s"


def _time_range_suffix(day: dict[str, Any]) -> str:
    tr = day.get("time_range")
    if not tr:
        return ""
    return f" {tr['start']}-{tr['end']}"


def render_text(day: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"🌊 {day['date']}{_time_range_suffix(day)} の波予報サマリ")
    lines.append("")
    lines.append(f"◆ 横断ベスト時間 TOP {len(day['ranking'])}")
    if not day["ranking"]:
        lines.append("  （該当データなし）")
    for i, r in enumerate(day["ranking"], 1):
        ext = " [参考値]" if r.get("is_extended") else ""
        comment = r.get("comment") or ""
        lines.append(
            f"{i}. {r['point_name']} {r['time']} ★{_fmt_score(r['composite'])}"
            f" ({_fmt_height(r['wave_height_m'])}/{r['wind_label']}{_fmt_wind(r['wind_ms'])}){ext}"
        )
        if comment:
            lines.append(f"   「{comment}」")
    lines.append("")
    lines.append("◆ 各ポイントの今日のひとこと")
    for ps in day["point_summaries"]:
        ext = " [参考値]" if ps.get("is_extended") else ""
        text = ps.get("text") or "（コメントなし）"
        lines.append(f"- {ps['point_name']}{ext}: {text}")
    return "\n".join(lines)


# ============================================================
# 整形（markdown）
# ============================================================
def render_markdown(day: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"## 🌊 {day['date']}{_time_range_suffix(day)} の波予報サマリ")
    lines.append("")
    lines.append(f"### 横断ベスト時間 TOP {len(day['ranking'])}")
    if not day["ranking"]:
        lines.append("- （該当データなし）")
    else:
        lines.append("")
        lines.append("| # | ポイント | 時間 | スコア | 波高 | 風 | コメント |")
        lines.append("|---|---|---|---|---|---|---|")
        for i, r in enumerate(day["ranking"], 1):
            ext = " *(参考値)*" if r.get("is_extended") else ""
            wind = f"{r['wind_label']} {_fmt_wind(r['wind_ms'])}".strip()
            comment = (r.get("comment") or "").replace("|", "/")
            lines.append(
                f"| {i} | {r['point_name']}{ext} | {r['time']} | ★{_fmt_score(r['composite'])} "
                f"| {_fmt_height(r['wave_height_m'])} | {wind} | {comment} |"
            )
    lines.append("")
    lines.append("### 各ポイントの今日のひとこと")
    lines.append("")
    for ps in day["point_summaries"]:
        ext = " *(参考値)*" if ps.get("is_extended") else ""
        text = ps.get("text") or "（コメントなし）"
        lines.append(f"- **{ps['point_name']}**{ext}: {text}")
    return "\n".join(lines)


# ============================================================
# CLI
# ============================================================
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="dashboard.json から日次サマリ＋横断ランキングを出力 (Phase B-2)",
    )
    parser.add_argument(
        "--date",
        type=parse_date_arg,
        default=None,
        help="基準日 YYYY-MM-DD（デフォルト: 今日）",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=1,
        help="連続出力する日数（デフォルト: 1）",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json", "markdown"),
        default="text",
        help="出力形式",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="横断ランキング件数（デフォルト: 10）",
    )
    parser.add_argument(
        "--exclude-extended",
        action="store_true",
        help="5日以降の参考値レンジ（is_extended=True）を集計から除外",
    )
    time_group = parser.add_mutually_exclusive_group()
    time_group.add_argument(
        "--time-range",
        dest="time_range",
        type=parse_time_range_arg,
        default=None,
        metavar="HH:MM-HH:MM",
        help="ranking を絞る時間帯 [start, end)。例: 04:00-09:00。24:00 跨ぎ未対応",
    )
    time_group.add_argument(
        "--morning",
        action="store_true",
        help=f"朝マズメ プリセット（{TIME_RANGE_PRESETS['morning'][0]}-{TIME_RANGE_PRESETS['morning'][1]}）",
    )
    time_group.add_argument(
        "--evening",
        action="store_true",
        help=f"夕マズメ プリセット（{TIME_RANGE_PRESETS['evening'][0]}-{TIME_RANGE_PRESETS['evening'][1]}）",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DASHBOARD_JSON,
        help=f"dashboard.json パス（デフォルト: {DASHBOARD_JSON}）",
    )
    return parser.parse_args(argv)


def resolve_time_range(args: argparse.Namespace) -> tuple[str, str] | None:
    """--time-range / --morning / --evening を解決して (start, end) or None。"""
    if getattr(args, "morning", False):
        return TIME_RANGE_PRESETS["morning"]
    if getattr(args, "evening", False):
        return TIME_RANGE_PRESETS["evening"]
    return getattr(args, "time_range", None)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    dashboard = load_dashboard(args.input)

    base_date = (
        date.fromisoformat(args.date) if args.date else datetime.now().date()
    )
    days = max(1, int(args.days))
    target_dates = [(base_date + timedelta(days=i)).isoformat() for i in range(days)]

    time_range = resolve_time_range(args)

    payloads = [
        build_day_payload(dashboard, d, args.top, args.exclude_extended, time_range)
        for d in target_dates
    ]

    if args.format == "json":
        out: dict[str, Any] = {
            "generated_at": dashboard.get("generated_at"),
        }
        if time_range is not None:
            out["time_range"] = {"start": time_range[0], "end": time_range[1]}
        out.update({
            "scoring_version": dashboard.get("scoring_version"),
            "tide_source": dashboard.get("tide_source"),
            "base_date": base_date.isoformat(),
            "days": days,
            "exclude_extended": args.exclude_extended,
            "results": payloads,
        })
        print(json.dumps(out, ensure_ascii=False, indent=2))
    elif args.format == "markdown":
        chunks = [render_markdown(p) for p in payloads]
        print("\n\n---\n\n".join(chunks))
    else:  # text
        chunks = [render_text(p) for p in payloads]
        print("\n\n--------\n\n".join(chunks))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
