"""宮崎9ポイントのビーチ向きを OSM coastline から推定し、
現状の scoring_rules.yaml の値と突合する。

使い方:
    cd /Users/takeda.toi/wavehack/output/code/wavehack-pipeline
    /Users/takeda.toi/.local/bin/uv run python scripts/estimate_beach_direction.py
    # オプション:
    #   --no-detect-bay   湾内検出を無効化
    #   --no-detect-river 河口検出を無効化

OSM coastline の慣習:
    natural=coastline の way は「進行方向の右手側が海、左手側が陸」と定義されている。
    したがって線分の方向ベクトルを時計回りに 90° 回した向きが「海側」になる。
    ビーチ向き (= ビーチが面している方向 = うねりが打ち寄せてくる方向) は
    海岸線の向こう（海側）の方向と一致するので、
    線分方向(始点→終点)を時計回りに 90° 回した方位が推定ビーチ向きになる。

河口/湾内検出:
    上記のみでは、湾内ポイント（伊勢ヶ浜など）や河口隣接ポイント（大堂津・伊比井）で
    OSM coastline が河川や湾の奥を拾い、誤った方位を返す。これを抑止するため:
      - 河口検出: 半径 RIVER_RADIUS_M 内に waterway=river/stream があれば「河口隣接」フラグ
      - 湾内検出: 半径 BAY_RADIUS_M 内の coastline 法線方位の標準偏差を計算し、
        BAY_STD_THRESHOLD_DEG を超えれば「湾内」と判定（=単純最近傍は信頼性低）
        加えて 8方位 1km レイキャストで陸地ヒット率を見て、6方位以上が陸なら強い湾内
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
POINTS_JSON = ROOT / "data" / "miyazaki_points.json"
SCORING_YAML = Path("/Users/takeda.toi/wavehack/output/data/scoring_rules.yaml")
REPORT_MD = Path("/Users/takeda.toi/wavehack/output/md/beach_direction_check.md")

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_TIMEOUT = 30
SLEEP_BETWEEN_REQUESTS_S = 1.5
DEFAULT_RADIUS_M = 500
FALLBACK_RADIUS_M = 1000

# 河口/湾内検出パラメータ
RIVER_RADIUS_M = 500          # 河口隣接判定半径
BAY_RADIUS_M = 800            # 湾内判定: coastline サンプル半径
BAY_STD_THRESHOLD_DEG = 60.0  # 法線方位の標準偏差がこれ以上 → 湾内
RAYCAST_DISTANCE_M = 1000     # 8方位レイキャストの距離
RAYCAST_LAND_HITS_FOR_BAY = 6 # 6方位以上が陸なら強い湾内

# 最寄り線分が遠すぎる時の警告閾値
NEAREST_FAR_WARN_M = 500.0
# 補助メトリクス: メインway重み付き平均の半径
AUX_MAIN_WAY_RADIUS_M = 500

# JSON は「高鍋」、yaml は「蚊口」で表記が分かれているので吸収する
NAME_REMAP = {"高鍋": "蚊口"}

DIRECTION_LABELS_16 = [
    (348.75, 11.25, "北"),
    (11.25, 33.75, "北北東"),
    (33.75, 56.25, "北東"),
    (56.25, 78.75, "東北東"),
    (78.75, 101.25, "東"),
    (101.25, 123.75, "東南東"),
    (123.75, 146.25, "南東"),
    (146.25, 168.75, "南南東"),
    (168.75, 191.25, "南"),
    (191.25, 213.75, "南南西"),
    (213.75, 236.25, "南西"),
    (236.25, 258.75, "西南西"),
    (258.75, 281.25, "西"),
    (281.25, 303.75, "西北西"),
    (303.75, 326.25, "北西"),
    (326.25, 348.75, "北北西"),
]


def deg_to_jp16(deg: float) -> str:
    """方位角(0=N, 90=E)を16方位日本語に丸める。"""
    d = deg % 360
    for from_deg, to_deg, label in DIRECTION_LABELS_16:
        if from_deg < to_deg:
            if from_deg <= d < to_deg:
                return label
        else:
            # 北だけ 348.75〜11.25 でラップする
            if d >= from_deg or d < to_deg:
                return label
    return "?"


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """2地点の距離(メートル)。"""
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """始点(lat1,lon1) から終点(lat2,lon2) への方位角 (deg, 0=N, 時計回り)。"""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def point_to_segment_distance_m(
    plat: float,
    plon: float,
    a_lat: float,
    a_lon: float,
    b_lat: float,
    b_lon: float,
) -> float:
    """点 P から線分 AB までの最短距離 (メートル)。
    緯度経度を局所平面に射影して計算する（500m〜1km 程度ならこの近似で十分)。
    """
    # 局所等距離投影 (m単位)
    lat0 = (a_lat + b_lat) / 2
    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * math.cos(math.radians(lat0))

    ax, ay = a_lon * m_per_deg_lon, a_lat * m_per_deg_lat
    bx, by = b_lon * m_per_deg_lon, b_lat * m_per_deg_lat
    px, py = plon * m_per_deg_lon, plat * m_per_deg_lat

    dx, dy = bx - ax, by - ay
    seg_len2 = dx * dx + dy * dy
    if seg_len2 == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / seg_len2
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


def _post_overpass(query: str, max_retries: int = 3, base_sleep: float = 5.0) -> dict:
    """Overpass POST に簡易リトライをかける（504/429/接続エラー対策）。"""
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = requests.post(
                OVERPASS_URL,
                data={"data": query},
                timeout=OVERPASS_TIMEOUT,
                headers={"User-Agent": "wavehack-pipeline/0.1 (beach-direction-check)"},
            )
            if resp.status_code in (429, 502, 503, 504):
                wait = base_sleep * (2 ** attempt)
                print(f"    Overpass {resp.status_code}, {wait:.1f}s 待って再試行...", flush=True)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except (requests.exceptions.RequestException, ValueError) as e:
            last_err = e
            wait = base_sleep * (2 ** attempt)
            print(f"    Overpass エラー({e.__class__.__name__}), {wait:.1f}s 待って再試行...", flush=True)
            time.sleep(wait)
    if last_err:
        raise last_err
    raise RuntimeError("Overpass: 無限ループ防止")


def fetch_coastline_ways(lat: float, lon: float, radius_m: int) -> list[list[tuple[float, float]]]:
    """Overpass API から半径 radius_m 内の natural=coastline の way 形状を取得。
    戻り値: [[(lat, lon), ...], ...]
    """
    query = (
        f"[out:json][timeout:25];"
        f'(way["natural"="coastline"](around:{radius_m},{lat},{lon}););'
        f"out geom;"
    )
    payload = _post_overpass(query)
    ways: list[list[tuple[float, float]]] = []
    for el in payload.get("elements", []):
        if el.get("type") != "way":
            continue
        geom = el.get("geometry") or []
        coords = [(g["lat"], g["lon"]) for g in geom if "lat" in g and "lon" in g]
        if len(coords) >= 2:
            ways.append(coords)
    return ways


def fetch_waterway_count(lat: float, lon: float, radius_m: int) -> tuple[int, list[str]]:
    """半径 radius_m 内に waterway=river/stream の way がいくつあるかを返す。
    戻り値: (件数, [waterway値, ...])
    """
    query = (
        f"[out:json][timeout:25];"
        f'(way["waterway"~"^(river|stream)$"](around:{radius_m},{lat},{lon}););'
        f"out tags;"
    )
    payload = _post_overpass(query)
    kinds: list[str] = []
    for el in payload.get("elements", []):
        if el.get("type") != "way":
            continue
        tags = el.get("tags") or {}
        kind = tags.get("waterway", "?")
        kinds.append(kind)
    return len(kinds), kinds


def fetch_natural_water_polygons(
    lat: float, lon: float, radius_m: int
) -> list[list[tuple[float, float]]]:
    """半径 radius_m 内の natural=water (=海/湖/河) ポリゴンの geometry を取得。
    ※ Overpass の `out geom` は relation を完全には展開しないが、
       way（閉路）として描かれている海岸近傍の water は geometry が返る。
    戻り値: [[(lat, lon), ...], ...]
    """
    query = (
        f"[out:json][timeout:25];"
        f'(way["natural"="water"](around:{radius_m},{lat},{lon}););'
        f"out geom;"
    )
    payload = _post_overpass(query)
    polys: list[list[tuple[float, float]]] = []
    for el in payload.get("elements", []):
        if el.get("type") != "way":
            continue
        geom = el.get("geometry") or []
        coords = [(g["lat"], g["lon"]) for g in geom if "lat" in g and "lon" in g]
        if len(coords) >= 3:
            polys.append(coords)
    return polys


def compute_segment_normals_in_radius(
    plat: float,
    plon: float,
    ways: list[list[tuple[float, float]]],
    radius_m: float,
) -> list[float]:
    """半径 radius_m 内に少なくとも一端がある線分について、
    OSM 慣習に従う法線方位（=ビーチ向き候補）のリストを返す。
    """
    normals: list[float] = []
    for coords in ways:
        for i in range(len(coords) - 1):
            a = coords[i]
            b = coords[i + 1]
            d = point_to_segment_distance_m(plat, plon, a[0], a[1], b[0], b[1])
            if d > radius_m:
                continue
            normals.append(estimate_beach_bearing(plat, plon, a, b))
    return normals


def circular_std_deg(angles_deg: list[float]) -> float:
    """方位角リスト（0-360）の循環標準偏差（度）。
    値が無い場合は 0 を返す。
    """
    if not angles_deg:
        return 0.0
    sin_sum = sum(math.sin(math.radians(a)) for a in angles_deg)
    cos_sum = sum(math.cos(math.radians(a)) for a in angles_deg)
    n = len(angles_deg)
    r = math.sqrt(sin_sum * sin_sum + cos_sum * cos_sum) / n
    # r が 1 に近いほどばらつきが小さい
    if r >= 1.0:
        return 0.0
    if r <= 1e-9:
        return 180.0
    return math.degrees(math.sqrt(-2.0 * math.log(r)))


def is_point_in_polygon(plat: float, plon: float, poly: list[tuple[float, float]]) -> bool:
    """点が緯度経度ポリゴン内にあるか（ray casting, 簡易）。"""
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        yi, xi = poly[i]
        yj, xj = poly[j]
        intersect = ((yi > plat) != (yj > plat)) and (
            plon < (xj - xi) * (plat - yi) / ((yj - yi) or 1e-12) + xi
        )
        if intersect:
            inside = not inside
        j = i
    return inside


def offset_latlon(lat: float, lon: float, bearing_deg_: float, distance_m: float) -> tuple[float, float]:
    """指定方位・距離だけ移動した緯度経度を返す（球面近似）。"""
    R = 6371000.0
    br = math.radians(bearing_deg_)
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    d_r = distance_m / R
    lat2 = math.asin(math.sin(lat1) * math.cos(d_r) + math.cos(lat1) * math.sin(d_r) * math.cos(br))
    lon2 = lon1 + math.atan2(
        math.sin(br) * math.sin(d_r) * math.cos(lat1),
        math.cos(d_r) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lon2)


def raycast_land_hits_8dir(
    plat: float,
    plon: float,
    ways: list[list[tuple[float, float]]],
    distance_m: float = RAYCAST_DISTANCE_M,
) -> tuple[int, list[bool]]:
    """中心から 8方位に distance_m 飛ばし、各方位で線分が交差するかを判定。
    交差した方位 = レイが coastline を越えた = その先は陸（OSM coastline は陸/海境界）
    と仮定。交差しない方位は陸ではない（=外洋）扱い。

    戻り値: (陸ヒット方位数, [True/False x 8])  bearings = 0,45,...,315
    """
    bearings = [b * 45.0 for b in range(8)]
    hits: list[bool] = []
    # 出発点を平面化する基準
    lat0 = plat
    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * math.cos(math.radians(lat0))
    px = plon * m_per_deg_lon
    py = plat * m_per_deg_lat

    for br in bearings:
        end_lat, end_lon = offset_latlon(plat, plon, br, distance_m)
        ex = end_lon * m_per_deg_lon
        ey = end_lat * m_per_deg_lat
        intersected = False
        for coords in ways:
            for i in range(len(coords) - 1):
                a = coords[i]
                b = coords[i + 1]
                ax = a[1] * m_per_deg_lon
                ay = a[0] * m_per_deg_lat
                bx = b[1] * m_per_deg_lon
                by = b[0] * m_per_deg_lat
                if _segments_intersect(px, py, ex, ey, ax, ay, bx, by):
                    intersected = True
                    break
            if intersected:
                break
        hits.append(intersected)
    return sum(1 for h in hits if h), hits


def _segments_intersect(
    p1x: float, p1y: float, p2x: float, p2y: float,
    p3x: float, p3y: float, p4x: float, p4y: float,
) -> bool:
    """線分 (p1-p2) と (p3-p4) が交差するか。"""
    def ccw(ax: float, ay: float, bx: float, by: float, cx: float, cy: float) -> float:
        return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)

    d1 = ccw(p3x, p3y, p4x, p4y, p1x, p1y)
    d2 = ccw(p3x, p3y, p4x, p4y, p2x, p2y)
    d3 = ccw(p1x, p1y, p2x, p2y, p3x, p3y)
    d4 = ccw(p1x, p1y, p2x, p2y, p4x, p4y)
    if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and (
        (d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)
    ):
        return True
    return False


def find_nearest_segment(
    plat: float,
    plon: float,
    ways: list[list[tuple[float, float]]],
) -> tuple[tuple[float, float], tuple[float, float], float] | None:
    """全 way の全線分のうち、点 (plat,plon) に最も近い線分を返す。
    戻り値: ((a_lat, a_lon), (b_lat, b_lon), distance_m) もしくは None
    """
    best: tuple[tuple[float, float], tuple[float, float], float] | None = None
    for coords in ways:
        for i in range(len(coords) - 1):
            a = coords[i]
            b = coords[i + 1]
            d = point_to_segment_distance_m(plat, plon, a[0], a[1], b[0], b[1])
            if best is None or d < best[2]:
                best = (a, b, d)
    return best


def estimate_main_way_weighted(
    plat: float,
    plon: float,
    ways: list[list[tuple[float, float]]],
    sample_radius_m: float,
) -> tuple[float | None, int, float | None, str | None]:
    """補助推定: 最寄り「開いた」coastline way 1本に絞り、
    その way 上で半径 sample_radius_m 内の線分群の重み付き円形平均を返す。

    side（cross product による P の左右）は最近傍線分1本で1回だけ判定し、
    その判定（+90 or -90）を way 全体に一律適用する。
    こうすることで、ビーチが弧を描いている場所で線分ごとに左右が入れ替わって
    法線方位が反転するのを防ぐ。

    閉じた way (始点==終点) は離岸礁/池/湖の輪郭の可能性が高いので除外する。

    戻り値: (推定方位deg, 採用線分数, 最寄りway距離m, 'L'/'R')
            open way が無ければ (None, 0, None, None)
    """
    open_ways = [w for w in ways if len(w) >= 2 and w[0] != w[-1]]
    if not open_ways:
        return None, 0, None, None

    # 最寄り open way を選定
    best_way = None
    best_dist = None
    best_idx = None
    for coords in open_ways:
        for i in range(len(coords) - 1):
            a = coords[i]
            b = coords[i + 1]
            d = point_to_segment_distance_m(plat, plon, a[0], a[1], b[0], b[1])
            if best_dist is None or d < best_dist:
                best_dist = d
                best_way = coords
                best_idx = i

    if best_way is None:
        return None, 0, None, None

    # 最近傍線分での side 判定（1回だけ）
    a0 = best_way[best_idx]
    b0 = best_way[best_idx + 1]
    lat0 = (a0[0] + b0[0]) / 2
    m_lat = 111320.0
    m_lon = 111320.0 * math.cos(math.radians(lat0))
    ax, ay = a0[1] * m_lon, a0[0] * m_lat
    bx, by = b0[1] * m_lon, b0[0] * m_lat
    px, py = plon * m_lon, plat * m_lat
    cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    if cross >= 0:
        add = 90
        side = "L"
    else:
        add = -90
        side = "R"

    # 半径内の線分すべての方位を距離逆数で重み付き平均
    sin_sum = 0.0
    cos_sum = 0.0
    count = 0
    for i in range(len(best_way) - 1):
        a = best_way[i]
        b = best_way[i + 1]
        d = point_to_segment_distance_m(plat, plon, a[0], a[1], b[0], b[1])
        if d > sample_radius_m:
            continue
        seg_brg = bearing_deg(a[0], a[1], b[0], b[1])
        normal = (seg_brg + add) % 360
        w = 1.0 / max(d, 1.0)
        sin_sum += w * math.sin(math.radians(normal))
        cos_sum += w * math.cos(math.radians(normal))
        count += 1
    if count == 0:
        return None, 0, best_dist, side
    mean = (math.degrees(math.atan2(sin_sum, cos_sum)) + 360) % 360
    return mean, count, best_dist, side


def estimate_beach_bearing(
    plat: float,
    plon: float,
    a: tuple[float, float],
    b: tuple[float, float],
) -> float:
    """coastline 線分 A→B からビーチ向き(=海側方位)を求める。
    OSM 慣習: way の進行方向の右手側が海。
    つまり線分の bearing を +90° したものが海側=ビーチ向き。

    ただしポイントが線分の左手(陸側)にあるか右手(海側)にあるかを確認し、
    陸側にあるはずなので、もし右手側にあった場合は way 方向が
    実際とは逆向きに引かれている可能性があるとみなし -90° を採る。
    """
    seg_bearing = bearing_deg(a[0], a[1], b[0], b[1])

    # ポイントが線分のどちら側にあるか (cross product で判定)
    lat0 = (a[0] + b[0]) / 2
    m_lat = 111320.0
    m_lon = 111320.0 * math.cos(math.radians(lat0))
    ax, ay = a[1] * m_lon, a[0] * m_lat
    bx, by = b[1] * m_lon, b[0] * m_lat
    px, py = plon * m_lon, plat * m_lat
    # cross = (B-A) x (P-A) ; >0 なら P は線分の左、<0 なら右
    cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)

    # 通常ケース: ポイントは陸=左手側 → cross > 0 → 海は右手 (+90°)
    # 逆ケース: ポイントが右手側 (way が逆向きに引かれている) → -90°
    if cross >= 0:
        return (seg_bearing + 90) % 360
    else:
        return (seg_bearing - 90) % 360


def angle_diff(a: float, b: float) -> float:
    """2方位の差を 0..180 に正規化。"""
    d = abs((a - b) % 360)
    return d if d <= 180 else 360 - d


def load_points() -> list[dict]:
    with POINTS_JSON.open(encoding="utf-8") as f:
        return json.load(f)


def load_yaml_points() -> tuple[dict[str, str], dict[str, float]]:
    """yaml から (name -> beach_direction日本語) と (日本語 -> deg) を返す。"""
    with SCORING_YAML.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    name_to_dir = {p["name"]: p["beach_direction"] for p in data.get("points", [])}
    dir_to_deg = data.get("beach_direction_deg", {})
    return name_to_dir, dir_to_deg


def classify_point_type(
    plat: float,
    plon: float,
    ways: list[list[tuple[float, float]]],
    river_count: int,
    detect_bay: bool,
    detect_river: bool,
) -> tuple[str, dict]:
    """ポイント種別を判定して (種別ラベル, 詳細dict) を返す。
    種別: "外洋" / "湾内" / "湾内・河口隣接" / "外洋・河口隣接"
    """
    info: dict = {
        "river_count": river_count,
        "coastline_normal_std_deg": None,
        "raycast_land_hits": None,
        "raycast_pattern": None,
    }

    is_bay = False
    if detect_bay:
        normals = compute_segment_normals_in_radius(plat, plon, ways, BAY_RADIUS_M)
        std = circular_std_deg(normals)
        info["coastline_normal_std_deg"] = std
        if std > BAY_STD_THRESHOLD_DEG:
            is_bay = True

        # レイキャストでも判定（外洋でも標準偏差が小さく出るケースの補強・別軸）
        hits, pattern = raycast_land_hits_8dir(plat, plon, ways)
        info["raycast_land_hits"] = hits
        info["raycast_pattern"] = pattern
        if hits >= RAYCAST_LAND_HITS_FOR_BAY:
            is_bay = True

    is_river_adjacent = detect_river and river_count > 0

    if is_bay and is_river_adjacent:
        return "湾内・河口隣接", info
    if is_bay:
        return "湾内", info
    if is_river_adjacent:
        return "外洋・河口隣接", info
    return "外洋", info


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OSM coastline からビーチ向きを推定")
    parser.add_argument(
        "--no-detect-bay",
        dest="detect_bay",
        action="store_false",
        help="湾内検出を無効化",
    )
    parser.add_argument(
        "--no-detect-river",
        dest="detect_river",
        action="store_false",
        help="河口検出を無効化",
    )
    parser.set_defaults(detect_bay=True, detect_river=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    points = load_points()
    yaml_dir_by_name, dir_to_deg = load_yaml_points()

    rows: list[dict] = []

    for i, p in enumerate(points):
        name_json = p["name"]
        name_yaml = NAME_REMAP.get(name_json, name_json)
        lat = p["latitude"]
        lon = p["longitude"]

        print(f"[{i+1}/{len(points)}] {name_json} ({lat:.4f},{lon:.4f}) 取得中...", flush=True)

        ways: list[list[tuple[float, float]]] = []
        used_radius = DEFAULT_RADIUS_M
        fetch_error: str | None = None
        river_count = 0
        river_kinds: list[str] = []

        try:
            ways = fetch_coastline_ways(lat, lon, DEFAULT_RADIUS_M)
            if not ways:
                # 半径を拡張して再試行
                time.sleep(SLEEP_BETWEEN_REQUESTS_S)
                used_radius = FALLBACK_RADIUS_M
                ways = fetch_coastline_ways(lat, lon, FALLBACK_RADIUS_M)

            # 河口検出（detect_river=True のときのみ問い合わせ）
            if args.detect_river:
                time.sleep(SLEEP_BETWEEN_REQUESTS_S)
                river_count, river_kinds = fetch_waterway_count(lat, lon, RIVER_RADIUS_M)

            # 湾内判定で半径 BAY_RADIUS_M の coastline も必要なので、
            # 不足分を再取得（既に FALLBACK_RADIUS_M で取得済みなら不要）
            if args.detect_bay and used_radius < BAY_RADIUS_M:
                time.sleep(SLEEP_BETWEEN_REQUESTS_S)
                ways_bay = fetch_coastline_ways(lat, lon, BAY_RADIUS_M)
                if ways_bay:
                    ways = ways_bay
                    used_radius = BAY_RADIUS_M
        except requests.exceptions.RequestException as e:
            fetch_error = f"requests失敗: {e.__class__.__name__}: {e}"
        except Exception as e:  # noqa: BLE001
            fetch_error = f"取得失敗: {e.__class__.__name__}: {e}"

        # 次のリクエストまでスリープ（最後のループでも軽くは入れて良い）
        if i < len(points) - 1:
            time.sleep(SLEEP_BETWEEN_REQUESTS_S)

        row: dict = {
            "name_json": name_json,
            "name_yaml": name_yaml,
            "lat": lat,
            "lon": lon,
            "used_radius_m": used_radius,
            "fetch_error": fetch_error,
            "yaml_dir_label": yaml_dir_by_name.get(name_yaml, "?"),
            "yaml_dir_deg": dir_to_deg.get(yaml_dir_by_name.get(name_yaml, ""), None),
            "estimated_deg": None,
            "estimated_label": None,
            "diff_deg": None,
            "verdict": "要確認",
            "nearest_segment": None,
            "nearest_segment_distance_m": None,
            "point_type": "外洋",
            "type_info": {},
            "river_kinds": river_kinds,
            # 補助メトリクス: 最寄りメインwayの重み付き円形平均
            "aux_main_way_deg": None,
            "aux_main_way_label": None,
            "aux_main_way_seg_count": 0,
            "aux_main_way_side": None,
            "aux_main_way_diff_deg": None,
            "nearest_far_warn": False,
        }

        if fetch_error is not None:
            row["verdict"] = "取得失敗"
            rows.append(row)
            continue

        if not ways:
            row["fetch_error"] = f"半径{used_radius}m内にcoastlineなし"
            row["verdict"] = "取得失敗"
            rows.append(row)
            continue

        nearest = find_nearest_segment(lat, lon, ways)
        if nearest is None:
            row["fetch_error"] = "最寄り線分の特定に失敗"
            row["verdict"] = "取得失敗"
            rows.append(row)
            continue

        a, b, dist = nearest
        beach_deg = estimate_beach_bearing(lat, lon, a, b)
        beach_label = deg_to_jp16(beach_deg)

        row["estimated_deg"] = beach_deg
        row["estimated_label"] = beach_label
        row["nearest_segment"] = (a, b)
        row["nearest_segment_distance_m"] = dist
        row["nearest_far_warn"] = dist > NEAREST_FAR_WARN_M

        # 補助: 最寄り open way 全体の重み付き円形平均（弧地形の補正用）
        aux_deg, aux_count, aux_main_dist, aux_side = estimate_main_way_weighted(
            lat, lon, ways, AUX_MAIN_WAY_RADIUS_M
        )
        row["aux_main_way_deg"] = aux_deg
        row["aux_main_way_seg_count"] = aux_count
        row["aux_main_way_side"] = aux_side
        if aux_deg is not None:
            row["aux_main_way_label"] = deg_to_jp16(aux_deg)
            if row["yaml_dir_deg"] is not None:
                row["aux_main_way_diff_deg"] = angle_diff(aux_deg, row["yaml_dir_deg"])

        # 種別判定
        point_type, type_info = classify_point_type(
            lat, lon, ways, river_count, args.detect_bay, args.detect_river
        )
        row["point_type"] = point_type
        row["type_info"] = type_info

        if row["yaml_dir_deg"] is not None:
            diff = angle_diff(beach_deg, row["yaml_dir_deg"])
            row["diff_deg"] = diff
            is_bay_or_river = point_type != "外洋"
            if beach_label == row["yaml_dir_label"] or diff <= 22.5:
                row["verdict"] = "一致"
            elif is_bay_or_river:
                # 湾内 or 河口隣接は yaml 値を信頼するため「許容（yaml推奨）」
                row["verdict"] = "不一致だがyaml推奨"
            else:
                row["verdict"] = "要再確認"
        rows.append(row)

    print_summary_table(rows)
    write_report(rows)
    print(f"\nレポート出力: {REPORT_MD}", flush=True)
    return 0


def print_summary_table(rows: list[dict]) -> None:
    """9ポイント全件の判定結果を標準出力に表形式で出す。"""
    print("")
    print("=" * 78)
    print("9ポイント判定結果サマリ")
    print("=" * 78)
    for r in rows:
        name = r["name_yaml"]
        yaml_label = r["yaml_dir_label"]
        yaml_deg = r["yaml_dir_deg"]
        est_deg = r["estimated_deg"]
        est_label = r["estimated_label"]
        ptype = r["point_type"]
        verdict = r["verdict"]

        info = r.get("type_info") or {}
        std = info.get("coastline_normal_std_deg")
        hits = info.get("raycast_land_hits")
        rcount = info.get("river_count", 0)

        kinds = r.get("river_kinds") or []
        kinds_str = ",".join(sorted(set(kinds))) if kinds else "-"

        print(f"\n{name}:")
        if yaml_deg is not None:
            print(f"  yaml値  : {yaml_deg}° ({yaml_label}向き)")
        else:
            print(f"  yaml値  : (なし)")
        if est_deg is not None:
            print(f"  推定値  : {est_deg:.0f}° ({est_label}向き) ← 単純最近傍")
            if r.get("nearest_far_warn"):
                d = r.get("nearest_segment_distance_m") or 0
                print(
                    f"           ⚠ 最寄りcoastlineまで {d:.0f}m と遠い（>{NEAREST_FAR_WARN_M:.0f}m）。"
                    f"座標が陸内または沖合に置かれている可能性があります。"
                )
            aux_deg = r.get("aux_main_way_deg")
            if aux_deg is not None:
                aux_label = r.get("aux_main_way_label")
                aux_count = r.get("aux_main_way_seg_count")
                aux_side = r.get("aux_main_way_side")
                aux_diff = r.get("aux_main_way_diff_deg")
                aux_diff_str = f" / yaml差 {aux_diff:.0f}°" if aux_diff is not None else ""
                print(
                    f"  補助推定: {aux_deg:.0f}° ({aux_label}向き) ← メインway重み付き平均"
                    f" / 半径{AUX_MAIN_WAY_RADIUS_M}m / {aux_count}線分 / side={aux_side}"
                    f"{aux_diff_str}"
                )
        else:
            print(f"  推定値  : (取得失敗)")

        type_detail_parts = []
        if rcount > 0:
            type_detail_parts.append(f"waterway={kinds_str} を半径{RIVER_RADIUS_M}mに{rcount}件検出")
        if std is not None:
            type_detail_parts.append(f"海岸線std={std:.0f}°")
        if hits is not None:
            type_detail_parts.append(f"陸ヒット={hits}/8方位")
        type_detail = " / ".join(type_detail_parts) if type_detail_parts else ""
        print(f"  種別    : {ptype}" + (f" ({type_detail})" if type_detail else ""))

        if verdict == "一致" and r.get("diff_deg") is not None:
            print(f"  判定    : ○ 一致 (差 {r['diff_deg']:.0f}°)")
        elif verdict == "不一致だがyaml推奨" and r.get("diff_deg") is not None:
            print(
                f"  判定    : △ 不一致だが yaml 値推奨"
                f"（湾内/河口隣接のため自動推定信頼性低 / 差 {r['diff_deg']:.0f}°）"
            )
        elif verdict == "要再確認" and r.get("diff_deg") is not None:
            print(f"  判定    : × 要再確認 (差 {r['diff_deg']:.0f}°)")
        elif verdict == "取得失敗":
            print(f"  判定    : - 取得失敗 ({r.get('fetch_error','')})")
        else:
            print(f"  判定    : {verdict}")
    print("")
    print("=" * 78)
    matched = sum(1 for r in rows if r["verdict"] == "一致")
    yaml_pref = sum(1 for r in rows if r["verdict"] == "不一致だがyaml推奨")
    review = sum(1 for r in rows if r["verdict"] == "要再確認")
    failed = sum(1 for r in rows if r["verdict"] == "取得失敗")
    print(
        f"集計: 一致 {matched} / 不一致だがyaml推奨 {yaml_pref} / 要再確認 {review} / 取得失敗 {failed}"
    )
    print("=" * 78)


def write_report(rows: list[dict]) -> None:
    lines: list[str] = []
    lines.append("# 宮崎ポイント ビーチ向き OSM 推定 突合レポート")
    lines.append("")
    lines.append("OpenStreetMap の `natural=coastline` データから、")
    lines.append("各ポイントの「ビーチが向いている方位（=うねりが打ち寄せる方位）」を")
    lines.append("自動推定し、現状 `scoring_rules.yaml` の値と突合した結果。")
    lines.append("")
    lines.append(f"- 入力: `data/miyazaki_points.json` ({len(rows)}ポイント)")
    lines.append("- 比較先: `output/data/scoring_rules.yaml` の `points[].beach_direction`")
    lines.append("- 名称差異: JSON「高鍋」 ↔ YAML「蚊口」を吸収")
    lines.append("- 注: 曽山寺は yaml 側で除外済み（木崎浜・青島と GFS 同一グリッド扱い）のため yaml 値なし")
    lines.append("")
    lines.append("## 突合結果")
    lines.append("")
    lines.append(
        "| ポイント | OSM推定方位(deg) | OSM推定(16方位) | 補助推定(deg) | 補助推定(16方位) | 現状yaml | 差分(度) | 補助差分(度) | 種別 | 判定 |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        name = r["name_yaml"]
        ptype = r.get("point_type", "外洋")
        if r["fetch_error"] and r["estimated_deg"] is None:
            lines.append(
                f"| {name} | - | - | - | - | {r['yaml_dir_label']} | - | - | {ptype} | 取得失敗 ({r['fetch_error']}) |"
            )
            continue
        deg_str = f"{r['estimated_deg']:.1f}" if r["estimated_deg"] is not None else "-"
        diff_str = f"{r['diff_deg']:.1f}" if r["diff_deg"] is not None else "-"
        aux_deg_str = f"{r['aux_main_way_deg']:.1f}" if r.get("aux_main_way_deg") is not None else "-"
        aux_label = r.get("aux_main_way_label") or "-"
        aux_diff_str = f"{r['aux_main_way_diff_deg']:.1f}" if r.get("aux_main_way_diff_deg") is not None else "-"
        lines.append(
            f"| {name} | {deg_str} | {r['estimated_label']} | {aux_deg_str} | {aux_label} | "
            f"{r['yaml_dir_label']} | {diff_str} | {aux_diff_str} | {ptype} | {r['verdict']} |"
        )
    lines.append("")

    # 集計
    matched = sum(1 for r in rows if r["verdict"] == "一致")
    yaml_pref = sum(1 for r in rows if r["verdict"] == "不一致だがyaml推奨")
    review = sum(1 for r in rows if r["verdict"] == "要再確認")
    failed = sum(1 for r in rows if r["verdict"] == "取得失敗")
    lines.append(f"- 一致: {matched} 件")
    lines.append(f"- 不一致だがyaml推奨（湾内/河口隣接）: {yaml_pref} 件")
    lines.append(f"- 要再確認: {review} 件")
    lines.append(f"- 取得失敗: {failed} 件")
    lines.append("")

    # 詳細
    lines.append("## 各ポイント詳細")
    lines.append("")
    for r in rows:
        lines.append(f"### {r['name_yaml']} ({r['name_json']})")
        lines.append("")
        lines.append(f"- 座標: ({r['lat']:.4f}, {r['lon']:.4f})")
        lines.append(f"- 使用半径: {r['used_radius_m']} m")
        if r["nearest_segment_distance_m"] is not None:
            lines.append(
                f"- 最寄りcoastline線分までの距離: {r['nearest_segment_distance_m']:.1f} m"
            )
        if r["nearest_segment"] is not None:
            a, b = r["nearest_segment"]
            lines.append(
                f"- 線分: ({a[0]:.5f}, {a[1]:.5f}) → ({b[0]:.5f}, {b[1]:.5f})"
            )
        if r["estimated_deg"] is not None:
            lines.append(
                f"- 推定ビーチ向き: {r['estimated_deg']:.1f}° ({r['estimated_label']})  ← 単純最近傍"
            )
        if r.get("nearest_far_warn"):
            d = r.get("nearest_segment_distance_m") or 0
            lines.append(
                f"  - ⚠ 最寄りcoastlineまで {d:.0f}m と遠い (>{NEAREST_FAR_WARN_M:.0f}m)。"
                f"座標が陸内/沖合に置かれている可能性"
            )
        if r.get("aux_main_way_deg") is not None:
            aux_diff_part = ""
            if r.get("aux_main_way_diff_deg") is not None:
                aux_diff_part = f" / yaml差 {r['aux_main_way_diff_deg']:.1f}°"
            lines.append(
                f"- 補助推定: {r['aux_main_way_deg']:.1f}° ({r['aux_main_way_label']}) "
                f"← メインway重み付き平均 / 半径{AUX_MAIN_WAY_RADIUS_M}m / {r['aux_main_way_seg_count']}線分 / "
                f"side={r.get('aux_main_way_side')}{aux_diff_part}"
            )
        if r["yaml_dir_label"]:
            lines.append(
                f"- 現状yaml: {r['yaml_dir_label']} ({r['yaml_dir_deg']}°)"
            )
        if r["diff_deg"] is not None:
            lines.append(f"- 差分: {r['diff_deg']:.1f}°")
        # 種別判定の根拠
        ptype = r.get("point_type", "外洋")
        info = r.get("type_info") or {}
        lines.append(f"- 種別: {ptype}")
        rcount = info.get("river_count", 0)
        kinds = r.get("river_kinds") or []
        if rcount > 0:
            kinds_str = ",".join(sorted(set(kinds))) if kinds else "-"
            lines.append(
                f"  - 河口検出: 半径{RIVER_RADIUS_M}mに waterway={kinds_str} を {rcount}件"
            )
        std = info.get("coastline_normal_std_deg")
        hits = info.get("raycast_land_hits")
        if std is not None:
            lines.append(f"  - 海岸線法線方位の循環標準偏差: {std:.1f}° (閾値 {BAY_STD_THRESHOLD_DEG}°)")
        if hits is not None:
            lines.append(
                f"  - 8方位レイキャスト陸ヒット: {hits}/8 (閾値 {RAYCAST_LAND_HITS_FOR_BAY}方位)"
            )
        lines.append(f"- 判定: {r['verdict']}")
        if r["fetch_error"]:
            lines.append(f"- 備考: {r['fetch_error']}")
        lines.append("")

    # 手法
    lines.append("## 手法")
    lines.append("")
    lines.append(f"1. 各ポイント (lat, lon) を中心に半径 {DEFAULT_RADIUS_M} m で")
    lines.append("   OSM Overpass API へ `natural=coastline` の way を問い合わせ")
    lines.append(f"2. ヒット 0 件なら半径 {FALLBACK_RADIUS_M} m に拡張して再取得")
    lines.append("3. 取得した way の geometry を線分に分解し、")
    lines.append("   ポイントへの最短距離を持つ線分を特定")
    lines.append("4. 線分の bearing(始点→終点) を計算し、OSM 慣習")
    lines.append("   「coastline の進行方向右手側が海」に従って +90° 方向を海側=ビーチ向きとする")
    lines.append("   （ポイントが線分の右手側にあった場合は way 方向が逆向きに引かれているとみなし -90°）")
    lines.append("5. 算出した方位を 11.25° 刻みの 16方位日本語に丸めて yaml と比較")
    lines.append("")
    lines.append("- 一致判定: 同じ16方位ラベル または 差分が 22.5° 以内")
    lines.append("- 不一致だがyaml推奨: 種別が「湾内」または「河口隣接」かつ差分 22.5° 超")
    lines.append("- 要再確認: 種別が「外洋」かつ差分 22.5° 超")
    lines.append("")
    lines.append("### 補助推定（メインway重み付き平均）")
    lines.append("")
    lines.append("単純最近傍方式は、岬の弧地形や離岸礁/池の輪郭を拾うと不安定。")
    lines.append("補助メトリクスとして以下を併記する:")
    lines.append("")
    lines.append("1. 中心から最も近い *open coastline way*（始点!=終点。離岸礁/湖などの閉路を除外）を1本選定")
    lines.append("2. その way 上で中心から半径 500m 以内の線分の方位を集める")
    lines.append("3. 「P が最近傍線分の左/右どちらにあるか」を **最近傍線分1本だけで** 判定し、")
    lines.append("   その +90 / -90 を way 全体の線分に一律適用（弧地形で左右が交互に出るのを防止）")
    lines.append("4. 距離逆数を重みにした円形平均で集約")
    lines.append("")
    lines.append("単純最近傍値と大きく食い違う場合は、座標がビーチライン上に置けていない")
    lines.append("（沖合/池の縁/河口の真ん中など）か、周辺地形が複雑であることを示唆する。")
    lines.append("")
    lines.append(f"- 最寄りcoastlineまでの距離が {NEAREST_FAR_WARN_M:.0f}m を超える場合は警告フラグを立てる")
    lines.append("")
    lines.append("### 河口/湾内検出ロジック")
    lines.append("")
    lines.append(f"- 河口検出: ポイント中心から半径 {RIVER_RADIUS_M} m 以内に")
    lines.append("  `waterway=river` または `waterway=stream` の way があれば「河口隣接」")
    lines.append(f"- 湾内検出A: ポイント中心から半径 {BAY_RADIUS_M} m 以内の coastline")
    lines.append("  線分を集めて法線方位（=ビーチ向き候補）の循環標準偏差を計算。")
    lines.append(f"  {BAY_STD_THRESHOLD_DEG}° を超えれば「湾内」と判定")
    lines.append(f"- 湾内検出B: 中心から 8方位（0/45/90/.../315°）に {RAYCAST_DISTANCE_M} m")
    lines.append("  飛ばし、各レイが coastline 線分と交差するかを判定。")
    lines.append(f"  {RAYCAST_LAND_HITS_FOR_BAY} 方位以上で陸（=交差あり）と判定されれば")
    lines.append("  ガッツリ湾内とみなす（A,B いずれかで True なら湾内扱い）")
    lines.append("")
    lines.append("### Overpass API クエリ全文")
    lines.append("")
    lines.append("```overpassql")
    lines.append("[out:json][timeout:25];")
    lines.append('(way["natural"="coastline"](around:RADIUS,LAT,LON););')
    lines.append("out geom;")
    lines.append("```")
    lines.append("")
    lines.append("河口検出用:")
    lines.append("")
    lines.append("```overpassql")
    lines.append("[out:json][timeout:25];")
    lines.append('(way["waterway"~"^(river|stream)$"](around:500,LAT,LON););')
    lines.append("out tags;")
    lines.append("```")
    lines.append("")
    lines.append(f"- エンドポイント: `{OVERPASS_URL}` (POST `data=...`)")
    lines.append(f"- HTTP タイムアウト: {OVERPASS_TIMEOUT}s")
    lines.append(f"- レート制御: 連続リクエスト間 {SLEEP_BETWEEN_REQUESTS_S}s スリープ、並列なし")
    lines.append("")

    # 既知の限界
    lines.append("## 既知の限界")
    lines.append("")
    lines.append("- coastline 方位はあくまで「砂浜が向いている平均方位」であって、")
    lines.append("  実際のサーフポイントの「ベスト入射方向」とは必ずしも一致しない。")
    lines.append("- ポイントブレイク（岩礁・岬で割れるリーフ／ロックブレイク）では、")
    lines.append("  リーフの向きや水深変化が支配的で、海岸線方位と20〜45°ズレることが普通。")
    lines.append("- 湾内ポイント（伊勢ヶ浜など）では、湾外うねりが回り込んで入るため、")
    lines.append("  「海岸線が向いている方向」とは異なる方角からのうねりにも反応する。")
    lines.append("- 河口ポイント（蚊口浜など）は河川の堆砂で年単位に bottom が変わり、")
    lines.append("  ビーチ向きも経年変化する可能性がある。")
    lines.append("- OSM の海岸線データの精度・更新頻度に依存する。岬の細かい屈曲は")
    lines.append("  way が粗く描かれている可能性があり、最寄り線分が実際の波打ち際と")
    lines.append("  少し違う角度を持つことがある。")
    lines.append("- 取得半径が大きいほど別ポイントの線分を拾うリスクが上がる。")
    lines.append(f"  本スクリプトでは {DEFAULT_RADIUS_M}m → {FALLBACK_RADIUS_M}m の段階拡張で抑制している。")
    lines.append("")
    lines.append("## 運用上の解釈ヒント")
    lines.append("")
    lines.append("- 差分 22.5° 以内: 共通スコアリング（うねり一致＝ビーチ向き）で問題なし")
    lines.append("- 差分 22.5°〜45°: ビーチが斜めに延びている可能性。yaml の値はヒアリング由来なので")
    lines.append("  そのまま採用しつつ、`exceptional_swell` で OSM 推定方位寄りも加点する手も検討")
    lines.append("- 差分 45° 超: 湾内 / ポイントブレイク / 岬影 等の地形効果が強いケース。")
    lines.append("  yaml 側の値（地元ヒアリング）を信頼するのが基本。OSM 値は参考値。")

    REPORT_MD.parent.mkdir(parents=True, exist_ok=True)
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
