# wavehack-pipeline

Wave Hack のデータパイプライン。
NOAA GFS 取得 / Windy スクレイピング / ML推論 を担当する。

現状は MVP 用ダミー予報生成のみ。

## セットアップ

```bash
cd /Users/takeda.toi/wavehack/output/code/wavehack-pipeline
python3 -m venv venv
source venv/bin/activate
pip install -e .
```

GFS / scraping / ML の依存は extra でオプション化:

```bash
pip install -e ".[gfs]"      # NOAA GFS取得
pip install -e ".[scraping]" # Playwrightスクレイピング
pip install -e ".[ml]"       # LightGBM推論
pip install -e ".[dev]"      # 開発ツール
```

## 使い方

```bash
# 宮崎10ポイントのダミー予報を生成
python scripts/generate_forecast.py

# API のデータディレクトリに直接出力
python scripts/generate_forecast.py --output ../wavehack-api/data/forecast.json
```

## ディレクトリ

- `src/wavehack_pipeline/` パッケージ本体
  - `points.py` ポイントマスタ読込
  - `forecast.py` 予報生成（現状ダミー、次フェーズでGFS取得に置換）
- `scripts/` エントリポイント
- `data/` 入力データ（ポイントマスタ）
- `output/` 生成物
- `tests/` テスト

## 次にやること

- GFS Wave モデル取得 (`xarray` + `cfgrib`)
- Windy スクレイピング（Playwright Python）
- 既存LightGBMモデル (`wave_scraping/model/model_lgb.pkl`) の推論動作確認
