# 予測精度検証ツール群
#
# 役割:
#   - analyze_prediction_accuracy: actuals.json を「霧あり/霧なし」で分け、
#     それぞれの夜に紐づく predictions.json の予測値(夜間0時〜6時の
#     湿度・T-Td・風速・下層/中層/上層雲・前日降水量)を比較できる
#     レポートテキストを組み立てる。実測値ではなく、あくまで「予測時点で
#     どんな値が出ていたか」を霧あり/霧なしで比較するのが目的
#     （＝次回以降の予測ルール調整の材料にする）。
#     数値の丸め・集計はすべてここで決定的に行い、LLMには
#     「そのまま出力する」ことだけを任せる。
#     ※predictions.json の hourly 自体は前夜21時〜翌日6時の10時間分を
#       保持しているが、分析対象として集計するのは0時〜6時の7時点のみ。
#   - save_fog_knowledge / load_fog_knowledge: 利用者が見つけた霧のパターンを
#     地点別にノウハウとして fog_knowledge.json に蓄積・参照する。
#
# 設計方針:
#   分析対象は「地点名で絞り込み + 全期間」(絞り込みなしで蓄積された実績を全部見る)。
#   霧あり/霧なしの判定は observations.radiation_fog を基準にする
#   （あり・少しあり → 霧あり、なし → 霧なし、不明 → 分析から除外）。
#   実績記録(save_actual)時に紐づけられた linked_prediction_id で
#   predictions.json の該当予測を引く。予測ログが見つからない実績は
#   比較対象外として除外する。

import os
from datetime import datetime, timezone, timedelta

from tools.storage import read_json, append_to_json_list
from tools.location import resolve_location
from tools.logger import get_logger

logger = get_logger(__name__)

ACTUALS_FILE_ID = os.environ.get("ACTUALS_FILE_ID", "")
PREDICTIONS_FILE_ID = os.environ.get("PREDICTIONS_FILE_ID", "")
FOG_KNOWLEDGE_FILE_ID = os.environ.get("FOG_KNOWLEDGE_FILE_ID", "")

# 放射霧の共通4条件(地形パターンA〜Eは加味しない、全地点共通の閾値)
HUMIDITY_THRESHOLD = 90
T_TD_THRESHOLD = 2
WIND_THRESHOLD = 2
CLOUD_LOW_THRESHOLD = 10


def _jst_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=9)))


def _classify_fog(observations: dict) -> str | None:
    """
    observations から 霧あり/霧なし/None(除外) を判定する。

    radiation_fog を優先し、それが「不明」の場合は fog フィールドにフォールバックする。
    RecordAgent の仕様上、ユーザーが「放射霧」と明示しない一般的な「霧」の報告は
    fog フィールドに入り radiation_fog は「不明」のまま残るため、
    radiation_fog だけを見ると実績のほとんどが分析対象から漏れてしまう。
    """
    radiation_fog_state = observations.get("radiation_fog", "不明")
    if radiation_fog_state in ("あり", "少しあり"):
        return "霧あり"
    if radiation_fog_state == "なし":
        return "霧なし"

    fog_state = observations.get("fog", "不明")
    if fog_state in ("あり", "少しあり"):
        return "霧あり"
    if fog_state == "なし":
        return "霧なし"

    return None  # 両方とも不明の場合のみ分析対象から除外


def _mmdd(observation_date: str) -> str:
    try:
        d = datetime.strptime(observation_date, "%Y-%m-%d").date()
        return d.strftime("%m-%d")
    except ValueError:
        return observation_date


async def _load_actuals(spot_name: str) -> list[dict]:
    if not ACTUALS_FILE_ID:
        return []
    try:
        data = await read_json(ACTUALS_FILE_ID)
    except Exception:
        logger.exception(f"analyze_prediction_accuracy: actuals.json 読み込み失敗 spot={spot_name}")
        return []

    if isinstance(data, dict):
        records = data.get("actuals", [])
    elif isinstance(data, list):
        records = data
    else:
        records = []

    return [
        r for r in records
        if isinstance(r, dict) and r.get("spot_name") == spot_name
    ]


async def _load_predictions_by_id(spot_name: str) -> dict[str, dict]:
    """predictions.json から指定地点ぶんを id をキーにした dict で返す。"""
    if not PREDICTIONS_FILE_ID:
        return {}
    try:
        data = await read_json(PREDICTIONS_FILE_ID)
    except Exception:
        logger.exception(f"analyze_prediction_accuracy: predictions.json 読み込み失敗 spot={spot_name}")
        return {}

    if isinstance(data, dict):
        records = data.get("predictions", [])
    elif isinstance(data, list):
        records = data
    else:
        records = []

    return {
        r["id"]: r
        for r in records
        if isinstance(r, dict) and r.get("spot_name") == spot_name and r.get("id")
    }


NIGHT_ANALYSIS_HOURS = set(range(0, 7))  # 分析対象は0時〜6時の7時点


