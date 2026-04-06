"""StrategySelector 테스트."""
import pytest

from engine.strategy_selector import StrategySelector
from core.enums import Regime
from strategies.trend_follower import TrendFollowerStrategy
from strategies.mean_reversion import MeanReversionStrategy
from strategies.vol_breakout import VolBreakoutStrategy


class TestStrategySelector:
    def test_trending_up_selects_mean_reversion(self):
        """상승 추세: TF 진입 PF 0.29로 비활성 → MR 폴백."""
        sel = StrategySelector()
        s = sel.select(Regime.TRENDING_UP)
        assert isinstance(s, MeanReversionStrategy)

    def test_trending_down_selects_trend_follower(self):
        sel = StrategySelector()
        s = sel.select(Regime.TRENDING_DOWN)
        assert isinstance(s, TrendFollowerStrategy)

    def test_ranging_selects_mean_reversion(self):
        sel = StrategySelector()
        s = sel.select(Regime.RANGING)
        assert isinstance(s, MeanReversionStrategy)

    def test_volatile_selects_vol_breakout(self):
        sel = StrategySelector()
        s = sel.select(Regime.VOLATILE)
        assert isinstance(s, VolBreakoutStrategy)

    def test_trending_up_and_ranging_share_mr(self):
        sel = StrategySelector()
        up = sel.select(Regime.TRENDING_UP)
        ranging = sel.select(Regime.RANGING)
        assert up is ranging  # 같은 MeanReversion 인스턴스

    def test_all_strategies(self):
        sel = StrategySelector()
        all_s = sel.all_strategies
        assert len(all_s) == 4
        assert Regime.TRENDING_UP in all_s
        assert Regime.RANGING in all_s

    def test_custom_register(self):
        sel = StrategySelector()
        custom = MeanReversionStrategy()
        sel.register(Regime.TRENDING_UP, custom)
        assert sel.select(Regime.TRENDING_UP) is custom
