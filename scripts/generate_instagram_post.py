"""Instagram 週1投稿「週末の波予想」の画像 + キャプション生成。

dashboard.json から、次の土日について主要ポイント (FEATURED_POINTS) の
日中ベストスロットを抜き出し、
  - post.jpg   (1080x1350 / 4:5 フィード推奨サイズ, JPEG = Graph API 要件)
  - post.json  ({"saturday", "sunday", "caption", "image": "post.jpg", "rows": [...]})
を出力する。投稿自体は scripts/publish_instagram.py が行う。
色はデザインレギュレーション v1 (output/md/design_regulation.md) 準拠。

実行例:
  uv run --extra social python scripts/generate_instagram_post.py \
      --dashboard out/data/dashboard.json --font-dir fonts --out-dir out/instagram
  # 対象週末を明示（既定は実行日 JST から見て次の土曜）
  ... --saturday 2026-10-10
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from wavehack_pipeline.daylight import slot_is_daylight  # noqa: E402

JST = timezone(timedelta(hours=9))
WEEKDAYS = "月火水木金土日"

# 投稿に載せる主要ポイント（表示順）。
# (エリア表示名, ポイント表示名, dashboard の point_name 候補, area で絞る場合の値)
# 候補が複数あるもの（国府ノ浜）はその日いちばん良い地点を採用する。
FEATURED_POINTS: list[tuple[str, str, tuple[str, ...], str | None]] = [
    ("千葉", "志田下", ("志田下",), None),
    ("湘南", "鵠沼", ("鵠沼",), None),
    ("伊豆", "白浜", ("白浜",), "静岡"),
    ("静岡", "御前崎", ("御前崎・メイン",), None),
    ("伊良湖", "ロングビーチ", ("ロングビーチ",), None),
    ("三重", "国府ノ浜", ("国府ノ浜・テトラ前", "国府ノ浜・三角〜ラスタ前", "国府ノ浜・松林"), None),
    ("和歌山", "磯ノ浦", ("磯ノ浦",), None),
    ("徳島", "生見", ("生見",), None),
    ("宮崎", "木崎浜", ("木崎浜",), None),
]

# design_regulation.md v1
INK = "#071a3a"
ACCENT = "#0877f9"
MUTED = "#52627a"
FAINT = "#718096"
PAGE_BG = "#f7f9fc"
CARD_BG = "#ffffff"
BORDER = "#dce5ef"
DIVIDER = "#edf2f7"
HIGHLIGHT_BG = "#eef6ff"
DARK_BG = "#071c3b"
HIGHLIGHT_SCORE = 55  # score >= 55 のみ azure highlight

W, H = 1080, 1350

HASHTAGS = [
    "#波情報", "#波予報", "#週末サーフィン", "#サーフィン", "#サーフィン好きな人と繋がりたい",
    "#surf", "#surfing", "#surfjapan", "#wavehack",
]


# ============================================================
# データ抽出
# ============================================================
def _is_daylight(slot: dict[str, Any], point: dict[str, Any]) -> bool:
    if "is_daylight" in slot:
        return bool(slot["is_daylight"])
    lat, lon = point.get("point_lat"), point.get("point_lon")
    if lat is not None and lon is not None:
        return slot_is_daylight(slot, lat=lat, lon=lon)
    return slot_is_daylight(slot)


def best_slot(
    point: dict[str, Any], target_date: str, allow_extended: bool = False
) -> dict[str, Any] | None:
    """そのポイントの指定日の日中ベストスロット（無ければ None）。"""
    best: dict[str, Any] | None = None
    for s in point.get("slots", []):
        if s.get("timestamp_jst", "")[:10] != target_date:
            continue
        if (s.get("is_extended") and not allow_extended) or not _is_daylight(s, point):
            continue
        if best is None or float(s.get("composite") or 0) > float(best.get("composite") or 0):
            best = s
    if best is None:
        return None
    return {
        "time": best.get("timestamp_jst", "")[11:16],
        "score": round(float(best.get("composite") or 0) * 100),
        "wave_height_m": best.get("htsgw_m"),
        "period_s": best.get("perpw_s"),
        "wind_label": best.get("wind_label", ""),
        "comment": best.get("comment", ""),
    }


def build_rows(
    dashboard: dict[str, Any], days: list[date], allow_extended: bool = False
) -> list[dict[str, Any]]:
    points = dashboard.get("points", [])
    rows: list[dict[str, Any]] = []
    for area_label, name, candidates, area_filter in FEATURED_POINTS:
        matched = [
            p for p in points
            if p.get("point_name") in candidates
            and (area_filter is None or p.get("area") == area_filter)
        ]
        if not matched:
            raise SystemExit(f"ERROR: dashboard に {name} ({candidates}) が見つかりません")
        row: dict[str, Any] = {"area": area_label, "point_name": name, "days": {}}
        for d in days:
            picks = [b for p in matched if (b := best_slot(p, d.isoformat(), allow_extended)) is not None]
            if not picks:
                raise SystemExit(f"ERROR: {name} の {d} 日中スロットがありません")
            row["days"][d.isoformat()] = max(picks, key=lambda b: b["score"])
        rows.append(row)
    return rows


def next_saturday(today: date) -> date:
    return today + timedelta(days=(5 - today.weekday()) % 7)


# ============================================================
# 描画
# ============================================================
class Fonts:
    def __init__(self, font_dir: Path):
        self.dir = font_dir

    def jp(self, size: int, weight: str = "Bold") -> ImageFont.FreeTypeFont:
        return ImageFont.truetype(str(self.dir / f"ZenKakuGothicNew-{weight}.ttf"), size)

    def num(self, size: int, weight: int = 800) -> ImageFont.FreeTypeFont:
        f = ImageFont.truetype(str(self.dir / "InterTight.ttf"), size)
        try:
            f.set_variation_by_axes([weight])
        except OSError:
            pass
        return f


def _fmt_m(v: float | None) -> str:
    return "-" if v is None else f"{v:.1f}m"


def _md(d: date) -> str:
    return f"{d.month}/{d.day}"


def render_image(rows: list[dict[str, Any]], days: list[date], fonts: Fonts, out: Path) -> None:
    img = Image.new("RGB", (W, H), PAGE_BG)
    d = ImageDraw.Draw(img)

    # ヘッダー (dark navy)
    header_h = 250
    d.rectangle([0, 0, W, header_h], fill=DARK_BG)
    d.text((64, 56), "WAVE HACK", font=fonts.num(28, 800), fill="#8fc2ff")
    d.text((64, 100), "週末の波予想", font=fonts.jp(66, "Black"), fill="#ffffff")
    sat, sun = days
    period = f"{_md(sat)}(土) – {_md(sun)}(日)"
    d.text((64, 190), period, font=fonts.jp(34, "Bold"), fill="#cfe3ff")

    # テーブル
    margin = 48
    col_x = [margin, 470, 775]  # ポイント / 土 / 日
    col_w = 280
    head_y = header_h + 28
    for i, day in enumerate(days):
        cx = col_x[i + 1] + col_w // 2 - 20
        d.text((cx, head_y + 18), f"{_md(day)} ({WEEKDAYS[day.weekday()]})",
               font=fonts.jp(28, "Bold"), fill=MUTED, anchor="mm")

    top = head_y + 52
    footer_h = 96
    row_h = (H - top - footer_h) // len(rows)
    d.rounded_rectangle([margin - 8, top, W - margin + 8, top + row_h * len(rows)],
                        radius=22, fill=CARD_BG, outline=BORDER, width=2)
    for r_i, row in enumerate(rows):
        y0 = top + r_i * row_h
        cy = y0 + row_h / 2
        if r_i:
            d.line([margin + 12, y0, W - margin - 12, y0], fill=DIVIDER, width=2)
        d.text((col_x[0] + 20, cy - 20), row["area"], font=fonts.jp(22, "Bold"),
               fill=FAINT, anchor="lm")
        d.text((col_x[0] + 20, cy + 16), row["point_name"], font=fonts.jp(38, "Black"),
               fill=INK, anchor="lm")
        for i, day in enumerate(days):
            v = row["days"][day.isoformat()]
            hi = v["score"] >= HIGHLIGHT_SCORE
            x0 = col_x[i + 1]
            if hi:
                d.rounded_rectangle([x0, y0 + 10, x0 + col_w - 40, y0 + row_h - 10], radius=14,
                                    fill=HIGHLIGHT_BG, outline=ACCENT, width=2)
            d.text((x0 + 24, cy), str(v["score"]), font=fonts.num(52, 900),
                   fill=ACCENT if hi else INK, anchor="lm")
            d.text((x0 + 118, cy - 15), v["time"], font=fonts.num(22, 700),
                   fill=MUTED, anchor="lm")
            d.text((x0 + 118, cy + 15), _fmt_m(v["wave_height_m"]), font=fonts.num(22, 700),
                   fill=MUTED, anchor="lm")

    # フッター
    d.text((W / 2, H - footer_h / 2 - 10), "SCORE 55以上がおすすめ ・ 全国151地点の予報は wavehack.surf",
           font=fonts.jp(26, "Bold"), fill=INK, anchor="mm")

    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "JPEG", quality=90, optimize=True)


# ============================================================
# キャプション
# ============================================================
def build_caption(rows: list[dict[str, Any]], days: list[date]) -> str:
    sat, sun = days
    lines = [f"🌊 週末の波予想 {_md(sat)}(土)・{_md(sun)}(日)", ""]

    # 土日を通したイチオシ
    pick_row, pick_day = max(
        ((r, day) for r in rows for day in days),
        key=lambda x: x[0]["days"][x[1].isoformat()]["score"],
    )
    pv = pick_row["days"][pick_day.isoformat()]
    lines.append(
        f"🏄 イチオシは {WEEKDAYS[pick_day.weekday()]}曜の{pick_row['point_name']}"
        f"（{pick_row['area']}）{pv['time']}〜 SCORE {pv['score']}"
    )
    if pv.get("comment"):
        lines.append(pv["comment"])
    lines.append("")

    for r in rows:
        s, u = (r["days"][day.isoformat()] for day in days)
        lines.append(
            f"・{r['point_name']}（{r['area']}）土 {s['score']}（{s['time']}〜{_fmt_m(s['wave_height_m'])}）"
            f" / 日 {u['score']}（{u['time']}〜{_fmt_m(u['wave_height_m'])}）"
        )
    lines += [
        "",
        "SCORE はうねりの向き・サイズ・風・潮回りから算出（55以上がおすすめ）。",
        "直前の最新予報と全国151地点はプロフィールのリンク（wavehack.surf）から。",
        "※ 予報は NOAA GFS / 気象庁データをもとにした自動生成です。",
        "",
        " ".join(HASHTAGS),
    ]
    return "\n".join(lines)


# ============================================================
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dashboard", type=Path, required=True)
    ap.add_argument("--font-dir", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--saturday", help="YYYY-MM-DD (既定: JST 今日から見て次の土曜)")
    ap.add_argument("--allow-extended", action="store_true",
                    help="5日超の参考値スロットも使う（サンプル確認用。本番の木曜投稿では不要）")
    args = ap.parse_args(argv)

    sat = (date.fromisoformat(args.saturday) if args.saturday
           else next_saturday(datetime.now(JST).date()))
    if sat.weekday() != 5:
        print(f"ERROR: {sat} は土曜ではありません", file=sys.stderr)
        return 1
    days = [sat, sat + timedelta(days=1)]

    dashboard = json.loads(args.dashboard.read_text(encoding="utf-8"))
    rows = build_rows(dashboard, days, args.allow_extended)

    image_path = args.out_dir / "post.jpg"
    render_image(rows, days, Fonts(args.font_dir), image_path)
    payload = {
        "saturday": days[0].isoformat(),
        "sunday": days[1].isoformat(),
        "generated_at": dashboard.get("generated_at"),
        "image": image_path.name,
        "caption": build_caption(rows, days),
        "rows": rows,
    }
    (args.out_dir / "post.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(payload["caption"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
