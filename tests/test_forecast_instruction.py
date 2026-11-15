# tests/test_forecast_instruction.py
# ForecastAgent の指示文に、霧氷と見解の書き方のルールが入っていること

from agents.forecast import FORECAST_INSTRUCTION as INS


class TestForecastInstruction:
    def test_all_types_follow_judgement_lines(self):
        for label in ("【湧水型霧氷ルール", "【湿原霧氷ルール", "【雲中型霧氷ルール"):
            assert label in INS

    def test_rime_reason_from_judgement_line(self):
        assert "判定行に書かれた内容" in INS
        assert "気温が高いことを理由に書いてよい" in INS

    def test_cloud_type_rime_follows_gas(self):
        assert "ガス（雲中）の確率と同じ向き" in INS

    def test_comment_limited_to_morning(self):
        assert "日中や午後の天気には触れない" in INS

    def test_no_llm_common_rule_for_rime(self):
        assert "共通条件3で判定する" not in INS

    def test_unknown_spot_passes_terrain_for_rime(self):
        assert "main_group と tags を" in INS and "fetch_forecast" in INS
        assert "湧水型は推定しない" in INS

    def test_no_freeze_is_zero(self):
        assert "判定0（一晩中氷点下にならない）は、どのタイプでも霧氷 0% と出すこと" in INS

    def test_ebi_note_for_strong_wind(self):
        assert "樹氷(エビのしっぽ)になりやすい" in INS and "確率は判定行どおりのまま" in INS
