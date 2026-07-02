"""予報データの生成。

ダミー（ランダム）と GFS（実データ）の両方をサポート。
GFS データは fetch_gfs_wave.py が出力した forecast_gfs_wave.json を入力にする。
公開スキーマ（ForecastSlot / PointForecast）は両ソースで共通。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Any

from .points import Point
from .scoring import ScoringRules, score_slot
from .tide import POINT_TO_PORT, TideDay, TidePhase, tide_at

# miyazaki_points.json と scoring_rules.yaml の名称差異吸収（高鍋→蚊口）
NAME_REMAP: dict[str, str] = {"高鍋": "蚊口"}

# 3時間刻み、4日分 = 32スロット
FORECAST_SLOTS = 32
SLOT_HOURS = 3


@dataclass(frozen=True)
class ForecastSlot:
    timestamp: str  # ISO8601 (JST)
    wave_height_m: float
    wave_period_s: float
    wave_direction_deg: int
    wind_speed_kmh: float
    wind_direction_deg: int
    tide_m: float
    temperature_c: float
    weather: str  # sunny / cloudy / rainy
    score: int  # 0-10


@dataclass(frozen=True)
class PointForecast:
    point_id: str
    point_name: str
    area: str
    latitude: float
    longitude: float
    beach_direction: str
    generated_at: str
    slots: list[ForecastSlot]


def _dummy_slot(base: datetime, slot_index: int, rng: random.Random) -> ForecastSlot:
    ts = base + timedelta(hours=slot_index * SLOT_HOURS)
    return ForecastSlot(
        timestamp=ts.isoformat(),
        wave_height_m=round(rng.uniform(0.4, 2.5), 2),
        wave_period_s=round(rng.uniform(6.0, 14.0), 1),
        wave_direction_deg=rng.randint(0, 359),
        wind_speed_kmh=round(rng.uniform(0.0, 28.0), 1),
        wind_direction_deg=rng.randint(0, 359),
        tide_m=round(rng.uniform(0.2, 1.9), 2),
        temperature_c=round(rng.uniform(15.0, 28.0), 1),
        weather=rng.choice(["sunny", "cloudy", "rainy"]),
        score=rng.randint(2, 9),
    )


def generate_dummy_forecast(point: Point, base_jst: datetime | None = None) -> PointForecast:
    """与えられたポイントについてダミー予報を返す。"""
    if base_jst is None:
        base_jst = datetime.now(timezone(timedelta(hours=9))).replace(
            minute=0, second=0, microsecond=0
        )
    # ポイントごとに seed を固定して、生成のたびに値が変わらないようにする
    rng = random.Random(f"{point.id}:{base_jst.date().isoformat()}")
    slots = [_dummy_slot(base_jst, i, rng) for i in range(FORECAST_SLOTS)]
    return PointForecast(
        point_id=point.id,
        point_name=point.name,
        area=point.area,
        latitude=point.latitude,
        longitude=point.longitude,
        beach_direction=point.beach_direction,
        generated_at=datetime.now(timezone(timedelta(hours=9))).isoformat(),
        slots=slots,
    )


def to_dict(forecast: PointForecast) -> dict[str, Any]:
    """JSON シリアライズ用に dict 化（dataclass のネスト対応）。"""
    d = asdict(forecast)
    return d


# ============================================================
# GFS Wave → ForecastSlot 変換
# ============================================================
# temperature_c / weather は別ソース未実装のため placeholder。tide は A-4 で実装済み。
_TEMP_PLACEHOLDER_C = 0.0
_WEATHER_PLACEHOLDER = "unknown"


def _gfs_slot_to_forecast_slot(
    gfs_slot: dict[str, Any],
    composite_0_1: float,
    tide_m: float | None,
) -> ForecastSlot:
    htsgw = gfs_slot.get("htsgw_m") or 0.0
    perpw = gfs_slot.get("perpw_s") or 0.0
    dirpw = gfs_slot.get("dirpw_deg")
    wind_ms = gfs_slot.get("wind_ms") or 0.0
    wdir = gfs_slot.get("wdir_deg")
    return ForecastSlot(
        timestamp=gfs_slot["timestamp_jst"],
        wave_height_m=round(float(htsgw), 2),
        wave_period_s=round(float(perpw), 1),
        wave_direction_deg=int(round(float(dirpw))) if dirpw is not None else 0,
        wind_speed_kmh=round(float(wind_ms) * 3.6, 1),
        wind_direction_deg=int(round(float(wdir))) if wdir is not None else 0,
        tide_m=round(tide_m, 2) if tide_m is not None else 0.0,
        temperature_c=_TEMP_PLACEHOLDER_C,
        weather=_WEATHER_PLACEHOLDER,
        score=int(round(max(0.0, min(1.0, composite_0_1)) * 10)),
    )


def _resolve_tide(
    tide_cache: dict[str, dict[Any, TideDay]] | None,
    point_name: str,
    timestamp_jst: str,
) -> tuple[float | None, TidePhase | None]:
    """ポイント名と時刻から (高さm, フェーズ) を返す。キャッシュなしなら (None, None)。"""
    if tide_cache is None:
        return None, None
    port = POINT_TO_PORT.get(point_name)
    if port is None:
        return None, None
    table = tide_cache.get(port)
    if not table:
        return None, None
    dt = datetime.fromisoformat(timestamp_jst)
    return tide_at(table, dt)


def gfs_to_point_forecast(
    gfs_point: dict[str, Any],
    point: Point,
    rules: ScoringRules,
    tide_cache: dict[str, dict[Any, TideDay]] | None = None,
) -> PointForecast | None:
    """GFS forecast の1ポイント分 dict と Point マスタから PointForecast を生成。

    tide_cache を渡すと tide_m と tide_phase を実データで埋める。
    scoring_rules.yaml に存在しないポイントは None を返す（呼び出し側でスキップ）。
    """
    raw_name = gfs_point["point_name"]
    scoring_name = NAME_REMAP.get(raw_name, raw_name)
    if scoring_name not in rules.points:
        return None

    slots: list[ForecastSlot] = []
    for s in gfs_point["slots"]:
        # 潮位は raw_name（マスタ名）でマッピング解決。POINT_TO_PORT は両表記対応済
        tide_m, tide_phase = _resolve_tide(tide_cache, point.name, s["timestamp_jst"])
        sb = score_slot(s, point_name=scoring_name, rules=rules, tide_phase=tide_phase)
        slots.append(_gfs_slot_to_forecast_slot(s, sb.composite, tide_m))

    return PointForecast(
        point_id=point.id,
        point_name=point.name,
        area=point.area,
        latitude=point.latitude,
        longitude=point.longitude,
        beach_direction=point.beach_direction,
        generated_at=datetime.now(timezone(timedelta(hours=9))).isoformat(),
        slots=slots,
    )
