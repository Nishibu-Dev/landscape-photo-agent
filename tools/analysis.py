# 夜間分析ツール群
#
# 役割:
#   - analyze_fog_patterns: actuals.json を「霧あり/霧なし」で分け、
#     夜間(0時〜6時)の湿度・T-Td・風速・下層/中層/上層雲・前日降水量を
#     比較できるレポートテキストを組み立てる。数値の丸め・集計はすべてここで
#     決定的に行い、LLM には「そのまま出力する」ことだけを任せる。
#     ※実績データ(hourly_actual)自体は前夜21時〜翌日6時の10時間分を保持しているが、
#       分析対象として集計するのは0時〜6時の7時点のみに絞り込む。
#   - save_fog_knowledge / load_fog_knowledge: 利用者が見つけた霧のパターンを
#     地点別にノウハウとして fog_knowledge.json に蓄積・参照する。
#
# 設計方針:
#   分析対象は「地点名で絞り込み + 全期間」(絞り込みなしで蓄積された実績を全部見る)。
#   霧あり/霧なしの判定は observations.radiation_fog を基準にする
#   （あり・少しあり → 霧あり、なし → 霧なし、不明 → 分析から除外）。

import os
from datetime import datetime, timezone, timedelta

from tools.storage import read_json, append_to_json_list
from tools.location import resolve_location
from tools.logger import get_logger

logger = get_logger(__name__)

ACTUALS_FILE_ID = os.environ.get("ACTUALS_FILE_ID", "")
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
        logger.exception(f"analyze_fog_patterns: actuals.json 読み込み失敗 spot={spot_name}")
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


def _fmt_stat(values: list[float], ndigits: int = 0) -> str:
    avg = round(sum(values) / len(values), ndigits)
    mn = round(min(values), ndigits)
    mx = round(max(values), ndigits)
    if ndigits == 0:
        avg, mn, mx = int(avg), int(mn), int(mx)
    return f"avg:{avg} min:{mn} max:{mx}"


NIGHT_ANALYSIS_HOURS = set(range(0, 7))  # 分析対象は0時〜6時の7時点


def _in_night_analysis_window(hour_entry: dict) -> bool:
    time_str = hour_entry.get("time", "")
    try:
        hour = int(time_str[11:13])
    except (ValueError, IndexError):
        return False
    return hour in NIGHT_ANALYSIS_HOURS


class _NightRecord:
    def __init__(self, record: dict):
        self.date_disp = _mmdd(record.get("observation_date", ""))
        self.fog_status = _classify_fog(record.get("observations", {}) or {})
        hw = record.get("historical_weather", {}) or {}
        all_hourly = hw.get("hourly_actual", []) or []
        self.hourly = [h for h in all_hourly if _in_night_analysis_window(h)]
        self.prev_day_precip = hw.get("prev_day_precip_mm", 0.0)
        self.memo = (record.get("memo") or "").strip()

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
        return [h["wind"] for h in self.hourly]

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


def _build_report(spot_name: str, nights: list[_NightRecord], knowledge_lines: list[str]) -> str:
    fog_nights = _sort_desc([n for n in nights if n.fog_status == "霧あり"])
    nofog_nights = _sort_desc([n for n in nights if n.fog_status == "霧なし"])

    lines = []
    lines.append(f"■{spot_name} 夜間分析")
    lines.append("【サマリー：夜間(0-6時)の条件達成時間 ※最大7h】")
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


async def analyze_fog_patterns(spot_name: str) -> dict:
    """
    actuals.json をもとに、指定地点の「霧が発生した夜」と「発生しなかった夜」を比較する
    夜間分析レポートを作成する。

    霧あり/霧なしの判定は observations.radiation_fog を基準にする
    （あり・少しあり→霧あり、なし→霧なし、不明は分析から除外）。
    地形特性(パターンA〜E)は加味せず、全地点共通の閾値(湿度≥90%・T-Td≤2℃・
    風速≤2m/s・下層雲≤10%)で夜間(0時〜6時)の条件達成時間を集計する。
    対象期間は絞り込まず、記録されている全期間の実績を使う。

    Args:
        spot_name: 地点名（例: 田ノ原湿原）。エイリアスや揺れも解決を試みる。

    Returns:
        status="ok" の場合 report_text にLINE送信用の完成テキストを含む。
        実績が無い場合は status="no_data"。
    """
    spots = await resolve_location(spot_name)
    resolved_name = spots[0]["name"] if spots else spot_name

    records = await _load_actuals(resolved_name)
    nights = [_NightRecord(r) for r in records]
    nights = [n for n in nights if n.valid]

    if not nights:
        return {
            "status": "no_data",
            "spot_name": resolved_name,
            "message": f"{resolved_name}の実績記録がまだありません。",
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
    以後、その地点の夜間分析(analyze_fog_patterns)のレポート末尾に蓄積ノウハウとして表示される。

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
