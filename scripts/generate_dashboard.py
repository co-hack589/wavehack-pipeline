"""forecast_gfs_wave.json + scoring_rules.yaml から
ポイント別ダッシュボード用 JSON & HTML を生成する。

デフォルト出力（本番用）:
  output/data/dashboard.json    スコア付きの予報データ
  output/html/dashboard.html    ブラウザで開く可視化（JSON埋め込み済）

Smoke test 用（本番出力を壊さない）:
  python scripts/generate_dashboard.py \
    --forecast /tmp/smoke_forecast.json \
    --output /tmp/smoke_dashboard.json \
    --no-html
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from datetime import datetime

from wavehack_pipeline.area_topic import build_area_topic
from wavehack_pipeline.comments import (
    build_daily_summaries,
    format_slot_comment,
    wind_relative_label,
)
from wavehack_pipeline.daylight import slot_is_daylight
from wavehack_pipeline.scoring import ScoringRules, score_slot
from wavehack_pipeline.tide import POINT_TO_PORT, TIDE_STATIONS, load_tide_cache, tide_at

# miyazaki_points.json と scoring_rules.yaml の名称差異吸収
# （miyazaki_points.json は「高鍋」、yaml は「蚊口」にリネーム済）
NAME_REMAP: dict[str, str] = {"高鍋": "蚊口"}

WAVEHACK_OUTPUT = ROOT.parent.parent  # /Users/takeda.toi/wavehack/output
FORECAST_JSON = ROOT / "output" / "forecast_gfs_wave.json"
TIDE_JSON = ROOT / "output" / "tide_jma.json"
DASHBOARD_JSON = WAVEHACK_OUTPUT / "data" / "dashboard.json"
DASHBOARD_HTML = WAVEHACK_OUTPUT / "html" / "dashboard.html"
TEMPLATE_HTML = WAVEHACK_OUTPUT / "html" / "dashboard_template.html"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--forecast", type=Path, default=FORECAST_JSON,
                        help=f"input forecast_gfs_wave.json (default: {FORECAST_JSON})")
    parser.add_argument("--output", type=Path, default=DASHBOARD_JSON,
                        help=f"output dashboard.json (default: {DASHBOARD_JSON})")
    parser.add_argument("--no-html", action="store_true",
                        help="skip HTML output (smoke test 用)")
    args = parser.parse_args()

    forecast_path: Path = args.forecast
    dashboard_out: Path = args.output

    if not forecast_path.exists():
        print(f"ERROR: {forecast_path} not found. Run fetch_gfs_wave.py first.", file=sys.stderr)
        return 1
    forecast = json.loads(forecast_path.read_text(encoding="utf-8"))
    rules = ScoringRules.default()

    tide_cache = None
    if TIDE_JSON.exists():
        tide_cache = load_tide_cache(TIDE_JSON)
        print(f"  tide cache: {sorted(tide_cache.keys())}")
    else:
        print(f"  WARN: {TIDE_JSON} not found, tide_score will be 0.5 fallback")

    enriched_points = []
    for p in forecast["points"]:
        raw_name = p["point_name"]
        name = NAME_REMAP.get(raw_name, raw_name)
        if name not in rules.points:
            print(f"  skip {raw_name} (not in scoring_rules.yaml)")
            continue
        if name != raw_name:
            print(f"  remap {raw_name} -> {name}")
        meta = rules.points[name]
        beach_dir_deg = rules.beach_dir_for(name)
        # raw_name でも mapped name でも tide_port を引けるように両対応
        port = POINT_TO_PORT.get(raw_name) or POINT_TO_PORT.get(name)
        tide_table = tide_cache.get(port) if (tide_cache and port) else None
        scored_slots = []
        point_lat = p.get("point_lat")
        point_lon = p.get("point_lon")
        for s in p["slots"]:
            tide_m = None
            tide_phase = None
            if tide_table is not None:
                dt = datetime.fromisoformat(s["timestamp_jst"])
                tide_m, tide_phase = tide_at(tide_table, dt)
            sb = score_slot(s, point_name=name, rules=rules, tide_phase=tide_phase)
            wind_label = wind_relative_label(s.get("wdir_deg"), beach_dir_deg)
            entry = {
                "timestamp_utc": s["timestamp_utc"],
                "timestamp_jst": s["timestamp_jst"],
                "fhour": s["fhour"],
                "is_extended": rules.is_extended_fhour(s["fhour"]),
                "htsgw_m": s["htsgw_m"],
                "perpw_s": s["perpw_s"],
                "dirpw_deg": s["dirpw_deg"],
                "swdir1_deg": s["swdir1_deg"],
                "swell1_m": s["swell1_m"],
                "swell_dir_label": rules.label_for_direction_deg(s["swdir1_deg"])["ja"] if s["swdir1_deg"] is not None else "",
                "wind_ms": s["wind_ms"],
                "wdir_deg": s["wdir_deg"],
                "wind_dir_label": rules.label_for_direction_deg(s["wdir_deg"])["ja"] if s["wdir_deg"] is not None else "",
                "wind_label": wind_label,
                "tide_m": round(tide_m, 2) if tide_m is not None else None,
                "tide_phase": tide_phase,
                "swell_score": round(sb.swell, 3),
                "wind_score": round(sb.wind, 3),
                "tide_score": round(sb.tide, 3),
                "size_score": round(sb.size, 3),
                "composite": round(sb.composite, 3),
                "rank": sb.rank,
            }
            entry["comment"] = format_slot_comment(entry, name, rules)
            # per-point の lat/lon が取れればポイント別に判定、駄目なら宮崎中心
            if point_lat is not None and point_lon is not None:
                entry["is_daylight"] = slot_is_daylight(entry, lat=point_lat, lon=point_lon)
            else:
                entry["is_daylight"] = slot_is_daylight(entry)
            scored_slots.append(entry)

        # best_time 選定: 夜間スロットは候補から除外（無ければ全体から選ぶフォールバック）
        daylight_slots = [s for s in scored_slots if s.get("is_daylight")]
        best_candidates = daylight_slots if daylight_slots else scored_slots
        best = max(best_candidates, key=lambda s: s["composite"]) if best_candidates else None

        # daily_summary も夜間スロット除外後の候補から（空ならフォールバック）
        summary_input = daylight_slots if daylight_slots else scored_slots
        daily_summaries = build_daily_summaries(name, summary_input, rules)

        enriched_points.append({
            "point_name": name,
            "raw_point_name": raw_name,
            "name_en": meta.get("name_en"),
            "area": meta.get("area", ""),
            "beach_direction": meta.get("beach_direction"),
            "beach_direction_deg": rules.beach_dir_for(name),
            "grid_lat": p["grid_lat"],
            "grid_lon": p["grid_lon"],
            "point_lat": p["point_lat"],
            "point_lon": p["point_lon"],
            "tide_port": port,
            "tide_station": TIDE_STATIONS.get(raw_name) or TIDE_STATIONS.get(name),
            "bottom_type": meta.get("bottom_type", ""),
            "tide_override": meta.get("tide_override", ""),
            "local_notes": meta.get("local_notes", ""),
            "best_time_jst": best["timestamp_jst"] if best else None,
            "best_composite": best["composite"] if best else None,
            "best_rank": best["rank"] if best else None,
            "daily_summaries": daily_summaries,
            "slots": scored_slots,
        })

    # エリア全体（宮崎9ポイント集約）の Today's Topic
    area_topic = build_area_topic(enriched_points)
    print(
        f"  area_topic: pattern={area_topic['pattern']} date={area_topic['date']} "
        f"fallback={area_topic['fallback']}"
    )

    payload = {
        "generated_at": forecast.get("generated_at"),
        "run_base_utc": forecast.get("run_base_utc"),
        "source": forecast.get("source"),
        "tide_source": "jma_suisan" if tide_cache else None,
        "scoring_version": rules.scoring.get("version"),
        "forecast_range": rules.forecast_range,
        "area_topic": area_topic,
        "points": enriched_points,
    }

    dashboard_out.parent.mkdir(parents=True, exist_ok=True)
    dashboard_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {dashboard_out} ({dashboard_out.stat().st_size:,} bytes)")

    if args.no_html:
        return 0

    # HTML 埋め込み
    if not TEMPLATE_HTML.exists():
        print(f"WARN: {TEMPLATE_HTML} not found, skipping HTML")
        return 0
    template = TEMPLATE_HTML.read_text(encoding="utf-8")
    embed = "const DATA = " + json.dumps(payload, ensure_ascii=False) + ";"
    new_html, n = re.subn(r"const DATA = \{.*?\};", embed, template, count=1, flags=re.DOTALL)
    if n != 1:
        # 初回はプレースホルダ "const DATA = null;" を置換
        new_html, n = re.subn(r"const DATA = null;", embed, template, count=1)
    DASHBOARD_HTML.parent.mkdir(parents=True, exist_ok=True)
    DASHBOARD_HTML.write_text(new_html, encoding="utf-8")
    print(f"wrote {DASHBOARD_HTML}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
