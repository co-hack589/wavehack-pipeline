"""Slack に動画ファイルを直接上げる（新方式: files.getUploadURLExternal → upload_url へ POST → files.completeUploadExternal）。
旧 files.upload は廃止済みのため使わない。bot に files:write が要る。

失敗しても例外を投げず False を返す（予告・投稿の流れを止めない）。トークンはログに出さない。
環境変数: SLACK_BOT_TOKEN（無ければ何もしない）
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://slack.com/api"
# これより大きければ変換し直した小さい版（<name>-alt.mp4）を選ぶ。Slack 内で再生しやすい大きさの目安
MAX_BYTES = 50 * 1024 * 1024


class SlackError(Exception):
    pass


def pick_video(video: Path, max_bytes: int = MAX_BYTES) -> Path | None:
    """上げる動画を選ぶ。本体が max_bytes 以下なら本体、超えていれば alt と比べて小さい方。どちらも無ければ None。"""
    alt = video.with_name(video.stem + "-alt.mp4")
    cands = [p for p in (video, alt) if p.exists() and p.stat().st_size > 0]
    if not cands:
        return None
    if video in cands and video.stat().st_size <= max_bytes:
        return video
    return min(cands, key=lambda p: p.stat().st_size)


def _call(token: str, method: str, params: dict, timeout: int = 60) -> dict:
    req = urllib.request.Request(f"{API}/{method}", data=urllib.parse.urlencode(params).encode(), method="POST",
                                 headers={"authorization": f"Bearer {token}",
                                          "content-type": "application/x-www-form-urlencoded; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read() or b"{}")
    if not body.get("ok"):
        raise SlackError(f"{method}: {body.get('error', 'unknown_error')}")
    return body


def upload_file(token: str, path: Path, channel: str, thread_ts: str | None = None,
                title: str | None = None, comment: str | None = None) -> str:
    """ファイルを channel（thread_ts があればそのスレッド）に共有し、file id を返す。失敗は SlackError / URLError。"""
    data = path.read_bytes()
    got = _call(token, "files.getUploadURLExternal", {"filename": path.name, "length": str(len(data))})
    req = urllib.request.Request(got["upload_url"], data=data, method="POST",
                                 headers={"content-type": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=600) as r:
        if r.status != 200:
            raise SlackError(f"upload_url: HTTP {r.status}")
    params = {"files": json.dumps([{"id": got["file_id"], "title": title or path.name}], ensure_ascii=False),
              "channel_id": channel}
    if thread_ts:
        params["thread_ts"] = thread_ts
    if comment:
        params["initial_comment"] = comment
    _call(token, "files.completeUploadExternal", params)
    return got["file_id"]


def share_video(video: Path, channel: str | None, thread_ts: str | None, title: str, comment: str | None = None,
                token: str | None = None) -> bool:
    """動画を Slack のスレッドに上げる。トークン・宛先・動画が無い、または失敗したら False（呼び出し側は続行する）。"""
    token = (token if token is not None else os.environ.get("SLACK_BOT_TOKEN", "")).strip()
    if not token:
        print("Slack に動画は上げない（SLACK_BOT_TOKEN なし。リンクの予告のみ）", flush=True)
        return False
    if not channel:
        print("Slack に動画は上げない（予告のチャンネルが不明）", flush=True)
        return False
    path = pick_video(video)
    if not path:
        print("Slack に動画は上げない（動画ファイルが無い）", flush=True)
        return False
    try:
        upload_file(token, path, channel, thread_ts, title, comment)
    except (SlackError, urllib.error.URLError, TimeoutError, OSError, KeyError, ValueError) as e:
        # missing_scope（files:write 未追加）などもここ。リンクの予告は出ているので流れは止めない
        msg = str(e).replace(token, "***")
        print(f"Slack への動画アップロードに失敗（リンクの予告のみ）: {msg[:200]}", flush=True)
        return False
    print(f"Slack に動画を上げた: {path.name}（{path.stat().st_size // 1024} KB）", flush=True)
    return True
