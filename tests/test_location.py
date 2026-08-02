# tests/test_location.py
# tools/location.py の純粋関数テスト

from tools.location import _sanitize_place_query, _looks_like_place_name


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
