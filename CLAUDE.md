# CLAUDE.md — landscape-photo-agent

## プロジェクト概要

長野高原の風景写真撮影スポット（霧氷・凝華・放射霧・雲海）の翌朝コンディションを予測する LINE Bot。
Google ADK によるマルチエージェント構成で、Cloud Run 上に FastAPI でホストしている。

### リクエストフロー

```
ユーザー → LINE → GAS（受付係：即時応答 + 転送）→ Cloud Run /webhook → ADK エージェント → LINE Push
```

GAS は薄い中継役（即時 reply + Cloud Run への POST）。予測・記録・分析ロジックは全て Cloud Run 側。

## アーキテクチャ

### エージェント階層

```
PhotoConcierge (coordinator.py)    ← root_agent。意図判定してルーティング
  ├── ForecastAgent (forecast.py)  ← 翌朝の撮影条件を予測
  ├── RecordAgent (record.py)      ← 撮影実績を保存
  └── AnalysisAgent (analysis.py)  ← 予測精度の検証 + ノウハウ蓄積
```

- 全エージェントが `LlmAgent`（モデル: `gemini-2.5-flash`）
- Coordinator は `thinking_budget=0` で即応答（ルーティングに深い思考は不要）
- 各エージェントの instruction はファイル内の Python 文字列で直接定義（外部 Markdown ではない）

### ディレクトリ構成

```
landscape-photo-agent/
├── main.py                  # FastAPI エントリポイント（/webhook, / ヘルスチェック）
├── agents/
│   ├── coordinator.py       # PhotoConcierge（root_agent）
│   ├── forecast.py          # ForecastAgent + 予測ロジック instruction
│   ├── record.py            # RecordAgent + 実績パース instruction
│   └── analysis.py          # AnalysisAgent + 精度検証 instruction
├── tools/                   # FunctionTool 群（エージェントが呼ぶ関数）
│   ├── weather.py           # fetch_forecast, fetch_all_forecasts, fetch_historical_weather
│   ├── location.py          # resolve_location, classify_spot_group, geocode_unknown_spot
│   ├── storage.py           # Google Drive JSON 読み書き（read_json, write_json, append_to_json_list）
│   ├── analysis.py          # analyze_prediction_accuracy, save_fog_knowledge
│   ├── line_client.py       # LINE Push API（push_message）
│   └── logger.py            # Cloud Logging 向け構造化ロガー（trace_id/user_id を ContextVar で伝搬）
├── config/
│   ├── spots.py             # DEFAULT_SPOTS（10地点）, EXTRA_SPOTS, SPOT_ALIASES, SPOT_ATTRIBUTES
│   └── adjustments.py       # WIND_ADJUSTMENTS（地点別風速補正係数）
├── tests/
│   ├── conftest.py          # sys.path 設定
│   ├── test_config.py       # spots / adjustments
│   ├── test_record_date.py  # 日付解決ロジック
│   ├── test_analysis.py     # 霧分類・閾値・時間窓
│   ├── test_weather.py      # 時刻生成・降水集計
│   └── test_location.py     # 地点名サニタイズ
├── Dockerfile
├── requirements.txt
└── .claude/
    └── settings.local.json
```

### agents/ と tools/ の対応関係

| Agent | 使う tools |
|---|---|
| ForecastAgent | `tools/weather.py`, `tools/location.py`, `tools/storage.py` |
| RecordAgent | `tools/storage.py`, `tools/location.py`, `tools/weather.py`（実況取得） |
| AnalysisAgent | `tools/analysis.py`（内部で `tools/storage.py`, `tools/location.py` を使用） |

`agents/analysis.py` と `tools/analysis.py` は同名だが別物。Agent 定義と Tool 実装のペア。

## 技術スタック

