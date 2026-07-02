"""Phase B-3.5: Anthropic Message Batches API による LLM コメント大量並列生成。

generate_llm_comment.py（B-3、単発呼び出し版）の batch 投入版。
9ポイント × 8日 × テンプレ指定 を 1 batch にまとめて 50% off で処理する。

Usage:
    # ドライラン（既定）：JSONL 生成 + トークン/コスト概算のみ。実 API 呼び出しなし
    uv run python scripts/generate_llm_comment_batch.py \\
        --templates daily_short,daily_long --date 2026-05-11

    # 実投入（ANTHROPIC_API_KEY 必須・対話確認）
    uv run python scripts/generate_llm_comment_batch.py \\
        --templates daily_short,daily_long --date 2026-05-11 --live

    # 進捗確認
    uv run python scripts/generate_llm_comment_batch.py --check-batch msgbatch_xxx

    # 結果取得（ストレージ adapter に保存）
    uv run python scripts/generate_llm_comment_batch.py --retrieve-batch msgbatch_xxx \\
        --store local

設計方針:
    - dry-run 時は anthropic SDK を import しない（依存無しで動作確認できる）
    - 既存 generate_llm_comment.py のプロンプト構築関数を再利用（重複実装しない）
    - prompt caching を最大活用：SYSTEM 部に cache_control を必ず付与
      → 同じ batch 内では同一 SYSTEM プレフィクスが何度も登場するため、
        cache write 1 回 → cache read N-1 回でコスト圧縮
    - batch 投入後の batch_id は output/batch_requests/<timestamp>.batch_id に保存
    - 部分失敗時のリトライ戦略：
        * succeeded → ストレージに保存
        * errored.invalid_request → スキップ + ログ（プロンプト不正、再投入しても同じ）
        * errored.server_error → 後で同じ custom_id で再投入（新 batch）
        * expired → 必ず再投入
        * canceled → 通常は無視（ユーザー意図でキャンセルされたため）

注意:
    - APIキー値はログ出力しない
    - batch API 上限: 100,000 リクエスト / 256 MB（Wave Hack 規模では余裕）
    - batch は最大 24 時間以内に完了（多くは 1 時間以内）
    - 結果は作成から 29 日間保持
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 既存モジュールの再利用（プロンプト構築ロジック）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate_llm_comment import (  # type: ignore  # noqa: E402
    MODEL_HAIKU,
    MODEL_SONNET,
    POINT_PROFILES,
    SYSTEM_PROMPT_FROZEN,
    TEMPLATE_TYPES,
    build_prompt_beginner_explanation,
    build_prompt_condition_compare,
    build_prompt_daily_long,
    build_prompt_daily_short,
    estimate_tokens,
    extract_point_date,
    load_dashboard,
)

ROOT = Path(__file__).resolve().parent.parent
WAVEHACK_OUTPUT = ROOT.parent.parent
BATCH_REQUESTS_DIR = WAVEHACK_OUTPUT / "batch_requests"
BATCH_REQUESTS_DIR.mkdir(parents=True, exist_ok=True)

# プロンプトテンプレートのバージョン（ストレージで再生成判定に使う）
PROMPT_VERSION = "2026-05-11.v1"

# 価格表（USD / 1M tokens）— claude-api skill 参照
PRICE_TABLE = {
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
}
USD_TO_JPY = 155
BATCH_DISCOUNT = 0.5  # 50% off
CACHE_WRITE_MULT = 1.25
CACHE_READ_MULT = 0.1


# ============================================================
# Batch リクエスト組み立て
# ============================================================
def build_batch_requests(
    dashboard: dict[str, Any],
    target_dates: list[str],
    templates: list[str],
    model: str,
    max_tokens: int = 1024,
) -> list[dict[str, Any]]:
    """Anthropic Message Batches API 形式の requests 配列を組み立てる。

    1 リクエストあたりのフォーマット:
        {
          "custom_id": "<point>__<date>__<template>",
          "params": {
            "model": "...",
            "max_tokens": 1024,
            "system": [{"type": "text", "text": "...", "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": "..."}]
          }
        }

    対象: len(target_dates) 日 × 9 ポイント × len(templates) テンプレ
    """
    requests = []
    point_names = [p["point_name"] for p in dashboard["points"]]

    for target_date in target_dates:
        for template in templates:
            if template not in TEMPLATE_TYPES:
                print(f"WARNING: unknown template '{template}', skipping", file=sys.stderr)
                continue

            if template == "condition_compare":
                # 全ポイント横断 → 1 リクエスト / 日
                system, user = build_prompt_condition_compare(target_date, dashboard["points"])
                custom_id = f"_global__{target_date}__{template}"
                requests.append(_build_one_request(custom_id, system, user, model, max_tokens))

            elif template == "beginner_explanation":
                # オンデマンド系。batch では事前 FAQ 生成用途のみ。
                # ここでは初日のみサンプル 1 件を含める（重複投入を避ける）
                if target_date != target_dates[0]:
                    continue
                system, user = build_prompt_beginner_explanation("ピーク時間ってどういう意味？")
                custom_id = f"_faq__{target_date}__{template}__peak_time"
                requests.append(_build_one_request(custom_id, system, user, model, max_tokens))

            else:
                # daily_short / daily_long → 9 ポイント × 当日
                for point_name in point_names:
                    try:
                        _point, slots, daily_summary = extract_point_date(
                            dashboard, point_name, target_date
                        )
                    except SystemExit:
                        # 当該 point/date のスロットが無い → スキップ
                        continue

                    if template == "daily_short":
                        system, user = build_prompt_daily_short(
                            point_name, target_date, slots, daily_summary
                        )
                    elif template == "daily_long":
                        system, user = build_prompt_daily_long(
                            point_name, target_date, slots, daily_summary
                        )
                    else:
                        continue

                    custom_id = f"{point_name}__{target_date}__{template}"
                    requests.append(_build_one_request(custom_id, system, user, model, max_tokens))

    return requests


def expand_date_range(start_date: str, days: int) -> list[str]:
    """start_date から days 日分の YYYY-MM-DD 文字列リストを返す。"""
    from datetime import datetime as _dt, timedelta
    base = _dt.strptime(start_date, "%Y-%m-%d")
    return [(base + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]


def _build_one_request(
    custom_id: str, system: str, user: str, model: str, max_tokens: int
) -> dict[str, Any]:
    """1 リクエスト分の dict を組み立てる。

    prompt caching: system 部に cache_control を付与。同じ batch 内で
    SYSTEM プレフィクスが共通のリクエストはキャッシュ読み出しで処理される。
    """
    return {
        "custom_id": custom_id,
        "params": {
            "model": model,
            "max_tokens": max_tokens,
            "system": [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": user}],
        },
    }


# ============================================================
# JSONL 保存 + コスト概算
# ============================================================
def save_requests_jsonl(requests: list[dict[str, Any]], timestamp: str) -> Path:
    """batch リクエストを JSONL ファイルに保存（debug & 監査用）。"""
    out_path = BATCH_REQUESTS_DIR / f"{timestamp}.jsonl"
    with out_path.open("w", encoding="utf-8") as f:
        for req in requests:
            f.write(json.dumps(req, ensure_ascii=False) + "\n")
    return out_path


def estimate_batch_cost(
    requests: list[dict[str, Any]],
    model: str,
    cache_hit_ratio: float = 0.5,
    avg_output_tokens: int = 200,
) -> dict[str, Any]:
    """batch 全体のコスト概算。

    cache_hit_ratio: SYSTEM 部のキャッシュヒット率想定。
        9 ポイント連続生成（同じ SYSTEM）の場合、理論最大 8/9 ≒ 0.89 だが、
        保守的に 0.5 で計算する。
    """
    if model not in PRICE_TABLE:
        return {"error": f"unknown model {model}"}

    p = PRICE_TABLE[model]
    n = len(requests)
    if n == 0:
        return {"requests": 0, "total_usd": 0, "total_jpy": 0}

    total_input = 0
    total_system = 0
    for req in requests:
        sys_text = req["params"]["system"][0]["text"]
        user_text = req["params"]["messages"][0]["content"]
        total_system += estimate_tokens(sys_text)
        total_input += estimate_tokens(sys_text) + estimate_tokens(user_text)

    avg_system = total_system / n
    total_user = total_input - total_system

    # cache write: 1 回 / cache read: (n-1) * cache_hit_ratio 回 / uncached: 残り
    cache_writes = avg_system * 1  # 最初の 1 回だけ書き込み
    cache_reads = avg_system * (n - 1) * cache_hit_ratio
    uncached_system = avg_system * (n - 1) * (1 - cache_hit_ratio) + avg_system  # 1 回目分
    # ※ 上の式は ergodic 近似。実際は 1 回目 = uncached + write、2 回目以降 hit/miss

    # batch 50% off
    input_uncached_cost = (total_user + uncached_system) * (p["input"] / 1_000_000) * BATCH_DISCOUNT
    cache_write_cost = cache_writes * (p["input"] * CACHE_WRITE_MULT / 1_000_000) * BATCH_DISCOUNT
    cache_read_cost = cache_reads * (p["input"] * CACHE_READ_MULT / 1_000_000) * BATCH_DISCOUNT
    output_cost = (n * avg_output_tokens) * (p["output"] / 1_000_000) * BATCH_DISCOUNT

    total_usd = input_uncached_cost + cache_write_cost + cache_read_cost + output_cost
    return {
        "requests": n,
        "model": model,
        "avg_system_tokens": int(avg_system),
        "total_input_tokens_uncached": int(total_user + uncached_system),
        "cache_writes_tokens": int(cache_writes),
        "cache_reads_tokens": int(cache_reads),
        "estimated_output_tokens": n * avg_output_tokens,
        "input_uncached_usd": round(input_uncached_cost, 4),
        "cache_write_usd": round(cache_write_cost, 4),
        "cache_read_usd": round(cache_read_cost, 4),
        "output_usd": round(output_cost, 4),
        "total_usd": round(total_usd, 4),
        "total_jpy": round(total_usd * USD_TO_JPY, 1),
        "cache_hit_ratio_assumed": cache_hit_ratio,
    }


# ============================================================
# Live 投入 / 進捗確認 / 結果取得
# ============================================================
def _check_api_key() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: 環境変数 ANTHROPIC_API_KEY が設定されていません。", file=sys.stderr)
        print("       export ANTHROPIC_API_KEY='sk-ant-...' を実行してから再試行してください。",
              file=sys.stderr)
        print("       （API キー値はログ出力しません）", file=sys.stderr)
        sys.exit(2)


def _import_anthropic():
    try:
        import anthropic  # noqa: F401
        return anthropic
    except ImportError:
        print("ERROR: anthropic SDK がインストールされていません。", file=sys.stderr)
        print("       uv sync を実行してください。", file=sys.stderr)
        sys.exit(3)


def submit_batch_live(requests: list[dict[str, Any]], timestamp: str) -> str:
    """batch を実投入。対話確認 + APIキーチェックあり。

    Returns: batch_id
    """
    _check_api_key()
    anthropic = _import_anthropic()

    print("=" * 70)
    print(f"[LIVE] {len(requests)} requests を Anthropic Message Batches API に投入します。")
    print("       課金が発生します。続行するには 'yes' を入力してください: ", end="")
    answer = input().strip().lower()
    if answer != "yes":
        print("中止しました。")
        sys.exit(0)

    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    client = anthropic.Anthropic()

    batch_requests = [
        Request(
            custom_id=req["custom_id"],
            params=MessageCreateParamsNonStreaming(**req["params"]),
        )
        for req in requests
    ]

    batch = client.messages.batches.create(requests=batch_requests)
    print(f"\n[OK] Batch 作成成功: {batch.id}")
    print(f"     processing_status: {batch.processing_status}")
    print(f"     created_at: {batch.created_at}")

    # batch_id をローカル保存
    id_file = BATCH_REQUESTS_DIR / f"{timestamp}.batch_id"
    id_file.write_text(batch.id, encoding="utf-8")
    print(f"     → batch_id を {id_file} に保存しました。")
    print(f"\n進捗確認: --check-batch {batch.id}")
    print(f"結果取得: --retrieve-batch {batch.id}")

    return batch.id


def check_batch_status(batch_id: str) -> None:
    _check_api_key()
    anthropic = _import_anthropic()
    client = anthropic.Anthropic()
    batch = client.messages.batches.retrieve(batch_id)
    print(f"Batch ID: {batch.id}")
    print(f"  processing_status: {batch.processing_status}")
    print(f"  created_at: {batch.created_at}")
    print(f"  expires_at: {batch.expires_at}")
    print(f"  request_counts:")
    print(f"    processing: {batch.request_counts.processing}")
    print(f"    succeeded: {batch.request_counts.succeeded}")
    print(f"    errored: {batch.request_counts.errored}")
    print(f"    canceled: {batch.request_counts.canceled}")
    print(f"    expired: {batch.request_counts.expired}")


def retrieve_batch_results(batch_id: str, store_kind: str = "local") -> None:
    """batch 結果を取得 → ストレージ adapter に保存。"""
    _check_api_key()
    anthropic = _import_anthropic()
    client = anthropic.Anthropic()

    batch = client.messages.batches.retrieve(batch_id)
    if batch.processing_status != "ended":
        print(f"WARNING: batch がまだ完了していません (status={batch.processing_status})。",
              file=sys.stderr)
        print("        --check-batch で進捗を確認してください。", file=sys.stderr)
        return

    # ストレージ選択（lifecycle helper と同じ adapter を再利用）
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from llm_comment_lifecycle import LocalFileStore  # type: ignore  # noqa: E402

    if store_kind == "local":
        store = LocalFileStore()
    else:
        print(f"ERROR: 未対応のストレージ種別: {store_kind}", file=sys.stderr)
        print(f"       現状サポート: local（B = postgres / C = s3_dynamo は未実装）",
              file=sys.stderr)
        sys.exit(4)

    succeeded = 0
    errored_invalid = 0
    errored_server = 0
    expired = 0

    for result in client.messages.batches.results(batch_id):
        rt = result.result.type
        cid = result.custom_id

        if rt == "succeeded":
            msg = result.result.message
            text = next((b.text for b in msg.content if b.type == "text"), "")
            usage = msg.usage
            store.put(
                custom_id=cid,
                content=text,
                model=msg.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_tokens=getattr(usage, "cache_read_input_tokens", 0),
                prompt_version=PROMPT_VERSION,
            )
            succeeded += 1

        elif rt == "errored":
            err_type = result.result.error.type if hasattr(result.result, "error") else "unknown"
            if err_type == "invalid_request":
                print(f"  [SKIP-INVALID] {cid}: 再投入しても同じエラー", file=sys.stderr)
                errored_invalid += 1
            else:
                print(f"  [RETRY-NEEDED] {cid}: {err_type}（後で同じ custom_id で再投入推奨）",
                      file=sys.stderr)
                errored_server += 1

        elif rt == "expired":
            print(f"  [EXPIRED] {cid}: 再投入必須", file=sys.stderr)
            expired += 1

    print(f"\n=== 取得結果 ===")
    print(f"  succeeded: {succeeded}")
    print(f"  errored (invalid_request): {errored_invalid}")
    print(f"  errored (server_error): {errored_server} ← 再投入候補")
    print(f"  expired: {expired} ← 再投入必須")


# ============================================================
# CLI
# ============================================================
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Phase B-3.5: Anthropic Message Batches API 統合",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--date", default="2026-05-11", help="対象開始日 YYYY-MM-DD")
    parser.add_argument(
        "--days",
        type=int,
        default=8,
        help="--date を起点に何日分生成するか（既定: 8、reliable 5日 + extended 3日）",
    )
    parser.add_argument(
        "--templates",
        default="daily_short,daily_long",
        help="カンマ区切りテンプレ名（既定: daily_short,daily_long）",
    )
    parser.add_argument(
        "--model", default=MODEL_HAIKU, choices=[MODEL_HAIKU, MODEL_SONNET]
    )
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument(
        "--cache-hit-ratio",
        type=float,
        default=0.5,
        help="prompt caching ヒット率の試算用想定値（既定: 0.5）",
    )
    parser.add_argument("--live", action="store_true", help="batch 実投入")
    parser.add_argument("--check-batch", metavar="BATCH_ID", help="既存 batch の進捗確認")
    parser.add_argument(
        "--retrieve-batch", metavar="BATCH_ID", help="batch 結果取得 + ストレージ保存"
    )
    parser.add_argument(
        "--store",
        default="local",
        choices=["local", "postgres", "s3_dynamo"],
        help="結果保存先 adapter（既定: local）",
    )
    args = parser.parse_args()

    # 進捗確認モード
    if args.check_batch:
        check_batch_status(args.check_batch)
        return 0

    # 結果取得モード
    if args.retrieve_batch:
        retrieve_batch_results(args.retrieve_batch, store_kind=args.store)
        return 0

    # 通常モード（dry-run or live）
    dashboard = load_dashboard()
    templates = [t.strip() for t in args.templates.split(",") if t.strip()]
    target_dates = expand_date_range(args.date, args.days)
    requests = build_batch_requests(
        dashboard, target_dates, templates, args.model, args.max_tokens
    )

    if not requests:
        print("ERROR: 有効なリクエストが 0 件でした。引数を確認してください。", file=sys.stderr)
        return 1

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    jsonl_path = save_requests_jsonl(requests, timestamp)
    cost = estimate_batch_cost(
        requests, args.model, cache_hit_ratio=args.cache_hit_ratio
    )

    print("=" * 70)
    print(f"[BATCH SUMMARY] timestamp={timestamp}")
    print("=" * 70)
    print(f"  date range    : {args.date} 〜 +{args.days - 1}日 ({len(target_dates)}日分)")
    print(f"  templates     : {templates}")
    print(f"  model         : {args.model}")
    print(f"  requests      : {len(requests)}")
    print(f"  JSONL saved   : {jsonl_path}")
    print()
    print(f"--- コスト概算 (batch 50%off + cache 想定 hit ratio={args.cache_hit_ratio}) ---")
    for k, v in cost.items():
        print(f"  {k}: {v}")
    print()

    if args.live:
        submit_batch_live(requests, timestamp)
    else:
        print("実投入は --live を付けてください（ANTHROPIC_API_KEY 必須・対話確認あり）。")
        print("=" * 70)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
