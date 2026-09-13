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
    required_dates = {(today + timedelta(days=i)).isoformat() for i in range(2)}
    for point in points:
        if not point.get("tide_station") or point.get("tide_port") != point["tide_station"].get("port"):
            raise ValueError(f"{point['name_en']}: missing tide reference station")
        available = set()
        for slot in point.get("slots", []):
            tide = slot.get("tide_m")
            if isinstance(tide, bool) or not isinstance(tide, (int, float)) or not math.isfinite(tide):
                raise ValueError(f"{point['name_en']}: missing tide prediction at {slot.get('timestamp_jst')}")
            wave = slot.get("htsgw_m")
            if isinstance(wave, (int, float)) and not isinstance(wave, bool) and math.isfinite(wave) and wave >= 0:
                available.add(datetime.fromisoformat(slot["timestamp_jst"]).astimezone(JST).date().isoformat())
        if not required_dates <= available:
            raise ValueError(f"{point['name_en']}: missing current wave forecasts for {sorted(required_dates - available)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dashboard", type=Path)
    args = parser.parse_args()
    dashboard = json.loads(args.dashboard.read_text())
    master = json.loads((ROOT / "data/all_points.json").read_text())
    validate_dashboard(dashboard, master, datetime.now(JST))
    print(f"Validated nationwide coverage: {len(master)} points, today and tomorrow")


if __name__ == "__main__":
    main()