def _in_night_analysis_window(hour_entry: dict) -> bool:
    time_str = hour_entry.get("time", "")
    try:
        hour = int(time_str[11:13])
    except (ValueError, IndexError):
        return False
    return hour in NIGHT_ANALYSIS_HOURS


class _NightRecord:
    """1回の実績記録につき、その夜に紐づく「予測値」を保持する。

    グループ分け(霧あり/霧なし)は実績(observations)の霧判定で行うが、
    集計する数値そのものは実測値ではなく predictions.json の予測値を使う。
    """

    def __init__(self, actual_record: dict, prediction: dict | None):
        self.date_disp = _mmdd(actual_record.get("observation_date", ""))
        self.fog_status = _classify_fog(actual_record.get("observations", {}) or {})
        self.memo = (actual_record.get("memo") or "").strip()

        weather_data = (prediction or {}).get("weather_data", {}) or {}
        all_hourly = weather_data.get("hourly", []) or []
        self.hourly = [h for h in all_hourly if _in_night_analysis_window(h)]
        self.prev_day_precip = weather_data.get("prev_day_precip_mm", 0.0)

    @property
    def valid(self) -> bool:
        return self.fog_status is not None and len(self.hourly) > 0

    @property
    def humidity_list(self) -> list[float]:
        return [h["humidity"] for h in self.hourly]

    @property
    def t_td_list(self) -> list[float]:
        return [round(h["temp"] - h["dew_point"], 1) for h in self.hourly]

    @property
    def wind_list(self) -> list[float]:
        return [h["wind_adjusted"] for h in self.hourly]

    @property
    def cloud_low_list(self) -> list[float]:
        return [h["cloud_low"] for h in self.hourly]

    @property
    def cloud_mid_list(self) -> list[float]:
        return [h["cloud_mid"] for h in self.hourly]

    @property
    def cloud_high_list(self) -> list[float]:
        return [h["cloud_high"] for h in self.hourly]

    def summary_line(self) -> str:
        h_hum = sum(1 for v in self.humidity_list if v >= HUMIDITY_THRESHOLD)
        h_ttd = sum(1 for v in self.t_td_list if v <= T_TD_THRESHOLD)
        h_wind = sum(1 for v in self.wind_list if v <= WIND_THRESHOLD)
        h_cloud = sum(1 for v in self.cloud_low_list if v <= CLOUD_LOW_THRESHOLD)
        return (
            f"{self.date_disp}  湿度：{h_hum}h T-Td：{h_ttd}h "
            f"風速：{h_wind}h 下層雲：{h_cloud}h 前日雨：{round(self.prev_day_precip, 1)}mm"
        )


def _sort_desc(nights: list[_NightRecord]) -> list[_NightRecord]:
    return sorted(nights, key=lambda n: n.date_disp, reverse=True)


def _fmt_stat(values: list[float], ndigits: int = 0) -> str:
    avg = round(sum(values) / len(values), ndigits)
    mn = round(min(values), ndigits)
    mx = round(max(values), ndigits)
    if ndigits == 0:
        avg, mn, mx = int(avg), int(mn), int(mx)
    return f"avg:{avg} min:{mn} max:{mx}"


def _build_report(spot_name: str, nights: list[_NightRecord], knowledge_lines: list[str]) -> str:
    fog_nights = _sort_desc([n for n in nights if n.fog_status == "霧あり"])
    nofog_nights = _sort_desc([n for n in nights if n.fog_status == "霧なし"])

    lines = []
    lines.append(f"■{spot_name} 予測精度検証（予測値ベース）")
    lines.append("【サマリー：夜間(0-6時)の条件達成時間 ※最大7h／予測値ベース】")
    lines.append("条件: 湿度≥90% / T-Td≤2℃ / 風速≤2m/s / 下層雲≤10%")
    lines.append("※地形特性(パターンA〜E等)は加味せず、全地点共通の条件で判定しています")
    lines.append("")
    lines.append("霧あり")
    if fog_nights:
        for n in fog_nights:
            lines.append(n.summary_line())
    else:
        lines.append("（該当なし）")
    lines.append("")
    lines.append("霧なし")
    if nofog_nights:
        for n in nofog_nights:
            lines.append(n.summary_line())
    else:
        lines.append("（該当なし）")

    def _section(title: str, ndigits: int, getter):
        lines.append("")
        lines.append(title)
        lines.append("霧あり")
        for n in fog_nights:
            lines.append(f"{n.date_disp} {_fmt_stat(getter(n), ndigits)}")
        lines.append("霧なし")
        for n in nofog_nights:
            lines.append(f"{n.date_disp} {_fmt_stat(getter(n), ndigits)}")

    _section("【湿度(%)】", 0, lambda n: n.humidity_list)
    _section("【T-Td(℃)】", 1, lambda n: n.t_td_list)
    _section("【風速(m/s)】", 1, lambda n: n.wind_list)
    _section("【下層雲(%)】", 0, lambda n: n.cloud_low_list)
    _section("【中層雲(%)】", 0, lambda n: n.cloud_mid_list)
    _section("【上層雲(%)】", 0, lambda n: n.cloud_high_list)

    lines.append("")
    lines.append("【メモ抜粋】")
    memo_found = False
    for n in fog_nights + nofog_nights:
        if n.memo:
            lines.append(f"・{n.date_disp}: {n.memo}")
            memo_found = True
    if not memo_found:
        lines.append("（メモの記載はありません）")

    lines.append("")
    if knowledge_lines:
        lines.append("※蓄積ノウハウ:")
        for k in knowledge_lines:
            lines.append(f"・{k}")
    else:
        lines.append("※蓄積ノウハウ: まだ記録がありません")

    return "\n".join(lines)


