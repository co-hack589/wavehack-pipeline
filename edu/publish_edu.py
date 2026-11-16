"""教育シリーズのリールを Instagram に投稿する（週次リールの publish_reels.IG を流用）。

第2弾（#11〜）は、運営者が Slack で許可した台本だけを wavehack-api のキュー（/social/ideas）から取って作る。
  python edu/publish_edu.py --next [--idea-id ID] [--dry-run]
      次に作る回を API から取り、edu/out/epNN.json（台本 + no）を書く。今日すでに教育リールを投稿済みなら何もしない。
      前日に予告したまま投稿しなかった回（status=preview）があれば、そちらを先に出す。
  python edu/publish_edu.py --preview --id ID --ep edu/out/ep11.json --video edu/out/ep11.mp4 --date 2026-11-16 [--now]
      動画を運営者が見られる URL に置き、POST /preview（予定時刻は当日 18:00 JST）。止められていれば previewed=false
  python edu/publish_edu.py --id ID --ep edu/out/ep11.json --video edu/out/ep11.mp4 [--story edu/out/story.jpg]
      投稿直前に GET /social/ideas/:id で状態を確かめ、preview のときだけ投稿 → POST /posted（失敗は POST /failed）
  python edu/publish_edu.py --ep edu/episodes/ep02.json --dry-run      # キャプションだけ表示
第1弾（edu/episodes/ep01〜10.json）は手本として残す。番号順に選ぶ仕組み（旧 --pick）は再投稿を防ぐため廃止した。
キャプションは ep*.json の caption から組み立てる。同じ1行目の投稿があれば投稿せず、その投稿を記録する。
環境変数: IG_USER_ID / IG_ACCESS_TOKEN / WAVEHACK_API_URL / WAVEHACK_CRON_SECRET
  EDU_PREVIEW_MODE — run（既定。GitHub Actions の実行ページ＝動画の artifact）/ r2（R2 social/ に mp4 を置き
                     https://wavehack.surf/api/social/<file> で配信。フロントの /api/social が mp4 に対応してから使う）
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "reel"))
sys.path.insert(0, str(HERE.parent / "social"))
from social_api import SocialApi  # noqa: E402
from publish_reels import COVER_BASE_URL, IG, publish_story  # noqa: E402

JST = datetime.timezone(datetime.timedelta(hours=9))
TAGS = "#波予報の読み方 #波情報 #波予報 #サーフィン #サーフィン初心者 #サーフィン上達 #surfjapan #wavehack"
HEAD = "🌊 波予報の読み方 #"
# rupload が一時的に 400 ProcessingFailedError を返すことが多い（教育リールは初回に多い）ので、間隔を伸ばして送り直す
RETRY_WAITS = (120, 300, 900)


def caption(ep: dict) -> str:
    head = f"{HEAD}{ep['no']:02d}｜{ep['title']}"
    body = ep.get("caption") or "\n".join(s["say"] for s in ep["scenes"][:-1])
    if ep.get("next"):
        tail = f"次回 #{ep['no'] + 1:02d} は「{ep['next']}」。毎日18時に更新（木曜は週末の波予想）"
    elif ep["no"] <= 10:
        tail = "シリーズ全10回、最後までありがとうございました。"
    else:  # 第2弾: 締めの呼びかけは下の「保存して、次の海選びに。」の1回だけ（フォローは動画の締め画面で見せる）
        tail = ""
    return "\n".join([head, "", body, "", tail, "保存して、次の海選びに。", f"※ {ep['source']}" if ep.get("source") else "", "", TAGS]).replace("\n\n\n", "\n\n")


def first_line(text: str) -> str:
    return text.split("\n", 1)[0]


class Held(Exception):
    """投稿の途中で運営者に止められた"""


def publish(ig: IG, video: Path, text: str, thumb_ms: int, waits: tuple[int, ...] = RETRY_WAITS,
            still_ok=None) -> str:
    # 同じファイルは何度送っても ProcessingFailedError になることがあるので、変換し直した版があれば交互に送る
    alt = video.with_name(video.stem + "-alt.mp4")
    files = [video, alt] if alt.exists() else [video]
    """still_ok: 公開の直前と送り直しの前に呼び、False なら Held を投げて公開しない。"""
    base = f"https://graph.facebook.com/{ig.v}"
    head = first_line(text)
    tries = len(waits) + 1
    for attempt in range(1, tries + 1):
        try:
            params = {"media_type": "REELS", "upload_type": "resumable", "caption": text, "share_to_feed": "true", "thumb_offset": str(thumb_ms)}
            container = ig._req("POST", f"{base}/{ig.user}/media", urllib.parse.urlencode(params).encode())["id"]
            f = files[(attempt - 1) % len(files)]
            print("送る動画:", f.name, flush=True)
            ig._req("POST", f"https://rupload.facebook.com/ig-api-upload/{ig.v}/{container}", f.read_bytes(),
                    {"offset": "0", "file_size": str(f.stat().st_size)})
            for _ in range(60):
                st = ig._req("GET", f"{base}/{container}?fields=status_code,status")
                if st.get("status_code") == "FINISHED":
                    break
                if st.get("status_code") in ("ERROR", "EXPIRED"):
                    raise SystemExit(f"container {container}: {st}")
                time.sleep(10)
            if still_ok and not still_ok():
                raise Held()
            return ig._req("POST", f"{base}/{ig.user}/media_publish", urllib.parse.urlencode({"creation_id": container}).encode())["id"]
        except SystemExit as e:
            print(f"投稿失敗 ({attempt}/{tries}):", str(e)[:300], flush=True)
            if attempt == tries:
                raise
            print(f"{waits[attempt - 1]} 秒待って送り直す", flush=True)
            time.sleep(waits[attempt - 1])
            if still_ok and not still_ok():
                raise Held()
            if head in {first_line(c) for c in ig.recent_captions(10)}:
                return "posted"  # 応答だけ失敗して実は公開済みなら二重投稿しない
    raise SystemExit("unreachable")


# ---------------------------------------------------------------- Instagram の投稿の確認
def recent_media(ig: IG, limit: int = 100) -> list[dict]:
    return ig._req("GET", f"https://graph.facebook.com/{ig.v}/{ig.user}/media?fields=id,caption,timestamp,permalink&limit={limit}").get("data", [])


def posted_today(media: list[dict], now: datetime.datetime | None = None) -> bool:
    """今日（JST）すでに教育リールを投稿したか。Instagram の投稿日時で判定する（API 経由でない投稿も数える）。"""
    today = (now or datetime.datetime.now(JST)).date()
    for m in media:
        if first_line(m.get("caption") or "").startswith(HEAD):
            if datetime.datetime.strptime(m["timestamp"], "%Y-%m-%dT%H:%M:%S%z").astimezone(JST).date() == today:
                return True
    return False


def find_media(media: list[dict], head: str) -> tuple[str, str] | None:
    m = next((m for m in media if first_line(m.get("caption") or "") == head), None)
    return (m["id"], m.get("permalink", "")) if m else None


# ---------------------------------------------------------------- API の台本 → レンダリング用の ep JSON
def episode(idea: dict) -> dict:
    """API の idea から render_edu.py 用の ep を作る（script に episode_no を no として入れる）。"""
    script = idea.get("script")
    if isinstance(script, str):
        script = json.loads(script)
    if not script or not idea.get("episode_no"):
        raise SystemExit(f"idea {idea.get('id')}: script か episode_no がありません")
    ep = {"no": int(idea["episode_no"]), **{k: v for k, v in script.items() if k != "no"}}
    sc = ep.get("scenes") or []
    if ep["no"] > 10 and sc and sc[-1].get("kind") == "next":
        from edu_rules import END_SAY
        sc[-1] = {**sc[-1], "say": END_SAY}
    return ep


def choose(api: SocialApi, media: list[dict], idea_id: str | None = None, dry: bool = False) -> dict | None:
    """次に作る回（idea の全体）を返す。無ければ None。"""
    if not dry and posted_today(media):
        print("今日はすでに教育リールを投稿済み")
        return None
    if idea_id:
        return api.get_idea(idea_id)
    # 予告したのに投稿していない回（23時を過ぎて見送った・途中で止まった）を先に出す
    left = sorted((i for i in api.list_ideas() if i.get("kind") == "edu" and i.get("status") in ("preview", "failed")),
                  key=lambda i: (i.get("episode_no") or 0, i.get("seq") or 0))
    for lo in left:
        idea = api.get_idea(lo["id"])
        head = first_line(caption(episode(idea)))
        found = find_media(media, head)
        if found:  # 投稿はできていたが記録できていなかった
            print("投稿済みなので記録だけする:", head)
            if not dry:
                api.posted(idea["id"], *found)
            continue
        print("前回予告した回を出す:", head)
        return idea
    return api.next_idea("edu")


def gh_output(**kw) -> None:
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            for k, v in kw.items():
                f.write(f"{k}={v}\n")


# ---------------------------------------------------------------- 予告（Slack は API 側が出す）
def preview_url(ep: dict, video: Path) -> str:
    mode = os.environ.get("EDU_PREVIEW_MODE", "run")
    if mode == "r2":
        key = f"edu-{ep['no']:02d}-{os.environ.get('GITHUB_RUN_ID', datetime.datetime.now(JST).strftime('%Y%m%d%H%M'))}.mp4"
        subprocess.run(["npx", "-y", "wrangler@4", "r2", "object", "put", f"wavehack-data/social/{key}",
                        "--file", str(video), "--content-type", "video/mp4", "--remote"], check=True, capture_output=True)
        return f"{COVER_BASE_URL}/{key}"
    run = os.environ.get("GITHUB_RUN_ID")
    if not run:
        raise SystemExit("GITHUB_RUN_ID が無いため予告の URL を作れません（EDU_PREVIEW_MODE=r2 を使う）")
    return f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{run}"


def scheduled_at(date: str, now: bool = False) -> str:
    if now:
        return datetime.datetime.now(JST).replace(microsecond=0).isoformat()
    return datetime.datetime.combine(datetime.date.fromisoformat(date), datetime.time(18, 0), JST).isoformat()


def do_preview(api: SocialApi, idea_id: str, ep: dict, video: Path, date: str, now: bool = False) -> bool:
    ok = api.preview(idea_id, preview_url(ep, video), scheduled_at(date, now))
    if not ok:
        # 前日に予告した回の出し直し・再試行の重複は 409 になりうる。状態が preview のままなら予定どおり進める
        status = api.get_idea(idea_id).get("status")
        print(f"予告が受け付けられなかった（409, status={status}）", flush=True)
        ok = status == "preview"
    return ok


# ---------------------------------------------------------------- 投稿
def do_publish(api: SocialApi, ig: IG, idea_id: str, ep: dict, video: Path, thumb_ms: int) -> str:
    """published（投稿した）/ recorded（投稿済みを記録しただけ）/ skipped（止められていた）を返す。"""
    idea = api.get_idea(idea_id)
    if idea.get("status") != "preview":
        print(f"状態が {idea.get('status')} のため投稿しない（運営者が止めた等）:", idea.get("title"))
        return "skipped"
    if int(idea.get("episode_no") or 0) != ep["no"]:
        raise SystemExit(f"番号が合わない（API #{idea.get('episode_no')} / 動画 #{ep['no']}）")
    text = caption(ep)
    head = first_line(text)
    found = find_media(recent_media(ig), head)
    if found:
        print("投稿済みのためスキップ（記録だけする）:", head)
        api.posted(idea_id, *found)
        return "recorded"
    try:
        media_id = publish(ig, video, text, thumb_ms,
                           still_ok=lambda: api.get_idea(idea_id).get("status") == "preview")
    except Held:
        print("投稿の途中で止められたため公開しない:", head, flush=True)
        return "skipped"
    except SystemExit as e:
        api.failed(idea_id, f"Instagram への投稿に失敗: {str(e)[:400]}")
        raise
    permalink = ""
    if media_id == "posted":
        media_id, permalink = find_media(recent_media(ig, 20), head) or ("", "")
    else:
        try:
            permalink = ig._req("GET", f"https://graph.facebook.com/{ig.v}/{media_id}?fields=permalink").get("permalink", "")
        except SystemExit as e:  # URL が取れなくても投稿は成功しているので記録は続ける
            print("permalink を取れず:", str(e)[:200], flush=True)
    for attempt in range(3):
        try:
            api.posted(idea_id, media_id, permalink)
            break
        except SystemExit as e:  # 投稿は済んでいる。記録だけ送り直す（残っても翌日の確認で記録される）
            print(f"記録に失敗 ({attempt + 1}/3):", str(e)[:200], flush=True)
            if attempt == 2:
                raise
            time.sleep(60)
    print("published", media_id, permalink, time.strftime("%H:%M"), flush=True)
    return "published"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--next", action="store_true", help="次に作る回を API から取って edu/out/epNN.json を書く")
    ap.add_argument("--idea-id", help="--next で回を指定する（確認用）")
    ap.add_argument("--preview", action="store_true", help="動画を予告として API に送る")
    ap.add_argument("--id", help="API の idea id")
    ap.add_argument("--date", help="投稿日（JST、YYYY-MM-DD）")
    ap.add_argument("--now", action="store_true", help="予定時刻を今にする（手動実行の post_now）")
    ap.add_argument("--video", type=Path)
    ap.add_argument("--ep", type=Path)
    ap.add_argument("--out-dir", type=Path, default=HERE / "out")
    ap.add_argument("--thumb-ms", type=int, default=2500, help="カバーに使うコマ（問いかけの画面）")
    ap.add_argument("--story", type=Path, help="リール投稿後にストーリーズへ出す画像（render_story.py --edu の出力）")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if a.next:
        idea = choose(SocialApi(), recent_media(IG()), a.idea_id, a.dry_run)
        if not idea:
            print("作る回なし（許可済みの台本が無い、または今日は投稿済み）")
            return 0
        ep = episode(idea)
        a.out_dir.mkdir(parents=True, exist_ok=True)
        path = a.out_dir / f"ep{ep['no']:02d}.json"
        path.write_text(json.dumps(ep, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"対象: #{ep['no']:02d} {ep['title']}（idea {idea['id']}, status={idea.get('status')}）")
        gh_output(id=idea["id"], ep=str(path.relative_to(HERE.parent)) if path.is_relative_to(HERE.parent) else str(path), name=path.stem)
        return 0

    ep = json.loads(a.ep.read_text(encoding="utf-8"))
    text = caption(ep)
    if a.dry_run:
        # 公開リポジトリの実行ログに残るので、1行目と字数だけ出す（全文は artifact の ep JSON で確認）
        print(first_line(text), f"（キャプション {len(text)}字）")
        return 0
    if a.preview:
        ok = do_preview(SocialApi(), a.id, ep, a.video, a.date, a.now)
        print("予告:", "送った" if ok else "止められているため投稿しない")
        gh_output(previewed="true" if ok else "false")
        return 0
    if not a.id:
        ap.error("投稿には --id（API の idea id）が必要です（第1弾の回を直接投稿する経路は廃止）")
    done = do_publish(SocialApi(), IG(), a.id, ep, a.video, a.thumb_ms)
    if a.story and done == "published" and a.story.exists():
        try:  # ストーリーズの失敗でリール投稿を失敗扱いにしない
            print("story", publish_story(a.story, f"story-edu-{ep['no']:02d}.jpg"), flush=True)
        except Exception as e:  # noqa: BLE001
            print("ストーリーズは投稿できず:", str(e)[:200], flush=True)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
