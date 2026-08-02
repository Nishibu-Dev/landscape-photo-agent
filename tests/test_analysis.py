# tests/test_analysis.py
# tools/analysis.py の純粋関数テスト
#
# _classify_fog はユーザー入力の霧判定ロジックの根幹。
# ここがズレると分析レポート全体が狂うため、境界値を網羅する。

from tools.analysis import (
    _classify_fog,
    _mmdd,
    _in_night_analysis_window,
    HUMIDITY_THRESHOLD,
    T_TD_THRESHOLD,
    WIND_THRESHOLD,
    CLOUD_LOW_THRESHOLD,
)


# =============================================
# _classify_fog
# =============================================

class TestClassifyFog:
    """霧あり/霧なし/除外(None) の分類ロジック"""

    def test_radiation_fog_ari(self):
        assert _classify_fog({"radiation_fog": "あり"}) == "霧あり"

    def test_radiation_fog_sukoshi(self):
        assert _classify_fog({"radiation_fog": "少しあり"}) == "霧あり"

    def test_radiation_fog_nashi(self):
        assert _classify_fog({"radiation_fog": "なし"}) == "霧なし"

    def test_radiation_fog_unknown_falls_through_to_fog(self):
        """radiation_fog=不明 → fog フィールドにフォールバック"""
        assert _classify_fog({"radiation_fog": "不明", "fog": "あり"}) == "霧あり"
        assert _classify_fog({"radiation_fog": "不明", "fog": "なし"}) == "霧なし"

    def test_both_unknown_returns_none(self):
        """両方「不明」→ 分析対象から除外 (None)"""
        assert _classify_fog({"radiation_fog": "不明", "fog": "不明"}) is None

    def test_radiation_fog_missing_falls_through(self):
        """radiation_fog キーが無い → fog にフォールバック"""
        assert _classify_fog({"fog": "あり"}) == "霧あり"
        assert _classify_fog({"fog": "なし"}) == "霧なし"

    def test_empty_observations(self):
        assert _classify_fog({}) is None

    def test_fog_sukoshi(self):
        assert _classify_fog({"fog": "少しあり"}) == "霧あり"

    def test_radiation_fog_takes_priority(self):
        """radiation_fog が確定値なら fog は見ない"""
        assert _classify_fog({"radiation_fog": "なし", "fog": "あり"}) == "霧なし"


# =============================================
# _mmdd
# =============================================

class TestMmdd:
    def test_normal(self):
        assert _mmdd("2026-01-15") == "01-15"

    def test_invalid(self):
        assert _mmdd("invalid") == "invalid"


# =============================================
# _in_night_analysis_window
# =============================================

class TestInNightAnalysisWindow:
    """分析対象は 0時〜6時 の7時点"""

    def test_midnight(self):
        assert _in_night_analysis_window({"time": "2026-01-16T00:00"}) is True

    def test_6am(self):
        assert _in_night_analysis_window({"time": "2026-01-16T06:00"}) is True

    def test_7am_excluded(self):
        assert _in_night_analysis_window({"time": "2026-01-16T07:00"}) is False

    def test_21pm_excluded(self):
        """前夜21時は hourly には含まれるが分析集計からは除外"""
        assert _in_night_analysis_window({"time": "2026-01-15T21:00"}) is False

    def test_invalid_time(self):
        assert _in_night_analysis_window({"time": ""}) is False
        assert _in_night_analysis_window({}) is False


# =============================================
# 閾値定数の確認
# =============================================

class TestThresholds:
    """定数が設計書v1.4 の値と一致していること"""

    def test_humidity(self):
        assert HUMIDITY_THRESHOLD == 90

    def test_t_td(self):
        assert T_TD_THRESHOLD == 2

    def test_wind(self):
        assert WIND_THRESHOLD == 2

    def test_cloud_low(self):
        assert CLOUD_LOW_THRESHOLD == 10
