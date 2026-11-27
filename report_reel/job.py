"""波レポートのリールの制作・投稿ジョブ（.github/workflows/report-reel.yml から呼ぶ）。

  python report_reel/job.py build   --reel-id <uuid> [--silent] [--no-upload]
      GET /internal/report-reels/:id → メディアを取得 → 台本・キャプション（report_reel/reel_script.py）
      → 音声（Gemini Aoede、失敗時 Cloud TTS Chirp）→ 書き出し → R2 wavehack-data/report-reels/<id>.mp4
      → Slack の元カードのスレッドに mp4 を上げる → POST /ready {video_key, caption}
  python report_reel/job.py publish --reel-id <uuid>
      GET で状態（publishing）と同意を確かめる → R2 から mp4 → Instagram（edu/publish_edu.publish）→ POST /published
失敗したら POST /failed {error} を送ってから終了コード 1（workflow の slack-alert が #wave-hack-error に出す）。

公開リポジトリの実行ログに残るので、投稿者の言葉・ニックネーム・キャプションの全文は出力しない。
環境変数: WAVEHACK_API_URL / WAVEHACK_CRON_SECRET / SLACK_BOT_TOKEN（files:write）/ CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN
          GEMINI_API_KEY（+ GOOGLE_ACCESS_TOKEN）/ IG_USER_ID / IG_ACCESS_TOKEN / IG_API_VERSION
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for d in ("reel", "edu", "social", "report_reel"):
    sys.path.insert(0, str(ROOT / d))
from reel_script import JST, build_caption, build_episode  # noqa: E402
from social_api import SocialApi  # noqa: E402

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
BUCKET = "wavehack-data"


def video_key(reel_id: str) -> str:
    return f"report-reels/{reel_id}.mp4"


class ReelApi(SocialApi):
    """wavehack-api の /internal/report-reels（Bearer WAVEHACK_CRON_SECRET）。リトライは SocialApi._req と同じ。"""

    def get(self, reel_id: str) -> dict:
        return self._req("GET", f"/internal/report-reels/{reel_id}")[1]

    def ready(self, reel_id: str, key: str, caption: str) -> bool:
        st, _ = self._req("POST", f"/internal/report-reels/{reel_id}/ready", {"video_key": key, "caption": caption}, ok=(409,))
        return st != 409

    def published(self, reel_id: str, media_id: str, permalink: str = "") -> None:
        self._req("POST", f"/internal/report-reels/{reel_id}/published", {"ig_media_id": media_id, "permalink": permalink})

    def failed(self, reel_id: str, error: str) -> None:
        try:
            self._req("POST", f"/internal/report-reels/{reel_id}/failed", {"error": error[:480]}, ok=(409,))
        except SystemExit as e:  # 失敗の記録の失敗で本来のエラーを隠さない
            print("failed の記録に失敗:", str(e)[:200], flush=True)


# ---------------------------------------------------------------- R2（wrangler。CLOUDFLARE_* は環境変数から）
def r2_put(local: Path, key: str) -> None:
    subprocess.run(["npx", "-y", "wrangler@4", "r2", "object", "put", f"{BUCKET}/{key}", "--file", str(local),
                    "--content-type", "video/mp4", "--remote"], check=True, capture_output=True)


def r2_get(key: str, local: Path) -> bool:
    r = subprocess.run(["npx", "-y", "wrangler@4", "r2", "object", "get", f"{BUCKET}/{key}", "--file", str(local), "--remote"],
                       capture_output=True)
    return r.returncode == 0 and local.exists() and local.stat().st_size > 0


def download(url: str, out: Path) -> Path:
    req = urllib.request.Request(url, headers={"user-agent": "wavehack-pipeline/report-reel"})
    with urllib.request.urlopen(req, timeout=180) as r, open(out, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    if out.stat().st_size == 0:
        raise SystemExit("メディアが空でした")
    return out


def media_suffix(content_type: str | None, kind: str) -> str:
    ct = (content_type or "").lower()
    return {"video/quicktime": ".mov", "image/png": ".png", "image/webp": ".webp", "image/heic": ".heic"}.get(
        ct, ".mp4" if kind == "video" else ".jpg")


# ---------------------------------------------------------------- 制作
def do_build(api: ReelApi, reel_id: str, *, silent: bool = False, upload: bool = True, work: Path | None = None) -> Path:
    import reel_render as R  # playwright を読むので制作のときだけ
    from slack_files import upload_file

    data = api.get(reel_id)
    if not data["consent"]["eligible"]:
        api.failed(reel_id, f"作れません: {data['consent'].get('reason') or '同意がありません'}")
        print("同意・公開の条件を満たさないため作らない", flush=True)
        return Path()
    rep = data["report"]
    work = work or Path(tempfile.mkdtemp(prefix="report-reel-"))
    work.mkdir(parents=True, exist_ok=True)
    src = download(rep["media_url"], work / f"src{media_suffix(rep.get('media_content_type'), rep['kind'])}")
    ep = build_episode(data, datetime.datetime.now(JST).date())
    caption = build_caption(data)
    (work / "ep.json").write_text(json.dumps(ep, ensure_ascii=False, indent=1), encoding="utf-8")
    out = work / f"{reel_id}.mp4"
    try:
        total, narr = R.build_video(ep, src, rep["kind"], out, silent_mode=silent, work=work)
    except subprocess.CalledProcessError:
        if rep["kind"] != "photo" or not rep.get("thumb_url"):
            raise
        # HEIC などで写真を読めないときはサムネイル（JPEG/PNG/WebP）で作る
        print("写真を読めずサムネイルで作る", flush=True)
        src = download(rep["thumb_url"], work / "thumb.jpg")
        total, narr = R.build_video(ep, src, rep["kind"], out, silent_mode=silent, work=work)
    print(f"動画 {total:.1f}s / 声 {','.join(sorted({m.get('voice', '?') for m in narr}))} / キャプション {len(caption)}字", flush=True)
    if not upload:
        return out
    alt = R.alt_version(out)
    key = video_key(reel_id)
    r2_put(out, key)
    if alt:
        r2_put(alt, key.replace(".mp4", "-alt.mp4"))
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    slack = data.get("slack") or {}
    if not token or not slack.get("channel"):
        raise SystemExit("Slack に動画を上げられません（SLACK_BOT_TOKEN か元カードのチャンネルが無い）")
    upload_file(token, out, slack["channel"], slack.get("thread_ts"),
                f"波レポートのリール {rep.get('point_name', '')} {rep.get('captured_label') or ''}".strip(),
                ":film_frames: リールの動画（ここで再生できます）")
    if not api.ready(reel_id, key, caption):
        print("ready が受け付けられなかった（409。見送り・作り直しが押された等）", flush=True)
    return out


# ---------------------------------------------------------------- 投稿
def do_publish(api: ReelApi, reel_id: str, work: Path | None = None) -> str:
    from publish_edu import Held, find_media, first_line, publish, recent_media
    from publish_reels import IG

    data = api.get(reel_id)
    reel = data["reel"]
    if reel["status"] != "publishing":
        print(f"状態が {reel['status']} のため投稿しない", flush=True)
        return "skipped"
    if not data["consent"]["eligible"]:
        api.failed(reel_id, f"投稿しません: {data['consent'].get('reason') or '同意がありません'}")
        return "skipped"
    caption, key = reel.get("caption") or "", reel.get("video_key") or ""
    if not caption or key != video_key(reel_id):
        raise SystemExit("キャプションか動画の key がありません")
    work = work or Path(tempfile.mkdtemp(prefix="report-reel-pub-"))
    video = work / f"{reel_id}.mp4"
    if not r2_get(key, video):
        raise SystemExit(f"R2 から動画を取れませんでした: {key}")
    r2_get(key.replace(".mp4", "-alt.mp4"), work / f"{reel_id}-alt.mp4")  # 無くてもよい
    ig = IG()
    head = first_line(caption)
    found = find_media(recent_media(ig, 30), head)
    if found:  # 応答だけ失敗して実は公開済み
        api.published(reel_id, found[0], found[1])
        return "recorded"
    try:
        media_id = publish(ig, video, caption, 1500, still_ok=lambda: api.get(reel_id)["reel"]["status"] == "publishing")
    except Held:
        print("投稿の途中で状態が変わったため公開しない", flush=True)
        return "skipped"
    permalink = ""
    if media_id == "posted":
        media_id, permalink = find_media(recent_media(ig, 20), head) or ("", "")
    else:
        try:
            permalink = ig._req("GET", f"https://graph.facebook.com/{ig.v}/{media_id}?fields=permalink").get("permalink", "")
        except SystemExit as e:
            print("permalink を取れず:", str(e)[:200], flush=True)
    for attempt in range(3):
        try:
            api.published(reel_id, media_id, permalink)
            break
        except SystemExit as e:
            print(f"記録に失敗 ({attempt + 1}/3):", str(e)[:200], flush=True)
            if attempt == 2:
                raise
            time.sleep(30)
    print("published", media_id, flush=True)
    return "published"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["build", "publish"])
    ap.add_argument("--reel-id", required=True)
    ap.add_argument("--silent", action="store_true", help="TTS を使わない（確認用）")
    ap.add_argument("--no-upload", action="store_true", help="R2・Slack・API に送らない（確認用）")
    a = ap.parse_args()
    if not UUID.match(a.reel_id or ""):
        print("reel_id の形が不正", flush=True)
        return 2
    api = ReelApi()
    try:
        if a.action == "build":
            do_build(api, a.reel_id, silent=a.silent, upload=not a.no_upload)
        else:
            do_publish(api, a.reel_id)
    except BaseException as e:  # noqa: BLE001  SystemExit（API・IG のエラー）も含めて記録してから落とす
        if isinstance(e, KeyboardInterrupt):
            raise
        msg = str(e) or e.__class__.__name__
        if isinstance(e, subprocess.CalledProcessError):
            msg = f"{e.cmd[0] if e.cmd else 'command'} が失敗（exit {e.returncode}）"
        if not a.no_upload:
            api.failed(a.reel_id, f"{'制作' if a.action == 'build' else '投稿'}に失敗: {msg[:400]}")
        print("失敗:", msg[:300], flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
