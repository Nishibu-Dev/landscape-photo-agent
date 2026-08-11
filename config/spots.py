# DEFAULT_SPOTS（10地点・「おすすめ」一括予測の対象）と地点属性の定義

DEFAULT_SPOTS = [
    {"name": "田ノ原湿原",      "lat": 36.7023992, "lng": 138.4902991, "elev": 1605},
    {"name": "菅平(菅平牧場)",  "lat": 36.5384217, "lng": 138.3723333, "elev": 1592},
    {"name": "田代湿原(上高地)","lat": 36.2365088, "lng": 137.6237352, "elev": 1493},
    {"name": "乗鞍高原",        "lat": 36.1065764, "lng": 137.6269188, "elev": 1456},
    {"name": "美ヶ原高原",      "lat": 36.225797,  "lng": 138.107442,  "elev": 2034},
    {"name": "踊場湿原",        "lat": 36.0852069, "lng": 138.162955,  "elev": 1535},
    {"name": "大阿原湿原",      "lat": 35.8863735, "lng": 138.1801778, "elev": 1830},
    {"name": "カヤの平",        "lat": 36.8012,    "lng": 138.4234,    "elev": 1500},
    {"name": "戦場ヶ原",        "lat": 36.7682,    "lng": 139.4825,    "elev": 1395},
    {"name": "尾瀬ヶ原",        "lat": 36.9167,    "lng": 139.2833,    "elev": 1400},
]

# 「おすすめ」には含めないが、個別指定すれば座標・属性を引ける登録地点。
# 千畳敷カールのような尾根スポットは、毎回Maps解決させず固定属性で扱いたいので
# ここに置く。get_spot_by_name は DEFAULT_SPOTS と EXTRA_SPOTS の両方を検索する。
EXTRA_SPOTS = [
    {"name": "千畳敷カール",    "lat": 35.7833,    "lng": 137.8081,    "elev": 2612},
]

# 夏場(霧シーズン 5-10月)の「おすすめ」一括からは除外したい地点。
# 霧のニーズが低い地点をここに追加する。DEFAULT_SPOTS 自体からは外さないため、
# 個別指定での予測や、11-4月(霧氷シーズン)の「おすすめ」には影響しない
# (resolve_location が季節に応じてフィルタする)。
SUMMER_RECOMMEND_EXCLUDE = [
    "田代湿原(上高地)",
]

SPOT_ALIASES = {
    "菅平":         "菅平(菅平牧場)",
    "上高地":       "田代湿原(上高地)",
    "田代":         "田代湿原(上高地)",
    "乗鞍":         "乗鞍高原",
    "美ヶ原":       "美ヶ原高原",
    "踊場":         "踊場湿原",
    "霧ヶ峰":       "踊場湿原",
    "大阿原":       "大阿原湿原",
    "カヤの平高原": "カヤの平",
    "戦場ヶ原":     "戦場ヶ原",
    "尾瀬":         "尾瀬ヶ原",
    "千畳敷":       "千畳敷カール",
    "千畳敷カール": "千畳敷カール",
}

def get_spot_by_name(name: str) -> dict | None:
    resolved = SPOT_ALIASES.get(name, name)
    # 「おすすめ」対象(DEFAULT_SPOTS)と個別登録(EXTRA_SPOTS)の両方を検索する
    for spot in DEFAULT_SPOTS + EXTRA_SPOTS:
        if spot["name"] == resolved:
            return spot
    return None


