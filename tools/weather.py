import asyncio
import os
import httpx
from datetime import datetime, timezone, timedelta
from config.adjustments import get_wind_factor

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

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


async def fetch_forecast(lat: float, lng: float, spot_name: str,
                         save_snapshot: bool = True) -> dict:
    """
    翌朝の撮影コンディション予測のため、前夜21時〜翌日6時の予報を取得する。

    save_snapshot=True のとき、取得結果を predictions.json に即保存する。
    「おすすめ」一括(fetch_all_forecasts)では並列書き込みの競合を避けるため
    False で呼び、呼び出し側が最後にまとめて1回だけ保存する。
    """
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
        "models": "jma_msm",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(OPEN_METEO_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

    hourly = data["hourly"]
    factor = get_wind_factor(spot_name)

    now_jst = _jst_now()
    # 「今夜21時〜明朝6時」を対象に。base_date は今日。
    today_str = now_jst.strftime("%Y-%m-%d")
    target_times = _build_target_times(today_str)

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
                "wind_adjusted": round(raw_wind * factor, 1),
                "cloud_low":     hourly["cloud_cover_low"][i],
                "cloud_mid":     hourly["cloud_cover_mid"][i],
                "cloud_high":    hourly["cloud_cover_high"][i],
            })

    # パターンC「前日雨リセット」判定用に、対象前日(今日0〜20時)の降水量合計を算出。
    # ForecastAgent はこの値を見て、乾燥湿原の閾値を緩めるか判断する。
    prev_day_precip = _sum_prev_day_precip(hourly, today_str)

    summary = _build_summary(spot_name, factor, result_hourly, prev_day_precip)

    elev = await _resolve_elev(spot_name, lat, lng)

    result = {
        "spot_name":  spot_name,
        "elev":       elev,
        "target_date": (now_jst + timedelta(days=1)).strftime("%Y-%m-%d"),
        "time_range": TIME_RANGE_LABEL,
        "prev_day_precip_mm": prev_day_precip,  # 前日降水量合計(mm)。パターンC判定用。
        "hourly":     result_hourly,
        "summary":    summary,
    }

    # 予測スナップショットを predictions.json へ即保存(LLMのsave_prediction呼び出しに依存しない)。
    # 確率テキストはこの時点で未確定のため空で保存し、weather_data(実値)を確実に残す。
    # 後で実績と突き合わせる際は spot_name + target_date で紐づけられる。
    # ※「おすすめ」一括時は save_snapshot=False で呼ばれ、呼び出し側がまとめ保存する
    #   (並列 read-modify-write による上書き競合を避けるため)。
    if save_snapshot:
        await _save_prediction_snapshot(result)

    return result


async def fetch_all_forecasts(user_input: str) -> list[dict]:
    from tools.location import resolve_location

    spots = await resolve_location(user_input)
    if not spots:
        return []

    async def _fetch_one(spot: dict) -> dict:
        try:
            # 並列取得。個別保存はオフ(save_snapshot=False)にして競合を避ける。
            return await fetch_forecast(
                lat=spot["lat"],
                lng=spot["lng"],
                spot_name=spot["name"],
                save_snapshot=False,
            )
        except Exception as e:
            return {"spot_name": spot["name"], "error": str(e)}

    results = await asyncio.gather(*[_fetch_one(s) for s in spots])
    results = list(results)

    # 全地点ぶんを1回の read-modify-write でまとめて保存する。
    # これにより並列書き込みの上書き競合(最後の1件だけ残る問題)を回避する。
    await _save_prediction_snapshots_bulk(results)

    return results


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
            hourly = None
    # 取得できない/古い日付は archive にフォールバック
    if hourly is None:
        try:
            hourly = await _fetch_hourly_archive(lat, lng, eve_date, obs)
        except Exception:
            hourly = None

    if hourly is None:
        return {"time_range": TIME_RANGE_LABEL, "hourly_actual": []}

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

    return {"time_range": TIME_RANGE_LABEL, "hourly_actual": result}


async def _fetch_hourly_forecast_pastdays(lat: float, lng: float, past_days: int) -> dict:
    params = {
        "latitude": lat,
        "longitude": lng,
        "hourly": (
            "temperature_2m,relative_humidity_2m,dew_point_2m,"
            "cloud_cover_low,cloud_cover_mid,cloud_cover_high,"
            "wind_speed_10m"
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
            "wind_speed_10m"
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


def _build_summary(spot_name: str, factor: float, hourly: list,
                   prev_day_precip: float = 0.0) -> str:
    lines = [f"■{spot_name}（風速補正係数: x{factor}）"]
    # 前日降水はパターンC判定の材料になるため明示
    lines.append(f"[前日降水量(当日0-20時)合計: {prev_day_precip}mm]")
    for h in hourly:
        t_td = round(h["temp"] - h["dew_point"], 1)
        lines.append(
            f"{h['time']}: "
            f"T:{h['temp']}℃, Td:{h['dew_point']}℃, T-Td:{t_td}℃, "
            f"H:{h['humidity']}%, "
            f"W:{h['wind_adjusted']}m/s(補正後/{h['wind_raw']}m/s生値), "
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
    except Exception as e:
        print(f"[WARN] classify_spot_group failed for {spot_name}: {e}")

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
    except Exception as e:
        print(f"[WARN] save_prediction_snapshot failed for {record['spot_name']}: {e}")


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
    except Exception as e:
        print(f"[WARN] save_prediction_snapshots_bulk failed ({len(records)}件): {e}")