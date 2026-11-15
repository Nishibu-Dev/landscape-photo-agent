import asyncio
import contextvars
import os
import httpx
from datetime import datetime, timezone, timedelta
import math
from config.adjustments import get_wind_factor
from config.spots import get_rime_type, RIME_TYPE_WETLAND, RIME_TYPE_SPRING
from tools.logger import get_logger

logger = get_logger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# jma_seamless: MSMの予報期間内はMSM、期間外(翌々朝など)はGSMで補完される
_OPEN_METEO_MODEL = "jma_seamless"
_OPEN_METEO_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_HOURLY_VARS = (
    "temperature_2m,relative_humidity_2m,dew_point_2m,"
    "cloud_cover_low,cloud_cover_mid,cloud_cover_high,"
    "wind_speed_10m,precipitation"
)
# fetch_all_forecasts がまとめ取得したデータを、地点ごとの fetch_forecast に渡す
_PREFETCHED: contextvars.ContextVar = contextvars.ContextVar("open_meteo_prefetched", default=None)

# 予測スナップショット保存先(predictions.json の Drive ファイルID)。
# fetch_forecast が気象データ取得時に、ここへ予測ログを即保存する。
# これにより LLM が save_prediction を呼び忘れても確実に記録される。
PREDICTIONS_FILE_ID = os.environ.get("PREDICTIONS_FILE_ID", "")

# 取得する時間帯: 前夜21時〜翌日6時の連続10時間（設計書v1.4で統一）
EVENING_HOURS = list(range(21, 24))   # 21, 22, 23
MORNING_HOURS = list(range(0, 7))     # 0..6
TIME_RANGE_LABEL = "前夜21時〜翌日6時"


def _get_elev_registered(spot_name: str) -> int | None:
    """登録地点(DEFAULT_SPOTS / EXTRA_SPOTS)の標高を返す。未登録は None。"""
    from config.spots import DEFAULT_SPOTS, EXTRA_SPOTS
    for spot in DEFAULT_SPOTS + EXTRA_SPOTS:
        if spot["name"] == spot_name:
            return spot["elev"]
    return None


async def _resolve_elev(spot_name: str, lat: float, lng: float) -> int | None:
    """
    標高を返す。まず登録地点から引き、無ければ Maps Elevation API で取得する。
    未知地点(白樺湖など)でも標高が残るようにするためのフォールバック。
    """
    elev = _get_elev_registered(spot_name)
    if elev is not None:
        return elev
    # 未登録地点は緯度経度から Elevation API で取得(location.py のヘルパーを再利用)
    try:
        from tools.location import _fetch_elevation
        return await _fetch_elevation(lat, lng)
    except Exception:
        logger.warning(f"_resolve_elev fallback failed spot={spot_name} lat={lat} lng={lng}", exc_info=True)
        return None


def _jst_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=9)))


def _build_target_times(base_date_str: str) -> list[str]:
    """
    base_date_str (YYYY-MM-DD) の前夜21時〜翌日6時のISO時刻文字列リストを作る。
    21〜23時は base_date 当日、0〜6時は翌日。
    """
    base = datetime.strptime(base_date_str, "%Y-%m-%d").date()
    next_day = base + timedelta(days=1)
    evening = [f"{base.isoformat()}T{h:02d}:00" for h in EVENING_HOURS]
    morning = [f"{next_day.isoformat()}T{h:02d}:00" for h in MORNING_HOURS]
    return evening + morning


def _for_llm(result: dict) -> dict:
    """
    エージェント(LLM)に返す形に整える。予測の記録には全項目を残すが、回答で
    風速の補正係数や補正前の値に触れないよう、LLMには判定用の風速だけを渡す。
    """
    if not isinstance(result, dict) or "hourly" not in result:
        return result
    out = dict(result)
    out["hourly"] = [
        {k: v for k, v in h.items() if k not in ("wind_raw", "wind_adjusted")}
        | {"wind": h.get("wind_adjusted")}
        for h in result["hourly"]
    ]
    return out


async def fetch_forecast(lat: float, lng: float, spot_name: str,
                         save_snapshot: bool = True,
                         target_offset_days: int = 1) -> dict:
    """
    撮影コンディション予測のため、対象朝の前夜21時〜翌日6時の予報を取得する。

    Args:
        target_offset_days: 何日後の朝を対象とするか。1=翌朝(デフォルト)、2=翌々朝。
    save_snapshot=True のとき、取得結果を predictions.json に即保存する。
    「おすすめ」一括(fetch_all_forecasts)では並列書き込みの競合を避けるため
    False で呼び、呼び出し側が最後にまとめて1回だけ保存する。
    翌々日予測(target_offset_days=2)では精度検証対象外のため保存しない。
    """
    result = await _fetch_forecast_full(lat, lng, spot_name, save_snapshot, target_offset_days)
    return _for_llm(result)


