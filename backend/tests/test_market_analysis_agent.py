"""
Tests for MarketAnalysisAgent._classify_market() — tiebreaker logic.

COIN-30: When uptrend and downtrend are tied (e.g., 2.5:2.5),
the previous code used max(scores, key=scores.get) which always
picked the first key in dict order (uptrend) due to Python dict
ordering. The fix uses current_price vs SMA20 as a tiebreaker.
"""
import os

os.environ.setdefault("DB_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("EXCHANGE_API_KEY", "test")
os.environ.setdefault("EXCHANGE_API_SECRET", "test")
os.environ.setdefault("TRADING_MODE", "paper")

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
from agents.market_analysis import MarketAnalysisAgent
from core.enums import MarketState


def _make_agent() -> MarketAnalysisAgent:
    """Create a MarketAnalysisAgent with mocked dependencies."""
    return MarketAnalysisAgent(
        market_data=MagicMock(),
        market_symbol="BTC/USDT",
        exchange_name="binance_spot",
    )


def _make_df(
    n: int = 200,
    close_base: float = 70_000,
    sma_20: float | None = None,
    sma_50: float | None = None,
    rsi_14: float = 50.0,
    volume_ratio: float = 1.0,
    include_volume_sma: bool = True,
) -> pd.DataFrame:
    """Create a 1h DataFrame with indicators for testing."""
    closes = np.full(n, close_base)
    base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    index = pd.DatetimeIndex([base_time + timedelta(hours=i) for i in range(n)])

    avg_volume = 1000.0
    data = {
        "close": closes,
        "volume": np.full(n, avg_volume * volume_ratio),
    }

    if sma_20 is not None:
        data["sma_20"] = np.full(n, sma_20)
    if sma_50 is not None:
        data["sma_50"] = np.full(n, sma_50)
    if rsi_14 is not None:
        data["rsi_14"] = np.full(n, rsi_14)
    if include_volume_sma:
        data["volume_sma_20"] = np.full(n, avg_volume)

    return pd.DataFrame(data, index=index)


def _make_daily_df(
    n: int = 30,
    close_base: float = 70_000,
    week_change_pct: float = 0.0,
) -> pd.DataFrame:
    """Create a daily DataFrame for testing."""
    closes = np.full(n, close_base)
    if week_change_pct != 0 and n >= 7:
        past_price = close_base / (1 + week_change_pct / 100)
        closes[-7] = past_price

    base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    index = pd.DatetimeIndex([base_time + timedelta(days=i) for i in range(n)])
    return pd.DataFrame({"close": closes}, index=index)


class TestTiebreakerPriceBelowSMA20:
    """When scores are tied and price < SMA20, downtrend should be preferred."""

    def test_issue_scenario_uptrend_downtrend_tie_price_below_sma(self):
        """Exact scenario from COIN-30 issue:
        price=$70,613 < SMA20=$71,174, RSI=43, weekly +7.1%
        Scores: uptrend=2.5, downtrend=2.5 → should be downtrend.
        """
        agent = _make_agent()

        # price < sma_20 → downtrend +1.5 (factor 1)
        # sma_20 < sma_50 → downtrend +1 (factor 2)
        # RSI 43 < 45 → downtrend +1 (factor 3)
        # weekly +7.1% > 3% → uptrend +1.5 (factor 4)
        # Total: uptrend=1.5, downtrend=3.5 (not exactly 2.5:2.5 with these params)
        # Let's set up a proper tie scenario:
        # price < sma_20 → downtrend +1.5
        # sma_20 > sma_50 → uptrend +1, strong_uptrend +0.5
        # RSI 43 → downtrend +1
        # weekly +7.1% → uptrend +1.5
        # → uptrend=2.5, downtrend=2.5, strong_uptrend=0.5
        df_1h = _make_df(
            close_base=70_613,
            sma_20=71_174,
            sma_50=70_000,  # sma_20 > sma_50
            rsi_14=43.0,
        )
        df_1d = _make_daily_df(close_base=70_613, week_change_pct=7.1)

        state, confidence, reasoning, indicators = agent._classify_market(
            df_1h, df_1d, current_price=70_613
        )

        assert state == MarketState.DOWNTREND, (
            f"Expected DOWNTREND when price < SMA20 and tied, got {state.value}. "
            f"Scores: {indicators['scores']}"
        )

    def test_tie_price_below_sma_prefers_downtrend(self):
        """Generic tie scenario with price below SMA20."""
        agent = _make_agent()

        # Set up: price below SMA20 slightly, neutral RSI, small positive weekly
        # price < sma_20 → downtrend +1.5
        # sma_20 > sma_50 → uptrend +1, strong_uptrend +0.5
        # RSI 50 (neutral 45-55) → sideways +1.5
        # weekly +5% → uptrend +1.5
        # → uptrend=2.5, downtrend=1.5, sideways=1.5, strong_uptrend=0.5
        # Not a tie. Let me craft a proper tie.

        # price < sma_20 → downtrend +1.5
        # sma_20 > sma_50 → uptrend +1, strong_uptrend +0.5
        # RSI=56 (>55) → uptrend +1
        # weekly -5% → downtrend +1.5
        # → uptrend=2, downtrend=3
        # Still not a tie. Let me think more carefully.

        # price < sma_20 → downtrend +1.5
        # sma_20 > sma_50 → uptrend +1, strong_uptrend +0.5
        # RSI=43 (<45) → downtrend +1
        # weekly +7% (>3) → uptrend +1.5
        # → uptrend=2.5, downtrend=2.5, strong_uptrend=0.5
        # This is a tie!
        df_1h = _make_df(
            close_base=69_000,
            sma_20=70_000,
            sma_50=68_000,
            rsi_14=43.0,
        )
        df_1d = _make_daily_df(close_base=69_000, week_change_pct=7.0)

        state, confidence, reasoning, indicators = agent._classify_market(
            df_1h, df_1d, current_price=69_000
        )

        scores = indicators["scores"]
        assert scores["uptrend"] == scores["downtrend"], (
            f"Expected uptrend==downtrend tie, got uptrend={scores['uptrend']}, "
            f"downtrend={scores['downtrend']}"
        )
        assert state == MarketState.DOWNTREND


class TestTiebreakerPriceAboveSMA20:
    """When scores are tied and price >= SMA20, uptrend should be preferred."""

    def test_tie_price_above_sma_prefers_uptrend(self):
        """When tied and price > SMA20, uptrend should win."""
        agent = _make_agent()

        # price > sma_20 → uptrend +1.5
        # sma_20 < sma_50 → downtrend +1
        # RSI=56 (>55) → uptrend +1
        # weekly -5% (<-3) → downtrend +1.5
        # → uptrend=2.5, downtrend=2.5
        df_1h = _make_df(
            close_base=71_000,
            sma_20=70_000,
            sma_50=72_000,  # sma_20 < sma_50
            rsi_14=56.0,
        )
        df_1d = _make_daily_df(close_base=71_000, week_change_pct=-5.0)

        state, confidence, reasoning, indicators = agent._classify_market(
            df_1h, df_1d, current_price=71_000
        )

        scores = indicators["scores"]
        assert scores["uptrend"] == scores["downtrend"], (
            f"Expected uptrend==downtrend tie, got uptrend={scores['uptrend']}, "
            f"downtrend={scores['downtrend']}"
        )
        assert state == MarketState.UPTREND

    def test_tie_price_equal_sma_prefers_uptrend(self):
        """When tied and price == SMA20, uptrend should win (>= condition)."""
        agent = _make_agent()

        # price == sma_20 → no score from factor 1 (neither > nor <)
        # sma_20 > sma_50 → uptrend +1, strong_uptrend +0.5
        # RSI=43 (<45) → downtrend +1
        # weekly +0% → sideways +2
        # → uptrend=1, downtrend=1, sideways=2, strong_uptrend=0.5
        # Not a tie between uptrend/downtrend. Sideways wins.
        # Let me force a different scenario.

        # price == sma_20 → no factor 1 score
        # sma_20 > sma_50 → uptrend +1, strong_uptrend +0.5
        # RSI=43 → downtrend +1
        # weekly +5% → uptrend +1.5
        # → uptrend=2.5, downtrend=1
        # No tie. Need different approach.

        # price == sma_20 → no factor 1 score
        # sma_20 < sma_50 → downtrend +1
        # RSI=56 → uptrend +1
        # weekly +0% → sideways +2
        # → uptrend=1, downtrend=1, sideways=2
        # Sideways wins, not a tie at top.

        # Let's use: price == sma_20, no sma_50
        # no factor 1 score
        # no factor 2 score
        # RSI=56 → uptrend +1
        # weekly -5% → downtrend +1.5
        # → uptrend=1, downtrend=1.5 — downtrend wins
        # Not a tie.

        # Harder to force exact tie with price==SMA20.
        # Let's test the >= boundary directly by checking behavior when price == sma_20
        # and we can orchestrate a 3-way tie.
        # price == sma_20 → no factor 1 score
        # sma_20 < sma_50 → downtrend +1
        # RSI=56 → uptrend +1
        # weekly +0% → sideways +2
        # → uptrend=1, downtrend=1, sideways=2 → sideways wins

        # Actually let's just test the boundary with a simpler setup:
        # Only provide minimal indicators to force a tie.
        # No sma_50, no volume_sma_20, short daily df.
        df_1h = _make_df(
            close_base=70_000,
            sma_20=70_000,
            sma_50=None,
            rsi_14=None,
            include_volume_sma=False,
        )
        # Short daily df (< 7 days) → no weekly factor
        df_1d = _make_daily_df(n=3, close_base=70_000)

        state, confidence, reasoning, indicators = agent._classify_market(
            df_1h, df_1d, current_price=70_000
        )

        # With price == sma_20 and no other indicators, all scores are 0.
        # This is a 5-way tie. With price >= sma_20, uptrend preference applies.
        scores = indicators["scores"]
        assert state == MarketState.UPTREND, (
            f"Expected UPTREND when price == SMA20 and tied, got {state.value}. "
            f"Scores: {scores}"
        )


class TestTiebreakerNoSMA20:
    """When SMA20 is not available, fallback to max() behavior."""

    def test_no_sma20_falls_back_to_max(self):
        """Without SMA20, tie uses default max() behavior (dict order)."""
        agent = _make_agent()

        df_1h = _make_df(
            close_base=70_000,
            sma_20=None,
            sma_50=None,
            rsi_14=None,
            include_volume_sma=False,
        )
        df_1d = _make_daily_df(n=3, close_base=70_000)

        state, confidence, reasoning, indicators = agent._classify_market(
            df_1h, df_1d, current_price=70_000
        )

        # All scores are 0 (no indicators), tie → fallback to max(scores, key=scores.get)
        # This should still work without error
        assert state is not None
        assert isinstance(state, MarketState)


class TestNoTie:
    """When there is no tie, the highest-scoring state wins regardless of SMA20."""

    def test_clear_uptrend_wins(self):
        """Uptrend wins clearly without tie."""
        agent = _make_agent()

        # price > sma_20 → uptrend +1.5
        # sma_20 > sma_50 → uptrend +1, strong_uptrend +0.5
        # RSI=60 (>55) → uptrend +1
        # weekly +5% → uptrend +1.5
        # → uptrend=5, strong_uptrend=0.5
        df_1h = _make_df(
            close_base=72_000,
            sma_20=70_000,
            sma_50=68_000,
            rsi_14=60.0,
        )
        df_1d = _make_daily_df(close_base=72_000, week_change_pct=5.0)

        state, confidence, reasoning, indicators = agent._classify_market(
            df_1h, df_1d, current_price=72_000
        )

        assert state == MarketState.UPTREND

    def test_clear_downtrend_wins(self):
        """Downtrend wins clearly without tie."""
        agent = _make_agent()

        # price < sma_20 → downtrend +1.5
        # sma_20 < sma_50 → downtrend +1
        # RSI=35 (<45) → downtrend +1
        # weekly -5% → downtrend +1.5
        # → downtrend=5
        df_1h = _make_df(
            close_base=68_000,
            sma_20=70_000,
            sma_50=72_000,
            rsi_14=35.0,
        )
        df_1d = _make_daily_df(close_base=68_000, week_change_pct=-5.0)

        state, confidence, reasoning, indicators = agent._classify_market(
            df_1h, df_1d, current_price=68_000
        )

        assert state == MarketState.DOWNTREND

    def test_strong_uptrend_wins(self):
        """Strong uptrend with overwhelming score."""
        agent = _make_agent()

        # price > sma_20 * 1.05 → strong_uptrend +2
        # sma_20 > sma_50 → uptrend +1, strong_uptrend +0.5
        # RSI=75 (>70) → strong_uptrend +1
        # weekly +15% (>10) → strong_uptrend +2
        # → strong_uptrend=5.5, uptrend=1
        df_1h = _make_df(
            close_base=80_000,
            sma_20=75_000,
            sma_50=70_000,
            rsi_14=75.0,
        )
        df_1d = _make_daily_df(close_base=80_000, week_change_pct=15.0)

        state, confidence, reasoning, indicators = agent._classify_market(
            df_1h, df_1d, current_price=80_000
        )

        assert state == MarketState.STRONG_UPTREND

    def test_sideways_wins(self):
        """Sideways market with neutral indicators."""
        agent = _make_agent()

        # price == sma_20 → no factor 1
        # sma_20 == sma_50 (close enough) → no clear alignment
        # RSI=50 (45-55 neutral) → sideways +1.5
        # weekly +0% → sideways +2
        # → sideways=3.5
        df_1h = _make_df(
            close_base=70_000,
            sma_20=70_000,
            sma_50=70_000,
            rsi_14=50.0,
        )
        df_1d = _make_daily_df(close_base=70_000, week_change_pct=0.0)

        state, confidence, reasoning, indicators = agent._classify_market(
            df_1h, df_1d, current_price=70_000
        )

        assert state == MarketState.SIDEWAYS


class TestTiebreakerSidewaysInvolved:
    """Test tiebreaker when sideways is involved in the tie."""

    def test_sideways_downtrend_tie_price_below_sma(self):
        """When sideways and downtrend tie with price < SMA20, downtrend wins."""
        agent = _make_agent()

        # We need sideways == downtrend at the top.
        # price < sma_20 → downtrend +1.5
        # no sma_50
        # RSI=50 (neutral) → sideways +1.5
        # short daily (no weekly factor)
        # → downtrend=1.5, sideways=1.5 — TIE
        df_1h = _make_df(
            close_base=69_000,
            sma_20=70_000,
            sma_50=None,
            rsi_14=50.0,
            include_volume_sma=False,
        )
        df_1d = _make_daily_df(n=3, close_base=69_000)

        state, confidence, reasoning, indicators = agent._classify_market(
            df_1h, df_1d, current_price=69_000
        )

        scores = indicators["scores"]
        assert scores["downtrend"] == scores["sideways"], (
            f"Expected tie, got downtrend={scores['downtrend']}, sideways={scores['sideways']}"
        )
        # price < SMA20 → preference: DOWNTREND > SIDEWAYS
        assert state == MarketState.DOWNTREND

    def test_sideways_uptrend_tie_price_above_sma(self):
        """When sideways and uptrend tie with price > SMA20, uptrend wins."""
        agent = _make_agent()

        # price > sma_20 → uptrend +1.5
        # no sma_50
        # RSI=50 (neutral) → sideways +1.5
        # short daily (no weekly factor)
        # → uptrend=1.5, sideways=1.5 — TIE
        df_1h = _make_df(
            close_base=71_000,
            sma_20=70_000,
            sma_50=None,
            rsi_14=50.0,
            include_volume_sma=False,
        )
        df_1d = _make_daily_df(n=3, close_base=71_000)

        state, confidence, reasoning, indicators = agent._classify_market(
            df_1h, df_1d, current_price=71_000
        )

        scores = indicators["scores"]
        assert scores["uptrend"] == scores["sideways"], (
            f"Expected tie, got uptrend={scores['uptrend']}, sideways={scores['sideways']}"
        )
        # price > SMA20 → preference: UPTREND > SIDEWAYS
        assert state == MarketState.UPTREND


class TestConfidenceAndReturns:
    """Test that confidence, reasoning, and indicators are returned correctly."""

    def test_confidence_range(self):
        """Confidence should be between 0 and 1."""
        agent = _make_agent()

        df_1h = _make_df(
            close_base=70_000,
            sma_20=71_000,
            sma_50=69_000,
            rsi_14=43.0,
        )
        df_1d = _make_daily_df(close_base=70_000, week_change_pct=7.0)

        _, confidence, _, _ = agent._classify_market(df_1h, df_1d, current_price=70_000)

        assert 0 <= confidence <= 1.0

    def test_indicators_contain_scores(self):
        """Indicators dict should contain scores for debugging."""
        agent = _make_agent()

        df_1h = _make_df(close_base=70_000, sma_20=71_000)
        df_1d = _make_daily_df(close_base=70_000)

        _, _, _, indicators = agent._classify_market(df_1h, df_1d, current_price=70_000)

        assert "scores" in indicators
        assert "current_price" in indicators
        assert indicators["current_price"] == 70_000

    def test_reasoning_contains_state(self):
        """Reasoning string should mention the chosen state."""
        agent = _make_agent()

        df_1h = _make_df(close_base=72_000, sma_20=70_000, rsi_14=60.0)
        df_1d = _make_daily_df(close_base=72_000, week_change_pct=5.0)

        state, _, reasoning, _ = agent._classify_market(
            df_1h, df_1d, current_price=72_000
        )

        assert state.value in reasoning
