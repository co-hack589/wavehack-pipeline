"""GFS Wave 予報値 + scoring_rules.yaml からポイント別スコアと言語化コメントを生成する。

スコアリングルール v0.3 (2026-06-21): ヒアリング第2弾フィールドを統合。
- うねり: ビーチ向きと一致するほど高得点
- 風  : ビーチ向き正反対=オフショアが満点、オンショアが0、弱風は底上げ
- 潮  : 動いている途中（特に上げ）が高得点
- サイズ: HTSGW と周期で評価。preferred_size_m / size_attenuation を反映
- 季節: best 月で +0.1 / off 月で -0.1 を composite に加算
- 強風ハードカット: wind_ms > max_wind_ms なら composite=0（安全側）

全派生機能は scoring.features.* で個別 ON/OFF 可能。yaml フィールドが
未定義 (null / 空) のポイントは自動でスキップし、後方互換を保つ。

潮位は GFS Wave に含まれないため Phase A-4 で別ソース（気象庁）から補完予定。
それまでは tide_phase=None のスロットでは tide_score=0.5 固定。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml

TidePhase = Literal[
    "mid_flood", "high_to_ebb", "low_to_flood", "mid_ebb", "high_slack", "low_slack"
]


@dataclass(frozen=True)
class ScoreBreakdown:
    swell: float
    wind: float
    tide: float
    size: float
    composite: float
    rank: str  # great / good / fair / poor
    # v0.3 追加: 派生フィールドの寄与（透明性のため breakdown 保持）
    seasonality_adj: float = 0.0       # composite に加算された季節補正 (±0.1 程度)
    effective_htsgw_m: float | None = None  # size_attenuation 適用後の実効サイズ
    wind_hard_cut: bool = False        # max_wind_ms ハードカットが発動したか
    surf_gate: float = 1.0             # v0.4: サーファビリティゲート（乗算係数）


# ============================================================
# yaml ロード
# ============================================================
class ScoringRules:
    def __init__(self, path: Path):
        self._raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.beach_direction_deg: dict[str, float] = self._raw["beach_direction_deg"]
        self.scoring: dict[str, Any] = self._raw["scoring"]
        self.points: dict[str, dict[str, Any]] = {p["name"]: p for p in self._raw["points"]}
        self.comments: dict[str, list[str]] = self._raw["comments"]
        # 表示・ラベル系セクション（v0.3 追加）
        self.variable_labels: dict[str, dict[str, Any]] = self._raw.get("variable_labels", {})
        self.direction_labels_16: list[dict[str, Any]] = self._raw.get("direction_labels_16", [])
        self.tide_phase_labels: dict[str, dict[str, str]] = self._raw.get("tide_phase_labels", {})
        self.forecast_range: dict[str, Any] = self._raw.get("forecast_range", {})

    @classmethod
    def default(cls) -> "ScoringRules":
        # __file__ = .../wavehack/output/code/wavehack-pipeline/src/wavehack_pipeline/scoring.py
        # parents[4] = .../wavehack/output
        return cls(Path(__file__).resolve().parents[4] / "data" / "scoring_rules.yaml")

    def beach_dir_for(self, point_name: str) -> float | None:
        p = self.points.get(point_name)
        if p is None:
            return None
        return self.beach_direction_deg.get(p["beach_direction"])

    # ------------------------------------------------------------
    # 表示用ヘルパー（v0.3）
    # ------------------------------------------------------------
    def label_for_variable(self, key: str) -> dict[str, Any]:
        """variable_labels から1変数の辞書を返す。未登録なら空dict。"""
        return self.variable_labels.get(key, {})

    def label_for_direction_deg(self, deg: float) -> dict[str, str]:
        """0..360 の角度を 16方位ラベルに変換。境界は yaml の direction_labels_16 を使う。
        返り値は {ja: "東", en: "E"}。未マッチなら {ja: "", en: ""}。"""
        a = deg % 360.0
        for row in self.direction_labels_16:
            f = float(row["from_deg"]) % 360.0
            t = float(row["to_deg"]) % 360.0
            # in_angle_range と同じロジック（from を含み to を含まない half-open）
            if f <= t:
                if f <= a < t:
                    return {"ja": row["ja"], "en": row["en"]}
            else:
                if a >= f or a < t:
                    return {"ja": row["ja"], "en": row["en"]}
        return {"ja": "", "en": ""}

    def is_extended_fhour(self, fhour: int) -> bool:
        """forecast_range.reliable_until_fhour を超えるなら参考値レンジ。"""
        threshold = int(self.forecast_range.get("reliable_until_fhour", 120))
        return fhour > threshold


# ============================================================
# 角度ユーティリティ
# ============================================================
def angle_diff(a: float, b: float) -> float:
    """0..180 に正規化した角度差。"""
    d = abs((a - b) % 360.0)
    return d if d <= 180.0 else 360.0 - d


def in_angle_range(angle: float, from_deg: float, to_deg: float) -> bool:
    """from_deg → to_deg（時計回り）の範囲内なら True。"""
    a = angle % 360.0
    f = from_deg % 360.0
    t = to_deg % 360.0
    if f <= t:
        return f <= a <= t
    return a >= f or a <= t


def piecewise_linear(x: float, curve: list[dict[str, float]], x_key: str, y_key: str) -> float:
    """昇順カーブ（x_key 昇順）で線形補間。範囲外は端の値で固定。"""
    if x <= curve[0][x_key]:
        return curve[0][y_key]
    if x >= curve[-1][x_key]:
        return curve[-1][y_key]
    for lo, hi in zip(curve, curve[1:]):
        if lo[x_key] <= x <= hi[x_key]:
            t = (x - lo[x_key]) / (hi[x_key] - lo[x_key])
            return lo[y_key] + t * (hi[y_key] - lo[y_key])
    return curve[-1][y_key]


# ============================================================
# ファクタースコア
# ============================================================
def swell_score(
    *,
    swell_dirs_deg: list[float | None],
    swell_heights_m: list[float | None],
    htsgw_m: float | None,
    beach_dir_deg: float,
    rules: ScoringRules,
    point_overrides: dict[str, Any] | None = None,
) -> float:
    """うねり3成分のうち最大スコア成分で評価し、合成有義波高の閾値で減衰補正する。"""
    cfg = rules.scoring["swell"]
    curve = cfg["direction_curve"]

    point_overrides = point_overrides or {}
    exceptional = point_overrides.get("exceptional_swell") or []
    no_swell = point_overrides.get("exceptional_no_swell") or []

    candidate_scores: list[float] = []
    for dir_deg, h_m in zip(swell_dirs_deg, swell_heights_m):
        if dir_deg is None or h_m is None or h_m < 0.05:
            continue

        # 基本: ビーチ向きとの角度差カーブ
        base = piecewise_linear(angle_diff(dir_deg, beach_dir_deg), curve, "diff_deg", "score")

        # 例外: 反応する角度範囲が指定されていれば、その範囲内で base を底上げ
        for ex in exceptional:
            if (
                ex.get("from_deg") is not None
                and ex.get("to_deg") is not None
                and in_angle_range(dir_deg, ex["from_deg"], ex["to_deg"])
            ):
                base = max(base, 0.85)

        # 入らない範囲: 角度的には合っても物理的に届かない場合は減衰
        for ns in no_swell:
            if (
                ns.get("from_deg") is not None
                and ns.get("to_deg") is not None
                and in_angle_range(dir_deg, ns["from_deg"], ns["to_deg"])
            ):
                base *= 0.2

        candidate_scores.append(base)

    if not candidate_scores:
        return 0.0
    score = max(candidate_scores) if cfg.get("use_top_score_among_swells", True) else sum(candidate_scores) / len(candidate_scores)

    # ノーサーフ判定
    threshold = cfg.get("no_surf_height_threshold_m", 0.3)
    multiplier = cfg.get("no_surf_score_multiplier", 0.5)
    if htsgw_m is None or htsgw_m < threshold:
        score *= multiplier

    return max(0.0, min(1.0, score))


def wind_score(
    *,
    wind_dir_deg: float | None,
    wind_speed_ms: float | None,
    beach_dir_deg: float,
    rules: ScoringRules,
    point_overrides: dict[str, Any] | None = None,
) -> float:
    """オフショア=ビーチ向き+180°との角度差で方向スコア、風速で減衰。"""
    if wind_dir_deg is None or wind_speed_ms is None:
        return 0.5  # 不明時は中点

    cfg = rules.scoring["wind"]
    curve = cfg["direction_curve"]
    point_overrides = point_overrides or {}

    offshore = (beach_dir_deg + 180.0) % 360.0
    diff = angle_diff(wind_dir_deg, offshore)
    dir_score = piecewise_linear(diff, curve, "diff_deg", "score")

    # ポイント別 override: 特定の風向き範囲を別スコアで底上げ（例: 蚊口の北風）
    for ovr in point_overrides.get("wind_direction_overrides") or []:
        if (
            ovr.get("from_deg") is not None
            and ovr.get("to_deg") is not None
            and in_angle_range(wind_dir_deg, ovr["from_deg"], ovr["to_deg"])
        ):
            dir_score = max(dir_score, float(ovr.get("score", dir_score)))
            break

    # 弱風底上げ
    weak_threshold = cfg.get("weak_threshold_ms", 3.0)
    weak_floor = cfg.get("weak_floor_score", 0.6)
    if wind_speed_ms <= weak_threshold:
        dir_score = max(dir_score, weak_floor)

    # 強風減衰
    max_wind = point_overrides.get("max_wind_ms") or cfg.get("default_max_wind_ms", 8.0)
    limit_wind = point_overrides.get("limit_wind_ms") or cfg.get("default_limit_wind_ms", 14.0)
    if wind_speed_ms > max_wind:
        if wind_speed_ms >= limit_wind:
            return 0.0
        # max..limit で線形にスコアを減衰
        t = (wind_speed_ms - max_wind) / (limit_wind - max_wind)
        dir_score *= 1.0 - t

    return max(0.0, min(1.0, dir_score))


def tide_score(
    *,
    tide_phase: TidePhase | None,
    rules: ScoringRules,
    point_overrides: dict[str, Any] | None = None,
) -> float:
    """潮の動きフェーズで評価。tide_phase=None なら 0.5 固定（A-4 で潮位取得後に有効化）。"""
    if tide_phase is None:
        return 0.5
    table = rules.scoring["tide"]["phase_score"]
    point_overrides = point_overrides or {}
    # ポイント別 override で完全に上書きする場合は別途実装（v0.2では未対応）
    return float(table.get(tide_phase, 0.5))


def _features(rules: ScoringRules) -> dict[str, bool]:
    """scoring.features の辞書（未定義キーは True デフォルト）。"""
    return rules.scoring.get("features", {}) or {}


def _defaults(rules: ScoringRules) -> dict[str, Any]:
    return rules.scoring.get("defaults", {}) or {}


def apply_size_attenuation(
    htsgw_m: float | None,
    point_overrides: dict[str, Any] | None,
    rules: ScoringRules,
) -> tuple[float | None, float]:
    """ポイント別 size_attenuation を htsgw_m に適用し (effective_h, coef) を返す。

    coef はそのまま乗算した係数。features 無効 or 値未定義の場合は (元値, 1.0)。
    """
    if htsgw_m is None:
        return None, 1.0
    if not _features(rules).get("size_attenuation_enabled", True):
        return htsgw_m, 1.0
    overrides = point_overrides or {}
    coef = overrides.get("size_attenuation")
    if coef is None:
        return htsgw_m, 1.0
    try:
        coef_f = float(coef)
    except (TypeError, ValueError):
        return htsgw_m, 1.0
    return htsgw_m * coef_f, coef_f


def preferred_size_score(htsgw_m: float | None, point_overrides: dict[str, Any] | None) -> float | None:
    """preferred_size_m=[min, max] が指定されていれば台形カーブで評価。

    範囲内: 1.0
    min より小: 0..min を 0.0..1.0 で線形
    max より大: max..(max*1.5) を 1.0..0.0 で線形（極端な大波もペナルティ）
    範囲外（min*0.3 未満や max*1.5 超）: 0.0
    未定義: None を返す（呼び出し側で従来 size_score にフォールバック）
    """
    if htsgw_m is None:
        return 0.0
    overrides = point_overrides or {}
    pref = overrides.get("preferred_size_m")
    if not pref or len(pref) != 2:
        return None
    try:
        lo = float(pref[0])
        hi = float(pref[1])
    except (TypeError, ValueError):
        return None
    if lo <= 0 or hi <= lo:
        return None
    h = float(htsgw_m)
    if lo <= h <= hi:
        return 1.0
    if h < lo:
        return max(0.0, min(1.0, h / lo))
    # h > hi
    upper = hi * 1.5
    if h >= upper:
        return 0.0
    return max(0.0, min(1.0, 1.0 - (h - hi) / (upper - hi)))


def size_score(
    *,
    htsgw_m: float | None,
    period_s: float | None,
    rules: ScoringRules,
    point_overrides: dict[str, Any] | None = None,
    effective_htsgw_m: float | None = None,
) -> float:
    """サイズ + 周期。

    優先順位:
      1. features.preferred_size_m_enabled=True かつ preferred_size_m 定義あり
         → preferred_size_score を採用（周期は wp 重みでブレンド）
      2. それ以外 → 従来の height_curve / period_curve カーブ
    effective_htsgw_m が渡されれば apply_size_attenuation 適用後の値を使う。
    """
    if htsgw_m is None or period_s is None:
        return 0.0
    eff_h = effective_htsgw_m if effective_htsgw_m is not None else htsgw_m
    cfg = rules.scoring["size"]
    wh = cfg.get("weight_height", 0.7)
    wp = cfg.get("weight_period", 0.3)
    p = piecewise_linear(period_s, cfg["period_curve"], "period_s", "score")

    if _features(rules).get("preferred_size_m_enabled", True):
        pref_h = preferred_size_score(eff_h, point_overrides)
        if pref_h is not None:
            return max(0.0, min(1.0, pref_h * wh + p * wp))

    h = piecewise_linear(eff_h, cfg["height_curve"], "height_m", "score")
    return max(0.0, min(1.0, h * wh + p * wp))


def seasonality_adjustment(
    *,
    month: int | None,
    point_overrides: dict[str, Any] | None,
    rules: ScoringRules,
) -> float:
    """月（1..12）と yaml seasonality から composite への加算値を返す。

    seasonality:
      best: [9, 10, 11]   # 該当月 → +best_bonus
      off:  [7, 8]         # 該当月 → -off_penalty
    未定義 or features OFF → 0.0
    """
    if month is None:
        return 0.0
    if not _features(rules).get("seasonality_enabled", True):
        return 0.0
    overrides = point_overrides or {}
    season = overrides.get("seasonality")
    if not season or not isinstance(season, dict):
        return 0.0
    defaults = _defaults(rules)
    best_bonus = float(defaults.get("seasonality_best_bonus", 0.10))
    off_penalty = float(defaults.get("seasonality_off_penalty", 0.10))
    best = season.get("best") or []
    off = season.get("off") or []
    if month in best:
        return best_bonus
    if month in off:
        return -off_penalty
    return 0.0


def is_wind_hard_cut(
    *,
    wind_speed_ms: float | None,
    point_overrides: dict[str, Any] | None,
    rules: ScoringRules,
) -> bool:
    """max_wind_ms ハードカット判定（共通デフォルトを fallback）。

    features.max_wind_hard_cut_enabled=False のとき常に False。
    既存 wind_score の弱風→強風線形減衰とは独立した「安全カット」。
    """
    if not _features(rules).get("max_wind_hard_cut_enabled", True):
        return False
    if wind_speed_ms is None:
        return False
    overrides = point_overrides or {}
    max_w = overrides.get("max_wind_ms")
    if max_w is None:
        max_w = _defaults(rules).get("max_wind_ms")
    if max_w is None:
        return False
    try:
        return float(wind_speed_ms) > float(max_w)
    except (TypeError, ValueError):
        return False


def surfability_gate(
    *,
    effective_htsgw_m: float | None,
    period_s: float | None,
    rules: ScoringRules,
) -> float:
    """サイズ・周期の「前提条件ゲート」(v0.4)。

    向き・風・潮がどれだけ完璧でも、波が無ければサーフィンは成立しない。
    加重平均に混ぜると薄まる（サイズ0でも他満点で0.85出る）ため、
    composite 全体に乗算する係数として扱う。
    yaml `scoring.surfability` 未定義なら 1.0（無効・後方互換）。
    """
    cfg = rules.scoring.get("surfability")
    if not cfg:
        return 1.0
    h = effective_htsgw_m
    if h is None:
        return 1.0

    # 波高 → ゲート値の折れ線（区分線形）。先頭閾値未満は 0 からの線形立ち上がり
    pts: list[list[float]] = cfg.get("height_gate") or []
    gate = 1.0
    if pts:
        x0, y0 = pts[0]
        if h <= x0:
            gate = y0 * (h / x0) if x0 > 0 else y0
        else:
            gate = pts[-1][1]
            for (ax, ay), (bx, by) in zip(pts, pts[1:]):
                if h <= bx:
                    gate = ay + (by - ay) * (h - ax) / (bx - ax)
                    break

    # 周期ペナルティ: 短周期の風波はサイズ表記ほどのパワーが無い
    factor = 1.0
    if period_s is not None:
        for th, f in cfg.get("period_factor") or []:
            if period_s < th:
                factor = f
                break

    return max(0.0, min(1.0, gate * factor))


def composite(
    *,
    swell: float,
    wind: float,
    tide: float,
    size: float,
    rules: ScoringRules,
    seasonality_adj: float = 0.0,
    wind_hard_cut: bool = False,
    effective_htsgw_m: float | None = None,
    surf_gate: float = 1.0,
) -> ScoreBreakdown:
    w = rules.scoring["weights"]
    score = swell * w["swell"] + wind * w["wind"] + tide * w["tide"] + size * w["size"]
    score += seasonality_adj
    # サイズ・周期ゲートは季節補正まで含めた全体に乗算（フラットな日は救済しない）
    score *= surf_gate
    if wind_hard_cut:
        score = 0.0
    score = max(0.0, min(1.0, score))
    th = rules.scoring["rank_thresholds"]
    if score >= th["great"]:
        rank = "great"
    elif score >= th["good"]:
        rank = "good"
    elif score >= th["fair"]:
        rank = "fair"
    else:
        rank = "poor"
    return ScoreBreakdown(
        swell=swell,
        wind=wind,
        tide=tide,
        size=size,
        composite=score,
        rank=rank,
        seasonality_adj=seasonality_adj,
        effective_htsgw_m=effective_htsgw_m,
        wind_hard_cut=wind_hard_cut,
        surf_gate=surf_gate,
    )


# ============================================================
# スロット単位のスコア計算（GFS forecast の1スロット dict を受ける）
# ============================================================
def score_slot(
    slot: dict[str, Any],
    *,
    point_name: str,
    rules: ScoringRules,
    tide_phase: TidePhase | None = None,
) -> ScoreBreakdown:
    """forecast_gfs_wave.json の slots[i] dict 形式を受けて総合スコアを返す。"""
    beach_dir = rules.beach_dir_for(point_name)
    if beach_dir is None:
        raise ValueError(f"point {point_name!r} not found or beach_direction unknown")
    overrides = rules.points.get(point_name, {})

    # サイズ減衰（大堂津の size_attenuation 等）を最初に適用
    raw_h = slot.get("htsgw_m")
    eff_h, _coef = apply_size_attenuation(raw_h, overrides, rules)

    # swell_score は「うねりが入るか」の角度評価が主なので raw_h で no-surf 判定。
    # （size_attenuation は実効サイズ評価のためのもので、ノーサーフ判定は元値で行う）
    sw = swell_score(
        swell_dirs_deg=[slot.get("swdir1_deg"), slot.get("swdir2_deg"), slot.get("swdir3_deg")],
        swell_heights_m=[slot.get("swell1_m"), slot.get("swell2_m"), slot.get("swell3_m")],
        htsgw_m=raw_h,
        beach_dir_deg=beach_dir,
        rules=rules,
        point_overrides=overrides,
    )
    wn = wind_score(
        wind_dir_deg=slot.get("wdir_deg"),
        wind_speed_ms=slot.get("wind_ms"),
        beach_dir_deg=beach_dir,
        rules=rules,
        point_overrides=overrides,
    )
    td = tide_score(tide_phase=tide_phase, rules=rules, point_overrides=overrides)
    sz = size_score(
        htsgw_m=raw_h,
        period_s=slot.get("perpw_s"),
        rules=rules,
        point_overrides=overrides,
        effective_htsgw_m=eff_h,
    )

    # 季節補正: timestamp_jst から月を取り出す
    month: int | None = None
    ts = slot.get("timestamp_jst")
    if ts:
        try:
            month = datetime.fromisoformat(ts).month
        except (TypeError, ValueError):
            month = None
    season_adj = seasonality_adjustment(month=month, point_overrides=overrides, rules=rules)

    hard_cut = is_wind_hard_cut(
        wind_speed_ms=slot.get("wind_ms"),
        point_overrides=overrides,
        rules=rules,
    )

    gate = surfability_gate(
        effective_htsgw_m=eff_h,
        period_s=slot.get("perpw_s"),
        rules=rules,
    )

    return composite(
        swell=sw,
        wind=wn,
        tide=td,
        size=sz,
        rules=rules,
        seasonality_adj=season_adj,
        wind_hard_cut=hard_cut,
        effective_htsgw_m=eff_h,
        surf_gate=gate,
    )