# ---------------------------------------------------------------------------
# 登録地点の地形属性（設計書5.7.3 spot_attributes 準拠）
#
# 登録地点の確定タグはここ(コード)に持たせる。Drive の spot_groups.json
# (SPOT_GROUPS_FILE_ID) が未配置でも、登録地点のタグ判定が確実に効くようにするため。
# Drive版 spot_groups.json は「未知地点の自動分類キャッシュ(auto_classified)」用途で
# 併用する。classify_spot_group は manual(=ここ) → auto(=Drive) の順で参照する。
#
# タグの意味:
#   水域近接 / 盆地地形 / 乾燥湿原 / 湿原内包 / 湖畔 / 谷筋 / 稜線上 / 峠
# pattern: 放射霧の A〜E パターン。該当なしは null。
# phenomena_priority: 各現象の優先度(高/中/低)。
#
# ★美ヶ原高原は尾根上の開けた高所のため「稜線上」タグを付与し、
#   放射霧=低・雲海=高 とする(放射霧は溜まらず、雲海/ガスが主役)。
# ---------------------------------------------------------------------------
SPOT_ATTRIBUTES = {
    "田ノ原湿原": {
        "main_group": "湿原系", "pattern": "A",
        "tags": ["盆地地形", "水域近接"],
        "phenomena_priority": {"霧氷": "高", "凝華": "高", "放射霧": "高", "雲海": "低"},
    },
    "大阿原湿原": {
        "main_group": "湿原系", "pattern": "C",
        "tags": ["盆地地形", "乾燥湿原"],
        "phenomena_priority": {"霧氷": "高", "凝華": "高", "放射霧": "中", "雲海": "低"},
    },
    "踊場湿原": {
        "main_group": "湿原系", "pattern": "A",
        "tags": ["盆地地形", "水域近接"],
        "phenomena_priority": {"霧氷": "高", "凝華": "中", "放射霧": "高", "雲海": "低"},
    },
    "田代湿原(上高地)": {
        "main_group": "湿原系", "pattern": "A",
        "tags": ["水域近接", "谷筋"],
        "phenomena_priority": {"霧氷": "高", "凝華": "中", "放射霧": "高", "雲海": "中"},
    },
    "戦場ヶ原": {
        "main_group": "湿原系", "pattern": "A",
        "tags": ["盆地地形", "水域近接"],
        "phenomena_priority": {"霧氷": "高", "凝華": "中", "放射霧": "高", "雲海": "低"},
    },
    "尾瀬ヶ原": {
        "main_group": "湿原系", "pattern": "A",
        "tags": ["盆地地形", "水域近接"],
        "phenomena_priority": {"霧氷": "高", "凝華": "中", "放射霧": "高", "雲海": "低"},
    },
    "美ヶ原高原": {
        # 尾根上の開けた高所。放射霧は溜まらず、雲海・ガス(雲中)が主役。
        "main_group": "高原系", "pattern": None,
        "tags": ["稜線上"],
        "phenomena_priority": {"霧氷": "中", "凝華": "低", "放射霧": "低", "雲海": "高"},
    },
    "菅平(菅平牧場)": {
        "main_group": "高原系", "pattern": "B",
        "tags": ["盆地地形", "湿原内包"],
        "phenomena_priority": {"霧氷": "高", "凝華": "中", "放射霧": "高", "雲海": "高"},
    },
    "乗鞍高原": {
        "main_group": "高原系", "pattern": None,
        "tags": ["水域近接"],
        "phenomena_priority": {"霧氷": "高", "凝華": "中", "放射霧": "中", "雲海": "高"},
    },
    "カヤの平": {
        "main_group": "高原系", "pattern": "B",
        "tags": ["水域近接", "湿原内包"],
        "phenomena_priority": {"霧氷": "中", "凝華": "中", "放射霧": "高", "雲海": "中"},
    },
    "千畳敷カール": {
        # 標高2612m、宝剣岳直下の氷河地形(カール)。高山帯の急斜面で放射霧は溜まらない。
        # 雲海・ガス(雲中)が主役のため稜線上タグを付与し、放射霧=低・雲海=高。
        "main_group": "高原系", "pattern": None,
        "tags": ["稜線上"],
        "phenomena_priority": {"霧氷": "中", "凝華": "低", "放射霧": "低", "雲海": "高"},
    },
}


def get_spot_attributes(name: str) -> dict | None:
    """登録地点の地形属性を返す（エイリアス解決込み）。未登録は None。"""
    resolved = SPOT_ALIASES.get(name, name)
    return SPOT_ATTRIBUTES.get(resolved)