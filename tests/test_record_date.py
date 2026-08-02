# tests/test_record_date.py
# agents/record.py の日付解決ロジック（純粋関数）のテスト
#
# _resolve_date は内部で datetime.now(JST) を呼ぶため、
# テストでは freezegun 等は使わず「固定日付を渡せる下位関数」を直接テストする。

from datetime import date, timedelta
import pytest

from agents.record import (
    _resolve_weekday,
    _resolve_relative_offset,
    _resolve_date,
    _norm_state,
    _score_from_state,
    DateResolutionError,
)


# =============================================
# _resolve_weekday
# =============================================

class TestResolveWeekday:
    """基準日を 2026-07-20（月曜）に固定してテスト"""

    BASE = date(2026, 7, 20)  # 月曜日

    def test_last_week_friday(self):
        result = _resolve_weekday("先週金曜日", self.BASE)
        assert result == date(2026, 7, 17)

    def test_this_week_wednesday(self):
        result = _resolve_weekday("今週水曜", self.BASE)
        assert result == date(2026, 7, 22)

    def test_next_week_monday(self):
        result = _resolve_weekday("来週月曜日", self.BASE)
        assert result == date(2026, 7, 27)

    def test_bare_weekday_same_day(self):
        """「月曜日」→ 当日（月曜）を返す"""
        result = _resolve_weekday("月曜日", self.BASE)
        assert result == date(2026, 7, 20)

    def test_bare_weekday_past(self):
        """「土曜日」→ 直近の過去の土曜を返す"""
        result = _resolve_weekday("土曜日", self.BASE)
        assert result == date(2026, 7, 18)

    def test_no_match(self):
        assert _resolve_weekday("昨日", self.BASE) is None
        assert _resolve_weekday("2026-07-20", self.BASE) is None


# =============================================
# _resolve_relative_offset
# =============================================

class TestResolveRelativeOffset:
    BASE = date(2026, 7, 20)

    def test_3_days_ago(self):
        result = _resolve_relative_offset("3日前", self.BASE)
        assert result == date(2026, 7, 17)

    def test_1_week_ago(self):
        result = _resolve_relative_offset("1週間前", self.BASE)
        assert result == date(2026, 7, 13)

    def test_2_weeks_ago(self):
        result = _resolve_relative_offset("2週間前", self.BASE)
        assert result == date(2026, 7, 6)

    def test_no_match(self):
        assert _resolve_relative_offset("昨日", self.BASE) is None
        assert _resolve_relative_offset("先週金曜日", self.BASE) is None


# =============================================
# _resolve_date（統合テスト。now() 依存のため一部は値の形式のみ検証）
# =============================================

class TestResolveDate:
    def test_iso_format(self):
        assert _resolve_date("2026-01-15") == "2026-01-15"

    def test_empty_returns_today_format(self):
        result = _resolve_date("")
        # YYYY-MM-DD 形式であること
        assert len(result) == 10
        assert result[4] == "-"

    def test_today(self):
        result = _resolve_date("今日")
        assert len(result) == 10

    def test_slash_format(self):
        """5/10 のようなスラッシュ区切り"""
        result = _resolve_date("5/10")
        assert result.endswith("-05-10")

    def test_invalid_raises(self):
        with pytest.raises(DateResolutionError):
            _resolve_date("来月の満月")


# =============================================
# _norm_state / _score_from_state
# =============================================

class TestNormState:
    def test_valid_states(self):
        assert _norm_state("あり") == "あり"
        assert _norm_state("少しあり") == "少しあり"
        assert _norm_state("なし") == "なし"
        assert _norm_state("不明") == "不明"

    def test_empty(self):
        assert _norm_state("") == "不明"

    def test_whitespace(self):
        assert _norm_state("  あり  ") == "あり"

    def test_unknown_value_passthrough(self):
        """定義外の値はそのまま返す"""
        assert _norm_state("バッチリ") == "バッチリ"


class TestScoreFromState:
    def test_scores(self):
        assert _score_from_state("あり") == 2
        assert _score_from_state("少しあり") == 1
        assert _score_from_state("なし") == 0

    def test_unknown(self):
        assert _score_from_state("不明") == -1
        assert _score_from_state("バッチリ") == -1
