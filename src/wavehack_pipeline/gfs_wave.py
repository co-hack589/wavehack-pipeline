"""NOAA GFS Wave (gfswave.global.0p16) からポイント別予報を取得する。

データソース: AWS Open Data S3 (NODD) 上の GRIB2
ライセンス : NOAA パブリックドメイン（商用利用OK・帰属表示推奨）

19変数（HTSGW / PERPW / DIRPW / WVHGT / WVPER / WVDIR / WIND / WDIR / UGRD / VGRD
+ うねり3成分 SWELL / SWPER / SWDIR）を取得。
fhour 0〜120 が 1時間刻み、それ以降は 3時間刻み。
"""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
import xarray as xr
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .points import Point

JST = timezone(timedelta(hours=9))
UTC = timezone.utc

# プライマリ: AWS Open Data S3 (NODD)
S3_BASE = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
# フォールバック: NOAA NCEP NOMADS
NOMADS_BASE = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod"
GRID = "global.0p16"

# requests のデフォルトタイムアウト: (connect, read)
DEFAULT_CONNECT_TIMEOUT = 15.0
DEFAULT_READ_TIMEOUT = 120.0
DEFAULT_TIMEOUT: tuple[float, float] = (DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT)

# Retry 設定: max=3 / backoff_factor=2 / 502,503,504,429 で再試行
_RETRY = Retry(
    total=3,
    connect=3,
    read=3,
    status=3,
    backoff_factor=2.0,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(("HEAD", "GET")),
    raise_on_status=False,
)

# requests.Session はスレッドセーフだが、スレッドごとに 1 つ持つほうが安定する
_thread_local = threading.local()


def _session() -> requests.Session:
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = requests.Session()
        adapter = HTTPAdapter(max_retries=_RETRY, pool_connections=8, pool_maxsize=8)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _thread_local.session = s
    return s

# cfgrib では surface/instant の10変数と orderedSequenceData の3変数 (×3 partition)
# を別 dataset として開く必要がある。両方合わせて idx の19レコードと一致。
SURFACE_FILTER = {"typeOfLevel": "surface", "stepType": "instant"}
SWELL_FILTER = {"typeOfLevel": "orderedSequenceData"}

# 陸地マスク回避：最近傍が陸（NaN）の場合に試すオフセット (dlat, dlon)。
# 宮崎沿岸は全て東向きなので「東に1グリッド」を最優先。0.16°=約18km。
LANDMASK_OFFSETS: list[tuple[float, float]] = [
    (0.0, 0.0),
    (0.0, 0.17),    # 東 1
    (0.17, 0.17),   # 北東
    (-0.17, 0.17),  # 南東
    (0.0, 0.33),    # 東 2
    (0.17, 0.0),    # 北
    (-0.17, 0.0),   # 南
    (0.0, 0.50),    # 東 3
]


@dataclass(frozen=True)
class GfsWaveSlot:
    timestamp_utc: str
    timestamp_jst: str
    fhour: int
    htsgw_m: float | None
    perpw_s: float | None
    dirpw_deg: float | None
    wvhgt_m: float | None
    wvper_s: float | None
    wvdir_deg: float | None
    swell1_m: float | None
    swell2_m: float | None
    swell3_m: float | None
    swper1_s: float | None
    swper2_s: float | None
    swper3_s: float | None
    swdir1_deg: float | None
    swdir2_deg: float | None
    swdir3_deg: float | None
    wind_ms: float | None
    wdir_deg: float | None
    ugrd_ms: float | None
    vgrd_ms: float | None


@dataclass(frozen=True)
class GfsForecast:
    point_name: str
    point_lat: float
    point_lon: float
    grid_lat: float
    grid_lon: float
    run_base_utc: str
    slots: list[GfsWaveSlot]


def _grib_url(run: datetime, fhour: int, *, source: str = "s3") -> str:
    """source: 's3' (AWS NODD) or 'nomads' (NCEP NOMADS)."""
    date_s = run.strftime("%Y%m%d")
    hh = f"{run.hour:02d}"
    fname = f"gfswave.t{hh}z.{GRID}.f{fhour:03d}.grib2"
    if source == "nomads":
        return f"{NOMADS_BASE}/gfs.{date_s}/{hh}/wave/gridded/{fname}"
    return f"{S3_BASE}/gfs.{date_s}/{hh}/wave/gridded/{fname}"


