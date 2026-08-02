---
name: add-spot
description: 新しい撮影地点を登録する。「地点を追加」「スポットを追加」「〇〇を登録して」と言われたときに使う。
---

## 必要な情報

ユーザーから以下を聞き取る（不明なものは後で調べる）:

| 項目 | 例 | 必須 |
|---|---|---|
| 地点名 | 白駒池 | ✅ |
| 緯度 | 36.0123 | △（Maps APIで取得可） |
| 経度 | 138.3456 | △（Maps APIで取得可） |
| 標高(m) | 2115 | △（Elevation APIで取得可） |
| 「おすすめ」対象にするか | はい/いいえ | ✅ |
| エイリアス（略称） | 白駒 | 任意 |
| メイングループ | 湿原系 / 高原系 | ✅ |
| 補助タグ | 湖畔, 水域近接 | ✅ |
| パターン | A〜E または null | ✅ |
| phenomena_priority | 霧氷:中, 放射霧:高 等 | ✅ |
| 風速補正係数 | 0.3 | ✅ |

## 更新するファイルと箇所（4箇所）

### 1. config/spots.py — 地点リスト
「おすすめ」対象なら `DEFAULT_SPOTS` に追加。個別指定のみなら `EXTRA_SPOTS` に追加。

```python
{"name": "白駒池", "lat": 36.0123, "lng": 138.3456, "elev": 2115},
```

### 2. config/spots.py — エイリアス
`SPOT_ALIASES` にエイリアスを追加（省略名がある場合）。

```python
"白駒": "白駒池",
```

### 3. config/spots.py — 地形属性
`SPOT_ATTRIBUTES` に地形タグ・パターン・phenomena_priority を追加。

```python
"白駒池": {
    "main_group": "高原系", "pattern": "D",
    "tags": ["湖畔", "水域近接"],
    "phenomena_priority": {"霧氷": "中", "凝華": "低", "放射霧": "高", "雲海": "低"},
},
```

### 4. config/adjustments.py — 風速補正係数
`WIND_ADJUSTMENTS` に追加。

```python
"白駒池": 0.3,
```

## 完了チェック

1. 上記4箇所すべてに追加したか確認する
2. テストを実行する
   ```
   pytest tests/test_config.py -v
   ```
   - `test_all_have_attributes` が新地点でもパスすることを確認
   - `test_all_have_wind_factor` が新地点でもパスすることを確認
3. 追加結果をユーザーに報告する
   - 地点名、座標、標高
   - パターン・タグ
   - 「おすすめ」対象かどうか
