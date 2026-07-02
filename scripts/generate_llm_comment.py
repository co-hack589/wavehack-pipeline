"""Phase B-3: Claude API による初心者向け補足コメント生成プロトタイプ。

ルールベース言語化（B-1）で生成された slot.comment / daily_summary を
context として Claude に渡し、初心者向けの補足解説や条件比較を生成する。

Usage:
    # ドライラン（実 API 呼び出しなし、プロンプトとトークン概算を表示）
    uv run python scripts/generate_llm_comment.py --point 木崎浜 --date 2026-05-10

    # ライブ呼び出し（ANTHROPIC_API_KEY 必須、ユーザー確認推奨）
    uv run python scripts/generate_llm_comment.py --point 木崎浜 --date 2026-05-10 --live

設計方針:
    - デフォルトは --dry-run。--live を明示しない限り実 API 呼び出しはしない
    - dry-run 時は anthropic SDK を import しない（依存も走らない）
    - prompt caching を意識して context を「共通部分（用語集・ポイント特性）」と
      「変動部分（当日のデータ）」に分割
    - batch API & prompt caching を併用すると Haiku 4.5 で月数百円レベルになる想定
      （詳細は output/md/llm_cost_estimate.md 参照）

モデル選定:
    - 既定: claude-haiku-4-5 （コスト最優先・短文生成に十分な品質）
    - 高品質モード: claude-sonnet-4-6 （月次レポート等で使い分け）

注意:
    - APIキー値そのものはログ出力しない
    - 環境変数 ANTHROPIC_API_KEY が無い場合、--live 時にエラーで終了する
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
WAVEHACK_OUTPUT = ROOT.parent.parent  # /Users/takeda.toi/wavehack/output
DASHBOARD_JSON = WAVEHACK_OUTPUT / "data" / "dashboard.json"

# モデル定数（claude-api skill ガイドに従い、日付サフィックスは付けない）
MODEL_HAIKU = "claude-haiku-4-5"
MODEL_SONNET = "claude-sonnet-4-6"

# テンプレート種別
TEMPLATE_TYPES = ("daily_short", "daily_long", "condition_compare", "beginner_explanation")


# ============================================================
# プロンプトテンプレート
# ============================================================
# prompt caching 想定:
#   - SYSTEM_PROMPT_FROZEN は 全リクエスト共通 → cache_control を付ける
#   - POINT_PROFILES（ポイント特性）も日付/時間に依存しない → cache_control 付ける
#   - 当日のスロット群だけが変動 → cache 対象外
SYSTEM_PROMPT_FROZEN = """あなたは宮崎の波情報サイト「Wave Hack」のサーフィン解説アシスタントです。
役割: ルールベースで生成された波予報の数値データに、初心者にもわかる補足解説を1〜3文で添える。

以下を厳守してください:
1. 数値の改ざん・誇張をしない（与えられたデータをそのまま尊重する）
2. 専門用語（オフショア・うねり・周期など）は最初の1回だけ簡単に補足
3. 「今行くべきか」の判断材料を1つ盛り込む（例: 朝マズメが狙い目、風が午後から強まる、など）
4. 文末は「ですます調」、絵文字は使わない
5. 出力は本文のみ。前置き（「了解しました」「以下が回答です」等）は不要"""

POINT_PROFILES = """=== 宮崎9ポイント基礎情報 ===
- 木崎浜（東向きビーチ）: 宮崎を代表する有名ポイント。砂底でビーチブレイク主体。東〜南東のうねりに反応
- 一ツ葉（東向き）: 初心者〜中級者向け。木崎浜ほど混まない
- 青島（東向き）: リーフあり。サイズアップ時は上級者向け
- お倉ヶ浜（東向き、日向）: 北部の代表ポイント。北東〜東のうねりが入りやすい
- 金ヶ浜（東向き、日向）: お倉ヶ浜の隣。地形変化が大きい
- 伊勢ヶ浜（東向き、日向）: 湾内で穏やか。クローズ時の避難先
- 伊比井（東向き）: 木崎浜の南。週末でも比較的空いている
- 大堂津（東向き、南郷）: 南部の代表。台風シーズンに本領発揮
- 蚊口（東向き、高鍋）: 北部のロングビーチ。広範囲に波が立つ