def latest_run_base(*, max_lookback: int = 8, timeout: float = 15.0) -> datetime:
    """利用可能な最新 GFS Wave run を S3 HEAD で判定して UTC datetime を返す。

    S3 で見つからない場合は NOMADS にもフォールバック。
    """
    now = datetime.now(UTC)
    aligned_h = (now.hour // 6) * 6
    base = now.replace(hour=aligned_h, minute=0, second=0, microsecond=0)
    sess = _session()
    for i in range(max_lookback):
        cand = base - timedelta(hours=6 * i)
        for source in ("s3", "nomads"):
            try:
                resp = sess.head(
                    _grib_url(cand, 0, source=source),
                    timeout=(DEFAULT_CONNECT_TIMEOUT, timeout),
                    allow_redirects=True,
                )
                if resp.status_code == 200:
                    return cand
            except requests.RequestException:
                continue
    raise RuntimeError(f"No GFS Wave run found in last {max_lookback * 6}h")


def _download_from(
    url: str,
    out: Path,
    *,
    timeout: tuple[float, float] = DEFAULT_TIMEOUT,
) -> None:
    """1 つの URL からダウンロードして out に書き出す（ストリーミング）。"""
    sess = _session()
    with sess.get(url, timeout=timeout, stream=True) as r:
        r.raise_for_status()
        tmp = out.with_suffix(out.suffix + ".part")
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
        tmp.rename(out)


def _download(
    run: datetime,
    fhour: int,
    dest_dir: Path,
    *,
    timeout: tuple[float, float] = DEFAULT_TIMEOUT,
    max_attempts: int = 3,
    progress: bool = False,
) -> Path:
    """grib2 を取得。S3 → NOMADS の順でフォールバックし、各ソースで max_attempts まで再試行。

    HTTPAdapter の Retry が低レベルの 5xx/接続エラーを吸収する。
    ここでは Read timeout など Retry が拾えない例外をアプリ層でリトライする。
    """
    out = dest_dir / f"f{fhour:03d}.grib2"
    if out.exists() and out.stat().st_size > 0:
        return out

    sources = ("s3", "nomads")
    last_err: Exception | None = None
    for source in sources:
        url = _grib_url(run, fhour, source=source)
        for attempt in range(1, max_attempts + 1):
            try:
                _download_from(url, out, timeout=timeout)
                if progress and (source != "s3" or attempt > 1):
                    print(
                        f"    f{fhour:03d}: ok via {source} (attempt {attempt})",
                        flush=True,
                    )
                return out
            except (requests.exceptions.RequestException, OSError) as e:
                last_err = e
                wait = min(2 ** attempt, 30)
                if progress:
                    print(
                        f"    f{fhour:03d}: {source} attempt {attempt}/{max_attempts} failed "
                        f"({type(e).__name__}: {e}); retry in {wait}s",
                        flush=True,
                    )
                # 部分ファイル掃除
                part = out.with_suffix(out.suffix + ".part")
                if part.exists():
                    try:
                        part.unlink()
                    except OSError:
                        pass
                if attempt < max_attempts:
                    time.sleep(wait)
        if progress:
            print(
                f"    f{fhour:03d}: {source} exhausted, falling back to next source",
                flush=True,
            )
    raise RuntimeError(
        f"download failed for f{fhour:03d} after S3+NOMADS retries: {last_err}"
    )


def _open_two_datasets(grib_path: Path) -> tuple[xr.Dataset, xr.Dataset]:
    ds_surface = xr.open_dataset(
        grib_path,
        engine="cfgrib",
        backend_kwargs={"filter_by_keys": SURFACE_FILTER, "indexpath": ""},
    )
    ds_swell = xr.open_dataset(
        grib_path,
        engine="cfgrib",
        backend_kwargs={"filter_by_keys": SWELL_FILTER, "indexpath": ""},
    )
    return ds_surface, ds_swell


def _safe_float(da: xr.DataArray) -> float | None:
    v = float(da.values)
    return None if v != v else round(v, 3)


def resolve_ocean_grid(
    ds_surface: xr.Dataset, lat: float, lon: float
) -> tuple[float, float]:
    """陸地マスクを避けて使う grid (lat, lon) を決める。

    各オフセット候補で最近傍点を試し、HTSGW (swh) が NaN でない最初のグリッドを採用。
    全部 NaN なら最近傍点を返す（後段で None として記録される）。
    """
    seen: set[tuple[float, float]] = set()
    fallback: tuple[float, float] | None = None
    for dlat, dlon in LANDMASK_OFFSETS:
        pt = ds_surface.sel(latitude=lat + dlat, longitude=lon + dlon, method="nearest")
        glat = round(float(pt.latitude), 4)
        glon = round(float(pt.longitude), 4)
        if (glat, glon) in seen:
            continue
        seen.add((glat, glon))
        if fallback is None:
            fallback = (glat, glon)
        v = float(pt.swh.values)
        if v == v:  # not NaN
            return glat, glon
    return fallback if fallback is not None else (lat, lon)


# cfgrib の変数名 (shortName) は eccodes のバージョンで変わる。
# 例: うねり有義波高は eccodes 2.4x → "shts" / 2.28 (Debian bookworm) → "swell"。
# 候補を順に探して最初に存在する名前を使う。
_SWELL_VAR_CANDIDATES: dict[str, tuple[str, ...]] = {
    "height": ("shts", "swell"),
    "period": ("mpts", "swper"),
    "direction": ("swdir", "dirsw"),
}


def _pick_var(ds: xr.Dataset, candidates: tuple[str, ...]) -> str:
    for name in candidates:
        if name in ds.data_vars:
            return name
    raise KeyError(
        f"none of {candidates} found in dataset vars: {list(ds.data_vars)}"
    )


def _extract_at_grid(
    ds_surface: xr.Dataset, ds_swell: xr.Dataset, glat: float, glon: float
) -> dict[str, float | None]:
    pt_s = ds_surface.sel(latitude=glat, longitude=glon, method="nearest")
    pt_w = ds_swell.sel(latitude=glat, longitude=glon, method="nearest")
    v_height = _pick_var(ds_swell, _SWELL_VAR_CANDIDATES["height"])
    v_period = _pick_var(ds_swell, _SWELL_VAR_CANDIDATES["period"])
    v_direction = _pick_var(ds_swell, _SWELL_VAR_CANDIDATES["direction"])

    vals: dict[str, float | None] = {
        "htsgw_m": _safe_float(pt_s.swh),
        "perpw_s": _safe_float(pt_s.perpw),
        "dirpw_deg": _safe_float(pt_s.dirpw),
        "wvhgt_m": _safe_float(pt_s.shww),
        "wvper_s": _safe_float(pt_s.mpww),
        "wvdir_deg": _safe_float(pt_s.wvdir),
        "wind_ms": _safe_float(pt_s.ws),
        "wdir_deg": _safe_float(pt_s.wdir),
        "ugrd_ms": _safe_float(pt_s.u),
        "vgrd_ms": _safe_float(pt_s.v),
    }
    for i in (1, 2, 3):
        slc = pt_w.sel(orderedSequenceData=i)
        vals[f"swell{i}_m"] = _safe_float(slc[v_height])
        vals[f"swper{i}_s"] = _safe_float(slc[v_period])
        vals[f"swdir{i}_deg"] = _safe_float(slc[v_direction])
    return vals


def fetch_forecast(
    points: Iterable[Point],
    *,
    run: datetime | None = None,
    fhours: Iterable[int] | None = None,
    max_workers: int = 8,
    cache_dir: Path | None = None,
    progress: bool = False,
) -> dict[str, GfsForecast]:
    """指定ポイント全部について GFS Wave 予報を取得する。

    fhours: 取得する forecast hour のリスト。Noneなら 0..80 (1時間刻み)。
    cache_dir: grib2 を保存するディレクトリ。Noneなら一時ディレクトリを使い終了時に削除。
    """
    if run is None:
        run = latest_run_base()
    fhour_list = list(fhours) if fhours is not None else list(range(0, 81))
    points_list = list(points)

    cleanup = False
    if cache_dir is None:
        cache_dir = Path(tempfile.mkdtemp(prefix="gfs-wave-"))
        cleanup = True
    cache_dir.mkdir(parents=True, exist_ok=True)

    rows: dict[str, dict[int, dict[str, float | None]]] = {p.name: {} for p in points_list}

    # 1) 最初に f0 だけ DL → 各ポイントの「使うグリッド点」を陸地マスク回避で決定
    f0_path = _download(run, fhour_list[0], cache_dir, progress=progress)
    ds_s0, ds_w0 = _open_two_datasets(f0_path)
    try:
        grids: dict[str, tuple[float, float]] = {
            p.name: resolve_ocean_grid(ds_s0, p.latitude, p.longitude) for p in points_list
        }
    finally:
        ds_s0.close()
        ds_w0.close()

    if progress:
        for p in points_list:
            glat, glon = grids[p.name]
            shifted = (glat, glon) != (
                round(p.latitude, 4),
                round(p.longitude, 4),
            )
            tag = " (shifted to ocean)" if shifted else ""
            print(f"  point {p.name}: grid ({glat:.3f}, {glon:.3f}){tag}", flush=True)

    def work(fh: int) -> tuple[int, dict[str, dict[str, float | None]]]:
        path = _download(run, fh, cache_dir, progress=progress)
        ds_s, ds_w = _open_two_datasets(path)
        try:
            per_point: dict[str, dict[str, float | None]] = {}
            for p in points_list:
                glat, glon = grids[p.name]
                per_point[p.name] = _extract_at_grid(ds_s, ds_w, glat, glon)
            return fh, per_point
        finally:
            ds_s.close()
            ds_w.close()

    completed = 0
    failed_fhours: list[int] = []
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(work, fh): fh for fh in fhour_list}
            for fut in as_completed(futures):
                fh = futures[fut]
                try:
                    fh_done, per_point = fut.result()
                    for name, vals in per_point.items():
                        rows[name][fh_done] = vals
                except Exception as e:
                    failed_fhours.append(fh)
                    if progress:
                        print(
                            f"  FAILED f{fh:03d}: {type(e).__name__}: {e}",
                            flush=True,
                        )
                    # 欠損として None スロットを記録（後段で None 入り slot として残す）
                    empty: dict[str, float | None] = {
                        k: None
                        for k in (
                            "htsgw_m", "perpw_s", "dirpw_deg",
                            "wvhgt_m", "wvper_s", "wvdir_deg",
                            "swell1_m", "swell2_m", "swell3_m",
                            "swper1_s", "swper2_s", "swper3_s",
                            "swdir1_deg", "swdir2_deg", "swdir3_deg",
                            "wind_ms", "wdir_deg", "ugrd_ms", "vgrd_ms",
                        )
                    }
                    for p in points_list:
                        rows[p.name][fh] = empty
                completed += 1
                if progress:
                    print(
                        f"  fetched f{fh:03d} ({completed}/{len(fhour_list)})",
                        flush=True,
                    )
        if failed_fhours and progress:
            print(
                f"\n  WARN: {len(failed_fhours)} slot(s) failed after all retries: "
                f"{sorted(failed_fhours)}",
                flush=True,
            )
    finally:
        if cleanup:
            shutil.rmtree(cache_dir, ignore_errors=True)

    result: dict[str, GfsForecast] = {}
    for p in points_list:
        slots: list[GfsWaveSlot] = []
        for fh in fhour_list:
            ts_utc = run + timedelta(hours=fh)
            ts_jst = ts_utc.astimezone(JST)
            v = rows[p.name][fh]
            slots.append(
                GfsWaveSlot(
                    timestamp_utc=ts_utc.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                    timestamp_jst=ts_jst.strftime("%Y-%m-%dT%H:%M:%S+09:00"),
                    fhour=fh,
                    **v,
                )
            )
        glat, glon = grids[p.name]
        result[p.name] = GfsForecast(
            point_name=p.name,
            point_lat=p.latitude,
            point_lon=p.longitude,
            grid_lat=glat,
            grid_lon=glon,
            run_base_utc=run.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            slots=slots,
        )
    return result


def to_dict(forecast: GfsForecast) -> dict[str, Any]:
    return asdict(forecast)
