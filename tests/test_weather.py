# tests/test_weather.py
# tools/weather.py の純粋関数テスト

from tools.weather import (
    _build_target_times,
    _sum_prev_day_precip,
    _get_elev_registered,
)


# =============================================
# _build_target_times
# =============================================

class TestBuildTargetTimes:
    def test_structure(self):
        times = _build_target_times("2026-01-15")
        # 前夜21,22,23時 + 翌日0,1,2,3,4,5,6時 = 10時間
        assert len(times) == 10

    def test_evening_hours(self):
        times = _build_target_times("2026-01-15")
        assert times[0] == "2026-01-15T21:00"
        assert times[1] == "2026-01-15T22:00"
        assert times[2] == "2026-01-15T23:00"

    def test_morning_hours(self):
        times = _build_target_times("2026-01-15")
        assert times[3] == "2026-01-16T00:00"
        assert times[9] == "2026-01-16T06:00"

    def test_date_boundary(self):
        """日付が正しく翌日に切り替わること"""
        times = _build_target_times("2026-12-31")
        assert times[3] == "2027-01-01T00:00"


# =============================================
# _sum_prev_day_precip
# =============================================

class TestSumPrevDayPrecip:
    def test_basic(self):
        hourly = {
            "time": [
                "2026-01-15T00:00", "2026-01-15T06:00",
                "2026-01-15T12:00", "2026-01-15T20:00",
                "2026-01-15T21:00",  # 21時は対象外
            ],
            "precipitation": [1.0, 2.0, 0.5, 0.0, 3.0],
        }
        result = _sum_prev_day_precip(hourly, "2026-01-15")
        assert result == 3.5  # 1.0 + 2.0 + 0.5 + 0.0（21時の 3.0 は含まない）

    def test_no_precipitation_key(self):
        hourly = {"time": ["2026-01-15T00:00"]}
        assert _sum_prev_day_precip(hourly, "2026-01-15") == 0.0

    def test_none_values(self):
        hourly = {
            "time": ["2026-01-15T06:00"],
            "precipitation": [None],
        }
        assert _sum_prev_day_precip(hourly, "2026-01-15") == 0.0


# =============================================
# _get_elev_registered
# =============================================

class TestGetElevRegistered:
    def test_known_spot(self):
        assert _get_elev_registered("田ノ原湿原") == 1605
        assert _get_elev_registered("千畳敷カール") == 2612

    def test_unknown_spot(self):
        assert _get_elev_registered("白駒池") is None
