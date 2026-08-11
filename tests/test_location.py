# tests/test_location.py
# tools/location.py の純粋関数テスト

from tools.location import (
    _sanitize_place_query,
    _looks_like_place_name,
    _is_summer_fog_season,
    _recommend_spots,
)
from config.spots import DEFAULT_SPOTS, SUMMER_RECOMMEND_EXCLUDE


# =============================================
# _sanitize_place_query
# =============================================

class TestSanitizePlaceQuery:
    def test_remove_request_phrase(self):
        assert _sanitize_place_query("千畳敷カールを予測して") == "千畳敷カール"

    def test_remove_weather_phrase(self):
        assert _sanitize_place_query("明日の上高地の天気は") == "上高地"

    def test_clean_input(self):
        assert _sanitize_place_query("田ノ原湿原") == "田ノ原湿原"

    def test_complex_phrase(self):
        result = _sanitize_place_query("千畳敷カールの予測をお願いします")
        assert result == "千畳敷カール"

    def test_trailing_particle(self):
        result = _sanitize_place_query("上高地の")
        assert result == "上高地"


# =============================================
# _looks_like_place_name
# =============================================

class TestLooksLikePlaceName:
    def test_valid_names(self):
        assert _looks_like_place_name("白駒池") is True
        assert _looks_like_place_name("八千穂高原") is True

    def test_empty(self):
        assert _looks_like_place_name("") is False

    def test_single_char(self):
        assert _looks_like_place_name("山") is False

    def test_leftover_request_words(self):
        """依頼語が残っている → まだ文章 → False"""
        assert _looks_like_place_name("予測") is False
        assert _looks_like_place_name("天気") is False


# =============================================
# _is_summer_fog_season / _recommend_spots
# =============================================

class TestIsSummerFogSeason:
    def test_summer_months(self):
        for month in range(5, 11):
            assert _is_summer_fog_season(month) is True

    def test_winter_months(self):
        for month in (11, 12, 1, 2, 3, 4):
            assert _is_summer_fog_season(month) is False


class TestRecommendSpots:
    def test_summer_excludes_configured_spots(self):
        """夏場(5-10月)は SUMMER_RECOMMEND_EXCLUDE の地点をおすすめから除外する"""
        names = [s["name"] for s in _recommend_spots(7)]
        for excluded in SUMMER_RECOMMEND_EXCLUDE:
            assert excluded not in names
        assert "カヤの平" in names

    def test_winter_includes_all_default_spots(self):
        """冬場(11-4月)は DEFAULT_SPOTS 全地点を返す(除外なし)"""
        names = [s["name"] for s in _recommend_spots(12)]
        assert len(names) == len(DEFAULT_SPOTS)
        for excluded in SUMMER_RECOMMEND_EXCLUDE:
            assert excluded in names

    def test_does_not_mutate_default_spots(self):
        """DEFAULT_SPOTS 自体は変更されない(個別指定への影響なし)"""
        _recommend_spots(7)
        assert len(DEFAULT_SPOTS) == 10
