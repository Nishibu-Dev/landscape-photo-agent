# tools/explain.py
# 「○○のロジックを教えて」に答えるための説明ツール。
#
# 霧氷の判定は、実際の判定に使っている定数(tools/weather.py)から説明文を組み立てるため、
# 閾値を変えれば説明も自動で追従する。
# 霧のパターンの基準は ForecastAgent の指示文(agents/forecast.py)に書かれているため、
# ここでは要約を FOG_PATTERN_SUMMARY に持つ。指示文の霧ロジックを変えたら、ここも合わせること。
# ※ユーザー向けの説明なので、地点別の風速補正係数(WIND_ADJUSTMENTS)には触れない。

from config.spots import (
    DEFAULT_SPOTS,
    EXTRA_SPOTS,
    RIME_TYPE_DEFAULT,
    RIME_TYPE_SPRING,
    RIME_TYPE_WETLAND,
    SPOT_ALIASES,
    get_rime_type,
    get_spot_attributes,
    get_spot_by_name,
)
from tools import weather as w

FOG_COMMON = "T-Td ≦ 1.5℃・湿度 ≧ 90%・風速 ≦ 1.5m/s"
FOG_PATTERN_SUMMARY = {
    "A": f"鉄板組(湿原系×盆地地形×水域近接)。共通3条件({FOG_COMMON})のうち T-Td を ≦ 2℃ に緩和。全条件そろえば 80〜90%",
    "B": f"隠れ強豪(高原系×水域近接×湿原内包)。パターンAと同等の緩めの基準。全条件そろえば 75〜85%",
    "C": "気まぐれ(湿原系×盆地地形×乾燥湿原)。T-Td ≦ 1.0℃ かつ 湿度 ≧ 95% に厳格化。全条件そろえば 70〜75%",
    "D": f"湖畔派(湖畔×水域近接)。緩和なしで共通3条件({FOG_COMMON})。全条件そろえば 70%",
    "E": f"谷筋派(谷筋×水域近接)。緩和なしで共通3条件({FOG_COMMON})。全条件そろえば 70%",
    None: f"パターンなし。地形による緩和なしで共通3条件({FOG_COMMON})。全条件そろえば 70%",
}
RIDGE_NOTE = "尾根沿い(稜線上・峠)のため霧は出にくく(原則20%以下)、代わりに雲海とガス(雲中)を予測する"

RIME_TYPE_DESCRIPTION = {
    RIME_TYPE_SPRING: "凍らない湧水・梓川の水蒸気が、乾いた晴れの夜に冷えた枝に凍りつくタイプ。"
                      "空気が乾いている夜ほど出やすく、気温は判定に使わない。",
    RIME_TYPE_WETLAND: "盆地にたまった湿った空気(雲・霧を含む)から霧氷が育つタイプ。"
                       "明け方まで湿り気が保たれるかが鍵で、晴れて乾いていく夜は出ない。風が強くても出る。",
    RIME_TYPE_DEFAULT: "地点が雲や霧の中に入り、過冷却の粒が枝について凍るタイプとして扱う"
                       "(タイプが確定していない地点もここ)。",
}


def _grades_text(grades: dict) -> list[str]:
    return [f"  判定{k}: {label} → 霧氷 {prob}" for k, (label, prob) in grades.items()]


def _rime_logic_lines(rime_type: str) -> list[str]:
    lines = [f"■霧氷のタイプ: {rime_type}", f"  {RIME_TYPE_DESCRIPTION[rime_type]}"]
    if rime_type == RIME_TYPE_SPRING:
        m = w.KAMIKOCHI_NEAR_MISS_MARGIN
        lines += [
            "■霧氷の判定(Pythonで機械判定。1時間ごと)",
            f"  前提条件: 気温 ≦ {w.KAMIKOCHI_RIME_TEMP_MAX:g}℃(氷点下でなければ枝で凍らない)",
            f"  成立条件: 露点 ≦ {w.KAMIKOCHI_RIME_DEWPOINT_MAX:g}℃ かつ 下層雲 < {w.KAMIKOCHI_RIME_CLOUD_LOW_MAX:g}% "
            f"かつ 風速 ≦ {w.KAMIKOCHI_RIME_WIND_MAX:g}m/s(強いと霧氷が飛ばされる)",
            f"  夜全体: {w.KAMIKOCHI_RIME_LONG_RUN_HOURS}時間以上の連続成立と、明け方"
            f"({'・'.join(h[:2] + '時' for h in w.KAMIKOCHI_RIME_DAWN_HOURS)})の成立を重視",
            f"  「惜しい」: 1条件だけが、露点{m['露点']:g}℃・下層雲{m['下層雲']:g}ポイント・風速{m['風速']:g}m/s 以内で足りない",
            *_grades_text(w.KAMIKOCHI_RIME_GRADES),
            "  根拠: 上高地の過去7夜の実績(霧氷あり・少しあり・なし)。氷点下の範囲では、気温の高低で霧氷のあり・なしが分かれなかった",
        ]
    elif rime_type == RIME_TYPE_WETLAND:
        m = w.WETLAND_NEAR_MISS_MARGIN
        lines += [
            "■霧氷の判定(Pythonで機械判定。1時間ごと)",
            f"  成立条件: 気温 ≦ {w.WETLAND_RIME_TEMP_MAX:g}℃ かつ 氷に対する湿度 ≧ {w.WETLAND_RIME_RHICE_MIN:g}% "
            f"かつ 下層雲 ≧ {w.WETLAND_RIME_CLOUD_LOW_MIN:g}%(風速の上限なし)",
            "  ※氷に対する湿度は気温と露点から計算。氷点下では、水に対して飽和に近い空気は氷に対して過飽和になる",
            f"  夜全体: 明け方を含む連続成立が{w.WETLAND_RIME_LONG_RUN_HOURS}時間以上かどうかで判定",
            f"  「惜しい」: 明け方の時刻に、1条件だけが湿度{m['氷に対する湿度']:g}ポイント・下層雲{m['下層雲']:g}ポイント・"
            f"気温{m['気温']:g}℃ 以内で足りない",
            f"  乾いていく夜: 0時→6時で T-Td が {w.WETLAND_RIME_DRYING_SPREAD:g}℃ 以上広がったら"
            f" {w.WETLAND_RIME_DRYING_DOWNGRADE_STEPS}段階格下げ",
            *_grades_text(w.WETLAND_RIME_GRADES),
            "  根拠: 田ノ原湿原5夜・踊場湿原3夜の実績(尾瀬ヶ原も同じ仕組みの盆地の湿原として適用)。"
            "氷点下の範囲では、気温の高低で霧氷のあり・なしが分かれなかった",
        ]
    else:
        m = w.CLOUD_NEAR_MISS_MARGIN
        lines += [
            "■霧氷の判定(Pythonで機械判定。1時間ごと)",
            f"  成立条件: 気温 ≦ {w.CLOUD_RIME_TEMP_MAX:g}℃ かつ T-Td ≦ {w.CLOUD_RIME_SPREAD_MAX:g}℃(ほぼ飽和)",
            f"  夜全体: 明け方を含む連続成立が{w.CLOUD_RIME_LONG_RUN_HOURS}時間以上かどうかで判定",
            f"  「惜しい」: 明け方の時刻に、1条件だけが気温{m['気温']:g}℃・T-Td {m['T-Td']:g}℃ 以内で足りない",
            *_grades_text(w.CLOUD_RIME_GRADES),
            "  尾根沿いの地点では、霧氷はガス(雲中)と同じ向きに見積もる(雲の上に出ている雲海の朝は出にくい)",
        ]
    lines.append("  ※閾値は暫定値。今期の実績で見直す予定")
    return lines


