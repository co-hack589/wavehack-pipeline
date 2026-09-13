"""気象庁 潮汐推算データ（JMAテキスト形式）の取得・解析・位相判定。

データソース:
    https://www.data.jma.go.jp/kaiyou/data/db/tide/suisan/txt/{YEAR}/{STN}.txt

フォーマット（136桁固定 / 1日1行）:
    1-72   : 24時間 × 3桁(cm) ＝ その日 0:00〜23:00 JST の毎時潮位
    73-78  : YY MM DD（半角2桁スペースパディング）
    79-80  : 港コード2文字
    81-108 : 満潮 4セット × (時刻4桁 + 潮位3桁)。データなし=9999/999
    109-136: 干潮 4セット × (時刻4桁 + 潮位3桁)

公開関数:
    fetch_jma_tide(year, port)      JMAからテキスト取得
    parse_jma_text(text, port)      テキスト全行を解析しdictへ
    tide_at(table, jst_dt)          (height_m, phase) を返す
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Literal

JST = timezone(timedelta(hours=9))

TidePhase = Literal[
    "mid_flood", "high_to_ebb", "low_to_flood", "mid_ebb", "high_slack", "low_slack"
]

# ピーク前後この時間内は slack 判定
SLACK_WINDOW = timedelta(minutes=45)

# 港コード（気象庁検潮所）
PORT_ABURATSU = "AB"
PORT_MIYAZAKI = "MG"

# ポイント名 → 港コード（推算潮位の代替地点）
POINT_TO_PORT: dict[str, str] = {
    "伊比井": PORT_ABURATSU,
    "大堂津": PORT_ABURATSU,
    "木崎浜": PORT_MIYAZAKI,
    "一ツ葉": PORT_MIYAZAKI,
    "青島": PORT_MIYAZAKI,
    "蚊口": PORT_MIYAZAKI,
    "高鍋": PORT_MIYAZAKI,  # NAME_REMAP前の名称も保険
    "伊勢ヶ浜": "Z6",
    "お倉ヶ浜": PORT_MIYAZAKI,
    "金ヶ浜": PORT_MIYAZAKI,
}

# Explicit, coast-reviewed reference stations; never choose a port at runtime
# using distance alone (nearby ports can be on the other side of a peninsula).
_STATIONS = json.loads((Path(__file__).resolve().parents[2] / "data/tide_stations.json").read_text())
TIDE_STATIONS = {s["point_name"]: s for s in _STATIONS["stations"]}
TIDE_STATIONS["蚊口"] = TIDE_STATIONS["高鍋"]
POINT_TO_PORT.update({name: s["port"] for name, s in TIDE_STATIONS.items()})


@dataclass(frozen=True)
class TideExtreme:
    when_jst: datetime
    height_cm: int


@dataclass(frozen=True)
class TideDay:
    date_jst: date
    hourly_cm: list[int]  # 長さ24（0時〜23時、cm）
    highs: list[TideExtreme]
    lows: list[TideExtreme]


# ============================================================
# 取得
# ============================================================
def fetch_jma_tide(year: int, port: str, *, timeout: float = 30.0) -> str:
    url = f"https://www.data.jma.go.jp/kaiyou/data/db/tide/suisan/txt/{year}/{port}.txt"
    req = urllib.request.Request(url, headers={"User-Agent": "wavehack-pipeline/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    text = body.decode("ascii", errors="replace")
    if "<html" in text.lower() or "<!doctype" in text.lower():
        raise RuntimeError(f"JMA returned HTML (port code may be invalid): {url}")
    return text


# ============================================================
# 解析
# ============================================================
def _parse_int(s: str) -> int:
    return int(s.strip())


def _parse_extreme(chunk: str, ymd: date) -> TideExtreme | None:
    """4桁時刻 + 3桁潮位cm を TideExtreme へ。9999/999 は欠損。"""
    if len(chunk) < 7:
        return None
    t_str = chunk[0:4]
    h_str = chunk[4:7]
    if t_str == "9999" or h_str == "999":
        return None
    try:
        hh = int(t_str[0:2])
        mm = int(t_str[2:4])
        height = _parse_int(h_str)
    except ValueError:
        return None
    if not (0 <= hh < 24 and 0 <= mm < 60):
        return None
    return TideExtreme(
        when_jst=datetime.combine(ymd, time(hh, mm), tzinfo=JST),
        height_cm=height,
    )


def parse_jma_line(line: str) -> TideDay | None:
    if len(line) < 80:
        return None
    hourly = [_parse_int(line[i : i + 3]) for i in range(0, 72, 3)]
    yy = _parse_int(line[72:74])
    mm = _parse_int(line[74:76])
    dd = _parse_int(line[76:78])
    ymd = date(2000 + yy, mm, dd)

    highs: list[TideExtreme] = []
    for i in range(4):
        ex = _parse_extreme(line[80 + i * 7 : 80 + (i + 1) * 7], ymd)
        if ex is not None:
            highs.append(ex)
    lows: list[TideExtreme] = []
    for i in range(4):
        ex = _parse_extreme(line[108 + i * 7 : 108 + (i + 1) * 7], ymd)
        if ex is not None:
            lows.append(ex)

    return TideDay(date_jst=ymd, hourly_cm=hourly, highs=highs, lows=lows)


def parse_jma_text(text: str) -> dict[date, TideDay]:
    table: dict[date, TideDay] = {}
    for raw in text.splitlines():
        if not raw.strip():
            continue
        day = parse_jma_line(raw)
        if day is None:
            continue
        table[day.date_jst] = day
    return table


# ============================================================
# 補間 + フェーズ判定
# ============================================================
def _hourly_height_m(table: dict[date, TideDay], dt: datetime) -> float | None:
    """毎時潮位を線形補間して高さ(m)を返す。"""
    dt_jst = dt.astimezone(JST)
    d = dt_jst.date()
    minutes = dt_jst.hour * 60 + dt_jst.minute
    h_idx_lo = minutes // 60
    h_idx_hi = h_idx_lo + 1
    frac = (minutes - h_idx_lo * 60) / 60.0

    today = table.get(d)
    if today is None:
        return None
    lo_cm = today.hourly_cm[h_idx_lo]
    if lo_cm == 999:
        return None
    if frac == 0:
        return lo_cm / 100.0

    if h_idx_hi < 24:
        hi_cm = today.hourly_cm[h_idx_hi]
    else:
        # 23時超は翌日の0時を参照
        nxt = table.get(d + timedelta(days=1))
        if nxt is None:
            return None
        hi_cm = nxt.hourly_cm[0]

    if hi_cm == 999:
        return None

    return (lo_cm + (hi_cm - lo_cm) * frac) / 100.0


def _surrounding_extremes(
    table: dict[date, TideDay], dt: datetime
) -> tuple[TideExtreme | None, TideExtreme | None]:
    """dt の直前と直後にある（満潮 or 干潮）イベントを返す。"""
    dt_jst = dt.astimezone(JST)
    candidates: list[tuple[TideExtreme, str]] = []
    for d_offset in (-1, 0, 1):
        day = table.get(dt_jst.date() + timedelta(days=d_offset))
        if day is None:
            continue
        for h in day.highs:
            candidates.append((h, "high"))
        for low in day.lows:
            candidates.append((low, "low"))
    candidates.sort(key=lambda x: x[0].when_jst)

    prev: TideExtreme | None = None
    nxt: TideExtreme | None = None
    for ex, _kind in candidates:
        if ex.when_jst <= dt_jst:
            prev = ex
        elif nxt is None and ex.when_jst > dt_jst:
            nxt = ex
            break
    return prev, nxt


def _phase_from_extremes(
    dt: datetime, prev: TideExtreme | None, nxt: TideExtreme | None
) -> TidePhase | None:
    """前後の満干から位相を決定。"""
    if prev is None or nxt is None:
        return None
    dt_jst = dt.astimezone(JST)
    rising = nxt.height_cm > prev.height_cm  # 次が満潮なら上昇

    near_prev = (dt_jst - prev.when_jst) <= SLACK_WINDOW
    near_next = (nxt.when_jst - dt_jst) <= SLACK_WINDOW

    if rising:
        # prev=干潮, nxt=満潮
        if near_prev:
            return "low_to_flood"
        if near_next:
            return "high_slack"
        return "mid_flood"
    else:
        # prev=満潮, nxt=干潮
        if near_prev:
            return "high_to_ebb"
        if near_next:
            return "low_slack"
        return "mid_ebb"


def tide_at(
    table: dict[date, TideDay], jst_dt: datetime
) -> tuple[float | None, TidePhase | None]:
    """指定JST時刻の (海面高さm, 潮位フェーズ) を返す。データ欠損時は (None, None)。"""
    h_m = _hourly_height_m(table, jst_dt)
    if h_m is None:
        return None, None
    prev, nxt = _surrounding_extremes(table, jst_dt)
    phase = _phase_from_extremes(jst_dt, prev, nxt)
    return h_m, phase


# ============================================================
# JSON 永続化（fetch_tide.py 出力用）
# ============================================================
def to_jsonable(table: dict[date, TideDay]) -> list[dict]:
    return [
        {
            "date": d.date_jst.isoformat(),
            "hourly_cm": d.hourly_cm,
            "highs": [{"when_jst": e.when_jst.isoformat(), "height_cm": e.height_cm} for e in d.highs],
            "lows": [{"when_jst": e.when_jst.isoformat(), "height_cm": e.height_cm} for e in d.lows],
        }
        for d in sorted(table.values(), key=lambda x: x.date_jst)
    ]


def from_jsonable(items: list[dict]) -> dict[date, TideDay]:
    table: dict[date, TideDay] = {}
    for it in items:
        d = date.fromisoformat(it["date"])
        table[d] = TideDay(
            date_jst=d,
            hourly_cm=list(it["hourly_cm"]),
            highs=[
                TideExtreme(
                    when_jst=datetime.fromisoformat(e["when_jst"]),
                    height_cm=int(e["height_cm"]),
                )
                for e in it["highs"]
            ],
            lows=[
                TideExtreme(
                    when_jst=datetime.fromisoformat(e["when_jst"]),
                    height_cm=int(e["height_cm"]),
                )
                for e in it["lows"]
            ],
        )
    return table


def load_tide_cache(path: Path) -> dict[str, dict[date, TideDay]]:
    """fetch_tide.py が出力した JSON を {port: {date: TideDay}} で返す。"""
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {port: from_jsonable(items) for port, items in raw["ports"].items()}
