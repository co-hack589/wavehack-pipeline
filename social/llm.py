"""SNS 投稿エージェント共通: Gemini 呼び出し（モデルの順次切替・検索グラウンディング）と JSON の取り出し。

money-reel/agents/common.py の gemini() / parse_json() を移植したもの。
環境変数: GEMINI_API_KEY（必須）/ GEMINI_TEXT_MODELS（カンマ区切りで上書き）/ GEMINI_SEARCH=off（検索を使わない）
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


def search_available() -> bool:
    """検索（グラウンディング）が使えるかを1回だけ試す。"""
    try:
        gemini("1+1は？数字だけ", search=True, temperature=0)
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
