# 地点別の風速補正係数
# 湿原・盆地は風が遮られるため、広域予報値より実際は低い
# ※ Open-Meteo は全体的に風速が大きく出る傾向あり（運用実績より）

# 地点別の風速補正係数

WIND_ADJUSTMENTS = {
    "田ノ原湿原":       0.1,
    "踊場湿原":         0.3,
    "大阿原湿原":       0.5,
    "田代湿原(上高地)": 0.2,
    "菅平(菅平牧場)":   0.3,
    "美ヶ原高原":       0.6,
    "乗鞍高原":         0.4,
    "カヤの平":         0.3,
    "戦場ヶ原":         0.3,
    "尾瀬ヶ原":         0.25,
    "千畳敷カール":     0.6,
    "default":          0.6,
}

def get_wind_factor(spot_name: str) -> float:
    return WIND_ADJUSTMENTS.get(spot_name, WIND_ADJUSTMENTS["default"])