async def _fetch_forecast_full(lat: float, lng: float, spot_name: str,
                               save_snapshot: bool = True,
                               target_offset_days: int = 1) -> dict:
    """fetch_forecast の本体。記録用に全項目(補正前の風速を含む)を返す。"""
    params = {
        "latitude": lat,
        "longitude": lng,
        "hourly": (
            "temperature_2m,relative_humidity_2m,dew_point_2m,"
            "cloud_cover_low,cloud_cover_mid,cloud_cover_high,"
            "wind_speed_10m,precipitation"
        ),
        "timezone": "Asia/Tokyo",
        # past_days=1 で前日分も取得し、パターンC「前日雨リセット」判定に使う
        "past_days": 1,
        "forecast_days": 3,
        "models": _OPEN_METEO_MODEL,
    }

    data = _PREFETCHED.get()
    if data is None:
        async with httpx.AsyncClient(
            timeout=_OPEN_METEO_TIMEOUT,
            transport=httpx.AsyncHTTPTransport(retries=2),
        ) as client:
            resp = await client.get(OPEN_METEO_URL, params=params)
            resp.raise_for_status()
            data = resp.json()

    hourly = data["hourly"]
    factor = get_wind_factor(spot_name)

    now_jst = _jst_now()
    # base_date: offset=1 なら今日(今夜21時〜明朝6時)、offset=2 なら明日(明夜21時〜明後朝6時)
    base_date = (now_jst + timedelta(days=target_offset_days - 1)).strftime("%Y-%m-%d")
    target_times = _build_target_times(base_date)

    result_hourly = []
    times = hourly["time"]
    for target in target_times:
        if target in times:
            i = times.index(target)
            raw_wind = hourly["wind_speed_10m"][i]
            result_hourly.append({
                "time":          times[i],
                "temp":          hourly["temperature_2m"][i],
                "dew_point":     hourly["dew_point_2m"][i],
                "humidity":      hourly["relative_humidity_2m"][i],
                "wind_raw":      raw_wind,
                "wind_adjusted": round(raw_wind * factor, 1) if raw_wind is not None else None,
                "cloud_low":     hourly["cloud_cover_low"][i],
                "cloud_mid":     hourly["cloud_cover_mid"][i],
                "cloud_high":    hourly["cloud_cover_high"][i],
            })

    # パターンC「前日雨リセット」判定用に、対象前日(base_date当日0〜20時)の降水量合計を算出。
    # ForecastAgent はこの値を見て、乾燥湿原の閾値を緩めるか判断する。
    prev_day_precip = _sum_prev_day_precip(hourly, base_date)

    summary = _build_summary(spot_name, factor, result_hourly, prev_day_precip)
    # 霧氷のタイプが確定している地点(湧水型・湿原・盆地型)は、Pythonで機械判定し、
    # 結果を要約の末尾に1行で明示する(LLMが数値の読み落としや解釈でぶれるのを防ぐため)。
    # それ以外(雲中型・未確定)は、指示文の共通ルール3でLLMが判定する。
    rime_type = get_rime_type(spot_name)
    rime_judgement = None
    if rime_type == RIME_TYPE_SPRING:
        rime_judgement = check_spring_rime(result_hourly)
    elif rime_type == RIME_TYPE_WETLAND:
        rime_judgement = check_wetland_rime(result_hourly)
    if rime_judgement is not None:
        summary = summary + "\n" + rime_judgement["summary_line"]

    elev = await _resolve_elev(spot_name, lat, lng)

    target_date_str = (now_jst + timedelta(days=target_offset_days)).strftime("%Y-%m-%d")
    result = {
        "spot_name":  spot_name,
        "elev":       elev,
        "target_date": target_date_str,
        "time_range": TIME_RANGE_LABEL,
        "prev_day_precip_mm": prev_day_precip,  # 前日降水量合計(mm)。パターンC判定用。
        "hourly":     result_hourly,
        "summary":    summary,
    }
    result["rime_type"] = rime_type
    if rime_judgement is not None:
        result["rime_judgement"] = {k: v for k, v in rime_judgement.items() if k != "summary_line"}

    # 予測スナップショットを predictions.json へ即保存(LLMのsave_prediction呼び出しに依存しない)。
    # 確率テキストはこの時点で未確定のため空で保存し、weather_data(実値)を確実に残す。
    # 後で実績と突き合わせる際は spot_name + target_date で紐づけられる。
    # ※「おすすめ」一括時は save_snapshot=False で呼ばれ、呼び出し側がまとめ保存する
    #   (並列 read-modify-write による上書き競合を避けるため)。
    # ※ 翌々日予測(offset=2)は精度検証対象外のため保存しない。
    if save_snapshot and target_offset_days == 1:
        await _save_prediction_snapshot(result)

    return result


