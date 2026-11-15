# config/models.py
# 各エージェントが使う Gemini モデルと思考レベル(thinking_level)の設定を1か所にまとめる。
#
# 2026-11: gemini-2.5-flash は 2026-12-01 から gemini-3.5-flash-lite へ自動転送される
#   (転送先の思考レベルは既定で minimal = ほぼ考えない)。自動転送に任せず、
#   モデル名と思考レベルを明示して挙動を管理する。
#
# Gemini 3 系の注意:
#   - thinking_budget(数値)ではなく thinking_level(minimal/low/medium/high)で指定する
#   - temperature / top_p / top_k は既定値のまま使うことが推奨されている
#     (以前は確率のブレを抑えるため temperature=0.1 にしていたが、Gemini 3 では外す。
#      霧氷の判定は Python で機械的に行うため、確率のブレは判定行の目安の範囲に収まる)

from google.genai import types

MODEL_DEFAULT = "gemini-3.5-flash-lite"

AGENT_MODELS = {
    "PhotoConcierge": MODEL_DEFAULT,
    "ForecastAgent": MODEL_DEFAULT,
    "RecordAgent": MODEL_DEFAULT,
    "AnalysisAgent": MODEL_DEFAULT,
}

# 思考レベル。質が足りなければ1段上げ、遅い・高いと感じたら1段下げる。
AGENT_THINKING_LEVELS = {
    # 振り分けとロジック説明ツールの呼び出しのみ。応答の速さを優先しつつ、
    # minimal だと「草」を地点名と誤認するなどの誤動作が出やすいため low。
    "PhotoConcierge": "low",
    # 最も使われ、指示文が最も長い。明日・明後日の2回のツール呼び出し、
    # 判定行への追従、出力形式の厳守が必要なため high。
    "ForecastAgent": "high",
    "RecordAgent": "medium",
    "AnalysisAgent": "medium",
}


def model_for(agent_name: str) -> str:
    return AGENT_MODELS.get(agent_name, MODEL_DEFAULT)


def thinking_config_for(agent_name: str) -> types.ThinkingConfig:
    return types.ThinkingConfig(thinking_level=AGENT_THINKING_LEVELS[agent_name])


def generate_content_config_for(agent_name: str) -> types.GenerateContentConfig:
    return types.GenerateContentConfig(thinking_config=thinking_config_for(agent_name))
