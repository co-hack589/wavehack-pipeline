"""長期ユーザートークン（Graph API Explorer → アクセストークンデバッガーで延長したもの）から、
Facebook ページ「Wave Hack」のページトークン（無期限）と Instagram ビジネスアカウント ID を取り、
/Users/takeda.toi/secret/wavehack/instagram.env（chmod 600）に保存する。

トークンは標準入力で渡す（コマンド履歴やプロセス一覧に残さないため）:
  pbpaste | python3 reel/setup_instagram_token.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

V = "v26.0"
PAGE_NAME = "Wave Hack"
PAGE_ID = "1340551445812528"  # business_management 無しだと me/accounts が空になるため ID で直接取得
OUT = Path("/Users/takeda.toi/secret/wavehack/instagram.env")


def get(path: str, token: str, **params) -> dict:
    q = urllib.parse.urlencode({**params, "access_token": token})
    with urllib.request.urlopen(f"https://graph.facebook.com/{V}/{path}?{q}", timeout=60) as r:
        return json.load(r)


def main() -> int:
    user_token = sys.stdin.read().strip()
    if not user_token:
        raise SystemExit("標準入力にトークンがありません")
    page = get(PAGE_ID, user_token, fields="id,name,access_token,instagram_business_account{id,username}")
    if page.get("name") != PAGE_NAME or "access_token" not in page:
        raise SystemExit(f"ページ「{PAGE_NAME}」のトークンを取得できません: {page.get('name')}")
    ig = page.get("instagram_business_account")
    if not ig:
        raise SystemExit("ページに Instagram ビジネスアカウントが紐付いていません")
    # ページトークンの有効期限を確認（長期ユーザートークン由来なら expires_at=0 = 無期限）
    debug = get("debug_token", page["access_token"], input_token=page["access_token"])["data"]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(OUT, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"IG_USER_ID={ig['id']}\nIG_ACCESS_TOKEN={page['access_token']}\nIG_API_VERSION={V}\n"
                f"FB_PAGE_ID={page['id']}\n")
    os.chmod(OUT, 0o600)
    print(f"saved {OUT}  instagram=@{ig.get('username')} ({ig['id']})  page={page['id']}  "
          f"expires_at={debug.get('expires_at')} scopes={debug.get('scopes')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
