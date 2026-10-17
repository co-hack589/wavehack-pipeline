"""ストーリーズ画像（1080x1920 JPEG）を template/story.html から作る。

  uv run --with playwright python reel/render_story.py --edu edu/episodes/ep02.json --out story.jpg
  uv run --with playwright python reel/render_story.py --week reel_data.json --out story.jpg
--edu: 教育シリーズの回の要点（sum シーン）／--week: 週末予報 9地点の土日ベスト SCORE 一覧
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

TEMPLATE = Path(__file__).resolve().parent / "template" / "story.html"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--edu", type=Path)
    g.add_argument("--week", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    if a.edu:
        story = {"mode": "edu", "ep": json.loads(a.edu.read_text(encoding="utf-8"))}
    else:
        d = json.loads(a.week.read_text(encoding="utf-8"))
        story = {"mode": "week", "period": d["period"], "points": d["points"]}
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": 1080, "height": 1920}, device_scale_factor=1)
        page.add_init_script(f"window.__STORY__ = {json.dumps(story, ensure_ascii=False)};")
        page.goto(TEMPLATE.as_uri())
        page.evaluate("document.fonts.ready")
        page.wait_for_function("window.__storyReady === true")
        page.wait_for_timeout(300)
        a.out.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(a.out), type="jpeg", quality=90)
        browser.close()
    print(a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
