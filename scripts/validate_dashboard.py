"""Reject incomplete nationwide feeds before either production store is changed."""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JST = timezone(timedelta(hours=9))
# The scoring dashboard calls 高鍋/蚊口 "Kakuchi"; the GFS master uses Takanabe.
POINT_ALIASES = {"kakuchi": "takanabe", "kizakihama": "kisakihama"}


def canonical_id(value: str) -> str:
    lowered = value.lower()
    return POINT_ALIASES.get(lowered, lowered)


def validate_dashboard(dashboard: dict, master: list[dict], now: datetime) -> None:
    expected = {canonical_id(p["name_en"]) for p in master}
    points = dashboard["points"]
    ids = [canonical_id(p.get("name_en", "")) for p in points]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate point IDs in dashboard")
    missing = expected - set(ids)
    if missing:
        raise ValueError(f"Missing {len(missing)} nationwide points: {sorted(missing)}")

    today = now.astimezone(JST).date()
    required_dates = {(today + timedelta(days=i)).isoformat() for i in range(7)}
    for point in points:
        if not point.get("tide_station") or point.get("tide_port") != point["tide_station"].get("port"):
            raise ValueError(f"{point['name_en']}: missing tide reference station")
        tide_hours = {s["timestamp_jst"]: s.get("tide_m") for s in point.get("tide_hourly", [])}
        for day in required_dates:
            for hour in range(24):
                stamp = f"{day}T{hour:02d}:00:00+09:00"
                value = tide_hours.get(stamp)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError(f"{point['name_en']}: missing hourly tide at {stamp}")
        available = set()
        for slot in point.get("slots", []):
            tide = slot.get("tide_m")
            if isinstance(tide, bool) or not isinstance(tide, (int, float)) or not math.isfinite(tide):
                raise ValueError(f"{point['name_en']}: missing tide prediction at {slot.get('timestamp_jst')}")
            for field in ("htsgw_m", "wind_ms"):
                value = slot.get(field)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                    raise ValueError(f"{point['name_en']}: missing current/future {field} at {slot.get('timestamp_jst')}")
            stamp = datetime.fromisoformat(slot["timestamp_jst"]).astimezone(JST)
            available.add(stamp.date().isoformat())
        stamps = sorted(datetime.fromisoformat(s["timestamp_jst"]).astimezone(JST) for s in point.get("slots", []))
        start = datetime.combine(today, datetime.min.time(), JST)
        end = start + timedelta(days=6, hours=21)
        if not stamps or stamps[0] > now.astimezone(JST) or stamps[-1] < end or any((b-a).total_seconds() > 10800 for a,b in zip(stamps, stamps[1:])):
            raise ValueError(f"{point['name_en']}: missing current/future 7-day coverage")

        if not required_dates <= available:
            raise ValueError(f"{point['name_en']}: missing current wave forecasts for {sorted(required_dates - available)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dashboard", type=Path)
    args = parser.parse_args()
    dashboard = json.loads(args.dashboard.read_text())
    master = json.loads((ROOT / "data/all_points.json").read_text())
    validate_dashboard(dashboard, master, datetime.now(JST))
    print(f"Validated nationwide coverage: {len(master)} points, 7 days, wave/wind/tide, gaps <= 3h")


if __name__ == "__main__":
    main()