async def analyze_prediction_accuracy(spot_name: str) -> dict:
    """
    actuals.json をもとに、指定地点の「霧が発生した夜」と「発生しなかった夜」を分け、
    それぞれの夜に紐づく predictions.json の予測値(夜間0時〜6時)を比較する
    レポートを作成する。実測値ではなく予測値どうしを霧あり/霧なしで比較することで、
    次回以降の予測ルール調整の材料にする。

    霧あり/霧なしの判定は observations.radiation_fog を基準にする
    （あり・少しあり→霧あり、なし→霧なし、不明は分析から除外）。
    地形特性(パターンA〜E)は加味せず、全地点共通の閾値(湿度≥90%・T-Td≤2℃・
    風速≤2m/s・下層雲≤10%)で夜間(0時〜6時)の予測値の条件達成時間を集計する。
    実績に linked_prediction_id が無い、または対応する予測ログが
    見つからない場合はその実績を分析から除外する。
    対象期間は絞り込まず、記録されている全期間の実績を使う。

    Args:
        spot_name: 地点名（例: 田ノ原湿原）。エイリアスや揺れも解決を試みる。

    Returns:
        status="ok" の場合 report_text にLINE送信用の完成テキストを含む。
        実績が無い場合は status="no_data"。
    """
    spots = await resolve_location(spot_name)
    resolved_name = spots[0]["name"] if spots else spot_name

    actual_records = await _load_actuals(resolved_name)
    if not actual_records:
        return {
            "status": "no_data",
            "spot_name": resolved_name,
            "message": f"{resolved_name}の実績記録がまだありません。",
        }

    predictions_by_id = await _load_predictions_by_id(resolved_name)

    nights = [
        _NightRecord(r, predictions_by_id.get(r.get("linked_prediction_id")))
        for r in actual_records
    ]
    nights = [n for n in nights if n.valid]

    if not nights:
        return {
            "status": "no_data",
            "spot_name": resolved_name,
            "message": (
                f"{resolved_name}の実績記録はありますが、突き合わせ可能な予測ログが"
                "見つかりませんでした。"
            ),
        }

    knowledge_lines = await load_fog_knowledge(resolved_name)
    report_text = _build_report(resolved_name, nights, knowledge_lines)

    return {
        "status": "ok",
        "spot_name": resolved_name,
        "night_count": len(nights),
        "report_text": report_text,
    }


async def _load_fog_knowledge_raw() -> list[dict]:
    if not FOG_KNOWLEDGE_FILE_ID:
        return []
    try:
        data = await read_json(FOG_KNOWLEDGE_FILE_ID)
    except Exception:
        logger.exception("load_fog_knowledge: fog_knowledge.json 読み込み失敗")
        return []

    if isinstance(data, dict):
        return data.get("fog_knowledge", []) or []
    if isinstance(data, list):
        return data
    return []


async def load_fog_knowledge(spot_name: str) -> list[str]:
    """指定地点に紐づくノウハウのメモ文字列一覧を、記録順(古い順)で返す。"""
    records = await _load_fog_knowledge_raw()
    matched = [
        r for r in records
        if isinstance(r, dict) and r.get("spot_name") == spot_name
    ]
    matched.sort(key=lambda r: r.get("recorded_at") or "")
    return [r.get("note", "") for r in matched if r.get("note")]


async def save_fog_knowledge(spot_name: str, note: str) -> dict:
    """
    利用者が見つけた霧のパターンを、地点別のノウハウとして fog_knowledge.json に保存する。
    以後、その地点の予測精度検証(analyze_prediction_accuracy)のレポート末尾に蓄積ノウハウとして表示される。

    Args:
        spot_name: 地点名（例: 田ノ原湿原）
        note: ノウハウの内容（自由記述。例:「5時過ぎから雨→低層雲→霧が発生するパターンが多い」）
    """
    spots = await resolve_location(spot_name)
    resolved_name = spots[0]["name"] if spots else spot_name

    record = {
        "spot_name": resolved_name,
        "note": note,
        "recorded_at": _jst_now().isoformat(),
    }

    if FOG_KNOWLEDGE_FILE_ID:
        await append_to_json_list(FOG_KNOWLEDGE_FILE_ID, record)

    return {"status": "ok", "saved": record}
