"""指定日の投稿時刻（JST）まで待つ。教育シリーズ・紹介リール用（週次リールは wait_until_post.py）。

GitHub の定期実行は数時間遅れるので朝から起動して待つ。1ジョブは最長6時間なので、
--cap で1ジョブ内の待ち時間を区切り、次のジョブで残りを待つ。
--last を過ぎていればその日は投稿しない（publish=false）。

  python reel/wait_slot.py --date 2026-10-18 --at 18:00 --last 23:00 [--cap 秒] [--now]
"""
from __future__ import annotations

import argparse
import datetime
import os
import time

JST = datetime.timezone(datetime.timedelta(hours=9))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--at", required=True, help="投稿時刻 HH:MM")
    ap.add_argument("--last", required=True, help="これを過ぎたら投稿しない HH:MM")
    ap.add_argument("--cap", type=float, help="このジョブで待つ最大秒数（残りは次のジョブで待つ）")
    ap.add_argument("--now", action="store_true", help="待たずに投稿する（手動実行の post_now）")
    a = ap.parse_args()
    day = datetime.date.fromisoformat(a.date)
    start, last = (datetime.datetime.combine(day, datetime.time.fromisoformat(t), JST) for t in (a.at, a.last))
    now = datetime.datetime.now(JST)
    ok = "true"
    if now >= last:
        print(f"{now:%m/%d %H:%M} は {last:%m/%d %H:%M} を過ぎているので投稿しない")
        ok = "false"
    elif now < start and not a.now:
        wait = (start - now).total_seconds()
        if a.cap is not None:
            wait = min(wait, a.cap)
        print(f"{start:%m/%d %H:%M} に向けて {wait:.0f} 秒待つ（残り {(start - now).total_seconds():.0f} 秒）", flush=True)
        time.sleep(wait)
    else:
        print("待たずに投稿する")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"publish={ok}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
