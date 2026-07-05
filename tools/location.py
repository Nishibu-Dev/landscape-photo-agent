# 地点解決ツール群
#
# 役割:
#   - resolve_location: 登録地点（DEFAULT_SPOTS / エイリアス）の解決。「おすすめ」一括対応。
#   - geocode_unknown_spot: 未登録地点を Google Maps API(Places + Elevation)で
#       緯度経度・標高・正式名称まで解決する。タグ推定は行わない（LLMに委ねる）。
#   - classify_spot_group / get_phenomena_priority: spot_groups.json の
#       手動シード(spot_attributes) と 自動分類キャッシュ(auto_classified) を参照。
#   - save_auto_classification: LLMが推定したタグ・パターンを auto_classified に保存。
#
# 設計方針(設計書 4.2.1 / 4.2.2 / PDF p.4 準拠):
#   手動シード優先 → 自動分類キャッシュ → 未知なら Maps で座標取得 + LLM がタグ推定。
#   タグ推定・パターン判定そのものは ForecastAgent の instruction 内で LLM が行う。
#   ここ(ツール側)は「地形を読むための材料(座標・標高)の取得」と「結果のキャッシュ」に徹する。

import os
import json
import httpx

from config.spots import get_spot_by_name, DEFAULT_SPOTS
from tools.logger import get_logger

logger = get_logger(__name__)

# spot_groups.json の Drive ファイルID。未設定ならキャッシュ機能は無効化(座標解決は動く)。
SPOT_GROUPS_FILE_ID = os.environ.get("SPOT_GROUPS_FILE_ID", "")
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")

PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
ELEVATION_URL = "https://maps.googleapis.com/maps/api/elevation/json"


async def resolve_location(user_input: str) -> list[dict]:
    """
    ユーザー入力から対象地点リストを返す。

    - 「おすすめ」を含む → DEFAULT_SPOTS 全10地点
    - 登録済み地点名（エイリアス含む）→ 該当の1地点
    - 未知の地点名 → Maps API で座標・標高を解決して1地点返す(is_unknown=True)。
      Maps が使えない/見つからない/地点名と判断できない場合は空リスト。

    返却 dict のキー: name, lat, lng, elev, (is_unknown)

    ※ ForecastAgent には instruction で「地点名だけを渡す」よう指示しているが、
      文章が渡ってきた場合の保険として、依頼語の除去と最低限の妥当性チェックを行う。
    """
    text = user_input.strip()

    # 「おすすめ」系は部分一致で判定(「おすすめスポットの予測をお願いします」等にも対応)
    if any(kw in text for kw in ("おすすめ", "オススメ", "お勧め", "オススメスポット")):
        return DEFAULT_SPOTS

    # 1) 手動シード(登録地点・エイリアス)を最優先(原文のまま)
    spot = get_spot_by_name(text)
    if spot:
        return [spot]

    # 2) 部分一致(登録地点名に含まれる/含む)
    for s in DEFAULT_SPOTS:
        if text in s["name"] or s["name"] in text:
            return [s]

    # 3) 未知地点解決の前に、依頼語・動詞を除去して地点名らしさを抽出
    cleaned = _sanitize_place_query(text)
    # 登録地点名がサニタイズ後に一致することもあるので再チェック
    spot = get_spot_by_name(cleaned)
    if spot:
        return [spot]
    for s in DEFAULT_SPOTS:
        if cleaned and (cleaned in s["name"] or s["name"] in cleaned):
            return [s]

    # サニタイズ後が空、または地点名として短すぎる場合は Maps に投げない(誤ヒット防止)
    if not _looks_like_place_name(cleaned):
        return []

    # 4) 未知地点 → Maps API で座標・標高を解決
    geocoded = await geocode_unknown_spot(cleaned)
    if geocoded:
        return [geocoded]

    # 5) 解決不能
    return []


# 地点名抽出の保険。ForecastAgent が文章を渡してきた場合に依頼語を除去する。
# 例: 「千畳敷カールを予測して」→「千畳敷カール」
_REQUEST_PHRASES = [
    "の天気予報を教えて", "の天気を教えて", "の予測をお願いします", "の予測をお願い",
    "を予測してください", "を予測して", "の予報を教えて", "の天気は", "の予報は",
    "を教えてください", "を教えて", "について教えて", "の撮影条件",
    "の明日の天気", "明日の天気", "今夜の", "明日の", "予測をお願いします",
    "予測して", "教えて", "お願いします", "お願い", "の天気", "の予報",
    "はどう", "どうですか", "を知りたい", "について",
]


