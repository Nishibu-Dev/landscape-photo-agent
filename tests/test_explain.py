# tests/test_explain.py
# tools/explain.py: 説明が実際の判定に使う定数と一致していること

from tools import weather as w
from tools.explain import explain_spot_logic


class TestExplainSpotLogic:
    def test_spring_type_uses_constants(self):
        r = explain_spot_logic("上高地")
        assert r["found"] is True
        assert "湧水型" in r["text"]
        assert f"露点 ≦ {w.KAMIKOCHI_RIME_DEWPOINT_MAX:g}℃" in r["text"]
        assert f"風速 ≦ {w.KAMIKOCHI_RIME_WIND_MAX:g}m/s" in r["text"]

    def test_wetland_type_uses_constants(self):
        r = explain_spot_logic("田ノ原")
        assert "湿原・盆地型" in r["text"]
        assert f"氷に対する湿度 ≧ {w.WETLAND_RIME_RHICE_MIN:g}%" in r["text"]
        assert "凝華" in r["text"]

    def test_kappabashi(self):
        r = explain_spot_logic("河童橋")
        assert "河童橋(上高地)" in r["text"] and "湧水型" in r["text"]

    def test_default_type(self):
        text = explain_spot_logic("美ヶ原")["text"]
        assert "雲中型・未確定" in text
        assert f"気温 ≦ {w.CLOUD_RIME_TEMP_MAX:g}℃" in text and "ガス(雲中)と同じ向き" in text

    def test_spring_has_freezing_precondition(self):
        assert "前提条件: 気温 ≦ 0℃" in explain_spot_logic("上高地")["text"]

    def test_oze_is_wetland(self):
        assert "湿原・盆地型" in explain_spot_logic("尾瀬ヶ原")["text"]

    def test_unknown_spot(self):
        r = explain_spot_logic("女神湖")
        assert r["found"] is False
        assert "登録地点ではない" in r["text"] and "雲中型・未確定" in r["text"]
        assert "湿原系×盆地地形" in r["text"] and "湿原・盆地型" in r["text"]
        assert "湧水型は推定しない" in r["text"]

    def test_overview(self):
        r = explain_spot_logic("全体")
        assert "田代湿原(上高地)、河童橋(上高地)" in r["text"]
        assert "霧氷の判定には使わない" in r["text"]

    def test_threshold_change_is_reflected(self, monkeypatch):
        monkeypatch.setattr(w, "WETLAND_RIME_RHICE_MIN", 90.0)
        assert "氷に対する湿度 ≧ 90%" in explain_spot_logic("田ノ原")["text"]

    def test_wind_correction_is_not_mentioned(self):
        """ユーザー向けの説明に、風速の補正係数は出さない"""
        for name in ("上高地", "河童橋", "田ノ原", "美ヶ原", "女神湖", "全体"):
            text = explain_spot_logic(name)["text"]
            assert "補正" not in text and "係数" not in text, name