- **ランタイム**: Python 3.12 / FastAPI / uvicorn
- **エージェント**: Google ADK (`google-adk==1.33.0`)
- **LLM**: Gemini 2.5 Flash（Google AI Studio 経由）
- **気象データ**: Open-Meteo API（JMA MSM モデル、forecast + archive）
- **地点解決**: Google Maps Places API (New) + Elevation API
- **データストア**: Google Drive 上の JSON ファイル群（Drive API でサービスアカウント認証）
- **インフラ**: Cloud Run / GAS（Webhook 中継）/ Cloud Scheduler（将来の月次学習用）
- **HTTP クライアント**: httpx（async）

## 環境変数

| 変数名 | 用途 |
|---|---|
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Push API 用トークン |
| `LINE_CHANNEL_SECRET` | LINE 署名検証用（現構成では GAS 中継のため検証スキップ） |
| `PREDICTIONS_FILE_ID` | predictions.json の Google Drive ファイル ID |
| `ACTUALS_FILE_ID` | actuals.json の Google Drive ファイル ID |
| `SPOT_GROUPS_FILE_ID` | spot_groups.json の Drive ファイル ID（自動分類キャッシュ） |
| `FOG_KNOWLEDGE_FILE_ID` | fog_knowledge.json の Drive ファイル ID |
| `GOOGLE_MAPS_API_KEY` | Places API + Elevation API 用 |
| `GOOGLE_API_KEY` | Gemini API キー（ADK が内部で使用） |
| `VERIFY_LINE_SIGNATURE` | `true` にすると LINE 署名検証を有効化（通常は不要） |

## データストア（Google Drive JSON）

| ファイル | 内容 | 読み書きする箇所 |
|---|---|---|
| `predictions.json` | 予測ログ（気象スナップショット付き） | `tools/weather.py` が自動保存、`tools/analysis.py` が分析時に参照 |
| `actuals.json` | 撮影実績（観測結果 + 実況気象） | `agents/record.py` の `save_actual` が保存 |
| `spot_groups.json` | 地点属性（手動シード + 自動分類キャッシュ） | `tools/location.py` が参照・保存 |
| `fog_knowledge.json` | ノウハウメモ（ユーザーの自由記述） | `tools/analysis.py` が保存・参照 |

### 予測ログの保存タイミング

- 単一地点: `fetch_forecast` が気象データ取得時に即保存（`save_snapshot=True`）
- 「おすすめ」一括: `fetch_all_forecasts` が全地点の気象取得後にまとめて1回保存（並列 read-modify-write 競合を回避）
- LLM の `save_prediction` 呼び出しには依存しない設計

## 主要な設計判断と経緯

### 設計書（v1.4）からの意図的な変更

設計書 `docs/landscape_photo_agent_design_v1_4.docx` は 2026年5月時点の計画。以下は実装過程で意図的に変更した点。

1. **学習パイプライン（LearningPipeline / DivergenceAnalyzer / InsightAgent）→ 廃止**
   - 設計書の「月次自動学習 + Markdown/JSON レポート + 開発者レビュー」は重すぎた
   - 代わりに `AnalysisAgent` がオンデマンドで霧あり/なしの予測値比較を返す軽量構成に
   - ノウハウ蓄積も `knowledge.json` の手動編集ではなく `fog_knowledge.json` への自由記述メモに変更

2. **prompts/ ディレクトリ（現象別 Markdown 外部管理）→ 廃止**
   - instruction は各エージェントファイル内の Python 文字列で直接定義
   - 理由: ファイル分割すると instruction の全体像が見えにくく、修正時に複数ファイルを触る必要があった

3. **photo_agent/ ディレクトリ（ADK 標準の root_agent 置き場）→ 不使用**
   - `main.py` が `agents.coordinator` から `root_agent` を直接 import する構成に簡略化

4. **config/settings.py, seasonal_filter.py → 不使用**
   - 環境変数は各ファイルで `os.environ.get` で直接読む
   - 季節フィルタは ForecastAgent の instruction 内に埋め込み

5. **pyproject.toml, .env.example, tests/ → 未作成**

### 放射霧の5パターン体制

ForecastAgent の instruction に直接埋め込み済み。地形タグとパターンの対応:

