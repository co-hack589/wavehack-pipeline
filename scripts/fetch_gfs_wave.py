"""宮崎10ポイントの GFS Wave 予報を取得して JSON に書き出す。

使い方:
    python scripts/fetch_gfs_wave.py
    python scripts/fetch_gfs_wave.py --hours 0 24      # 0..24時間先のみ
    python scripts/fetch_gfs_wave.py --workers 16
    python scripts/fetch_gfs_wave.py --cache /tmp/gfs-cache    # ファイル再利用
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from wavehack_pipeline.gfs_wave import fetch_forecast, latest_run_base, to_dict
from wavehack_pipeline.points import load_points


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--points", type=Path, default=ROOT / "data" / "miyazaki_points.json")
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "forecast_gfs_wave.json")
    parser.add_argument(
        "--hours",
        nargs=2,
        type=int,
        metavar=("START", "END"),
        default=[0, 80],
        help="forecast hour 範囲 [start end] inclusive (default: 0 80)",
    )
    parser.add_argument(
        "--step-3h-after",
        type=int,
        default=120,
        help="この時間以降は3時間刻みでファイル生成（GFS Wave仕様: f120以降は1時間刻み非提供）",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="並列 DL ワーカー数 (default: 4。NOAA S3 への並列度を下げて安定化)",
    )
    parser.add_argument("--cache", type=Path, default=None)
    parser.add_argument(
        "--filter-points",
        nargs="+",
        help="ポイント名(JP)/name_en/id(=name_en.lower()) いずれかで絞り込み（テスト用）",
    )
    args = parser.parse_args()

    points = load_points(args.points)
    if args.filter_points:
        keys = set(args.filter_points)
        keys_lower = {k.lower() for k in keys}
        points = [
            p for p in points
            if p.name in keys or p.name_en in keys or p.id in keys_lower
        ]
        if not points:
            print(f"ERROR: no points matched {sorted(keys)}", file=sys.stderr)
            return 1

    # f0..step_3h_after は1時間刻み、それ以降は3時間刻み
    fhours: list[int] = []
    for h in range(args.hours[0], args.hours[1] + 1):
        if h <= args.step_3h_after:
            fhours.append(h)
        elif (h - args.step_3h_after) % 3 == 0:
            fhours.append(h)

    if not fhours:
        raise ValueError("No forecast hours requested")
    run = latest_run_base(required_fhour=max(fhours))

    print(f"run base UTC: {run.strftime('%Y-%m-%dT%H:%M:%S')}")
    print(f"run base JST: {run.astimezone(timezone(timedelta(hours=9))).strftime('%Y-%m-%dT%H:%M:%S')}")
    print(f"points: {len(points)} ({', '.join(p.name for p in points)})")
    print(f"fhours: {fhours[0]}..{fhours[-1]} ({len(fhours)} slots)")
    print(f"workers: {args.workers}")
    print()

    forecasts = fetch_forecast(
        points,
        run=run,
        fhours=fhours,
        max_workers=args.workers,
        cache_dir=args.cache,
        progress=True,
    )

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "source": "NOAA GFS Wave (gfswave.global.0p16) via NODD S3",
        "run_base_utc": run.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "points": [to_dict(forecasts[p.name]) for p in points],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {args.output}")
    print(f"size: {args.output.stat().st_size:,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
