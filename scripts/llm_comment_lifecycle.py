"""Phase B-3.5: LLM コメント生成のライフサイクル管理（ストア adapter 含む）。

1日1回の運用想定:
    1. dashboard.json を読む
    2. 対象 (point, date, template) について既存ストアにあるか確認（重複排除）
    3. 不足分のみ batch リクエストを組む
    4. batch 投入 → 待機 → 結果取得
    5. ストア adapter に保存

Usage:
    # 既存ストアを参照して、不足分の batch JSONL を生成（dry-run）
    uv run python scripts/llm_comment_lifecycle.py --date today --templates daily_short,daily_long

    # 実投入 → 待機 → 取得まで一気通貫（ANTHROPIC_API_KEY 必須）
    uv run python scripts/llm_comment_lifecycle.py --date today --templates daily_short --live --wait

    # ストレージ adapter 切り替え
    uv run python scripts/llm_comment_lifecycle.py --date today --store local
    uv run python scripts/llm_comment_lifecycle.py --date today --store postgres
    uv run python scripts/llm_comment_lifecycle.py --date today --store s3_dynamo

設計方針:
    - ストレージ adapter pattern: A/B/C どれでも差し替え可能
        * LocalFileStore   ... 開発時推奨（output/llm_comments/<date>/<point>__<template>.json）
        * PostgresStore    ... C-1 完了後の本命（llm_comments テーブル）
        * S3DynamoStore    ... 公開時のスケール対応
    - 重複排除: ハッシュベース。プロンプト内容（system+user の SHA256）を保存し、
      同一プロンプトの再生成は skip。プロンプトテンプレ更新時は prompt_version で世代管理。
    - 失敗時の再投入は generate_llm_comment_batch.py --retrieve-batch で個別対応

実装状況:
    - LocalFileStore: 実装済み
    - PostgresStore : スケルトン（C-1 で DB 構築後に本実装）
    - S3DynamoStore : スケルトン（AWS 移行時に本実装）

注意:
    - 実 API 呼び出しは generate_llm_comment_batch.py に委譲（このスクリプトは編成のみ）
    - --live --wait を指定した時のみ batch 投入 + ポーリング + 取得まで実行
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
WAVEHACK_OUTPUT = ROOT.parent.parent
LOCAL_STORE_DIR = WAVEHACK_OUTPUT / "llm_comments"


# ============================================================
# ストレージ Adapter インタフェース
# ============================================================
class LLMCommentStore(ABC):
    """LLM コメントの保存・参照・重複排除インタフェース。

    A/B/C どのバックエンドでも同じ呼び出しで使える。
    """

    @abstractmethod
    def has(self, point: str, target_date: str, template: str, prompt_version: str) -> bool:
        """指定 (point, date, template, prompt_version) のコメントが既に存在するか。"""

    @abstractmethod
    def get(self, point: str, target_date: str, template: str) -> dict[str, Any] | None:
        """指定 (point, date, template) のコメントを取得（最新バージョン）。"""

    @abstractmethod
    def put(
        self,
        custom_id: str,
        content: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int,
        prompt_version: str,
    ) -> None:
        """コメントを保存。custom_id 形式: <point>__<date>__<template>"""


def _parse_custom_id(custom_id: str) -> tuple[str, str, str]:
    """custom_id からpoint/date/templateを取り出す。

    形式:
        <point_name>__<YYYY-MM-DD>__<template>
        _global__<YYYY-MM-DD>__condition_compare
        _faq__<YYYY-MM-DD>__beginner_explanation__<question_key>
    """
    parts = custom_id.split("__")
    if len(parts) < 3:
        return ("__unknown__", "1970-01-01", "unknown")
    return (parts[0], parts[1], parts[2])


# ============================================================
# A. LocalFileStore（開発時の既定）
# ============================================================
class LocalFileStore(LLMCommentStore):
    """ローカル JSON ファイルベースのストア。

    パス: output/llm_comments/<YYYY-MM-DD>/<point>__<template>.json

    メリット:
        - 依存なし、開発時にすぐ使える
        - git 管理したくないなら .gitignore で除外
    デメリット:
        - スケールしない（ファイル数 = point × date × template）
        - 検索が grep ベース
    """

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir or LOCAL_STORE_DIR
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, point: str, target_date: str, template: str) -> Path:
        date_dir = self.base_dir / target_date
        date_dir.mkdir(parents=True, exist_ok=True)
        # スラッシュや日本語ポイント名の安全化はファイル名に point をそのまま使う前提
        # （Wave Hack ポイント名は日本語のみで filesystem-safe）
        return date_dir / f"{point}__{template}.json"

    def has(self, point: str, target_date: str, template: str, prompt_version: str) -> bool:
        p = self._path(point, target_date, template)
        if not p.exists():
            return False
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data.get("prompt_version") == prompt_version
        except (json.JSONDecodeError, OSError):
            return False

    def get(self, point: str, target_date: str, template: str) -> dict[str, Any] | None:
        p = self._path(point, target_date, template)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def put(
        self,
        custom_id: str,
        content: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int,
        prompt_version: str,
    ) -> None:
        point, target_date, template = _parse_custom_id(custom_id)
        p = self._path(point, target_date, template)
        record = {
            "custom_id": custom_id,
            "point": point,
            "target_date": target_date,
            "template": template,
            "content": content,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "prompt_version": prompt_version,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }
        p.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


# ============================================================
# B. PostgresStore（C-1 で DB 構築後の本命）
# ============================================================
class PostgresStore(LLMCommentStore):
    """PostgreSQL ベースのストア。

    スキーマ (詳細は output/md/llm_storage_design.md を参照):
        CREATE TABLE llm_comments (
            id BIGSERIAL PRIMARY KEY,
            point_id INTEGER NOT NULL REFERENCES points(id),
            target_date DATE NOT NULL,
            template TEXT NOT NULL,
            content TEXT NOT NULL,
            model TEXT NOT NULL,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            cached_tokens INTEGER NOT NULL DEFAULT 0,
            prompt_version TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (point_id, target_date, template, prompt_version)
        );
        CREATE INDEX idx_llm_comments_lookup ON llm_comments(point_id, target_date, template);

    メリット:
        - 既存 forecast_scores と JOIN しやすい
        - TTL/再生成管理しやすい
        - 集計クエリが書ける
    デメリット:
        - DB が必須（C-1 完了が前提）
    """

    DEFAULT_DSN = "postgresql://wavehack:wavehack_dev_local@localhost:5432/wavehack"

    def __init__(self, dsn: str | None = None) -> None:
        self.dsn = dsn or os.environ.get("WAVEHACK_DB_DSN") or self.DEFAULT_DSN
        try:
            import psycopg  # noqa: F401
        except ModuleNotFoundError as e:  # pragma: no cover
            raise RuntimeError(
                "PostgresStore: psycopg が必要です。`uv run` で実行してください。"
            ) from e
        self._psycopg = __import__("psycopg")
        # 接続は都度確立（lifecycle は短命プロセスで psycopg3 の作法上シンプル）。
        # 起動時に疎通確認しておき、DB 未起動なら早期に分かるようにする。
        try:
            with self._connect() as conn:
                conn.execute("SELECT 1")
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"PostgresStore: DB へ接続できません（dsn={self.dsn}）: {e}"
            ) from e
        # point 名 -> point_id の解決キャッシュ（points マスタは小さく不変）
        self._point_id_cache: dict[str, int | None] = {}

    def _connect(self):
        """新規コネクションを開く（with でクローズ）。"""
        return self._psycopg.connect(self.dsn)

    def _resolve_point_id(self, point: str) -> int | None:
        """日本語ポイント名 -> points.id。特殊 point（_global 等）は None。"""
        if point in self._point_id_cache:
            return self._point_id_cache[point]
        pid: int | None = None
        if not point.startswith("_"):
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT id FROM points WHERE name = %s", (point,)
                ).fetchone()
                if row is not None:
                    pid = int(row[0])
        self._point_id_cache[point] = pid
        return pid

    def has(self, point: str, target_date: str, template: str, prompt_version: str) -> bool:
        point_id = self._resolve_point_id(point)
        if point_id is None:
            return False
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM llm_comments
                WHERE point_id = %s AND target_date = %s
                  AND template = %s AND prompt_version = %s
                LIMIT 1
                """,
                (point_id, target_date, template, prompt_version),
            ).fetchone()
            return row is not None

    def get(self, point: str, target_date: str, template: str) -> dict[str, Any] | None:
        point_id = self._resolve_point_id(point)
        if point_id is None:
            return None
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT id, point_id, target_date, template, content, model,
                       input_tokens, output_tokens, cached_tokens,
                       prompt_version, content_hash, generated_at
                FROM llm_comments
                WHERE point_id = %s AND target_date = %s AND template = %s
                ORDER BY generated_at DESC
                LIMIT 1
                """,
                (point_id, target_date, template),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cols = [d[0] for d in cur.description]
            rec = dict(zip(cols, row))
            # LocalFileStore と互換の形に整える（point 名 / ISO 文字列）
            return {
                "custom_id": f"{point}__{rec['target_date']}__{rec['template']}",
                "point": point,
                "target_date": rec["target_date"].isoformat(),
                "template": rec["template"],
                "content": rec["content"],
                "model": rec["model"],
                "input_tokens": rec["input_tokens"],
                "output_tokens": rec["output_tokens"],
                "cached_tokens": rec["cached_tokens"],
                "prompt_version": rec["prompt_version"],
                "generated_at": rec["generated_at"].isoformat(),
                "content_hash": rec["content_hash"],
            }

    def put(
        self,
        custom_id: str,
        content: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int,
        prompt_version: str,
    ) -> None:
        point, target_date, template = _parse_custom_id(custom_id)
        point_id = self._resolve_point_id(point)
        if point_id is None:
            raise ValueError(
                f"PostgresStore.put: point '{point}' が points マスタに存在しません"
                f" (custom_id={custom_id})。特殊 point は未対応。"
            )
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO llm_comments (
                    point_id, target_date, template, content, model,
                    input_tokens, output_tokens, cached_tokens,
                    prompt_version, content_hash
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (point_id, target_date, template, prompt_version)
                DO UPDATE SET
                    content       = EXCLUDED.content,
                    model         = EXCLUDED.model,
                    input_tokens  = EXCLUDED.input_tokens,
                    output_tokens = EXCLUDED.output_tokens,
                    cached_tokens = EXCLUDED.cached_tokens,
                    content_hash  = EXCLUDED.content_hash,
                    generated_at  = now()
                """,
                (
                    point_id,
                    target_date,
                    template,
                    content,
                    model,
                    input_tokens,
                    output_tokens,
                    cached_tokens,
                    prompt_version,
                    content_hash,
                ),
            )
            conn.commit()


