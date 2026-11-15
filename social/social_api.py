"""wavehack-api の SNS ネタ管理（/social/ideas）を呼ぶクライアント。Slack への投稿と承認状態の管理は API 側が持つ。

環境変数: WAVEHACK_API_URL（例: https://wavehack-api.onrender.com）/ WAVEHACK_CRON_SECRET（Bearer）
  POST /social/ideas/batch          {batch, ideas}         → 200 {ids} / 409 {error: "batch_exists"}
  GET  /social/ideas?limit=200                              → {ideas: [{id, batch, seq, kind, title, status, episode_no}]}
  GET  /social/ideas/next?kind=edu                          → {idea} / {idea: null}（許可済みで最も古い1件。episode_no 採番済み）
  GET  /social/ideas/:id                                    → {idea}
  POST /social/ideas/:id/preview    {preview_url, scheduled_at} → 200 / 409（許可済み以外）
  POST /social/ideas/:id/posted     {media_id, permalink}
  POST /social/ideas/:id/failed     {reason}
状態: proposed / approved / rejected / preview / held / posted / failed
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request


class ApiError(SystemExit):
    def __init__(self, status: int, body: dict | str, where: str):
        self.status, self.body = status, body
        super().__init__(f"{where} -> {status}: {str(body)[:300]}")


class SocialApi:
    def __init__(self, base: str | None = None, secret: str | None = None) -> None:
        self.base = (base or os.environ["WAVEHACK_API_URL"]).strip().rstrip("/")
        self.secret = (secret or os.environ["WAVEHACK_CRON_SECRET"]).strip()

    def _req(self, method: str, path: str, body: dict | None = None, ok: tuple[int, ...] = ()) -> tuple[int, dict]:
        """(HTTP 状態, 本文) を返す。2xx と ok に挙げた状態以外は ApiError。
        Render のコールドスタート（〜45秒）と一時的な 5xx・通信断は 3回まで試す。"""
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        headers = {"authorization": f"Bearer {self.secret}", "content-type": "application/json"}
        last: Exception | None = None
        for attempt in range(3):
            req = urllib.request.Request(f"{self.base}{path}", data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return r.status, _json(r.read())
            except urllib.error.HTTPError as e:
                raw = _json(e.read())
                if e.code in ok:
                    return e.code, raw
                last = ApiError(e.code, raw, f"{method} {path.split('?')[0]}")
                if e.code < 500:
                    raise last from e
            except (urllib.error.URLError, TimeoutError) as e:
                last = ApiError(0, str(e), f"{method} {path.split('?')[0]}")
            if attempt < 2:
                time.sleep(20 * (attempt + 1))
        raise last  # type: ignore[misc]

    # ---- ネタ出し
    def post_batch(self, batch: str, ideas: list[dict]) -> tuple[bool, list]:
        """送れたら (True, ids)。その週の batch が既にあれば (False, [])。"""
        st, d = self._req("POST", "/social/ideas/batch", {"batch": batch, "ideas": ideas}, ok=(409,))
        if st == 409:
            return False, []
        return True, d.get("ids", [])

    def list_ideas(self, limit: int = 200) -> list[dict]:
        return self._req("GET", f"/social/ideas?limit={limit}")[1].get("ideas", [])

    # ---- 制作・投稿
    def next_idea(self, kind: str = "edu") -> dict | None:
        return self._req("GET", f"/social/ideas/next?kind={urllib.parse.quote(kind)}")[1].get("idea")

    def get_idea(self, idea_id: str) -> dict:
        return self._req("GET", f"/social/ideas/{urllib.parse.quote(str(idea_id))}")[1]["idea"]

    def preview(self, idea_id: str, preview_url: str, scheduled_at: str) -> bool:
        """予告にできたら True。許可済み以外（止められた等）で 409 なら False。"""
        st, _ = self._req("POST", f"/social/ideas/{urllib.parse.quote(str(idea_id))}/preview",
                          {"preview_url": preview_url, "scheduled_at": scheduled_at}, ok=(409,))
        return st != 409

    def posted(self, idea_id: str, media_id: str, permalink: str) -> None:
        self._req("POST", f"/social/ideas/{urllib.parse.quote(str(idea_id))}/posted", {"media_id": media_id, "permalink": permalink})

    def failed(self, idea_id: str, reason: str) -> None:
        self._req("POST", f"/social/ideas/{urllib.parse.quote(str(idea_id))}/failed", {"reason": reason[:500]})


def _json(raw: bytes) -> dict:
    try:
        return json.loads(raw.decode() or "{}")
    except ValueError:
        return {"raw": raw.decode(errors="replace")[:300]}