async def _fetch_open_meteo_batch(spots: list[dict]) -> list[dict]:
    """全地点を1回のリクエストで取得する。
    Open-Meteoは同一IPからの同時接続を5本程度に絞り、残りを待たせるため、
    地点ごとに接続するとTLSハンドシェイクでタイムアウトする(2026-11-15に確認)。"""
    params = {
        "latitude":  ",".join(str(s["lat"]) for s in spots),
        "longitude": ",".join(str(s["lng"]) for s in spots),
        "hourly": _HOURLY_VARS,
        "timezone": "Asia/Tokyo",
        "past_days": 1,
        "forecast_days": 3,
        "models": _OPEN_METEO_MODEL,
    }
    async with httpx.AsyncClient(
        timeout=_OPEN_METEO_TIMEOUT,
        transport=httpx.AsyncHTTPTransport(retries=2),
    ) as client:
        resp = await client.get(OPEN_METEO_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
    if isinstance(data, dict):  # 1地点のときはリストでなく辞書で返る
        data = [data]
    if len(data) != len(spots):
        raise ValueError(f"Open-Meteo returned {len(data)} results for {len(spots)} spots")
    return data


async def fetch_all_forecasts(user_input: str, target_offset_days: int = 1) -> list[dict]:
    """
    全おすすめ地点の予報を一括取得する。

    Args:
        user_input: ユーザー入力テキスト（resolve_location に渡す）
        target_offset_days: 何日後の朝を対象とするか。1=翌朝、2=翌々朝。
    """
    from tools.location import resolve_location

    spots = await resolve_location(user_input)
    if not spots:
        return []

    async def _fetch_one(spot: dict, delay: float) -> dict:
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            return await _fetch_forecast_full(
                lat=spot["lat"],
                lng=spot["lng"],
                spot_name=spot["name"],
                save_snapshot=False,
                target_offset_days=target_offset_days,
            )
        except Exception as e:
            logger.exception(f"fetch_forecast failed in fetch_all_forecasts spot={spot['name']}")
            return {"spot_name": spot["name"], "error": str(e)}

    # Open-Meteo へは全地点を1回のリクエストでまとめて取得し、各地点の加工処理に配る
    try:
        batch = await _fetch_open_meteo_batch(spots)
    except Exception as e:
        logger.exception("Open-Meteo batch fetch failed")
        batch = None
        results = [{"spot_name": s["name"], "error": str(e)} for s in spots]

    if batch is not None:
        async def _fetch_one_with_data(spot: dict, data: dict) -> dict:
            _PREFETCHED.set(data)  # このタスクの中だけで有効
            return await _fetch_one(spot, 0)

        results = await asyncio.gather(
            *[_fetch_one_with_data(s, d) for s, d in zip(spots, batch)]
        )
    results = list(results)

    # 翌日予測のみ predictions.json に保存（翌々日は精度検証対象外）
    if target_offset_days == 1:
        await _save_prediction_snapshots_bulk(results)

    return [_for_llm(r) for r in results]


async def fetch_historical_weather(
    lat: float,
    lng: float,
    spot_name: str,
    observation_date: str,
) -> dict:
    """
    実績記録用に、観測日の実況気象（前夜21時〜翌日6時）を取得する。

    Open-Meteo forecast API の past_days を使い、観測日当日の朝6時までを含む
    前夜21時〜翌日6時の10時間を取得する。観測日が古く past_days の範囲外の場合は
    archive API にフォールバックする。

    Args:
        lat: 緯度
        lng: 経度
        spot_name: 地点名
        observation_date: 観測日 YYYY-MM-DD（この日の朝の撮影実績に対応）

    Returns:
        time_range と hourly_actual を持つ dict。取得できない場合は hourly_actual=[]。
    """
    factor = get_wind_factor(spot_name)
    # observation_date の「前夜」は前日21時。前夜21時〜当日6時を取得する。
    obs = datetime.strptime(observation_date, "%Y-%m-%d").date()
    eve_date = obs - timedelta(days=1)
    target_times = (
        [f"{eve_date.isoformat()}T{h:02d}:00" for h in EVENING_HOURS] +
        [f"{obs.isoformat()}T{h:02d}:00" for h in MORNING_HOURS]
    )

    now_jst = _jst_now()
    days_ago = (now_jst.date() - eve_date).days

    hourly = None
    # 直近(おおむね数日以内)は forecast API の past_days で取得
    if 0 <= days_ago <= 7:
        try:
            hourly = await _fetch_hourly_forecast_pastdays(lat, lng, days_ago + 2)
        except Exception:
            logger.warning(f"fetch_hourly_forecast_pastdays failed spot={spot_name} days_ago={days_ago}", exc_info=True)
            hourly = None
    # 取得できない/古い日付は archive にフォールバック
    if hourly is None:
        try:
            hourly = await _fetch_hourly_archive(lat, lng, eve_date, obs)
        except Exception:
            logger.warning(f"fetch_hourly_archive failed spot={spot_name} eve_date={eve_date} obs={obs}", exc_info=True)
            hourly = None

    if hourly is None:
        logger.warning(f"fetch_historical_weather: no data available spot={spot_name} observation_date={observation_date}")
        return {"time_range": TIME_RANGE_LABEL, "hourly_actual": [], "prev_day_precip_mm": 0.0}

    times = hourly["time"]
    result = []
    for target in target_times:
        if target in times:
            i = times.index(target)
            raw_wind = hourly["wind_speed_10m"][i]
            result.append({
                "time":       times[i],
                "temp":       hourly["temperature_2m"][i],
                "dew_point":  hourly["dew_point_2m"][i],
                "humidity":   hourly["relative_humidity_2m"][i],
                "wind":       round(raw_wind * factor, 1),
                "wind_raw":   raw_wind,
                "cloud_low":  hourly["cloud_cover_low"][i],
                "cloud_mid":  hourly["cloud_cover_mid"][i],
                "cloud_high": hourly["cloud_cover_high"][i],
            })

    # 前夜(=前日)0〜20時の降水量合計。分析エージェントが「前日雨」として集計に使う。
    prev_day_precip = _sum_prev_day_precip(hourly, eve_date.isoformat())

    return {
        "time_range": TIME_RANGE_LABEL,
        "hourly_actual": result,
        "prev_day_precip_mm": prev_day_precip,
    }


async def _fetch_hourly_forecast_pastdays(lat: float, lng: float, past_days: int) -> dict:
    params = {
        "latitude": lat,
        "longitude": lng,
        "hourly": (
            "temperature_2m,relative_humidity_2m,dew_point_2m,"
            "cloud_cover_low,cloud_cover_mid,cloud_cover_high,"
            "wind_speed_10m,precipitation"
        ),
        "timezone": "Asia/Tokyo",
        "past_days": min(past_days, 92),
        "forecast_days": 1,
        "models": "jma_msm",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(OPEN_METEO_URL, params=params)
        resp.raise_for_status()
        return resp.json()["hourly"]


async def _fetch_hourly_archive(lat: float, lng: float, start_date, end_date) -> dict:
    archive_url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": lat,
        "longitude": lng,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "hourly": (
            "temperature_2m,relative_humidity_2m,dew_point_2m,"
            "cloud_cover_low,cloud_cover_mid,cloud_cover_high,"
            "wind_speed_10m,precipitation"
        ),
        "timezone": "Asia/Tokyo",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(archive_url, params=params)
        resp.raise_for_status()
        return resp.json()["hourly"]


def _sum_prev_day_precip(hourly: dict, target_today_str: str) -> float:
    """
    対象日の前日(=今日 target_today_str の 0〜20時)の降水量合計(mm)を返す。
    放射霧パターンC(乾燥湿原)で「前日に雨があれば閾値を緩める」判定に使う。
    precipitation が取得できない場合は 0.0。
    """
    if "precipitation" not in hourly:
        return 0.0
    times = hourly.get("time", [])
    precip = hourly.get("precipitation", [])
    total = 0.0
    for i, t in enumerate(times):
        # 今日の日付かつ 0〜20時(夜の予測対象21時より前)を前日扱いで集計
        if t.startswith(target_today_str):
            hour = int(t[11:13])
            if hour <= 20 and i < len(precip) and precip[i] is not None:
                total += precip[i]
    return round(total, 1)


# ---------------------------------------------------------------------------
# 湧水型の霧氷ルール(田代湿原・河童橋など上高地エリア)
#   露点 ≦ -15℃ かつ 下層雲 < 10% かつ 風速(補正後) ≦ 2.5m/s が同じ時刻にそろえば成立。
#   - 気温は判定に使わない(実績では霧氷あり/なしを分けなかったため。参考表示のみ)
#   - 下層雲: 強い冷え込みでは湿った空気でも露点が下がるため、
#     「冷えているだけで湿った(雲のある)夜」を除外する役割
#   - 風速: 強いと霧氷が飛ばされるため上限を設ける
#   - 霧氷(樹霜)は時間をかけて育つため、連続時間と明け方(撮影時間帯)の成立も見る
#   閾値は過去の上高地7夜の実績(霧氷あり/少しあり/なし)を見て決定。
# ---------------------------------------------------------------------------
KAMIKOCHI_RIME_DEWPOINT_MAX = -15.0   # 露点(℃) これ以下
KAMIKOCHI_RIME_CLOUD_LOW_MAX = 10.0   # 下層雲(%) これ未満
KAMIKOCHI_RIME_WIND_MAX = 2.5         # 風速 補正後(m/s) これ以下
KAMIKOCHI_RIME_LONG_RUN_HOURS = 3     # この時間以上の連続成立で「長時間」
KAMIKOCHI_RIME_DAWN_HOURS = ("04:00", "05:00", "06:00")  # 明け方(撮影時間帯)
# 「惜しい」: 1条件だけが、この幅以内で足りない(残り2条件は満たす)
KAMIKOCHI_NEAR_MISS_MARGIN = {"露点": 2.0, "下層雲": 10.0, "風速": 0.5}

# 判定区分 → (表示名, 確率の目安)。確率の割り当ては ForecastAgent の指示文と一致させること。
KAMIKOCHI_RIME_GRADES = {
    "A": ("成立・長時間・明け方まで継続", "85〜95%"),
    "B": ("成立・長時間だが明け方は不成立", "50〜70%"),
    "C": ("成立・短時間または途切れ途切れ", "40〜60%"),
    "D": ("不成立・惜しい", "30〜50%"),
    "E": ("不成立", "20%以下"),
}


def _kamikochi_gaps(td: float, cl: float, w: float) -> dict:
    """各条件の不足分(0なら満たしている)。下層雲は「10%未満」なので 10%ちょうどは不足1。"""
    return {
        "露点": round(max(0.0, td - KAMIKOCHI_RIME_DEWPOINT_MAX), 1),
        "下層雲": round(cl - KAMIKOCHI_RIME_CLOUD_LOW_MAX + 1, 1) if cl >= KAMIKOCHI_RIME_CLOUD_LOW_MAX else 0.0,
        "風速": round(max(0.0, w - KAMIKOCHI_RIME_WIND_MAX), 1),
    }


def _is_next_hour(prev_time: str, time: str) -> bool:
    fmt = "%Y-%m-%dT%H:%M"
    return datetime.strptime(time, fmt) - datetime.strptime(prev_time, fmt) == timedelta(hours=1)


def check_spring_rime(hourly: list[dict]) -> dict:
    """
    上高地の霧氷特別ルールを時間ごとに判定し、A〜E の区分を返す。

    A: 3時間以上連続で成立し、その連続に明け方(4〜6時)を含む
    B: 3時間以上連続で成立したが、その連続に明け方を含まない
    C: 成立はしたが、最長連続が3時間未満
    D: 不成立だが、1条件だけが小さい幅で足りない時刻がある(惜しい)
    E: 不成立

    Returns:
        grade, grade_label, probability, met_hours, longest_run, near_miss, summary_line
    """
    met_times = []          # 成立した時刻(フォーマット: YYYY-MM-DDTHH:MM)
    near_miss = None
    near_miss_score = None
    has_data = False
    for h in hourly:
        td, cl, w = h.get("dew_point"), h.get("cloud_low"), h.get("wind_adjusted")
        if td is None or cl is None or w is None:
            continue
        has_data = True
        gaps = _kamikochi_gaps(td, cl, w)
        short = {k: v for k, v in gaps.items() if v > 0}
        if not short:
            met_times.append(h["time"])
            continue
        if len(short) == 1:
            (name, gap), = short.items()
            margin = KAMIKOCHI_NEAR_MISS_MARGIN[name]
            if gap <= margin:
                score = gap / margin  # 幅に対する割合で「より惜しい」を比較
                if near_miss_score is None or score < near_miss_score:
                    near_miss_score = score
                    near_miss = {"time": h["time"][11:16], "condition": name, "shortfall": gap,
                                 "dew_point": td, "cloud_low": cl, "wind_adjusted": w,
                                 "temp": h.get("temp")}

    # 最長の連続成立区間を求める(欠損や不成立の時刻で途切れる)
    runs, cur = [], []
    for t in met_times:
        if cur and _is_next_hour(cur[-1], t):
            cur.append(t)
        else:
            if cur:
                runs.append(cur)
            cur = [t]
    if cur:
        runs.append(cur)
    longest = max(runs, key=len) if runs else []
    longest_hhmm = [t[11:16] for t in longest]
    longest_has_dawn = any(t in KAMIKOCHI_RIME_DAWN_HOURS for t in longest_hhmm)

    if longest and len(longest) >= KAMIKOCHI_RIME_LONG_RUN_HOURS:
        grade = "A" if longest_has_dawn else "B"
    elif met_times:
        grade = "C"
    elif near_miss:
        grade = "D"
    else:
        grade = "E"
    label, prob = KAMIKOCHI_RIME_GRADES[grade]

    rule = (f"露点≦{KAMIKOCHI_RIME_DEWPOINT_MAX:g}℃・下層雲<{KAMIKOCHI_RIME_CLOUD_LOW_MAX:g}%・"
            f"風速≦{KAMIKOCHI_RIME_WIND_MAX:g}m/s")
    head = f"【湧水型霧氷ルール({rule})】判定{grade}: {label}(霧氷の確率目安 {prob})"
    if not has_data:
        line = f"【湧水型霧氷ルール({rule})】判定不能(データ欠損)"
        grade, label, prob = None, "判定不能", None
    elif met_times:
        line = (f"{head} / 成立 計{len(met_times)}時間 / 最長連続{len(longest)}時間"
                f"({longest_hhmm[0]}〜{longest_hhmm[-1]}) / 明け方(4〜6時)の成立"
                f"{'あり' if longest_has_dawn else 'なし'}")
    elif near_miss:
        unit = {"露点": "℃", "下層雲": "%", "風速": "m/s"}[near_miss["condition"]]
        line = (f"{head} / 最も惜しいのは{near_miss['time']}: {near_miss['condition']}があと"
                f"{near_miss['shortfall']}{unit}不足 (Td:{near_miss['dew_point']}℃, "
                f"下層雲:{near_miss['cloud_low']}%, W:{near_miss['wind_adjusted']}m/s, "
                f"参考T:{near_miss['temp']}℃)")
    else:
        line = head

    return {"rime_type": RIME_TYPE_SPRING, "grade": grade, "grade_label": label, "probability": prob,
            "met_hours": [t[11:16] for t in met_times],
            "longest_run": longest_hhmm, "near_miss": near_miss, "summary_line": line}


# 互換のため旧名も残す
check_kamikochi_rime = check_spring_rime


# ---------------------------------------------------------------------------
# 湿原・盆地型の霧氷ルール(田ノ原湿原・踊場湿原)
#   盆地にたまった湿った空気(雲・霧を含む)から霧氷が育つ。1時間ごとに
#     気温 ≦ -2℃ かつ 氷に対する湿度 ≧ 85% かつ 下層雲 ≧ 20%
#   がそろえば成立。撮影は明け方なので、明け方(4〜6時)の成立を重視する。
#   さらに、0時→6時で T-Td が 1℃以上広がった夜(乾いていく夜)は1段階格下げする。
#   - 風速の上限は設けない(実績では風の強い夜にも霧氷が出ている)
#   - 湿度は水ではなく氷に対する値で見る(氷点下では、水に対して飽和に近い空気は
#     氷に対しては過飽和になり、枝に霧氷が積もり続けるため)
#   閾値は田ノ原5夜・踊場湿原3夜の実績から決めた暫定値。今期の実績で見直す。
# ---------------------------------------------------------------------------
WETLAND_RIME_TEMP_MAX = -2.0          # 気温(℃) これ以下
WETLAND_RIME_RHICE_MIN = 85.0         # 氷に対する湿度(%) これ以上
WETLAND_RIME_CLOUD_LOW_MIN = 20.0     # 下層雲(%) これ以上
WETLAND_RIME_LONG_RUN_HOURS = 3       # 明け方を含む連続成立がこの時間以上で判定A
WETLAND_RIME_DRYING_SPREAD = 1.0      # 0時→6時で T-Td がこれ以上広がったら格下げ
WETLAND_RIME_DRYING_DOWNGRADE_STEPS = 1  # 乾いていく夜に何段階下げるか
# 「惜しい」: 明け方の時刻に、1条件だけがこの幅以内で足りない
WETLAND_NEAR_MISS_MARGIN = {"氷に対する湿度": 5.0, "下層雲": 10.0, "気温": 1.0}

WETLAND_RIME_GRADES = {
    "A": ("成立・明け方まで3時間以上継続", "80〜90%"),
    "B": ("明け方に成立・継続は短い", "50〜70%"),
    "C": ("明け方は不成立・惜しい", "20〜40%"),
    "D": ("不成立", "10%以下"),
}
_WETLAND_GRADE_ORDER = ["A", "B", "C", "D"]


def rh_ice(temp: float, dew_point: float) -> float:
    """気温と露点(水に対する値)から、氷に対する相対湿度(%)を求める(Magnusの式)。"""
    e = 6.112 * math.exp(17.62 * dew_point / (243.12 + dew_point))
    e_ice = 6.112 * math.exp(22.46 * temp / (272.62 + temp))
    return 100.0 * e / e_ice


def _wetland_gaps(t: float, td: float, cl: float) -> dict:
    rhi = rh_ice(t, td)
    return {
        "気温": round(max(0.0, t - WETLAND_RIME_TEMP_MAX), 1),
        "氷に対する湿度": round(max(0.0, WETLAND_RIME_RHICE_MIN - rhi), 1),
        "下層雲": round(max(0.0, WETLAND_RIME_CLOUD_LOW_MIN - cl), 1),
    }


def check_wetland_rime(hourly: list[dict]) -> dict:
    """
    湿原・盆地型の霧氷ルールを時間ごとに判定し、A〜D の区分を返す。

    A: 明け方(4〜6時)を含んで3時間以上連続で成立
    B: 明け方に成立しているが、その連続が3時間未満
    C: 明け方は不成立だが、明け方の時刻に1条件だけが小さい幅で足りない(惜しい)
    D: 不成立
    0時→6時で T-Td が1℃以上広がった夜は、1段階格下げ(Dはそのまま)。
    """
    met_times = []
    near_miss = None
    near_miss_score = None
    has_data = False
    spread_at = {}
    for h in hourly:
        t, td, cl = h.get("temp"), h.get("dew_point"), h.get("cloud_low")
        if t is None or td is None or cl is None:
            continue
        has_data = True
        hhmm = h["time"][11:16]
        if hhmm in ("00:00", "06:00"):
            spread_at[hhmm] = t - td
        gaps = _wetland_gaps(t, td, cl)
        short = {k: v for k, v in gaps.items() if v > 0}
        if not short:
            met_times.append(h["time"])
            continue
        if hhmm in KAMIKOCHI_RIME_DAWN_HOURS and len(short) == 1:
            (name, gap), = short.items()
            margin = WETLAND_NEAR_MISS_MARGIN[name]
            if gap <= margin:
                score = gap / margin
                if near_miss_score is None or score < near_miss_score:
                    near_miss_score = score
                    near_miss = {"time": hhmm, "condition": name, "shortfall": gap,
                                 "temp": t, "dew_point": td, "cloud_low": cl,
                                 "rh_ice": round(rh_ice(t, td))}

    rule = (f"気温≦{WETLAND_RIME_TEMP_MAX:g}℃・氷に対する湿度≧{WETLAND_RIME_RHICE_MIN:g}%・"
            f"下層雲≧{WETLAND_RIME_CLOUD_LOW_MIN:g}%")
    if not has_data:
        return {"rime_type": RIME_TYPE_WETLAND, "grade": None, "grade_label": "判定不能",
                "probability": None, "met_hours": [], "dawn_run": [], "near_miss": None,
                "spread_change": None, "downgraded": False,
                "summary_line": f"【湿原霧氷ルール({rule})】判定不能(データ欠損)"}

    # 連続成立区間のうち、明け方を含むものの最長を求める
    runs, cur = [], []
    for t in met_times:
        if cur and _is_next_hour(cur[-1], t):
            cur.append(t)
        else:
            if cur:
                runs.append(cur)
            cur = [t]
    if cur:
        runs.append(cur)
    dawn_runs = [r for r in runs if any(x[11:16] in KAMIKOCHI_RIME_DAWN_HOURS for x in r)]
    dawn_run = [x[11:16] for x in max(dawn_runs, key=len)] if dawn_runs else []

    if len(dawn_run) >= WETLAND_RIME_LONG_RUN_HOURS:
        base_grade = "A"
    elif dawn_run:
        base_grade = "B"
    elif near_miss:
        base_grade = "C"
    else:
        base_grade = "D"

    spread_change = None
    if "00:00" in spread_at and "06:00" in spread_at:
        spread_change = round(spread_at["06:00"] - spread_at["00:00"], 1)
    downgraded = (spread_change is not None and spread_change >= WETLAND_RIME_DRYING_SPREAD
                  and base_grade != "D")
    if downgraded:
        idx = min(_WETLAND_GRADE_ORDER.index(base_grade) + WETLAND_RIME_DRYING_DOWNGRADE_STEPS,
                  len(_WETLAND_GRADE_ORDER) - 1)
        grade = _WETLAND_GRADE_ORDER[idx]
    else:
        grade = base_grade
    label, prob = WETLAND_RIME_GRADES[grade]

    parts = [f"【湿原霧氷ルール({rule})】判定{grade}: {label}(霧氷の確率目安 {prob})"]
    if dawn_run:
        parts.append(f"明け方を含む連続成立 {len(dawn_run)}時間({dawn_run[0]}〜{dawn_run[-1]})")
    elif met_times:
        parts.append(f"成立は明け方以外のみ({', '.join(x[11:16] for x in met_times)})")
    if near_miss and not dawn_run:
        unit = {"氷に対する湿度": "%", "下層雲": "%", "気温": "℃"}[near_miss["condition"]]
        parts.append(f"明け方で最も惜しいのは{near_miss['time']}: {near_miss['condition']}があと"
                     f"{near_miss['shortfall']}{unit}不足 (T:{near_miss['temp']}℃, "
                     f"氷に対する湿度:{near_miss['rh_ice']}%, 下層雲:{near_miss['cloud_low']}%)")
    if spread_change is not None:
        txt = f"0時→6時のT-Td変化 {spread_change:+.1f}℃"
        if downgraded:
            txt += (f"(乾いていく夜のため{WETLAND_RIME_DRYING_DOWNGRADE_STEPS}段階格下げ。"
                    f"格下げ前は判定{base_grade})")
        parts.append(txt)

    return {"rime_type": RIME_TYPE_WETLAND, "grade": grade, "grade_label": label,
            "probability": prob, "met_hours": [x[11:16] for x in met_times],
            "dawn_run": dawn_run, "near_miss": near_miss, "spread_change": spread_change,
            "downgraded": downgraded, "summary_line": " / ".join(parts)}


def _build_summary(spot_name: str, factor: float, hourly: list,
                   prev_day_precip: float = 0.0) -> str:
    lines = [f"■{spot_name}"]
    # 前日降水はパターンC判定の材料になるため明示
    lines.append(f"[前日降水量(当日0-20時)合計: {prev_day_precip}mm]")
    for h in hourly:
        t_td = (round(h["temp"] - h["dew_point"], 1)
                if h["temp"] is not None and h["dew_point"] is not None else None)
        lines.append(
            f"{h['time']}: "
            f"T:{h['temp']}℃, Td:{h['dew_point']}℃, T-Td:{t_td}℃, "
            f"H:{h['humidity']}%, "
            f"W:{h['wind_adjusted']}m/s, "
            f"C_Low:{h['cloud_low']}%, C_Mid:{h['cloud_mid']}%, C_High:{h['cloud_high']}%"
        )
    return "\n".join(lines)


async def _build_prediction_record(forecast_result: dict) -> dict | None:
    """
    fetch_forecast の結果を predictions.json 用レコードに変換。error付きは None。

    spot_group(湿原系/高原系等)・tags・pattern・phenomena_priority は
    classify_spot_group から取得して保存する。これにより分析エージェントが
    predictions.json を読むだけで「予測当時の地形判定」を取得できる。
    未知地点で初回(まだ自動分類キャッシュに無い)の場合は空のままになるが、
    save_auto_classification 後の2回目以降は埋まる。
    """
    if forecast_result.get("error"):
        return None
    now = _jst_now()
    spot_name = forecast_result.get("spot_name", "")

    # タグ・パターン情報を取得(分析エージェント用に予測当時のスナップショットとして残す)。
    # 取得失敗時(spot_groups.json未配置等)も予測本体は止めない。
    spot_group = ""
    tags: list[str] = []
    pattern = ""
    phenomena_priority: dict = {}
    try:
        from tools.location import classify_spot_group  # 遅延import(循環回避)
        info = await classify_spot_group(spot_name)
        if info.get("found"):
            spot_group = info.get("main_group") or ""
            tags = info.get("tags", []) or []
            pattern = info.get("pattern") or ""
            phenomena_priority = info.get("phenomena_priority", {}) or {}
    except Exception:
        logger.warning(f"classify_spot_group failed for {spot_name}", exc_info=True)

    return {
        "id": f"pred_{now.strftime('%Y%m%d%H%M%S%f')}_{spot_name}",
        "spot_name": spot_name,
        "spot_group": spot_group,                       # 例: 湿原系 / 高原系
        "tags": tags,                                   # 例: ["盆地地形","水域近接"]
        "pattern": pattern,                             # 例: "A" / "B" / ""
        "phenomena_priority": phenomena_priority,       # 例: {"霧氷":"高",...}
        "elev": forecast_result.get("elev"),            # 標高(m)。登録地点 or Elevation API由来。
        "target_date": forecast_result.get("target_date", ""),
        "predicted_at": now.isoformat(),
        "target_phenomena": [],                          # スナップショット時点では未確定
        "weather_data": {
            "time_range": forecast_result.get("time_range", TIME_RANGE_LABEL),
            "prev_day_precip_mm": forecast_result.get("prev_day_precip_mm", 0.0),
            "hourly": forecast_result.get("hourly", []),
        },
        "rime_type": forecast_result.get("rime_type", ""),       # 霧氷のタイプ
        "rime_judgement": forecast_result.get("rime_judgement"),  # 機械判定の結果(該当地点のみ)
        "prediction_text": "",  # 確率本文は後紐づけ
    }


async def _save_prediction_snapshot(forecast_result: dict) -> None:
    """
    単一地点の予測スナップショットを predictions.json に追記する。

    設計書v1.4 predictions.json スキーマ準拠。確率テキスト(prediction_text)は
    気象取得時点では未確定のため空文字で保存し、weather_data に実値を残す。
    後日 actuals と突き合わせる際は spot_name + target_date で紐づけられる。

    エラーが起きても予測本体(ユーザーへの応答)を止めないよう、例外は握りつぶす。
    """
    if not PREDICTIONS_FILE_ID:
        return
    record = await _build_prediction_record(forecast_result)
    if record is None:
        return
    from tools.storage import append_to_json_list  # 遅延import(循環回避)
    try:
        await append_to_json_list(PREDICTIONS_FILE_ID, record)
    except Exception:
        logger.warning(f"save_prediction_snapshot failed for {record['spot_name']}", exc_info=True)


async def _save_prediction_snapshots_bulk(forecast_results: list[dict]) -> None:
    """
    複数地点の予測スナップショットを「1回の read-modify-write」でまとめて保存する。

    「おすすめ」一括予測で各地点が個別に保存すると、並列の読み書きが衝突して
    最後の1件しか残らない競合が起きる。そこで全地点ぶんのレコードを作ってから
    predictions.json を一度だけ読み、まとめて追記し、一度だけ書き戻す。

    エラーが起きても予測本体の応答は止めない(例外は握りつぶす)。
    """
    if not PREDICTIONS_FILE_ID:
        return
    records = []
    for r in forecast_results:
        rec = await _build_prediction_record(r)
        if rec is not None:
            records.append(rec)
    if not records:
        return

    from tools.storage import read_json, write_json  # 遅延import(循環回避)
    try:
        try:
            data = await read_json(PREDICTIONS_FILE_ID)
        except Exception:
            data = {"predictions": []}

        # ラッパー形式 {"predictions":[...]} と素の list の両方に対応
        if isinstance(data, dict):
            data.setdefault("predictions", [])
            if not isinstance(data["predictions"], list):
                data["predictions"] = []
            data["predictions"].extend(records)
        elif isinstance(data, list):
            data.extend(records)
        else:
            data = {"predictions": records}

        await write_json(PREDICTIONS_FILE_ID, data)
    except Exception:
        logger.warning(f"save_prediction_snapshots_bulk failed ({len(records)}件)", exc_info=True)