=== 用語ミニ辞典 ===
- うねり高: 波の高さ（m）。0.5未満=小波、0.5〜1.0=膝〜腰、1.0〜1.5=腰〜胸、1.5以上=胸以上
- 周期: 波と波の間隔（秒）。長いほど良い波。8秒以上=パワーあり、12秒以上=ロングインターバルでクラシック
- オフショア: 陸から海へ吹く風。波の面を整える理想的な風
- オンショア: 海から陸へ吹く風。波がぐちゃつきやすい
- サイドオフ/サイドオン: その中間。サイドオフは比較的良好
- ピークタイム: その日のベストコンディションの時間帯
- composite スコア: 0〜1。0.7以上=Good、0.85以上=Great、0.95以上=Excellent"""


def build_prompt_daily_short(point_name: str, date: str, slots: list[dict[str, Any]],
                              daily_summary: dict[str, Any] | None) -> tuple[str, str]:
    """LINE通知用 短文（120文字以内目標）。

    Returns: (system_prompt, user_prompt)
    """
    rule_summary = daily_summary.get("text", "") if daily_summary else ""
    best_time = daily_summary.get("best_time", "") if daily_summary else ""

    user = f"""以下は {date} の {point_name} のルールベース予報サマリです。

【ルールベース出力】
{rule_summary}
ベストタイム: {best_time}

これを元に、サーフィン初心者にも伝わるLINE通知を作ってください。
- 120文字以内
- 「行くべきかどうか」の判断材料を1つ
- 絵文字なし
- 改行なし（1行で）"""
    return SYSTEM_PROMPT_FROZEN, user


def build_prompt_daily_long(point_name: str, date: str, slots: list[dict[str, Any]],
                             daily_summary: dict[str, Any] | None) -> tuple[str, str]:
    """Web表示用 長文（300文字程度、初心者向け解説付き）。"""
    # 上位3スロットを context に含める
    sorted_slots = sorted(
        [s for s in slots if not s.get("is_extended")],
        key=lambda s: s.get("composite", 0.0),
        reverse=True,
    )[:3]

    slot_lines = []
    for s in sorted_slots:
        slot_lines.append(
            f"- {s.get('timestamp_jst', '')[11:16]}: rank={s.get('rank')} "
            f"波高={s.get('htsgw_m')}m 周期={s.get('perpw_s')}s "
            f"風={s.get('wind_label', '')}{s.get('wind_ms')}m/s "
            f"comment={s.get('comment', '')}"
        )

    user = f"""以下は {date} の {point_name} の波予報データです。

【ポイント】{point_name}
【日付】{date}

【上位3スロット】
{chr(10).join(slot_lines)}

【1日サマリ】
{daily_summary.get('text', '') if daily_summary else 'なし'}

これを元に、Web表示用の解説文を300文字程度で書いてください。
- 「ベストタイム」「うねりの傾向」「風の傾向」「初心者向け一言アドバイス」を含める
- 専門用語が出たら1回だけ簡単に補足
- 段落分けOK"""
    return SYSTEM_PROMPT_FROZEN + "\n\n" + POINT_PROFILES, user


def build_prompt_condition_compare(date: str, points_data: list[dict[str, Any]]) -> tuple[str, str]:
    """複数ポイント比較解説（「今日は伊勢ヶ浜より木崎浜の方が良い理由」など）。"""
    lines = []
    for pd in points_data:
        ds = pd.get("daily_summaries", [])
        target = next((d for d in ds if d.get("date") == date), None)
        if target:
            lines.append(
                f"- {pd['point_name']}: best_composite={target.get('best_composite')} "
                f"summary={target.get('text', '')}"
            )

    user = f"""以下は {date} の宮崎9ポイント横並び比較データです。

