"""教育シリーズのリールを Instagram に投稿する（週次リールの publish_reels.IG を流用）。

  python edu/publish_edu.py --pick                 # 次に投稿する ep*.json のパス（今日投稿済み・全話投稿済みなら空）
  python edu/publish_edu.py --video edu/out/ep02.mp4 --ep edu/episodes/ep02.json [--dry-run]
キャプションは ep*.json の caption から組み立てる。同じ1行目の投稿があればスキップ。
環境変数: IG_USER_ID / IG_ACCESS_TOKEN
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "reel"))
from publish_reels import IG, publish_story  # noqa: E402

JST = datetime.timezone(datetime.timedelta(hours=9))
TAGS = "#波予報の読み方 #波情報 #波予報 #サーフィン #サーフィン初心者 #サーフィン上達 #surfjapan #wavehack"


def caption(ep: dict) -> str:
    head = f"🌊 波予報の読み方 #{ep['no']:02d}｜{ep['title']}"
    body = ep.get("caption") or "\n".join(s["say"] for s in ep["scenes"][:-1])
    return "\n".join([head, "", body, "", f"次回 #{ep['no'] + 1:02d} は「{ep['next']}」。毎日18時に更新（木曜は週末の波予想）" if ep.get("next") else "シリーズ全10回、最後までありがとうございました。",
                      "保存して、次の海選びに。", f"※ {ep['source']}" if ep.get("source") else "", "", TAGS]).replace("\n\n\n", "\n\n")


def publish(ig: IG, video: Path, text: str, thumb_ms: int, retries: int = 3) -> str:
    base = f"https://graph.facebook.com/{ig.v}"
    head = text.split("\n", 1)[0]
    for attempt in range(1, retries + 1):
        try:
            params = {"media_type": "REELS", "upload_type": "resumable", "caption": text, "share_to_feed": "true", "thumb_offset": str(thumb_ms)}
            container = ig._req("POST", f"{base}/{ig.user}/media", urllib.parse.urlencode(params).encode())["id"]
            ig._req("POST", f"https://rupload.facebook.com/ig-api-upload/{ig.v}/{container}", video.read_bytes(),
                    {"offset": "0", "file_size": str(video.stat().st_size)})
            for _ in range(60):
                st = ig._req("GET", f"{base}/{container}?fields=status_code,status")
                if st.get("status_code") == "FINISHED":
                    break
                if st.get("status_code") in ("ERROR", "EXPIRED"):
                    raise SystemExit(f"container {container}: {st}")
                time.sleep(10)
            return ig._req("POST", f"{base}/{ig.user}/media_publish", urllib.parse.urlencode({"creation_id": container}).encode())["id"]
        except SystemExit as e:
            print(f"投稿失敗 ({attempt}/{retries}):", str(e)[:300], flush=True)
            if attempt == retries:
                raise
            time.sleep(120)
            if head in {c.split("\n", 1)[0] for c in ig.recent_captions(10)}:
                return "posted"
    raise SystemExit("unreachable")


def pick(ig: IG) -> str:
    """1日1本・番号順。今日（JST）すでにシリーズを投稿していれば空。"""
    media = ig._req("GET", f"https://graph.facebook.com/{ig.v}/{ig.user}/media?fields=caption,timestamp&limit=50").get("data", [])
    posted, today = set(), datetime.datetime.now(JST).date()
    for m in media:
        head = (m.get("caption") or "").split("\n", 1)[0]
        if head.startswith("🌊 波予報の読み方 #"):
            posted.add(head)
            if datetime.datetime.strptime(m["timestamp"], "%Y-%m-%dT%H:%M:%S%z").astimezone(JST).date() == today:
                return ""
    for f in sorted((HERE / "episodes").glob("ep*.json")):
        if caption(json.loads(f.read_text(encoding="utf-8"))).split("\n", 1)[0] not in posted:
            return str(f.relative_to(HERE.parent))
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pick", action="store_true")
    ap.add_argument("--video", type=Path)
    ap.add_argument("--ep", type=Path)
    ap.add_argument("--thumb-ms", type=int, default=2500, help="カバーに使うコマ（問いかけの画面）")
    ap.add_argument("--story", type=Path, help="リール投稿後にストーリーズへ出す画像（render_story.py --edu の出力）")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.pick:
        print(pick(IG()))
        return 0
    ep = json.loads(a.ep.read_text(encoding="utf-8"))
    text = caption(ep)
    if a.dry_run:
        print(text)
        return 0
    ig = IG()
    if text.split("\n", 1)[0] in {c.split("\n", 1)[0] for c in ig.recent_captions(20)}:
        print("投稿済みのためスキップ:", text.split("\n", 1)[0])
        return 0
    print("published", publish(ig, a.video, text, a.thumb_ms), time.strftime("%H:%M"), flush=True)
    if a.story:
        try:  # ストーリーズの失敗でリール投稿を失敗扱いにしない
            print("story", publish_story(a.story, f"story-edu-{ep['no']:02d}.jpg"), flush=True)
        except Exception as e:  # noqa: BLE001
            print("ストーリーズは投稿できず:", str(e)[:200], flush=True)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
