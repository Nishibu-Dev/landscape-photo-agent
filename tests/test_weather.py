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


# =============================================
# check_kamikochi_rime（上高地の霧氷特別ルール）
# =============================================

from tools.weather import check_kamikochi_rime

GOOD = (-20.0, 0, 1.0)    # 露点, 下層雲, 風速: 3条件を満たす
BAD = (-8.0, 60, 3.5)     # 明確に満たさない


def _night(**hours):
    """前夜21時〜翌6時の10時間。指定のない時刻は BAD。キーは h21〜h06。"""
    order = ["21", "22", "23", "00", "01", "02", "03", "04", "05", "06"]
    rows = []
    for hh in order:
        date = "2026-01-15" if hh in ("21", "22", "23") else "2026-01-16"
        td, cl, w = hours.get(f"h{hh}", BAD)
        rows.append({"time": f"{date}T{hh}:00", "temp": -8.0,
                     "dew_point": td, "cloud_low": cl, "wind_adjusted": w})
    return rows


class TestKamikochiRime:
    def test_grade_a_long_run_through_dawn(self):
        r = check_kamikochi_rime(_night(h03=GOOD, h04=GOOD, h05=GOOD, h06=GOOD))
        assert r["grade"] == "A"
        assert r["longest_run"] == ["03:00", "04:00", "05:00", "06:00"]
        assert "判定A" in r["summary_line"] and "85〜95%" in r["summary_line"]

    def test_run_across_midnight_is_continuous(self):
        r = check_kamikochi_rime(_night(h22=GOOD, h23=GOOD, h00=GOOD, h01=GOOD))
        assert r["longest_run"] == ["22:00", "23:00", "00:00", "01:00"]
        assert r["grade"] == "B"   # 長時間だが明け方を含まない

    def test_grade_c_broken_runs(self):
        r = check_kamikochi_rime(_night(h02=GOOD, h04=GOOD, h06=GOOD))
        assert r["grade"] == "C"
        assert len(r["met_hours"]) == 3
        assert len(r["longest_run"]) == 1

    def test_boundary_values_are_met(self):
        r = check_kamikochi_rime(_night(h06=(-15.0, 9, 2.5)))
        assert r["met_hours"] == ["06:00"]

    def test_temperature_is_not_used(self):
        rows = _night(h06=GOOD)
        rows[-1]["temp"] = -6.0
        assert check_kamikochi_rime(rows)["met_hours"] == ["06:00"]

    def test_grade_d_single_small_shortfall(self):
        """実績で霧氷なしだった夜: 露点-15.9℃・下層雲11%・風1.3 → 惜しい(下層雲だけ不足)"""
        r = check_kamikochi_rime(_night(h06=(-15.9, 11, 1.3)))
        assert r["grade"] == "D"
        assert r["near_miss"]["condition"] == "下層雲"
        assert r["near_miss"]["shortfall"] == 2.0
        assert "下層雲があと2.0%不足" in r["summary_line"]

    def test_two_conditions_short_is_not_near_miss(self):
        r = check_kamikochi_rime(_night(h06=(-14.0, 12, 1.0)))
        assert r["grade"] == "E"

    def test_shortfall_beyond_margin_is_not_near_miss(self):
        assert check_kamikochi_rime(_night(h06=(-12.5, 0, 1.0)))["grade"] == "E"   # 露点2.5℃不足
        assert check_kamikochi_rime(_night(h06=(-20.0, 0, 3.1)))["grade"] == "E"   # 風速0.6不足
        assert check_kamikochi_rime(_night(h06=(-20.0, 20, 1.0)))["grade"] == "E"  # 下層雲11不足

    def test_shortfall_within_margin_each_condition(self):
        assert check_kamikochi_rime(_night(h06=(-13.0, 0, 1.0)))["grade"] == "D"   # 露点2.0℃不足
        assert check_kamikochi_rime(_night(h06=(-20.0, 0, 3.0)))["grade"] == "D"   # 風速0.5不足
        assert check_kamikochi_rime(_night(h06=(-20.0, 19, 1.0)))["grade"] == "D"  # 下層雲10不足

    def test_missing_data(self):
        rows = _night()
        for r in rows:
            r["cloud_low"] = None
        res = check_kamikochi_rime(rows)
        assert res["grade"] is None
        assert "判定不能" in res["summary_line"]


# =============================================
# check_wetland_rime（湿原・盆地型の霧氷ルール）
# =============================================

from tools.weather import check_wetland_rime, rh_ice


def _wnight(T, D, C):
    """0〜6時の7時間分。T=気温, D=露点, C=下層雲 のリスト。"""
    hrs = ["00", "01", "02", "03", "04", "05", "06"]
    return [{"time": f"2026-01-16T{h}:00", "temp": t, "dew_point": d,
             "cloud_low": c, "wind_adjusted": 5.0} for h, t, d, c in zip(hrs, T, D, C)]


