"""SNS 投稿エージェント共通: 文章生成（Claude を優先、無ければ Gemini）と JSON の取り出し。

generate() が入口。ANTHROPIC_API_KEY があれば Claude（Messages API・サーバー側の web 検索）、無ければ Gemini。
gemini() / parse_json() は money-reel/agents/common.py から移植したもの。読み上げ音声（TTS）は reel/ 側で Gemini のまま。
環境変数: ANTHROPIC_API_KEY / CLAUDE_TEXT_MODELS（カンマ区切りで上書き）/ CLAUDE_SEARCH=off（Claude の検索を使わない）
          GEMINI_API_KEY（Claude が無いとき）/ GEMINI_TEXT_MODELS / GEMINI_SEARCH=off
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

GEMINI = "https://generativelanguage.googleapis.com/v1beta/models"
# 先頭から順に試す（無い・枠切れなら次へ）。週次リールのナレーションと同じ系列
TEXT_MODELS = os.environ.get("GEMINI_TEXT_MODELS", "gemini-3.8-flash,gemini-3.7-flash,gemini-flash-latest").split(",")
SEARCH_OK = os.environ.get("GEMINI_SEARCH", "auto") != "off"  # 検索（グラウンディング）が使えるか。無料枠では使えないことがある

ANTHROPIC = "https://api.anthropic.com/v1/messages"
CLAUDE_MODELS = os.environ.get("CLAUDE_TEXT_MODELS", "claude-sonnet-5-5,claude-opus-5-5").split(",")
# サーバー側の web 検索。1回の呼び出しで検索は5回まで
CLAUDE_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}
CLAUDE_RETRY_WAIT = (20, 60, 0)  # 529（混雑）・5xx・応答待ちの打ち切りは待って同じモデルで再試行（3回まで）

_MODELS: set[str] | None = None


def _key() -> str:
    return os.environ["GEMINI_API_KEY"].strip()  # Secrets にコピペした値の末尾改行対策


def available_models() -> set[str]:
    """generateContent が使えるモデル名（ListModels）。取れなければ空。"""
    global _MODELS
    if _MODELS is None:
        try:
            req = urllib.request.Request(f"{GEMINI}?pageSize=200", headers={"x-goog-api-key": _key()})
            with urllib.request.urlopen(req, timeout=60) as r:
                ms = json.load(r).get("models", [])
            _MODELS = {x["name"].split("/", 1)[1] for x in ms if "generateContent" in x.get("supportedGenerationMethods", [])}
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError):
            _MODELS = set()
    return _MODELS


def gemini(prompt: str, *, search: bool = False, temperature: float = 0.7, models: list[str] | None = None) -> tuple[str, list[str]]:
    """テキストと、検索で参照した URL の一覧を返す。モデルが無い・混雑・枠切れなら次のモデルへ。
    検索が枠切れなら、以後は検索なしで呼ぶ（呼び出し側は SEARCH_OK を見て指示を変える）。"""
    global SEARCH_OK
    last = ""
    cands = [x.strip() for x in (models or TEXT_MODELS) if x.strip()]
    order = [x for x in cands if x in available_models()] or cands
    # 混雑（503）や応答待ちの打ち切りは一時的なことが多いので、全モデルを一巡したら待ってもう一巡する
    for rnd, m in enumerate(order * 2):
        if rnd == len(order):
            print("[gemini] 全モデルが混雑。3分待ってもう一巡します", flush=True)
            time.sleep(180)
        for attempt in range(3):
            body: dict = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": temperature}}
            if search and SEARCH_OK:
                body["tools"] = [{"google_search": {}}]
            req = urllib.request.Request(f"{GEMINI}/{m}:generateContent", json.dumps(body).encode(),
                                         {"Content-Type": "application/json", "x-goog-api-key": _key()})
            try:
                with urllib.request.urlopen(req, timeout=300) as r:
                    d = json.load(r)
                cand = d["candidates"][0]
                text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
                urls = [c["web"]["uri"] for c in cand.get("groundingMetadata", {}).get("groundingChunks", []) if "web" in c]
                print(f"[gemini] {m} ok ({len(text)}字, 検索{'あり' if 'tools' in body else 'なし'}, 参照{len(urls)}件)", flush=True)
                return text, urls
            except urllib.error.HTTPError as e:
                raw = e.read().decode(errors="replace")
                last = f"{m} {e.code} {raw.replace(chr(10), ' ')[:200]}"
                if e.code == 429 and "tools" in body and "free_tier" not in raw:
                    SEARCH_OK = False  # 検索の枠が無い → 検索なしでやり直す
                    print("[gemini] 検索が使えないため、検索なしに切り替えます", flush=True)
                    continue
                if e.code in (400, 403, 404, 429):
                    break  # このモデルは使えない・枠切れ → 次のモデル
                time.sleep((30, 90, 0)[attempt])  # 5xx（混雑）は待って再試行
            except (urllib.error.URLError, TimeoutError) as e:
                last = f"{m} {e}"
                if attempt == 0:
                    continue  # 応答待ちの打ち切り・通信断は同じモデルでもう1回
                break
            except (KeyError, IndexError) as e:
                last = f"{m} {e}"
                break  # 応答が壊れている → 次のモデル
        print(f"[gemini] {last}", flush=True)
    raise SystemExit(f"Gemini が使えません: {last}")


def _claude_key() -> str:
    return os.environ.get("ANTHROPIC_API_KEY", "").strip()  # Secrets にコピペした値の末尾改行対策


def provider() -> str:
    """文章生成に使う先。ANTHROPIC_API_KEY があれば Claude、無ければ Gemini。"""
    return "claude" if _claude_key() else "gemini"


def _claude_post(body: dict, timeout: int = 600) -> dict:
    req = urllib.request.Request(ANTHROPIC, json.dumps(body).encode(), {
        "Content-Type": "application/json", "x-api-key": _claude_key(), "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _claude_text(content: list) -> tuple[str, list[str]]:
    """応答の本文（最後の検索結果より後の text）と、検索で参照した URL（引用を先に）を取り出す。"""
    last_tool = max((i for i, b in enumerate(content) if b.get("type") in ("server_tool_use", "web_search_tool_result")), default=-1)
    text = "".join(b.get("text", "") for b in content[last_tool + 1:] if b.get("type") == "text")
    if not text.strip():  # 検索の後に本文が無い形なら、text を全部つなぐ
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
    cited = [c["url"] for b in content if b.get("type") == "text" for c in (b.get("citations") or []) if c.get("url")]
    found = [r["url"] for b in content if b.get("type") == "web_search_tool_result" and isinstance(b.get("content"), list)
             for r in b["content"] if isinstance(r, dict) and r.get("url")]
    return text, list(dict.fromkeys(cited + found))


def _search_tool_error(raw: str) -> bool:
    """400/403 の理由が web 検索ツール（組織で無効など）か。"""
    s = raw.lower()
    return "web_search" in s or "web search" in s


def claude(prompt: str, *, search: bool = False, temperature: float = 0.7, models: list[str] | None = None) -> tuple[str, list[str]]:
    """gemini() と同じ形（テキスト, 参照 URL）で返す。529/5xx/応答待ちの打ち切りは待って再試行し、
    それでも駄目・モデルが無い・断られた（refusal）なら次のモデルへ。検索ツールが使えなければ以後は検索なし（SEARCH_OK=False）。
    temperature は受け取るだけで送らない（Sonnet 5.5 / Opus 5.5 は既定以外の値を 400 で断る）。"""
    global SEARCH_OK
    del temperature
    last = ""
    cands = [x.strip() for x in (models or CLAUDE_MODELS) if x.strip()]
    for m in cands:
        attempt = 0
        while attempt < len(CLAUDE_RETRY_WAIT):
            use_search = search and SEARCH_OK
            messages: list[dict] = [{"role": "user", "content": prompt}]
            body: dict = {"model": m, "max_tokens": 16000, "messages": messages}
            if use_search:
                body["tools"] = [CLAUDE_SEARCH_TOOL]
            try:
                d = _claude_post(body)
                content = list(d.get("content") or [])
                for _ in range(3):  # 検索が長いと pause_turn で止まる → そこまでの応答を付けて続きを頼む
                    if d.get("stop_reason") != "pause_turn":
                        break
                    body["messages"] = messages + [{"role": "assistant", "content": content}]
                    d = _claude_post(body)
                    content += list(d.get("content") or [])
                if d.get("stop_reason") == "refusal":
                    last = f"{m} refusal"
                    break  # 断られた → 次のモデル
                text, urls = _claude_text(content)
                if not text.strip():
                    last = f"{m} 本文が空（stop_reason={d.get('stop_reason')}）"
                    break
                print(f"[claude] {m} ok ({len(text)}字, 検索{'あり' if use_search else 'なし'}, 参照{len(urls)}件)", flush=True)
                return text, urls
            except urllib.error.HTTPError as e:
                raw = e.read().decode(errors="replace")
                last = f"{m} {e.code} {raw.replace(chr(10), ' ')[:200]}"
                if e.code in (400, 403) and use_search and _search_tool_error(raw):
                    SEARCH_OK = False  # 検索ツールが使えない → 検索なしでやり直す（確認済みの出典一覧の方式になる）
                    print("[claude] 検索が使えないため、検索なしに切り替えます", flush=True)
                    continue
                if e.code == 429 or e.code == 529 or e.code >= 500:
                    time.sleep(CLAUDE_RETRY_WAIT[attempt])  # 混雑・枠 → 待って再試行
                    attempt += 1
                    continue
                if e.code in (401,):
                    raise SystemExit(f"Claude が使えません（API キー）: {last}")
                break  # 400/403/404 など → このモデルは使えない。次のモデル
            except (urllib.error.URLError, TimeoutError) as e:
                last = f"{m} {e}"
                time.sleep(CLAUDE_RETRY_WAIT[attempt])
                attempt += 1
            except (KeyError, IndexError, TypeError, ValueError, AttributeError) as e:
                last = f"{m} 応答が壊れている {e}"
                break
        print(f"[claude] {last}", flush=True)
    raise SystemExit(f"Claude が使えません: {last}")


if os.environ.get("CLAUDE_SEARCH", "auto") == "off" and _claude_key():
    SEARCH_OK = False


def generate(prompt: str, *, search: bool = False, temperature: float = 0.7, models: list[str] | None = None) -> tuple[str, list[str]]:
    """文章生成の入口。Claude（ANTHROPIC_API_KEY があるとき）か Gemini。"""
    fn = claude if provider() == "claude" else gemini
    return fn(prompt, search=search, temperature=temperature, models=models)


def search_available() -> bool:
    """検索が使えるかを1回だけ試す（Claude なら web 検索ツール、Gemini ならグラウンディング）。"""
    try:
        generate("1+1は？数字だけ", search=True, temperature=0)
    except SystemExit:
        pass
    return SEARCH_OK


def parse_json(text: str):
    """応答から JSON を取り出す（```json ブロック or 最初の { / [ から）。"""
    m = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    s = m.group(1) if m else text
    start = min([i for i in (s.find("{"), s.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise ValueError("JSON が見つかりません")
    return json.JSONDecoder().raw_decode(s[start:])[0]
