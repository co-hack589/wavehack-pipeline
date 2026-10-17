"""紹介リールを promo/schedule.json の日付どおりに投稿する（動画は R2 wavehack-data/promo-src/ に置いてある）。

  python promo/post_promo.py --pick            # 今日（JST）の投稿があり未投稿なら動画名を出力
  python promo/post_promo.py --video promo2.mp4 --file work/promo2.mp4
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "edu"))
sys.path.insert(0, str(HERE.parent / "reel"))
from publish_edu import publish  # noqa: E402
from publish_reels import IG  # noqa: E402

JST = datetime.timezone(datetime.timedelta(hours=9))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pick", action="store_true")
    ap.add_argument("--video")
    ap.add_argument("--file", type=Path)
    a = ap.parse_args()
    posts = json.loads((HERE / "schedule.json").read_text(encoding="utf-8"))["posts"]
    ig = IG()
    posted = {c.split("\n", 1)[0] for c in ig.recent_captions(30)}
    if a.pick:
        today = datetime.datetime.now(JST).date().isoformat()
        print(next((p["video"] for p in posts if p["date"] == today and p["caption"].split("\n", 1)[0] not in posted), ""))
        return 0
    p = next(p for p in posts if p["video"] == a.video)
    if p["caption"].split("\n", 1)[0] in posted:
        print("投稿済み"); return 0
    print("published", publish(ig, a.file, p["caption"], 2500))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