{chr(10).join(lines)}

これを元に、「今日どこに行くのがベストか」を200文字程度で解説してください。
- TOP3ポイントを composite スコア順で挙げる
- それぞれの選定理由を1文で
- 初心者・中級者向けの注意点があれば末尾に1文"""
    return SYSTEM_PROMPT_FROZEN + "\n\n" + POINT_PROFILES, user


def build_prompt_beginner_explanation(question: str) -> tuple[str, str]:
    """用語解説（「ピーク時間ってどういう意味？」など）。"""
    user = f"""ユーザーからの質問です。サーフィン初心者にもわかるように150文字程度で答えてください。

質問: {question}"""
    return SYSTEM_PROMPT_FROZEN + "\n\n" + POINT_PROFILES, user


# ============================================================
# トークン概算（実 API を呼ばずに済むよう簡易計算）
# ============================================================
def estimate_tokens(text: str) -> int:
    """日本語混在テキストの簡易トークン概算。

    厳密ではないが、Anthropic の count_tokens API を呼ばずに費用感を掴むための目安。
    一般的に英語: 1 token ≒ 4 chars、日本語: 1 token ≒ 1.5〜2 chars 相当。
    保守的に「全文字数 / 2.5」で見積もる。
    """
    return max(1, len(text) // 2 + len(text.encode("utf-8")) // 8)


# ============================================================
# dashboard.json から指定 point/date のスロット群を抽出
# ============================================================
def load_dashboard() -> dict[str, Any]:
    if not DASHBOARD_JSON.exists():
        print(f"ERROR: {DASHBOARD_JSON} が見つかりません。", file=sys.stderr)
        print("  先に scripts/generate_dashboard.py を実行してください。", file=sys.stderr)
        sys.exit(1)
    return json.loads(DASHBOARD_JSON.read_text(encoding="utf-8"))


def extract_point_date(dashboard: dict[str, Any], point_name: str,
                       date: str) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any] | None]:
    """指定 point の指定日のスロット群と daily_summary を抽出。"""
    point = next((p for p in dashboard["points"] if p["point_name"] == point_name), None)
    if point is None:
        valid = [p["point_name"] for p in dashboard["points"]]
        print(f"ERROR: ポイント '{point_name}' が見つかりません。候補: {valid}", file=sys.stderr)
        sys.exit(1)

    slots = [s for s in point["slots"] if s.get("timestamp_jst", "").startswith(date)]
    if not slots:
        print(f"ERROR: {point_name} の {date} のスロットが見つかりません。", file=sys.stderr)
        sys.exit(1)

    daily_summary = next(
        (d for d in point.get("daily_summaries", []) if d.get("date") == date),
        None,
    )
    return point, slots, daily_summary


# ============================================================
# dry-run / live 実行
# ============================================================
def run_dry(template: str, system: str, user: str, model: str) -> None:
    """組み立てたプロンプトを stdout に出力（実 API は呼ばない）。"""
    sys_tokens = estimate_tokens(system)
    user_tokens = estimate_tokens(user)
    print("=" * 70)
    print(f"[DRY-RUN] template={template} model={model}")
    print("=" * 70)
    print(f"\n--- SYSTEM PROMPT ({sys_tokens} tokens 概算) ---")
    print(system)
    print(f"\n--- USER PROMPT ({user_tokens} tokens 概算) ---")
    print(user)
    print(f"\n--- 推定コスト（1リクエスト） ---")
    print(f"  入力トークン: 約 {sys_tokens + user_tokens} tokens")
    print(f"  想定出力: 100〜400 tokens")
    print(f"  prompt caching 適用時: SYSTEM部 ({sys_tokens} tokens) は2回目以降キャッシュヒット (~0.1x)")
    print()
    print("実呼び出しは --live を付けてください（ANTHROPIC_API_KEY 必須）。")
    print("=" * 70)


def run_live(template: str, system: str, user: str, model: str) -> None:
    """実 API 呼び出し（--live 指定時のみ）。

    NOTE: この関数に到達した時点でユーザーが --live を明示している。
    APIキー値そのものはログに出さない。
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: 環境変数 ANTHROPIC_API_KEY が設定されていません。", file=sys.stderr)
        print("       export ANTHROPIC_API_KEY='sk-ant-...' を実行してから再試行してください。", file=sys.stderr)
        print("       （API キー値はログ出力しません）", file=sys.stderr)
        sys.exit(2)

    # 実呼び出し時のみ import（dry-run時は依存無しで動かせるように）
    try:
        import anthropic  # noqa: F401
    except ImportError:
        print("ERROR: anthropic SDK がインストールされていません。", file=sys.stderr)
        print("       uv add anthropic を実行してください。", file=sys.stderr)
        sys.exit(3)

    print(f"[LIVE] model={model} template={template}")
    print("注意: 課金が発生します。続行しますか？ (yes/no): ", end="")
    answer = input().strip().lower()
    if answer not in ("yes", "y"):
        print("中止しました。")
        return

    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY を環境変数から自動取得

    # prompt caching を有効化（共通 SYSTEM 部分をキャッシュ対象に）
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},  # 5min TTL でキャッシュ
            }
        ],
        messages=[{"role": "user", "content": user}],
    )

    print("\n--- RESPONSE ---")
    for block in response.content:
        if block.type == "text":
            print(block.text)
    print("\n--- USAGE ---")
    print(f"  input_tokens (uncached): {response.usage.input_tokens}")
    print(f"  cache_creation_input_tokens: {response.usage.cache_creation_input_tokens}")
    print(f"  cache_read_input_tokens: {response.usage.cache_read_input_tokens}")
    print(f"  output_tokens: {response.usage.output_tokens}")


