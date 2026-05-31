# RecordAgent: 撮影実績を入力・保存するエージェント

import os
from datetime import datetime, timezone, timedelta
from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool
from google.genai import types

from tools.storage import append_to_json_list, find_latest_prediction_id
from tools.location import resolve_location
from tools.weather import fetch_historical_weather

ACTUALS_FILE_ID = os.environ.get("ACTUALS_FILE_ID", "")
PREDICTIONS_FILE_ID = os.environ.get("PREDICTIONS_FILE_ID", "")


def _today_jst() -> str:
    jst = timezone(timedelta(hours=9))
    return datetime.now(jst).strftime("%Y-%m-%d")


def _resolve_date(date_input: str) -> str:
    jst = timezone(timedelta(hours=9))
    today = datetime.now(jst).date()

    if not date_input or date_input in ("今日", "本日"):
        return today.strftime("%Y-%m-%d")
    if date_input == "昨日":
        return (today - timedelta(days=1)).strftime("%Y-%m-%d")
    if date_input in ("一昨日", "おととい"):
        return (today - timedelta(days=2)).strftime("%Y-%m-%d")

    try:
        d = datetime.strptime(date_input, "%Y-%m-%d").date()
        return d.strftime("%Y-%m-%d")
    except ValueError:
        pass

    norm = date_input.replace("/", "-")
    try:
        parts = norm.split("-")
        month, day = int(parts[0]), int(parts[1])
        candidate = datetime(today.year, month, day).date()
        if candidate > today:
            candidate = datetime(today.year - 1, month, day).date()
        return candidate.strftime("%Y-%m-%d")
    except (ValueError, IndexError):
        return today.strftime("%Y-%m-%d")


def _norm_state(value: str) -> str:
    """「あり/少しあり/なし/不明」のいずれかに正規化。"""
    if not value:
        return "不明"
    v = value.strip()
    if v in ("あり", "少しあり", "なし", "不明"):
        return v
    return v


def _score_from_state(state: str) -> int:
    """状態を 0〜2 のスコアに変換（分析用）。不明は -1。"""
    return {"あり": 2, "少しあり": 1, "なし": 0}.get(state, -1)


async def save_actual(
    spot_name: str,
    rime_ice: str,
    deposition: str,
    radiation_fog: str,
    unkai: str,
    fog: str,
    light: str,
    note: str,
    date: str,
) -> dict:
    """
    撮影実績を actuals.json に保存する（設計書v1.4スキーマ準拠）。
    観測日の実況気象（前夜21時〜翌日6時）を取得して一緒に保存し、
    直近の予測ログがあれば linked_prediction_id で紐づける。

    Args:
        spot_name: 地点名（例: 田ノ原湿原）
        rime_ice: 霧氷の状況（あり / 少しあり / なし / 不明）
        deposition: 凝華の状況（あり / 少しあり / なし / 不明）。言及なければ "不明"
        radiation_fog: 放射霧の状況（あり / 少しあり / なし / 不明）。言及なければ "不明"
        unkai: 雲海の状況（あり / 少しあり / なし / 不明）。言及なければ "不明"
        fog: 霧の状況（あり / なし / 不明）
        light: 光の状況（あり / 少しあり / なし / 不明）
        note: 備考。ない場合は空文字 ""
        date: 撮影日の生表現または空文字。換算はシステム側で行う
    """
    jst = timezone(timedelta(hours=9))
    resolved_date = _resolve_date(date)

    rime_ice      = _norm_state(rime_ice)
    deposition    = _norm_state(deposition)
    radiation_fog = _norm_state(radiation_fog)
    unkai         = _norm_state(unkai)
    light         = _norm_state(light)
    fog           = _norm_state(fog)

    # 地点情報を解決（緯度経度・グループ取得のため）
    spots = await resolve_location(spot_name)
    spot = spots[0] if spots else None
    resolved_name = spot["name"] if spot else spot_name

    # 観測日の実況気象を取得
    historical_weather = {"time_range": "前夜21時〜翌日6時", "hourly_actual": []}
    if spot:
        try:
            historical_weather = await fetch_historical_weather(
                lat=spot["lat"],
                lng=spot["lng"],
                spot_name=resolved_name,
                observation_date=resolved_date,
            )
        except Exception as e:
            historical_weather = {
                "time_range": "前夜21時〜翌日6時",
                "hourly_actual": [],
                "error": str(e),
            }

    # 直近の予測IDと紐づけ
    linked_prediction_id = None
    if PREDICTIONS_FILE_ID:
        try:
            linked_prediction_id = await find_latest_prediction_id(
                PREDICTIONS_FILE_ID,
                spot_name=resolved_name,
                target_date=resolved_date,
            )
        except Exception:
            linked_prediction_id = None

    record = {
        "id": f"actual_{resolved_date.replace('-', '')}_{resolved_name}",
        "spot_name": resolved_name,
        "observation_date": resolved_date,
        "recorded_at": datetime.now(jst).isoformat(),
        "observations": {
            "rime": rime_ice,
            "rime_score": _score_from_state(rime_ice),
            "deposition": deposition,
            "deposition_score": _score_from_state(deposition),
            "radiation_fog": radiation_fog,
            "radiation_fog_score": _score_from_state(radiation_fog),
            "unkai": unkai,
            "fog": fog,
            "light": light,
        },
        "memo": note,
        "historical_weather": historical_weather,
        "linked_prediction_id": linked_prediction_id,
    }

    if ACTUALS_FILE_ID:
        await append_to_json_list(ACTUALS_FILE_ID, record)

    return {"status": "ok", "saved": record}