def _sanitize_place_query(text: str) -> str:
    """依頼語・動詞句を除去して地点名らしき部分を残す。"""
    result = text.strip()
    for phrase in _REQUEST_PHRASES:
        result = result.replace(phrase, "")
    # 前後の助詞・記号を軽く除去
    result = result.strip(" 　、。!?！？\n\t")
    for particle in ("の", "は", "を", "が", "で", "に"):
        if result.endswith(particle):
            result = result[:-1]
    return result.strip()


def _looks_like_place_name(text: str) -> bool:
    """
    Maps に投げてよい「地点名らしさ」の最低限チェック。
    - 空や1文字は弾く(誤ヒットの温床)
    - 明らかな依頼文の残骸(助詞だらけ等)を弾く
    """
    if not text or len(text) < 2:
        return False
    # 依頼語が残っているならまだ文章。地点名として扱わない。
    leftover = ["予測", "天気", "予報", "教え", "お願", "撮影", "条件", "どう"]
    if any(w in text for w in leftover):
        return False
    return True


async def geocode_unknown_spot(place_name: str) -> dict | None:
    """
    未登録地点を Google Maps API で解決する。

    Places API(New) の Text Search で緯度経度・正式名称を取得し、
    Elevation API で標高を取得して返す。

    Args:
        place_name: ユーザーが入力した地点名(例: 「白駒池」「八千穂高原」)

    Returns:
        {name, lat, lng, elev, is_unknown: True} or None(解決不能/APIキー無し)
    """
    if not GOOGLE_MAPS_API_KEY:
        # キー未設定では未知地点を解決できない。登録地点のみで運用される。
        return None

    # --- Places API(New): テキスト検索で座標と正式名称を取得 ---
    # FieldMask 必須。location(緯度経度) と displayName(正式名称) のみ要求。
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                PLACES_SEARCH_URL,
                headers={
                    "Content-Type": "application/json",
                    "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
                    "X-Goog-FieldMask": "places.displayName,places.location",
                },
                json={
                    "textQuery": place_name,
                    "languageCode": "ja",
                    # 日本国内にバイアス(誤って同名の海外地点を拾わないように)
                    "regionCode": "JP",
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        logger.exception(f"Places API search failed place_name={place_name}")
        return None

    places = data.get("places", [])
    if not places:
        return None

    top = places[0]
    loc = top.get("location", {})
    lat = loc.get("latitude")
    lng = loc.get("longitude")
    if lat is None or lng is None:
        return None
    name = top.get("displayName", {}).get("text", place_name)

    # --- Elevation API: 標高を取得 ---
    elev = await _fetch_elevation(lat, lng)

    return {
        "name": name,
        "lat": lat,
        "lng": lng,
        "elev": elev,        # 取得失敗時は None
        "is_unknown": True,  # 未登録地点である印。ForecastAgent がタグ推定の起点に使う。
    }


async def _fetch_elevation(lat: float, lng: float) -> int | None:
    """Elevation API で標高(m)を返す。失敗時 None。"""
    if not GOOGLE_MAPS_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                ELEVATION_URL,
                params={"locations": f"{lat},{lng}", "key": GOOGLE_MAPS_API_KEY},
            )
            resp.raise_for_status()
            data = resp.json()
        results = data.get("results", [])
        if results and "elevation" in results[0]:
            return round(results[0]["elevation"])
    except Exception:
        logger.warning(f"Elevation API failed lat={lat} lng={lng}", exc_info=True)
    return None


# ---------------------------------------------------------------------------
# spot_groups.json 参照系(手動シード + 自動分類キャッシュ)
# ---------------------------------------------------------------------------

async def _read_spot_groups() -> dict:
    """spot_groups.json を読む。未設定/失敗時は最小構造を返す。"""
    if not SPOT_GROUPS_FILE_ID:
        return {"spot_attributes": {}, "auto_classified": {}}
    # 循環 import を避けるため遅延 import
    from tools.storage import read_json
    try:
        data = await read_json(SPOT_GROUPS_FILE_ID)
        if isinstance(data, dict):
            data.setdefault("spot_attributes", {})
            # auto_classified は設計書では list だが、地点名キーの dict として扱うと
            # 参照が O(1) になり実装が素直。両形式を吸収する。
            if "auto_classified" not in data:
                data["auto_classified"] = {}
            return data
    except Exception:
        logger.exception(f"spot_groups.json 読み込み失敗 file_id={SPOT_GROUPS_FILE_ID}")
    return {"spot_attributes": {}, "auto_classified": {}}


