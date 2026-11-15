# tests/test_models.py
# config/models.py: 全エージェントのモデルと思考レベルが Gemini 3 の形式で設定されていること

from config.models import AGENT_MODELS, AGENT_THINKING_LEVELS, generate_content_config_for

VALID_LEVELS = {"minimal", "low", "medium", "high"}


class TestModels:
    def test_no_gemini_25(self):
        """gemini-2.5-flash は 2026-12-01 から自動転送されるため使わない"""
        for name, model in AGENT_MODELS.items():
            assert not model.startswith("gemini-2.5"), name

    def test_thinking_levels_are_valid(self):
        assert set(AGENT_THINKING_LEVELS) == set(AGENT_MODELS)
        for name, level in AGENT_THINKING_LEVELS.items():
            assert level in VALID_LEVELS, name

    def test_thinking_level_not_budget(self):
        cfg = generate_content_config_for("ForecastAgent").thinking_config
        assert cfg.thinking_budget is None
        assert str(cfg.thinking_level).lower().endswith("high")

    def test_temperature_left_default(self):
        """Gemini 3 は temperature を既定のまま使う"""
        assert generate_content_config_for("ForecastAgent").temperature is None


class TestAgentsWired:
    def test_agents_use_config(self):
        from agents.coordinator import photo_concierge
        agents = [photo_concierge, *photo_concierge.sub_agents]
        for a in agents:
            assert a.model == AGENT_MODELS[a.name], a.name
        assert photo_concierge.planner.thinking_config.thinking_level is not None
        for a in photo_concierge.sub_agents:
            assert a.generate_content_config.thinking_config.thinking_level is not None
