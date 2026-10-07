"""週末リール用データを dashboard.json から作る。

出力 (JSON):
  {"saturday", "sunday", "generated_at", "dayLabel", "period", "points": [...]}
points[] は主要9地点（scripts/generate_instagram_post.FEATURED_POINTS）ごとに
  days.sat/sun: 日中スロット（時刻・スコア・波高・周期・うねり/風）
  members: 同じエリア（nationwide_points の area_name）の全地点の日中平均スコア
  tide.sat/sun: 0〜23時の潮位
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from generate_instagram_post import FEATURED_POINTS, next_saturday  # noqa: E402

JST = timezone(timedelta(hours=9))
MIYAZAKI_PORTS = {"MG", "AB"}  # 宮崎の初期9地点は nationwide_points に無いので潮位港で判定


def area_group(point: dict, area_names: dict[str, str]) -> str | None:
    if point["point_name"] in area_names:
        return area_names[point["point_name"]]
    if point.get("tide_port") in MIYAZAKI_PORTS or not point.get("area"):
        return "宮崎"
    return None


def day_slots(point: dict, day: str) -> list[dict]:
    slots = [s for s in point.get("slots", []) if s.get("timestamp_jst", "")[:10] == day and s.get("is_daylight")]
    reliable = [s for s in slots if not s.get("is_extended")]
    return reliable or slots  # 木曜生成なら日曜も予報レンジ内。念のため参考値にフォールバック


def slot_view(s: dict) -> dict:
    return {
        "t": s["timestamp_jst"][11:16],
        "score": round(float(s.get("composite") or 0) * 100),
        "h": s.get("htsgw_m"),
        "per": round(s.get("perpw_s") or 0, 1),
        "swell": s.get("swell_dir_label", ""),
        "swdeg": round(s.get("swdir1_deg") or s.get("dirpw_deg") or 0),
        "wind": s.get("wind_label", ""),
        "wms": round(s.get("wind_ms") or 0, 1),
        "wdeg": round(s.get("wdir_deg") or 0),
        "wdir": s.get("wind_dir_label"),
        "tide": s.get("tide_m"),
        "comment": s.get("comment", ""),
    }


def build(dashboard: dict, area_names: dict[str, str], sat: date) -> dict:
    days = {"sat": sat.isoformat(), "sun": (sat + timedelta(days=1)).isoformat()}
    points = dashboard["points"]
    out = []
    for area, name, candidates, area_filter in FEATURED_POINTS:
        matched = [p for p in points if p["point_name"] in candidates and (area_filter is None or p.get("area") == area_filter)]
        if not matched:
            raise SystemExit(f"ERROR: {name} が dashboard にありません")
        # 候補が複数（国府ノ浜）なら週末の最高スコアの地点
        best = max(matched, key=lambda p: max((s.get("composite") or 0 for d in days.values() for s in day_slots(p, d)), default=0))
        group = area_group(best, area_names)
        row = {
            "area": area, "name": name, "raw": best["point_name"], "group": group,
            "beach": best.get("beach_direction"), "beach_deg": best.get("beach_direction_deg"),
            "bottom": best.get("bottom_type"), "notes": best.get("local_notes"),
            "days": {k: [slot_view(s) for s in day_slots(best, d)] for k, d in days.items()},
            "tide": {k: [round(x["tide_m"], 2) for x in best.get("tide_hourly", []) if x["timestamp_jst"][:10] == d]
                     for k, d in days.items()},
            "members": [],
        }
        for k, slots in row["days"].items():
            if not slots:
                raise SystemExit(f"ERROR: {name} の {days[k]} 日中スロットがありません")
        for p in points:
            if area_group(p, area_names) != group:
                continue
            m = {"name": p["point_name"], "lat": p["point_lat"], "lon": p["point_lon"]}
            for k, d in days.items():
                v = [s.get("composite") or 0 for s in day_slots(p, d)]
                m[k] = round(statistics.mean(v) * 100) if v else None
            row["members"].append(m)
        out.append(row)
    sat_d, sun_d = sat, sat + timedelta(days=1)
    return {
        "saturday": days["sat"], "sunday": days["sun"], "generated_at": dashboard.get("generated_at"),
        "dayLabel": {"sat": f"{sat_d.month}/{sat_d.day} SAT", "sun": f"{sun_d.month}/{sun_d.day} SUN"},
        "period": f"{sat_d.month}/{sat_d.day} – {sun_d.month}/{sun_d.day}",
        "points": out,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dashboard", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--saturday", help="YYYY-MM-DD（既定: JST 今日から見て次の土曜）")
    args = ap.parse_args()
    sat = date.fromisoformat(args.saturday) if args.saturday else next_saturday(datetime.now(JST).date())
    nationwide = json.loads((Path(__file__).parent / "assets" / "nationwide_points.json").read_text(encoding="utf-8"))
    area_names = {p["name"]: p["area_name"] for p in nationwide["points"]}
    data = build(json.loads(args.dashboard.read_text(encoding="utf-8")), area_names, sat)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(args.out, data["saturday"], data["generated_at"], [f"{p['name']}:{p['group']}:{len(p['members'])}" for p in data["points"]])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