# ============================================================
# C. S3DynamoStore（公開時のスケール対応）
# ============================================================
class S3DynamoStore(LLMCommentStore):
    """AWS S3 (本文) + DynamoDB (メタデータ + TTL) のストア。

    S3 path: s3://wavehack-llm-cache/<YYYY-MM-DD>/<point>/<template>.json
    DynamoDB table: wavehack_llm_comments
        PK: point#date (e.g. '木崎浜#2026-05-11')
        SK: template (e.g. 'daily_long')
        attrs: s3_key, model, input_tokens, output_tokens, cached_tokens,
               prompt_version, content_hash, generated_at, ttl

    メリット:
        - 公開時のスケール対応
        - CloudFront キャッシュ可能
        - DynamoDB TTL で自動失効
    デメリット:
        - ローカル開発に向かない（LocalStack で代用可）
        - AWS コスト（少量なら誤差レベル）
    """

    def __init__(self, bucket: str | None = None, table: str | None = None) -> None:
        self.bucket = bucket or os.environ.get("WAVEHACK_S3_BUCKET", "wavehack-llm-cache")
        self.table = table or os.environ.get("WAVEHACK_DDB_TABLE", "wavehack_llm_comments")
        # NOTE: 実装時は boto3 で client 生成。今は未実装。
        raise NotImplementedError(
            "S3DynamoStore は AWS 移行時 (Phase D 以降想定) に実装します。"
            " 現状は LocalFileStore / PostgresStore を使ってください。"
        )

    def has(self, point, target_date, template, prompt_version) -> bool:  # noqa: D401
        raise NotImplementedError

    def get(self, point, target_date, template):  # noqa: D401
        raise NotImplementedError

    def put(self, custom_id, content, model, input_tokens, output_tokens, cached_tokens, prompt_version):  # noqa: D401
        raise NotImplementedError