def _auto_map(data: dict) -> dict:
    """auto_classified を {地点名: 属性dict} の形に正規化して返す。"""
    auto = data.get("auto_classified", {})
    if isinstance(auto, dict):
        return auto
    if isinstance(auto, list):
        # list[{"name":..., ...}] 形式を dict 化
        return {e.get("name"): e for e in auto if isinstance(e, dict) and e.get("name")}
    return {}


async def classify_spot_group(spot_name: str) -> dict:
    """
    地点のグループ・タグ・パターンを返す。
    参照順: コード手動シード(spots.py) → Drive手動シード(spot_attributes)
            → 自動分類キャッシュ(auto_classified) → 未分類。

    Returns:
        {found: bool, source: "manual"|"auto"|"none",
         main_group, tags, pattern, phenomena_priority}
        見つからなければ found=False(ForecastAgent が LLM 推定を行う合図)。
    """
    # 0) コード側の手動シード(登録地点)を最優先。Drive未配置でも確実に効く。
    from config.spots import get_spot_attributes
    coded = get_spot_attributes(spot_name)
    if coded:
        return {
            "found": True,
            "source": "manual",
            "main_group": coded.get("main_group"),
            "tags": coded.get("tags", []),
            "pattern": coded.get("pattern"),
            "phenomena_priority": coded.get("phenomena_priority", {}),
        }

    data = await _read_spot_groups()

    # 1) Drive側の手動シード(spot_attributes)
    attrs = data.get("spot_attributes", {})
    if spot_name in attrs:
        a = attrs[spot_name]
        return {
            "found": True,
            "source": "manual",
            "main_group": a.get("main_group"),
            "tags": a.get("tags", []),
            "pattern": a.get("pattern"),
            "phenomena_priority": a.get("phenomena_priority", {}),
        }

    # 2) 自動分類キャッシュ
    auto = _auto_map(data)
    if spot_name in auto:
        a = auto[spot_name]
        return {
            "found": True,
            "source": "auto",
            "main_group": a.get("main_group"),
            "tags": a.get("tags", []),
            "pattern": a.get("pattern"),
            "phenomena_priority": a.get("phenomena_priority", {}),
        }

    # 3) 未分類 → LLM 推定が必要
    return {
        "found": False,
        "source": "none",
        "main_group": None,
        "tags": [],
        "pattern": None,
        "phenomena_priority": {},
    }


async def get_phenomena_priority(spot_name: str) -> dict:
    """
    地点の phenomena_priority を返す(手動シード → 自動分類キャッシュ)。
    未分類なら空 dict(ForecastAgent が推定する)。
    """
    info = await classify_spot_group(spot_name)
    return info.get("phenomena_priority", {})


async def save_auto_classification(
    spot_name: str,
    main_group: str,
    tags: list[str],
    pattern: str,
    phenomena_priority: dict,
) -> dict:
    """
    LLM が推定した未知地点の分類結果を spot_groups.json の auto_classified に保存する。
    次回以降は再推定せずキャッシュを参照できる(設計書 4.2.1)。

    Args:
        spot_name: 地点の正式名称(geocode_unknown_spot の name)
        main_group: "湿原系" / "高原系" など。判断不能なら ""。
        tags: 推定した補助タグのリスト(例: ["盆地地形","水域近接"])
        pattern: "A"〜"E" または ""(該当パターンなし=標準ロジック)
        phenomena_priority: {"霧氷":"中","凝華":"低","放射霧":"高","雲海":"中"} など
    """
    if not SPOT_GROUPS_FILE_ID:
        # キャッシュ先が無い場合でも予測自体は続行できるよう、保存はスキップ。
        return {"status": "skipped", "reason": "SPOT_GROUPS_FILE_ID未設定"}

    from tools.storage import read_json, write_json
    try:
        data = await read_json(SPOT_GROUPS_FILE_ID)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        logger.exception(f"save_auto_classification: spot_groups.json 読み込み失敗 spot={spot_name}")
        data = {}

    data.setdefault("spot_attributes", {})
    auto = data.get("auto_classified")
    # dict 形式に統一して保存(参照が速く、重複も防げる)
    if not isinstance(auto, dict):
        auto = _auto_map({"auto_classified": auto}) if auto else {}

    auto[spot_name] = {
        "name": spot_name,
        "main_group": main_group,
        "tags": tags,
        "pattern": pattern,
        "phenomena_priority": phenomena_priority,
    }
    data["auto_classified"] = auto

    try:
        await write_json(SPOT_GROUPS_FILE_ID, data)
    except Exception as e:
        logger.exception(f"save_auto_classification: spot_groups.json 書き込み失敗 spot={spot_name}")
        return {"status": "error", "reason": str(e)}

    return {"status": "ok", "cached": spot_name}