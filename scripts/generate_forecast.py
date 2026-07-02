"""宮崎ポイントの予報を生成して JSON に書き出す。

使い方:
    # 既存ダミーデータ（互換維持）
    python scripts/generate_forecast.py
    python scripts/generate_forecast.py --source dummy

    # GFS Wave 実データから生成（先に fetch_gfs_wave.py を実行しておく）
    python scripts/generate_forecast.py --source gfs
    python scripts/generate_forecast.py --source gfs \
        --gfs-input output/forecast_gfs_wave.json \
        --output ../wavehack-api/data/forecast.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from wavehack_pipeline.forecast import (
    generate_dummy_forecast,
    gfs_to_point_forecast,
    to_dict,
)
from wavehack_pipeline.points import Point, load_points
from wavehack_pipeline.scoring import ScoringRules
from wavehack_pipeline.tide import load_tide_cache


def _build_dummy(points: list[Point]) -> dict:
    base_jst = datetime.now(timezone(timedelta(hours=9))).replace(
        minute=0, second=0, microsecond=0
    )
    return {
        "generated_at": base_jst.isoformat(),
        "source": "dummy",
        "points": [to_dict(generate_dummy_forecast(p, base_jst)) for p in points],
    }


def _build_gfs(
    points: list[Point],
    gfs_input: Path,
    rules: ScoringRules,
    tide_input: Path | None,
) -> dict:
    if not gfs_input.exists():
        raise FileNotFoundError(
            f"GFS input not found: {gfs_input}. Run fetch_gfs_wave.py first."
        )
    gfs = json.loads(gfs_input.read_text(encoding="utf-8"))
    by_name: dict[str, dict] = {p["point_name"]: p for p in gfs["points"]}

    tide_cache = None
    if tide_input is not None and tide_input.exists():
        tide_cache = load_tide_cache(tide_input)
        print(f"  tide cache loaded: {sorted(tide_cache.keys())} from {tide_input.name}", file=sys.stderr)
    elif tide_input is not None:
        print(f"  WARN: tide input {tide_input} not found, tide_score will be 0.5 fallback", file=sys.stderr)

    out_points: list[dict] = []
    skipped: list[str] = []
    for p in points:
        gp = by_name.get(p.name)
        if gp is None:
            skipped.append(f"{p.name} (not in GFS output)")
            continue
        pf = gfs_to_point_forecast(gp, p, rules, tide_cache=tide_cache)
        if pf is None:
            skipped.append(f"{p.name} (not in scoring_rules.yaml)")
            continue
        out_points.append(to_dict(pf))

    if skipped:
        for s in skipped:
            print(f"  skip {s}", file=sys.stderr)

    return {
        "generated_at": datetime.now(timezone(timedelta(hours=9))).isoformat(),
        "source": "gfs",
        "run_base_utc": gfs.get("run_base_utc"),
        "tide_source": "jma_suisan" if tide_cache else "fallback_0.5",
        "points": out_points,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source",
        choices=["dummy", "gfs"],
        default="dummy",
        help="データソース（dummy: ランダム生成 / gfs: forecast_gfs_wave.json から変換）",
    )
    parser.add_argument(
        "--points",
        type=Path,
        default=ROOT / "data" / "miyazaki_points.json",
    )
    parser.add_argument(
        "--gfs-input",
        type=Path,
        default=ROOT / "output" / "forecast_gfs_wave.json",
    )
    parser.add_argument(
        "--tide-input",
        type=Path,
        default=ROOT / "output" / "tide_jma.json",
        help="気象庁潮汐推算データ。未指定/不在なら tide_score は 0.5 fallback",
    )
    parser.add_argument(
        "--no-tide",
        action="store_true",
        help="潮位データを読み込まない（fallback 0.5）",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "output" / "forecast.json",
    )
    args = parser.parse_args()

    points = load_points(args.points)

    if args.source == "dummy":
        payload = _build_dummy(points)
    else:
        rules = ScoringRules.default()
        tide_input = None if args.no_tide else args.tide_input
        payload = _build_gfs(points, args.gfs_input, rules, tide_input)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"OK: source={args.source} points={len(payload['points'])} → {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
