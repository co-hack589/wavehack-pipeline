"""週末リールの投稿開始時刻（対象の土曜の2日前＝木曜 19:00 JST）まで待つ。

GitHub Actions の1ジョブは最長6時間なので、朝に動画を作ってから 19:00 まで待つときは
--cap で1ジョブ内の待ち時間を区切り、次のジョブで残りを待つ。
土曜 9:00 を過ぎていれば週末の予報としては遅すぎるので publish=false を出力する。

  python reel/wait_until_post.py --saturday 2026-10-17 [--cap 秒] [--now]
"""
from __future__ import annotations

import argparse
import datetime
import os
import time

JST = datetime.timezone(datetime.timedelta(hours=9))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--saturday", required=True)
    ap.add_argument("--cap", type=float, help="このジョブで待つ最大秒数（残りは次のジョブで待つ）")
    ap.add_argument("--now", action="store_true", help="待たずに投稿する（手動実行の post_now）")
    a = ap.parse_args()
    sat = datetime.date.fromisoformat(a.saturday)
    start = datetime.datetime.combine(sat - datetime.timedelta(days=2), datetime.time(19, 0), JST)
    deadline = datetime.datetime.combine(sat, datetime.time(9, 0), JST)
    now = datetime.datetime.now(JST)
    ok = "true"
    if now >= deadline:
        print(f"{now:%m/%d %H:%M} は投稿期限 {deadline:%m/%d %H:%M} を過ぎているので投稿しない")
        ok = "false"
    elif now < start and not a.now:
        wait = (start - now).total_seconds()
        if a.cap is not None:
            wait = min(wait, a.cap)
        print(f"{start:%m/%d %H:%M} に向けて {wait:.0f} 秒待つ（開始まで残り {(start - now).total_seconds():.0f} 秒）", flush=True)
        time.sleep(wait)
    else:
        print("待たずに投稿する")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"publish={ok}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