def _overview() -> dict:
    lines = ["【霧氷タイプ別の登録地点】"]
    for t in (RIME_TYPE_SPRING, RIME_TYPE_WETLAND, RIME_TYPE_DEFAULT):
        names = [s["name"] for s in DEFAULT_SPOTS + EXTRA_SPOTS if get_rime_type(s["name"]) == t]
        lines.append(f"■{t}: {'、'.join(names)}")
        lines.append(f"  {RIME_TYPE_DESCRIPTION[t]}")
    lines += [
        "■未登録の地点: Google Mapsで座標と標高を調べ、地形タグと霧のパターンをLLMが推定する。"
        "霧氷は雲中型・未確定として判定する",
        "※霧のパターン(A〜E)は霧の判定専用で、霧氷の判定には使わない",
    ]
    return {"found": True, "text": "\n".join(lines)}


def explain_spot_logic(spot_name: str) -> dict:
    """
    このBot(FORESTA)の予測ロジック(霧・霧氷・凝華の判定基準)を説明するための情報を返す。

    Args:
        spot_name: 地点名(例: 上高地, 田ノ原, 河童橋)。全体の概要が知りたいときは "全体"。

    Returns:
        found: 登録地点かどうか(全体の概要のときは True)
        text: 説明の材料となるテキスト(実際の判定に使っている閾値から生成)
    """
    name = (spot_name or "").strip()
    if name in ("", "全体", "全地点", "すべて", "全部"):
        return _overview()

    spot = get_spot_by_name(name)
    if spot is None:
        for s in DEFAULT_SPOTS + EXTRA_SPOTS:
            if name in s["name"] or s["name"] in name:
                spot = s
                break

    if spot is None:
        lines = [
            f"「{name}」は登録地点ではない。予測を頼まれたときは次のように扱う。",
            "・Google Mapsで座標と標高を調べる",
            "・地名と標高から、LLMが地形タグと霧のパターン(A〜E)を推定し、次回以降のために保存する",
            "・霧: 推定した霧のパターンの基準で判定する",
            *_rime_logic_lines(RIME_TYPE_DEFAULT),
        ]
        return {"found": False, "text": "\n".join(lines)}

    sname = spot["name"]
    attrs = get_spot_attributes(sname) or {}
    pattern = attrs.get("pattern")
    tags = attrs.get("tags", [])
    lines = [
        f"■地点: {sname}(標高{spot['elev']}m)",
        f"■霧のパターン: {pattern or 'なし'}(地形タグ: {'・'.join(tags) or 'なし'})",
        f"  {FOG_PATTERN_SUMMARY.get(pattern, FOG_PATTERN_SUMMARY[None])}",
    ]
    if "稜線上" in tags or "峠" in tags:
        lines.append(f"  {RIDGE_NOTE}")
    lines += _rime_logic_lines(get_rime_type(sname))
    lines.append("■凝華(ダイヤモンドダスト): 気温 ≦ -10℃ かつ 風速 ≦ 2m/s かつ 快晴")
    if sname == "田ノ原湿原":
        lines.append("  田ノ原は冷気溜まりになりやすく、快晴・無風・-10℃以下なら凝華の確率を特に高く見積もる")
    prio = attrs.get("phenomena_priority")
    if prio:
        lines.append("■現象の優先度: " + "、".join(f"{k}={v}" for k, v in prio.items()))
    aliases = [a for a, v in SPOT_ALIASES.items() if v == sname and a != sname]
    if aliases:
        lines.append(f"■別名: {'、'.join(aliases)}")
    return {"found": True, "text": "\n".join(lines)}