class TestRhIce:
    def test_saturated_air_is_supersaturated_over_ice(self):
        assert 109 < rh_ice(-10.0, -10.0) < 111

    def test_spread_reduces_humidity(self):
        assert rh_ice(-10.0, -13.0) < rh_ice(-10.0, -11.0)


class TestWetlandRime:
    # 田ノ原の実績(グラフ目読み)から
    def test_humid_cloudy_night_is_grade_a(self):
        r = check_wetland_rime(_wnight(
            [-6.4, -6.6, -6.7, -6.8, -6.8, -6.8, -7.1],
            [-6.7, -6.8, -7.0, -7.2, -7.1, -7.1, -7.5],
            [96, 79, 75, 60, 48, 39, 41]))
        assert r["grade"] == "A"
        assert r["dawn_run"][-1] == "06:00"

    def test_strong_wind_does_not_block(self):
        r = check_wetland_rime(_wnight([-9.0] * 7, [-11.0] * 7, [30] * 7))
        assert r["grade"] == "A"   # 風速5.0でも成立(上限なし)

    def test_clearing_and_drying_night_is_grade_d(self):
        """晴れていき乾いていった夜(田ノ原・霧氷なし): 惜しいC → 乾燥化で格下げ D"""
        r = check_wetland_rime(_wnight(
            [-6.4, -6.6, -7.1, -8.2, -9.0, -9.7, -10.3],
            [-8.5, -9.0, -9.7, -11.1, -12.1, -13.5, -15.5],
            [23, 19, 17, 14, 12, 6, 1]))
        assert r["downgraded"] is True
        assert r["spread_change"] == 3.1
        assert r["grade"] == "D"
        assert "格下げ前は判定C" in r["summary_line"]

    def test_short_dawn_run_is_grade_b(self):
        r = check_wetland_rime(_wnight([-9.0] * 7, [-11.0] * 7, [0, 0, 0, 0, 0, 30, 30]))
        assert r["grade"] == "B"
        assert r["dawn_run"] == ["05:00", "06:00"]

    def test_met_only_before_dawn_is_not_counted(self):
        r = check_wetland_rime(_wnight([-9.0] * 7, [-11.0] * 7, [30, 30, 30, 0, 0, 0, 0]))
        assert r["grade"] == "D"
        assert "明け方以外のみ" in r["summary_line"]

    def test_near_miss_at_dawn_is_grade_c(self):
        r = check_wetland_rime(_wnight([-9.0] * 7, [-11.0] * 7, [0, 0, 0, 0, 12, 0, 0]))
        assert r["grade"] == "C"
        assert r["near_miss"]["condition"] == "下層雲"
        assert r["near_miss"]["shortfall"] == 8.0

    def test_two_conditions_short_is_not_near_miss(self):
        r = check_wetland_rime(_wnight([-9.0] * 7, [-14.0] * 7, [12] * 7))
        assert r["grade"] == "D"

    def test_above_minus_two_is_not_met(self):
        r = check_wetland_rime(_wnight([-1.5] * 7, [-1.6] * 7, [50] * 7))
        assert r["grade"] == "C"   # 気温だけ0.5℃不足 → 惜しい

    def test_missing_data(self):
        rows = _wnight([-9.0] * 7, [-11.0] * 7, [None] * 7)
        r = check_wetland_rime(rows)
        assert r["grade"] is None
        assert "判定不能" in r["summary_line"]


# =============================================
# 予測の回答に風速の補正係数を出さない
# =============================================

from tools.weather import _for_llm, _build_summary


class TestNoWindCorrectionInAnswers:
    HOUR = {"time": "2026-01-16T06:00", "temp": -8.0, "dew_point": -9.0, "humidity": 92,
            "wind_raw": 5.0, "wind_adjusted": 1.0, "cloud_low": 30, "cloud_mid": 0, "cloud_high": 0}

    def test_summary_has_no_factor_or_raw_wind(self):
        text = _build_summary("田ノ原湿原", 0.1, [self.HOUR], 0.0)
        assert "W:1.0m/s" in text
        for word in ("補正", "係数", "生値", "5.0m/s"):
            assert word not in text

    def test_llm_result_has_only_judgement_wind(self):
        r = _for_llm({"spot_name": "田ノ原湿原", "hourly": [self.HOUR], "summary": "x"})
        h = r["hourly"][0]
        assert h["wind"] == 1.0
        assert "wind_raw" not in h and "wind_adjusted" not in h

    def test_original_result_is_kept_for_records(self):
        original = {"spot_name": "田ノ原湿原", "hourly": [self.HOUR]}
        _for_llm(original)
        assert original["hourly"][0]["wind_raw"] == 5.0   # 記録用の元データは変更しない

    def test_error_result_passes_through(self):
        err = {"spot_name": "x", "error": "timeout"}
        assert _for_llm(err) == err
