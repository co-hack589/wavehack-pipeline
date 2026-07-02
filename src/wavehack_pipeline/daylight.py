"""日の出・日の入りに基づく日中判定ヘルパー。

dashboard.json 生成 / daily_summary 抽出時のフィルタ層として利用する。
スコアリング本体 (scoring.py) には触らず、候補スロットの除外のみ責務とする。

仕様（ユーザー確定）:
- ライブラリ: astral (>=3.2)
- 既定エリア: 宮崎中心 (lat=31.91, lon=131.42 / Asia/Tokyo)
- 薄明バッファ: 日の出 30 分前 〜 日の入り 30 分後
- per-point 拡張余地として lat/lon 引数を持つ

使い方:
    >>> from datetime import datetime
    >>> from wavehack_pipeline.daylight import is_daylight
    >>> is_daylight(datetime.fromisoformat("2026-06-29T05:30:00+09:00"))
    True
    >>> is_daylight(datetime.fromisoformat("2026-06-29T03:00:00+09:00"))
    False
"""

from __future__ import annotations

import zoneinfo
from datetime import date, datetime, timedelta
from functools import lru_cache

from astral import LocationInfo
from astral.sun import sun

# 宮崎中心。MVP 範囲のポイント (lat 31.6-32.4 程度) はすべて同一エリアと見なす。
DEFAULT_LAT = 31.91
DEFAULT_LON = 131.42
DEFAULT_TZ = "Asia/Tokyo"
DEFAULT_BUFFER_MINUTES = 30


@lru_cache(maxsize=256)
def _sun_times(
    target_date: date,
    lat: float,
    lon: float,
    tz_name: str,
) -> tuple[datetime, datetime]:
    """指定日 / 地点の (sunrise, sunset) を JST aware datetime で返す。"""
    tz = zoneinfo.ZoneInfo(tz_name)
    loc = LocationInfo(
        name="point",
        region="",
        timezone=tz_name,
        latitude=lat,
        longitude=lon,
    )
    s = sun(loc.observer, date=target_date, tzinfo=tz)
    return s["sunrise"], s["sunset"]


def sunrise_sunset(
    target_date: date,
    lat: float = DEFAULT_LAT,
    lon: float = DEFAULT_LON,
    tz_name: str = DEFAULT_TZ,
) -> tuple[datetime, datetime]:
    """指定日の (sunrise, sunset) を返す（純粋関数 / キャッシュ）。"""
    return _sun_times(target_date, lat, lon, tz_name)


def is_daylight(
    dt: datetime,
    lat: float = DEFAULT_LAT,
    lon: float = DEFAULT_LON,
    buffer_minutes: int = DEFAULT_BUFFER_MINUTES,
    tz_name: str = DEFAULT_TZ,
) -> bool:
    """`dt` が「日の出 - buffer 〜 日の入り + buffer」の薄明帯に入っているか。

    - `dt` が naive ならローカルタイムゾーン (`tz_name`) として解釈
    - 範囲は閉区間扱い（境界は True）
    - astral 内部例外時はフェイルセーフで True（夜間の取りこぼしより、
      昼スロット候補を不当に消す方を避ける）
    """
    tz = zoneinfo.ZoneInfo(tz_name)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    else:
        dt = dt.astimezone(tz)

    try:
        sunrise, sunset = sunrise_sunset(dt.date(), lat, lon, tz_name)
    except Exception:
        return True

    start = sunrise - timedelta(minutes=buffer_minutes)
    end = sunset + timedelta(minutes=buffer_minutes)
    return start <= dt <= end


def slot_is_daylight(
    slot: dict,
    lat: float = DEFAULT_LAT,
    lon: float = DEFAULT_LON,
    buffer_minutes: int = DEFAULT_BUFFER_MINUTES,
) -> bool:
    """dashboard.json の slot dict (`timestamp_jst` を持つ) を判定するヘルパー。

    `timestamp_jst` が無い / パース不能なら False（安全側で除外せず通したい
    場合は呼び出し側で短絡すること）。
    """
    ts = slot.get("timestamp_jst", "")
    if not ts:
        return False
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return False
    return is_daylight(dt, lat=lat, lon=lon, buffer_minutes=buffer_minutes)