# ============================================================
# Adapter ファクトリ
# ============================================================
def make_store(kind: str) -> LLMCommentStore:
    if kind == "local":
        return LocalFileStore()
    if kind == "postgres":
        return PostgresStore()
    if kind == "s3_dynamo":
        return S3DynamoStore()
    raise ValueError(f"unknown store kind: {kind}")


# ============================================================
# Lifecycle メイン処理
# ============================================================
def filter_missing_requests(
    requests: list[dict[str, Any]],
    store: LLMCommentStore,
    prompt_version: str,
) -> list[dict[str, Any]]:
    """既にストアに保存済みのリクエストを除外する。"""
    missing = []
    for req in requests:
        point, target_date, template = _parse_custom_id(req["custom_id"])
        if store.has(point, target_date, template, prompt_version):
            continue
        missing.append(req)
    return missing


def main() -> int:
    parser = argparse.ArgumentParser(
        description="LLM コメント生成のライフサイクル管理（重複排除 + batch 投入 + 保存）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--date",
        default="today",
        help="対象開始日 YYYY-MM-DD（'today' は当日 JST）",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=8,
        help="--date を起点に何日分（既定: 8）",
    )
    parser.add_argument(
        "--templates",
        default="daily_short,daily_long",
        help="カンマ区切りテンプレ名",
    )
    parser.add_argument(
        "--store",
        default="local",
        choices=["local", "postgres", "s3_dynamo"],
        help="ストア adapter（既定: local）",
    )
    parser.add_argument(
        "--model", default="claude-haiku-4-5",
        choices=["claude-haiku-4-5", "claude-sonnet-4-6"],
    )
    parser.add_argument("--live", action="store_true", help="batch 実投入")
    parser.add_argument(
        "--wait",
        action="store_true",
        help="--live と併用。batch 完了までポーリングして結果取得まで実施",
    )
    parser.add_argument(
        "--poll-interval", type=int, default=60, help="--wait 時のポーリング間隔（秒）"
    )
    parser.add_argument(
        "--max-wait", type=int, default=3600, help="--wait の最大待機時間（秒、既定: 1時間）"
    )
    args = parser.parse_args()

    # 既存モジュール取り込み
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from generate_llm_comment_batch import (  # type: ignore  # noqa: E402
        BATCH_REQUESTS_DIR,
        PROMPT_VERSION,
        build_batch_requests,
        check_batch_status,
        estimate_batch_cost,
        expand_date_range,
        retrieve_batch_results,
        save_requests_jsonl,
        submit_batch_live,
    )
    from generate_llm_comment import load_dashboard  # type: ignore  # noqa: E402

    # 日付解決
    if args.date == "today":
        target_date = datetime.now().strftime("%Y-%m-%d")
    else:
        target_date = args.date

    templates = [t.strip() for t in args.templates.split(",") if t.strip()]

    # ストア初期化（postgres/s3_dynamo は NotImplementedError → ユーザに伝える）
    try:
        store = make_store(args.store)
    except NotImplementedError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    # 全リクエスト組み立て
    dashboard = load_dashboard()
    target_dates = expand_date_range(target_date, args.days)
    all_requests = build_batch_requests(
        dashboard, target_dates, templates, args.model, max_tokens=1024
    )

    # 重複排除
    missing = filter_missing_requests(all_requests, store, PROMPT_VERSION)

    print("=" * 70)
    print(f"[LIFECYCLE] date={target_date}+{args.days - 1}日 templates={templates} store={args.store}")
    print("=" * 70)
    print(f"  全リクエスト数: {len(all_requests)}")
    print(f"  既存スキップ : {len(all_requests) - len(missing)}")
    print(f"  新規投入対象 : {len(missing)}")

    if not missing:
        print("\n[DONE] 全コメントが既にストアに存在します。投入不要。")
        return 0

    # JSONL 保存 + コスト概算
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    jsonl_path = save_requests_jsonl(missing, timestamp)
    cost = estimate_batch_cost(missing, args.model)
    print(f"\n  JSONL: {jsonl_path}")
    print(f"  概算コスト: ${cost.get('total_usd')} ≒ {cost.get('total_jpy')}円")

    if not args.live:
        print("\n--live を付けると batch 投入します（ANTHROPIC_API_KEY 必須）。")
        return 0

    # 実投入
    batch_id = submit_batch_live(missing, timestamp)

    if not args.wait:
        print(f"\n--wait 未指定。後で `--retrieve-batch {batch_id}` で取得してください。")
        return 0

    # ポーリング
    print(f"\n--- batch 完了を待機（最大 {args.max_wait}s, 間隔 {args.poll_interval}s）---")
    waited = 0
    while waited < args.max_wait:
        time.sleep(args.poll_interval)
        waited += args.poll_interval
        # status を CLI 経由ではなく直接見たいが、ここでは check_batch_status を使う
        # （冗長な print が出るが運用ログとしては有用）
        print(f"\n[{waited}s elapsed]")
        check_batch_status(batch_id)
        # ended 判定は check_batch_status 内で出力されるが、retrieve 試行で代替
        try:
            retrieve_batch_results(batch_id, store_kind=args.store)
            print(f"\n[OK] batch {batch_id} の結果取得完了。")
            return 0
        except Exception as e:  # noqa: BLE001
            # まだ ended でない場合は WARNING を出して継続
            if "まだ完了していません" in str(e) or "in_progress" in str(e).lower():
                continue
            # その他のエラーは即終了
            print(f"ERROR during retrieve: {e}", file=sys.stderr)
            return 3

    print(f"\n[TIMEOUT] {args.max_wait}s 経過しても batch が完了しませんでした。", file=sys.stderr)
    print(f"          後で `--retrieve-batch {batch_id}` で取得してください。", file=sys.stderr)
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