| パターン | タグ条件 | 代表地点 |
|---|---|---|
| A 鉄板組 | 湿原系 × 盆地地形 × 水域近接 | 田ノ原湿原, 踊場湿原, 戦場ヶ原, 尾瀬ヶ原 |
| B 隠れ強豪 | 高原系 × 水域近接 × 湿原内包 | カヤの平, 菅平 |
| C 気まぐれ | 湿原系 × 盆地地形 × 乾燥湿原 | 大阿原湿原（前日雨リセットあり） |
| D 湖畔派 | 湖畔 × 水域近接 | （登録地点なし。未知地点で該当） |
| E 谷筋派 | 谷筋 × 水域近接 | （登録地点なし。未知地点で該当） |

稜線上・峠タグの地点（美ヶ原高原, 千畳敷カール）は放射霧=低、雲海/ガス(雲中)で予測。

### 未知地点の処理フロー

```
resolve_location（Maps API で座標・標高取得、is_unknown=True）
  → classify_spot_group（手動シード → auto_classified キャッシュ → 未分類）
    → 未分類なら LLM がタグ推定 → save_auto_classification でキャッシュ保存
```

## ローカル開発

```bash
# 依存インストール
pip install -r requirements.txt

# 環境変数を設定（.env ファイルは .gitignore 対象）
cp .env.example .env

# 起動
uvicorn main:app --reload --port 8080
```

### デプロイ（Cloud Run）

```bash
gcloud run deploy landscape-photo-agent \
  --source . \
  --region asia-northeast1 \
  --no-cpu-throttling \
  --min-instances 0
```

`--no-cpu-throttling`: バックグラウンドタスク（ADK エージェント処理）が CPU 割り当て外で止まる問題の対策。
`--min-instances 0`: コスト優先設定（2026-08-02変更）。`--min-instances 1`（常時1台起動）は月あたりCloud Run実費が約7,000円まで膨らんだため0に変更。トレードオフとしてアイドル後の初回リクエストにコールドスタート遅延（数秒程度）が発生する。`--no-cpu-throttling`は維持しているため、起動後のバックグラウンド処理の信頼性は変わらない。

## コーディング規約

- **instruction の変更**: agents/ 内の Python 文字列を直接編集する。外部ファイル化しない
- **新しい FunctionTool**: tools/ に関数を定義 → agents/ で `FunctionTool(func=...)` でラップ
- **新しい登録地点**: `config/spots.py` の `DEFAULT_SPOTS`（おすすめ対象）または `EXTRA_SPOTS`（個別指定のみ）に追加。`SPOT_ALIASES` と `SPOT_ATTRIBUTES` も忘れずに
- **風速補正係数**: `config/adjustments.py` の `WIND_ADJUSTMENTS` に追加
- **ログ**: `tools/logger.py` の `get_logger(__name__)` を使う。`print()` は使わない
- **非同期**: tools の関数は `async def` で書く（Drive API / 外部 API 呼び出しのため）
- **エラーハンドリング**: ユーザー応答を止めないことを最優先。Drive 保存失敗などは `logger.exception` で記録して握りつぶす
- **型ヒント**: 厳密な型付けはしていないが、関数の引数・戻り値には docstring で説明する

## Claude Code スキル（.claude/skills/）

| コマンド | 内容 |
|---|---|
| `/deploy` | テスト実行 → Cloud Run デプロイ → ヘルスチェックを一括実行 |
| `/add-spot` | 新しい撮影地点の登録。2ファイル・4箇所の更新を漏れなく実施 |

## .claude/settings.local.json

`deny` で以下の破壊的操作をブロック済み：`rm -rf`、`gcloud run services delete`、`git push --force`、`git reset --hard`

## 既知の課題・TODO

- [x] `.env.example` の作成
- [x] `tests/` の整備（最低限 `tools/analysis.py` の `_classify_fog` 等の純粋関数）
- [ ] 設計書 v1.4 を `docs/archive/` に移動し、設計変更の経緯を `docs/decisions.md` に記録
- [ ] `config/settings.py` で環境変数を一元管理（現状は各ファイルで `os.environ.get` が散在）
