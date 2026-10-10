"""template/cover.html から、リールのカバー画像（1080x1920 JPEG）をポイントごとに作る。

使い方:
  uv run --with playwright python reel/render_cover.py --data reel_data.json --out-dir covers/ [ポイント名 ...]
出力: <out-dir>/<ポイント名>.jpg
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "template" / "cover.html"
# エリア背景（Codex 生成。無いエリアはグラデーションのみ）— template/reel.html の BG と揃える
BG = {"千葉北": "chiba-north", "伊豆・伊東": "izu", "宮崎": "miyazaki"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("names", nargs="*")
    a = ap.parse_args()
    data = json.loads(a.data.read_text(encoding="utf-8"))
    a.out_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", args=["--window-size=1080,1920"])
        page = browser.new_page(viewport={"width": 1080, "height": 1920}, device_scale_factor=1)
        for P in data["points"]:
            if a.names and P["name"] not in a.names:
                continue
            bg = BG.get(P["group"])
            cover = {"point": P, "period": data["period"],
                     "bg": (HERE / "assets" / "bg" / f"{bg}.jpg").as_uri() if bg else None}
            page.add_init_script(f"window.__COVER__ = {json.dumps(cover, ensure_ascii=False)};")
            page.goto(TEMPLATE.as_uri())
            page.evaluate("document.fonts.ready")
            page.wait_for_function("window.__coverReady === true")
            page.wait_for_timeout(300)  # 背景画像のデコード待ち
            out = a.out_dir / f"{P['name']}.jpg"
            page.screenshot(path=str(out), type="jpeg", quality=90, clip={"x": 0, "y": 0, "width": 1080, "height": 1920})
            print(out, flush=True)
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
