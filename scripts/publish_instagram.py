"""generate_instagram_post.py の出力を Instagram Graph API でフィード投稿する。

Content Publishing の 3 ステップ:
  1. POST /{ig-user-id}/media           (image_url + caption → container id)
  2. GET  /{container-id}?fields=status_code  が FINISHED になるまで待つ
  3. POST /{ig-user-id}/media_publish   (creation_id)

環境変数:
  IG_USER_ID       — Instagram プロアカウントの ID
  IG_ACCESS_TOKEN  — 投稿権限 (instagram_business_content_publish 等) を持つトークン
  IG_API_HOST      — graph.instagram.com (Instagram ログイン方式, 既定)
                     / graph.facebook.com (Facebook ページ連携方式)
  IG_API_VERSION   — 既定 v23.0

実行例:
  python scripts/publish_instagram.py --post-json out/instagram/post.json \
      --image-url https://wavehack.surf/api/social/2026-10-06.jpg
  python scripts/publish_instagram.py ... --dry-run   # API を叩かず内容だけ表示
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def _call(method: str, url: str, params: dict[str, str]) -> dict[str, Any]:
    data = urllib.parse.urlencode(params).encode()
    if method == "GET":
        req = urllib.request.Request(f"{url}?{data.decode()}")
    else:
        req = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            return json.loads(res.read())
    except urllib.error.HTTPError as e:
        # エラー本文にトークンは含まれないので、そのまま出して原因を追えるようにする
        raise SystemExit(f"Instagram API {e.code}: {e.read().decode(errors='replace')}") from e


def publish(image_url: str, caption: str, *, poll_sec: int = 5, timeout_sec: int = 300) -> str:
    user_id = os.environ["IG_USER_ID"]
    token = os.environ["IG_ACCESS_TOKEN"]
    host = os.environ.get("IG_API_HOST", "graph.instagram.com")
    version = os.environ.get("IG_API_VERSION", "v23.0")
    base = f"https://{host}/{version}"

    container = _call("POST", f"{base}/{user_id}/media", {
        "image_url": image_url,
        "caption": caption,
        "access_token": token,
    })["id"]
    print(f"container: {container}")

    deadline = time.monotonic() + timeout_sec
    while True:
        status = _call("GET", f"{base}/{container}", {
            "fields": "status_code,status",
            "access_token": token,
        })
        code = status.get("status_code")
        print(f"status: {code}")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise SystemExit(f"container {container} failed: {status}")
        if time.monotonic() > deadline:
            raise SystemExit(f"container {container} not ready after {timeout_sec}s: {status}")
        time.sleep(poll_sec)

    media_id = _call("POST", f"{base}/{user_id}/media_publish", {
        "creation_id": container,
        "access_token": token,
    })["id"]
    print(f"published media: {media_id}")
    return media_id


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--post-json", type=Path, required=True)
    ap.add_argument("--image-url", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    post = json.loads(args.post_json.read_text(encoding="utf-8"))
    caption = post["caption"]
    if len(caption) > 2200:
        print("ERROR: caption が 2200 文字を超えています", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"[dry-run] image_url={args.image_url}\n{caption}")
        return 0

    # Meta 側から画像が取得できない状態で container を作るとエラーになるので先に確認
    with urllib.request.urlopen(args.image_url, timeout=30) as res:
        if res.headers.get_content_type() != "image/jpeg":
            print(f"ERROR: {args.image_url} が JPEG ではありません", file=sys.stderr)
            return 1

    publish(args.image_url, caption)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
