#!/bin/bash
# 毎週木曜の「週末の波予想」リール: データ取得 → ナレーション → 動画 → 19:00 から順に投稿。
# launchd (~/Library/LaunchAgents/surf.wavehack.weekly-reel.plist) から木曜 17:40 に起動する。
#   - 17:40 開始: Gemini 無料枠の日次上限は太平洋時間 0 時（JST 16:00）にリセットされる。
#     16:00 の daily-update（60〜90分）の結果があればそれを使い、無ければ朝 04:00 の結果を使う。
#   - 手動: bash reel/weekly_reel.sh [--now] [--dry-run]
#       --now     19:00 を待たずにすぐ投稿   --dry-run 投稿せずキャプション表示
set -euo pipefail

export PATH="/Users/takeda.toi/.local/bin:/Users/takeda.toi/.nvm/versions/node/v24.14.1/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
REEL="$(cd "$(dirname "$0")" && pwd)"
PIPE="$(dirname "$REEL")"
WEEK="$(date +%Y-%m-%d)"
WORK="/Users/takeda.toi/wavehack/output/reels/$WEEK"
LOG="$WORK/run.log"
NOW=0; DRY=""
for a in "$@"; do case "$a" in --now) NOW=1 ;; --dry-run) DRY="--dry-run" ;; esac; done
mkdir -p "$WORK"
exec >>"$LOG" 2>&1
echo "=== $(date '+%F %T') start (now=$NOW dry=${DRY:-no})"

set -a
. /Users/takeda.toi/secret/google/gemini.env
[ -f /Users/takeda.toi/secret/wavehack/instagram.env ] && . /Users/takeda.toi/secret/wavehack/instagram.env
set +a

cd "$PIPE"
# 1. 本番 dashboard.json（R2）
npx -y wrangler@4 r2 object get wavehack-data/dashboard.json --remote --file "$WORK/dashboard.json"
# 2. 週末データ
uv run --extra social python reel/build_reel_data.py --dashboard "$WORK/dashboard.json" --out "$WORK/reel_data.json"
# 3. ナレーション（Gemini TTS / Aoede。誘導の一言は共通音声を使い回し、毎週 27 本）
python3 reel/narration_scenes.py --data "$WORK/reel_data.json" --out-dir "$WORK/voice" \
  || echo "WARN: ナレーションが一部未生成。生成できたポイントだけ動画化する"
# 4. 動画
uv run --with playwright --with imageio-ffmpeg python reel/render_reel.py \
  --data "$WORK/reel_data.json" --voice-dir "$WORK/voice" --out-dir "$WORK/videos"
ls -la "$WORK/videos"
# 5. 投稿（19:00 から 15 分おき）
if [ -z "$DRY" ] && [ -z "${IG_ACCESS_TOKEN:-}" ]; then
  echo "ERROR: /Users/takeda.toi/secret/wavehack/instagram.env が無いので投稿できません（動画は $WORK/videos に生成済み）"
  exit 1
fi
if [ "$NOW" = 0 ] && [ -z "$DRY" ]; then
  target=$(date -j -f "%Y-%m-%d %H:%M" "$WEEK 19:00" +%s); now=$(date +%s)
  [ "$now" -lt "$target" ] && { echo "waiting until 19:00"; sleep $((target - now)); }
fi
python3 reel/publish_reels.py --data "$WORK/reel_data.json" --video-dir "$WORK/videos" --interval-min 15 \
  --log "$WORK/published.jsonl" $DRY
echo "=== $(date '+%F %T') done"
