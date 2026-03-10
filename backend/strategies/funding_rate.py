"""
펀딩비 역방향 전략 (Funding Rate Contrarian).

과도한 펀딩비 = 과밀 포지션 → 역방향 진입.
- 펀딩비 > +0.03% (연 26%+): 숏 진입 (롱이 과밀)
- 펀딩비 < -0.03%: 롱 진입 (숏이 과밀)
- 중립: HOLD

펀딩비 데이터는 백테스트에서 OHLCV와 별도로 프리페치.
라이브에서는 8시간마다 갱신.
"""
import pandas as pd
from exchange.data_models import Ticker
from strategies.base import BaseStrategy, Signal
from strategies.registry import StrategyRegistry
from core.enums import SignalType


@StrategyRegistry.register
class FundingRateStrategy(BaseStrategy):
    """펀딩비 역방향 전략."""

    name = "funding_rate"
    display_name = "펀딩비 역방향"
    applicable_market_types = ["all"]
    default_coins = ["BTC/USDT", "ETH/USDT"]
    required_timeframe = "4h"
    min_candles_required = 10

    def __init__(
        self,
        high_threshold: float = 0.0003,   # +0.03% (연 26%+)
        extreme_threshold: float = 0.001,  # +0.1% (연 88%+)
        lookback: int = 3,                 # 최근 3개 펀딩비 확인
    ):
        self._high_threshold = high_threshold
        self._extreme_threshold = extreme_threshold
        self._lookback = lookback
        # 외부에서 주입하는 펀딩비 데이터 (symbol → Series)
        self._funding_data: dict[str, pd.Series] = {}

    def set_funding_data(self, symbol: str, funding_series: pd.Series):
        """백테스트/라이브에서 펀딩비 데이터 주입."""
        self._funding_data[symbol] = funding_series

    async def analyze(self, df: pd.DataFrame, ticker: Ticker) -> Signal:
        symbol = ticker.symbol
        funding = self._funding_data.get(symbol)

        if funding is None or len(funding) < self._lookback:
            return self._hold("펀딩비 데이터 없음")

        # 현재 시점에 가장 가까운 펀딩비 조회
        current_ts = df.index[-1] if hasattr(df.index, '__len__') else None
        if current_ts is not None:
            # 현재 시점 이전의 펀딩비만 사용 (미래 정보 방지)
            valid_funding = funding[funding.index <= current_ts]
            if len(valid_funding) < self._lookback:
                return self._hold("펀딩비 히스토리 부족")
            recent = valid_funding.iloc[-self._lookback:]
        else:
            recent = funding.iloc[-self._lookback:]

        avg_rate = float(recent.mean())
        current_rate = float(recent.iloc[-1])

        indicators = {
            "current_rate": round(current_rate * 100, 4),
            "avg_rate_3": round(avg_rate * 100, 4),
            "annualized_pct": round(avg_rate * 3 * 365 * 100, 1),  # 8h × 3 = 1d
        }

        # 극단적 펀딩비: 강한 시그널
        if avg_rate >= self._extreme_threshold:
            return Signal(
                signal_type=SignalType.SELL,
                confidence=0.85,
                strategy_name=self.name,
                reason=f"극단적 양 펀딩비: {avg_rate*100:.3f}% "
                f"(연 {indicators['annualized_pct']:.0f}%). 롱 과밀 → 숏",
                indicators=indicators,
            )

        if avg_rate <= -self._extreme_threshold:
            return Signal(
                signal_type=SignalType.BUY,
                confidence=0.85,
                strategy_name=self.name,
                reason=f"극단적 음 펀딩비: {avg_rate*100:.3f}% "
                f"(연 {indicators['annualized_pct']:.0f}%). 숏 과밀 → 롱",
                indicators=indicators,
            )

        # 높은 펀딩비: 중간 시그널
        if avg_rate >= self._high_threshold:
            conf = 0.60 + min(0.20, (avg_rate - self._high_threshold) / self._high_threshold * 0.20)
            return Signal(
                signal_type=SignalType.SELL,
                confidence=round(min(conf, 0.80), 2),
                strategy_name=self.name,
                reason=f"높은 양 펀딩비: {avg_rate*100:.3f}% → 숏 유리",
                indicators=indicators,
            )

        if avg_rate <= -self._high_threshold:
            conf = 0.60 + min(0.20, (abs(avg_rate) - self._high_threshold) / self._high_threshold * 0.20)
            return Signal(
                signal_type=SignalType.BUY,
                confidence=round(min(conf, 0.80), 2),
                strategy_name=self.name,
                reason=f"높은 음 펀딩비: {avg_rate*100:.3f}% → 롱 유리",
                indicators=indicators,
            )

        return self._hold(
            f"중립 펀딩비: {avg_rate*100:.3f}%",
            indicators=indicators,
        )

    def _hold(self, reason: str, indicators: dict | None = None) -> Signal:
        return Signal(
            signal_type=SignalType.HOLD,
            confidence=0.0,
            strategy_name=self.name,
            reason=reason,
            indicators=indicators or {},
        )

    def get_params(self) -> dict:
        return {
            "high_threshold": self._high_threshold,
            "extreme_threshold": self._extreme_threshold,
            "lookback": self._lookback,
        }

    def set_params(self, params: dict) -> None:
        for key in self.get_params():
            if key in params:
                setattr(self, f"_{key}", params[key])
