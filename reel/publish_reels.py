"""render_reel.py の MP4 を Instagram リールとして投稿する（Instagram API with Facebook Login）。

動画は resumable upload で rupload.facebook.com に直接送るので、公開 URL は不要。
  1. POST graph.facebook.com/{v}/{ig-user-id}/media  media_type=REELS upload_type=resumable caption
  2. POST rupload.facebook.com/ig-api-upload/{v}/{container}  (Authorization: OAuth, offset, file_size, 本体)
  3. GET  /{container}?fields=status_code  が FINISHED になるまで待つ
  4. POST /{ig-user-id}/media_publish  creation_id

環境変数（/Users/takeda.toi/secret/wavehack/instagram.env）:
  IG_USER_ID / IG_ACCESS_TOKEN（Facebook ページのアクセストークン）/ IG_API_VERSION（既定 v26.0）

使い方:
  python reel/publish_reels.py --data reel_data.json --video-dir videos/ [--interval-min 15] [--dry-run] [ポイント名 ...]
  python reel/publish_reels.py --data reel_data.json --pending   # 今週まだ投稿していないポイント名を空白区切りで出力
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from narration_scenes import size_words  # noqa: E402

DAYJ = {"sat": "土", "sun": "日"}
AREA_TAGS = {"千葉北": "千葉サーフィン", "湘南": "湘南サーフィン", "伊豆・伊東": "伊豆サーフィン", "静岡・浜松": "静岡サーフィン",
             "伊良湖": "伊良湖サーフィン", "伊勢・志摩": "伊勢サーフィン", "和歌山": "和歌山サーフィン", "四国": "徳島サーフィン",
             "宮崎": "宮崎サーフィン"}


def caption(data: dict, P: dict) -> str:
    key, s = max(((k, x) for k in ("sat", "sun") for x in P["days"][k]), key=lambda kx: kx[1]["score"])
    good = sum(1 for m in P["members"] if (m[key] or 0) >= 55)
    lines = [
        f"🌊 今週末の{P['group']}エリア｜{P['name']}（{data['period']}）",
        "",
        f"ベスト: {DAYJ[key]}曜 {s['t']}〜／{size_words(s['h']).replace('くらい', '')}（{s['h']:.1f}m）／SCORE {s['score']}",
        f"うねり: {s['swell']}から 周期{round(s['per'])}秒　風: {s['wind']} {s['wms']:.0f}m/s",
        (f"{P['group']}エリアは{len(P['members'])}地点中{good}地点がおすすめ（{DAYJ[key]}曜・日中平均SCORE55以上）" if good
         else f"{P['group']}エリアは全体的に穏やかなコンディション（日中平均SCORE55以上の地点なし）"),
        "",
        "直前の最新予報・潮回り・7日間予報はプロフィールのリンク（wavehack.surf）から。",
        "※ NOAA GFS / 気象庁データをもとにした自動生成の予報です。",
        "",
        " ".join(["#波情報", "#波予報", "#週末サーフィン", f"#{P['name'].replace('・', '')}", f"#{AREA_TAGS.get(P['group'], 'サーフィン')}",
                  "#サーフィン", "#surf", "#surfjapan", "#wavehack"]),
    ]
    return "\n".join(lines)


class IG:
    def __init__(self) -> None:
        # Secrets にコピペした値の末尾改行でヘッダーが不正にならないよう strip する
        self.user = os.environ["IG_USER_ID"].strip()
        self.token = os.environ["IG_ACCESS_TOKEN"].strip()
        self.v = os.environ.get("IG_API_VERSION", "v26.0")

    def _req(self, method: str, url: str, data: bytes | None = None, headers: dict | None = None) -> dict:
        h = {"Authorization": f"OAuth {self.token}", **(headers or {})}
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:  # エラー本文にトークンは含まれない
            raise SystemExit(f"{method} {url.split('?')[0]} -> {e.code}: {e.read().decode(errors='replace')[:500]}") from e

    def recent_captions(self, limit: int = 50) -> list[str]:
        d = self._req("GET", f"https://graph.facebook.com/{self.v}/{self.user}/media?fields=caption&limit={limit}")
        return [m.get("caption") or "" for m in d.get("data", [])]

    def publish_reel(self, video: Path, text: str, cover_url: str | None = None, wait_sec: int = 600) -> str:
        base = f"https://graph.facebook.com/{self.v}"
        params = {"media_type": "REELS", "upload_type": "resumable", "caption": text, "share_to_feed": "true"}
        if cover_url:
            params["cover_url"] = cover_url  # Meta が取りに来るので公開 URL が必要
        body = urllib.parse.urlencode(params).encode()
        container = self._req("POST", f"{base}/{self.user}/media", body)["id"]
        size = video.stat().st_size
        self._req("POST", f"https://rupload.facebook.com/ig-api-upload/{self.v}/{container}", video.read_bytes(),
                  {"offset": "0", "file_size": str(size)})
        deadline = time.monotonic() + wait_sec
        while True:
            st = self._req("GET", f"{base}/{container}?fields=status_code,status")
            code = st.get("status_code")
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED") or time.monotonic() > deadline:
                raise SystemExit(f"container {container} {code}: {st.get('status')}")
            time.sleep(10)
        media = self._req("POST", f"{base}/{self.user}/media_publish", urllib.parse.urlencode({"creation_id": container}).encode())
        return media["id"]


COVER_BASE_URL = "https://wavehack.surf/api/social"  # wavehack-front の /api/social/[file] が R2 social/*.jpg を公開配信


def upload_cover(cover: Path, saturday: str, index: int) -> str:
    """カバー画像を R2 (wavehack-data/social/) に置き、公開 URL を返す。
    ファイル名は /api/social の許可パターン（英小文字・数字・ハイフン）に合わせる。"""
    key = f"cover-{saturday}-{index}.jpg"
    subprocess.run(["npx", "-y", "wrangler@4", "r2", "object", "put", f"wavehack-data/social/{key}",
                    "--file", str(cover), "--content-type", "image/jpeg", "--remote"],
                   check=True, capture_output=True)
    return f"{COVER_BASE_URL}/{key}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--video-dir", type=Path, help="投稿時は必須")
    ap.add_argument("--interval-min", type=float, default=15, help="投稿間隔（分）。フィードが埋まらないようにずらす")
    ap.add_argument("--log", type=Path, help="投稿結果を追記する JSONL")
    ap.add_argument("--cover-dir", type=Path, help="render_cover.py の出力。あれば各リールのカバーに設定する")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pending", action="store_true", help="今週まだ投稿していないポイント名を出力して終わる（動画は不要）")
    ap.add_argument("--retries", type=int, default=3, help="1本あたりの投稿試行回数。失敗しても次のポイントへ進む")
    ap.add_argument("names", nargs="*")
    a = ap.parse_args()
    data = json.loads(a.data.read_text(encoding="utf-8"))
    if a.pending:
        posted = {c.split("\n", 1)[0] for c in IG().recent_captions()}
        print(" ".join(P["name"] for P in data["points"]
                       if (not a.names or P["name"] in a.names) and caption(data, P).split("\n", 1)[0] not in posted))
        return 0
    if not a.video_dir:
        ap.error("--video-dir が必要です")
    targets = [P for P in data["points"] if (not a.names or P["name"] in a.names) and (a.video_dir / f"{P['name']}.mp4").exists()]
    ig = None if a.dry_run else IG()
    if ig:
        # 再実行・予備スケジュールで同じ週の同じポイントを二重投稿しない（キャプション1行目で判定）
        posted = {c.split("\n", 1)[0] for c in ig.recent_captions()}
        skipped = [P["name"] for P in targets if caption(data, P).split("\n", 1)[0] in posted]
        if skipped:
            print("今週分は投稿済みのためスキップ:", " ".join(skipped), flush=True)
        targets = [P for P in targets if P["name"] not in skipped]
    failed = []
    for i, P in enumerate(targets):
        if i and not a.dry_run:
            time.sleep(a.interval_min * 60)
        text = caption(data, P)
        if a.dry_run:
            print(f"--- {P['name']} ({len(text)}字)\n{text}\n", flush=True)
            continue
        cover = a.cover_dir / f"{P['name']}.jpg" if a.cover_dir else None
        cover_url = None
        if cover and cover.exists():
            try:
                cover_url = upload_cover(cover, data["saturday"], data["points"].index(P))
            except subprocess.CalledProcessError as e:  # カバーが無くても投稿自体は続ける
                print(P["name"], "カバーのアップロード失敗:", (e.stderr or b"").decode(errors="replace")[-200:], flush=True)
        media_id = None
        for attempt in range(1, a.retries + 1):
            try:
                media_id = ig.publish_reel(a.video_dir / f"{P['name']}.mp4", text, cover_url)
                break
            except SystemExit as e:
                # 2026-10-15 は rupload が一時的に 400 ProcessingFailedError を返し、以降の地点が全部止まった
                print(P["name"], f"投稿失敗 ({attempt}/{a.retries}):", str(e)[:300], flush=True)
                if attempt < a.retries:
                    time.sleep(120)
                    if text.split("\n", 1)[0] in {c.split("\n", 1)[0] for c in ig.recent_captions(10)}:
                        media_id = "posted"  # 応答だけ失敗して実は公開済みなら二重投稿しない
                        break
        if not media_id:
            failed.append(P["name"])
            continue
        print(P["name"], "published", media_id, time.strftime("%H:%M"), flush=True)
        if a.log:
            with a.log.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"name": P["name"], "media_id": media_id, "saturday": data["saturday"],
                                    "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, ensure_ascii=False) + "\n")
    if failed:
        print("投稿できなかったポイント:", " ".join(failed), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