RECORD_INSTRUCTION = """
あなたは撮影実績を記録するアシスタントです。
ユーザーのメッセージから撮影結果を読み取り、save_actual ツールで保存してください。

【読み取りルール】
- 地点名: DEFAULT_SPOTSの地点名またはエイリアスで解決する
- 日付: ユーザーが言った日付表現を、計算せずそのままの文字列で date に渡すこと
  ※日付の言及が無ければ空文字 "" を渡す
  ※自分で今日の日付を推測したり計算したりしないこと。換算はシステム側で行う
- 各現象は「あり」「少しあり」「なし」のいずれかに分類する
  ※言及がない現象は「不明」を渡す
  - rime_ice: 霧氷
  - deposition: 凝華・ダイヤモンドダスト（「凝華」「ダイヤモンドダスト」の言及）
  - radiation_fog: 放射霧（「放射霧」の言及。ただの「霧」は fog 側で扱う）
  - unkai: 雲海
  - fog: 霧全般（放射霧と区別がつかない一般的な「霧」はこちら）
  - light: 光・朝焼け
- note: 備考がない場合は空文字 "" を渡すこと

【入力例とパース例】
「田ノ原 霧氷あり 霧なし 光あり」
spot_nameは田ノ原湿原、rime_iceはあり、fogはなし、lightはあり、
depositionとradiation_fogとunkaiは不明、noteは空文字、dateは空文字

「昨日の上高地、霧氷少しあり、霧あり」
spot_nameは田代湿原(上高地)、dateは"昨日"、rime_iceは少しあり、fogはあり、
lightは不明、depositionとradiation_fogとunkaiは不明、noteは空文字

「一昨日の戦場ヶ原、霧氷バッチリ」
spot_nameは戦場ヶ原、rime_iceはあり、その他は不明、noteは空文字、dateは"一昨日"

「5/10の田ノ原、放射霧あり、雲海なし」
spot_nameは田ノ原湿原、radiation_fogはあり、unkaiはなし、rime_iceは不明、
fogは不明、lightは不明、noteは空文字、dateは"5/10"

【保存後の返答】
save_actual ツールの戻り値（saved の中身）をもとに、実際の値に置き換えて
以下の形式で返答すること。saved に含まれる observation_date を必ずそのまま使うこと。
「不明」の項目は表示しなくてよい。

「（地点名）（観測日）の実績を記録しました。
 霧氷: （状況） / 霧: （状況） / 光: （状況）
 （凝華・放射霧・雲海のうち不明でないものがあれば追記）
 ありがとうございます！」
"""

save_actual_tool = FunctionTool(func=save_actual)

record_agent = LlmAgent(
    name="RecordAgent",
    model="gemini-2.5-flash",
    description="ユーザーの撮影実績（霧氷・凝華・放射霧・雲海・霧・光）を記録するエージェント。",
    instruction=RECORD_INSTRUCTION,
    tools=[save_actual_tool],
    output_key="record_result",
    generate_content_config=types.GenerateContentConfig(
        temperature=0.1,
    ),
)