# ============================================================
# CLI
# ============================================================
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Phase B-3: Claude API による初心者向け補足コメント生成プロトタイプ",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--point", default="木崎浜", help="対象ポイント名（既定: 木崎浜）")
    parser.add_argument("--date", default="2026-05-10", help="対象日 YYYY-MM-DD（既定: 2026-05-10）")
    parser.add_argument(
        "--template",
        choices=TEMPLATE_TYPES,
        default="daily_short",
        help="プロンプトテンプレート種別（既定: daily_short）",
    )
    parser.add_argument(
        "--question",
        default="ピーク時間ってどういう意味？",
        help="beginner_explanation テンプレ用の質問文",
    )
    parser.add_argument(
        "--model",
        default=MODEL_HAIKU,
        choices=[MODEL_HAIKU, MODEL_SONNET],
        help=f"使用モデル（既定: {MODEL_HAIKU}）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="ドライラン（実 API 呼び出しなし、既定で ON）",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="実 API 呼び出しを行う（ANTHROPIC_API_KEY 必須・課金発生）",
    )

    args = parser.parse_args()

    # condition_compare は全ポイントが必要
    dashboard = load_dashboard()

    if args.template == "condition_compare":
        system, user = build_prompt_condition_compare(args.date, dashboard["points"])
    elif args.template == "beginner_explanation":
        system, user = build_prompt_beginner_explanation(args.question)
    else:
        _point, slots, daily_summary = extract_point_date(dashboard, args.point, args.date)
        if args.template == "daily_short":
            system, user = build_prompt_daily_short(args.point, args.date, slots, daily_summary)
        else:  # daily_long
            system, user = build_prompt_daily_long(args.point, args.date, slots, daily_summary)

    if args.live:
        run_live(args.template, system, user, args.model)
    else:
        run_dry(args.template, system, user, args.model)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
