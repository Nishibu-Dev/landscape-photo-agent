# tests/test_config.py
# config/spots.py, config/adjustments.py の純粋関数テスト

from config.spots import (
    DEFAULT_SPOTS,
    EXTRA_SPOTS,
    SPOT_ALIASES,
    SPOT_ATTRIBUTES,
    get_spot_by_name,
    get_spot_attributes,
)
from config.adjustments import get_wind_factor, WIND_ADJUSTMENTS


# =============================================
# config/spots.py
# =============================================

class TestDefaultSpots:
    """DEFAULT_SPOTS の整合性チェック"""

    def test_count(self):
        assert len(DEFAULT_SPOTS) == 10

    def test_required_keys(self):
        for spot in DEFAULT_SPOTS:
            assert "name" in spot
            assert "lat" in spot
            assert "lng" in spot
            assert "elev" in spot

    def test_all_have_attributes(self):
        """全 DEFAULT_SPOTS に SPOT_ATTRIBUTES が定義されていること"""
        for spot in DEFAULT_SPOTS:
            attrs = get_spot_attributes(spot["name"])
            assert attrs is not None, f"{spot['name']} の SPOT_ATTRIBUTES が未定義"

    def test_all_have_wind_factor(self):
        """全 DEFAULT_SPOTS に風速補正係数が定義されていること"""
        for spot in DEFAULT_SPOTS:
            factor = WIND_ADJUSTMENTS.get(spot["name"])
            assert factor is not None, f"{spot['name']} の WIND_ADJUSTMENTS が未定義"


class TestGetSpotByName:
    def test_direct_name(self):
        spot = get_spot_by_name("田ノ原湿原")
        assert spot is not None
        assert spot["name"] == "田ノ原湿原"

    def test_alias(self):
        spot = get_spot_by_name("上高地")
        assert spot is not None
        assert spot["name"] == "田代湿原(上高地)"

    def test_alias_kamikochi(self):
        spot = get_spot_by_name("乗鞍")
        assert spot is not None
        assert spot["name"] == "乗鞍高原"

    def test_extra_spot(self):
        spot = get_spot_by_name("千畳敷カール")
        assert spot is not None
        assert spot["elev"] == 2612

    def test_extra_spot_alias(self):
        spot = get_spot_by_name("千畳敷")
        assert spot is not None
        assert spot["name"] == "千畳敷カール"

    def test_unknown(self):
        assert get_spot_by_name("白駒池") is None

    def test_empty(self):
        assert get_spot_by_name("") is None


class TestGetSpotAttributes:
    def test_tanohara(self):
        attrs = get_spot_attributes("田ノ原湿原")
        assert attrs["main_group"] == "湿原系"
        assert attrs["pattern"] == "A"
        assert "盆地地形" in attrs["tags"]
        assert attrs["phenomena_priority"]["霧氷"] == "高"

    def test_utsukushigahara(self):
        """美ヶ原高原は稜線上タグ → 放射霧=低、雲海=高"""
        attrs = get_spot_attributes("美ヶ原高原")
        assert "稜線上" in attrs["tags"]
        assert attrs["phenomena_priority"]["放射霧"] == "低"
        assert attrs["phenomena_priority"]["雲海"] == "高"

    def test_alias_resolution(self):
        attrs = get_spot_attributes("上高地")
        assert attrs is not None
        assert attrs["main_group"] == "湿原系"

    def test_unknown(self):
        assert get_spot_attributes("白駒池") is None


# =============================================
# config/adjustments.py
# =============================================

class TestWindFactor:
    def test_registered_spot(self):
        assert get_wind_factor("田ノ原湿原") == 0.1

    def test_default_fallback(self):
        assert get_wind_factor("未知の地点") == 0.6

    def test_all_factors_in_range(self):
        """補正係数は 0 < factor <= 1 の範囲であること"""
        for name, factor in WIND_ADJUSTMENTS.items():
            assert 0 < factor <= 1, f"{name} の係数 {factor} が範囲外"
