import pandas as pd
from datetime import datetime, timezone
from exchange.data_models import Ticker
from strategies.base import BaseStrategy, Signal
from strategies.registry import StrategyRegistry
from core.enums import SignalType


@StrategyRegistry.register
class DCAMomentumStrategy(BaseStrategy):
    """
    Strategy 7: DCA + Momentum
    Periodic fixed-amount buys only when MA trend is upward.
    Long-term accumulation strategy that follows its own schedule.
    """

    name = "dca_momentum"
    display_name = "DCA + 모멘텀"
    applicable_market_types = ["all"]
    default_coins = ["BTC/KRW", "ETH/KRW"]
    required_timeframe = "1d"
    min_candles_required = 55

    def __init__(
        self,
        buy_amount_krw: float = 100_000,
        buy_interval_hours: int = 24,  # daily
        ma_period: int = 50,
        ma_slope_periods: int = 5,  # Check MA slope over last N periods
    ):
        self._buy_amount_krw = buy_amount_krw
        self._buy_interval_hours = buy_interval_hours
        self._ma_period = ma_period
        self._ma_slope_periods = ma_slope_periods
        self._last_buy_time: dict[str, datetime] = {}

    def _should_buy_now(self, symbol: str) -> bool:
        """Check if enough time has passed since last DCA buy."""
        now = datetime.now(timezone.utc)
        last = self._last_buy_time.get(symbol)
        if last is None:
            return True
        elapsed_hours = (now - last).total_seconds() / 3600
        return elapsed_hours >= self._buy_interval_hours

    async def analyze(self, df: pd.DataFrame, ticker: Ticker) -> Signal:
        symbol = ticker.symbol

        if len(df) < self.min_candles_required:
            return Signal(
                signal_type=SignalType.HOLD,
                confidence=0.0,
                strategy_name=self.name,
                reason="Insufficient data for DCA momentum analysis",
            )

        # Check if it's time for a DCA purchase
        if not self._should_buy_now(symbol):
            return Signal(
                signal_type=SignalType.HOLD,
                confidence=0.3,
                strategy_name=self.name,
                reason=f"DCA 대기: 다음 매수 주기 미도달 "
                f"(주기: {self._buy_interval_hours}시간)",
            )

        # Check MA trend
        ma_col = f"sma_{self._ma_period}"
        if ma_col not in df.columns:
            df[ma_col] = df["close"].rolling(self._ma_period).mean()

        current_ma = df[ma_col].iloc[-1]
        prev_ma = df[ma_col].iloc[-self._ma_slope_periods] if len(df) >= self._ma_slope_periods else df[ma_col].iloc[0]

        if pd.isna(current_ma) or pd.isna(prev_ma):
            return Signal(
                signal_type=SignalType.HOLD,
                confidence=0.0,
                strategy_name=self.name,
                reason="MA values not available for momentum check",
            )

        ma_slope_pct = (current_ma - prev_ma) / prev_ma * 100 if prev_ma > 0 else 0
        ma_is_rising = current_ma > prev_ma
        price_above_ma = ticker.last > current_ma

        indicators = {
            f"sma_{self._ma_period}": round(current_ma, 0),
            "ma_slope_pct": round(ma_slope_pct, 2),
            "ma_is_rising": ma_is_rising,
            "price_above_ma": price_above_ma,
            "current_price": ticker.last,
            "buy_amount_krw": self._buy_amount_krw,
        }

        if ma_is_rising:
            # MA is trending up → execute DCA buy
            amount = self._buy_amount_krw / ticker.last
            confidence = 0.6
            if price_above_ma:
                confidence += 0.1  # Price above MA = stronger trend
            if ma_slope_pct > 1.0:
                confidence += 0.1  # Strong upward slope

            return Signal(
                signal_type=SignalType.BUY,
                confidence=round(min(confidence, 0.9), 2),
                strategy_name=self.name,
                reason=f"DCA 매수 실행: MA{self._ma_period} 상승 중 "
                f"(기울기: {ma_slope_pct:+.2f}%). "
                f"{'가격이 MA 위' if price_above_ma else '가격이 MA 아래(할인 매수)'}. "
                f"매수 금액: {self._buy_amount_krw:,.0f}원",
                suggested_price=ticker.last,
                suggested_amount=amount,
                indicators=indicators,
            )

        # MA is flat or declining → skip this DCA period
        return Signal(
            signal_type=SignalType.HOLD,
            confidence=0.5,
            strategy_name=self.name,
            reason=f"DCA 보류: MA{self._ma_period} 하락/횡보 중 "
            f"(기울기: {ma_slope_pct:+.2f}%). "
            f"상승 추세 전환 시까지 대기",
            indicators=indicators,
        )

    def record_buy(self, symbol: str) -> None:
        """Record that a DCA buy was executed."""
        self._last_buy_time[symbol] = datetime.now(timezone.utc)

    def get_params(self) -> dict:
        return {
            "buy_amount_krw": self._buy_amount_krw,
            "buy_interval_hours": self._buy_interval_hours,
            "ma_period": self._ma_period,
            "ma_slope_periods": self._ma_slope_periods,
        }

    def set_params(self, params: dict) -> None:
        for key in ["buy_amount_krw", "buy_interval_hours", "ma_period", "ma_slope_periods"]:
            if key in params:
                setattr(self, f"_{key}", params[key])
