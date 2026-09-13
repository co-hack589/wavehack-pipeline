"""Fetch reference-port predictions for the complete forecast date range.

An incomplete download must not replace the last valid cache or be published.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from wavehack_pipeline.tide import JST, POINT_TO_PORT, fetch_jma_tide, parse_jma_text, to_jsonable


def forecast_dates(forecast: dict) -> set:
    dates = {datetime.fromisoformat(s["timestamp_jst"]).astimezone(JST).date()
             for p in forecast["points"] for s in p["slots"]}
    if not dates:
        raise ValueError("Forecast has no dates")
    return dates | {min(dates) - timedelta(days=1), max(dates) + timedelta(days=1)}


def fetch_tables(ports: list[str], dates: set) -> dict:
    years = sorted({d.year for d in dates})
    def fetch_one(job):
        port, year = job
        for attempt in range(3):
            try:
                table = parse_jma_text(fetch_jma_tide(year, port))
                expected = {d for d in dates if d.year == year}
                if not expected <= table.keys():
                    raise ValueError(f"{port}/{year}: missing forecast dates")
                if any(len(table[d].hourly_cm) != 24 or 999 in table[d].hourly_cm for d in expected):
                    raise ValueError(f"{port}/{year}: missing hourly predictions")
                return port, table
            except Exception:
                if attempt == 2:
                    raise
    tables = {port: {} for port in ports}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for port, table in pool.map(fetch_one, [(p, y) for p in ports for y in years]):
            tables[port].update(table)
    return tables


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, help="Explicit full-year fetch for maintenance")
    parser.add_argument("--ports", nargs="+")
    parser.add_argument("--points", type=Path, default=ROOT / "data/all_points.json")
    parser.add_argument("--forecast", type=Path, default=ROOT / "output/forecast_gfs_wave.json")
    parser.add_argument("--output", type=Path, default=ROOT / "output/tide_jma.json")
    args = parser.parse_args()
    if args.year:
        first = datetime(args.year, 1, 1).date()
        dates = {first + timedelta(days=i) for i in range((datetime(args.year + 1, 1, 1).date() - first).days)}
    else:
        dates = forecast_dates(json.loads(args.forecast.read_text()))
    ports = sorted(set(args.ports or [POINT_TO_PORT[p["name"]] for p in json.loads(args.points.read_text())]))
    tables = fetch_tables(ports, dates)
    payload = {"fetched_at": datetime.now(timezone.utc).isoformat(),
               "years": sorted({d.year for d in dates}), "source": "jma_suisan_txt",
               "ports": {p: to_jsonable(t) for p, t in tables.items()}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(args.output)
    print(f"Validated {len(ports)} ports, {len(dates)} forecast/adjacent dates; wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
