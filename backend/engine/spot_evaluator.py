"""
SpotEvaluator — 현물 4전략 기반 선물 양방향 이밸류에이터.

현물 4전략(cis_momentum, bnf_deviation, donchian_channel, larry_williams)을
선물 롱+숏 시그널 소스로 사용한다.

4h 캔들 기반, SignalCombiner(SPOT_WEIGHTS)로 가중 투표.
BUY → 롱 진입 / 숏 청산, SELL → 숏 진입 / 롱 청산.

현물 라이브 성과: WR 80%, PF 8.85 (10일)
선물 롱온리 백테스트: +69.6% (365일, 3x)
"""

import time
import structlog
import pandas as pd

from core.enums import Direction, SignalType
from engine.direction_evaluator import DirectionDecision
from engine.position_state_tracker import PositionState
from exchange.data_models import Ticker
from services.market_data import MarketDataService
from strategies.base import BaseStrategy
from strategies.combiner import SignalCombiner

logger = structlog.get_logger(__name__)


class SpotEvaluator:
    """현물 4전략 기반 선물 양방향 이밸류에이터.

    4h 캔들 기반, SignalCombiner로 가중 투표.
    BUY → 롱 진입 / 숏 청산, SELL → 숏 진입 / 롱 청산.

    DirectionEvaluator 프로토콜을 구현한다:
    - evaluate(symbol, current_position, *, df_5m, df_1h) -> DirectionDecision
    - eval_interval_sec property
    """

    def __init__(
        self,
        strategies: list[BaseStrategy],
        combiner: SignalCombiner,
        market_data: MarketDataService,
        *,
        eval_interval: int = 300,
        min_confidence: float = 0.50,
        cooldown_hours: float = 60.0,
        sl_atr_mult: float = 5.0,
        tp_atr_mult: float = 14.0,
        trail_activation_atr_mult: float = 3.0,
        trail_stop_atr_mult: float = 1.5,
    ) -> None:
        self._strategies = strategies
        self._combiner = combiner
        self._market_data = market_data
        self._eval_interval = eval_interval
        self._min_confidence = min_confidence
        self._cooldown_sec = cooldown_hours * 3600
        self._sl_atr_mult = sl_atr_mult
        self._tp_atr_mult = tp_atr_mult
        self._trail_activation_atr_mult = trail_activation_atr_mult
        self._trail_stop_atr_mult = trail_stop_atr_mult
        # 방향별 독립 쿨다운
        self._long_cooldowns: dict[str, float] = {}  # symbol → timestamp
        self._short_cooldowns: dict[str, float] = {}  # symbol → timestamp

    @property
    def eval_interval_sec(self) -> int:
        return self._eval_interval

    async def evaluate(
        self,
        symbol: str,
        current_position: PositionState | None,
        *,
        df_5m: pd.DataFrame | None = None,
        df_1h: pd.DataFrame | None = None,
    ) -> DirectionDecision:
        """현물 4전략으로 양방향 진입/청산 판단.

        Args:
            symbol: 거래 심볼 (e.g., "BTC/USDT")
            current_position: 현재 포지션 상태 (없으면 None)
            df_5m: 사전 조회된 5분 캔들 (close/atr 추출용)
            df_1h: 사전 조회된 1시간 캔들 (미사용, 프로토콜 호환)

        Returns:
            DirectionDecision: open/close/hold 결정
        """
        # 1. 4h 캔들 + 인디케이터 fetch (현물 전략의 required_timeframe)
        df_4h = await self._fetch_candles(symbol, "4h", 100)
        if df_4h is None or len(df_4h) < 30:
            return _hold_decision("candle_error", "spot_eval")

        # 2. ticker 조회 (전략 analyze() 인터페이스 요구)
        ticker = await self._fetch_ticker(symbol)
        if ticker is None:
            return _hold_decision("ticker_error", "spot_eval")

        # 3. 4전략 시그널 수집
        signals = []
        for strategy in self._strategies:
            try:
                signal = await strategy.analyze(df_4h, ticker)
                signals.append(signal)
            except Exception as e:
                logger.warning(
                    "spot_eval_strategy_error",
                    strategy=strategy.name,
                    symbol=symbol,
                    error=str(e),
                )

        if not signals:
            return _hold_decision("no_signals", "spot_eval")

        # 4. SignalCombiner로 결합
        combined = self._combiner.combine(signals, symbol=symbol)

        # 5. 5m 캔들에서 close/atr 추출 (Tier1Manager가 재사용)
        last_close, last_atr = self._extract_close_atr(df_5m, df_4h)

        # 6. 방향 매핑
        current_dir = current_position.direction if current_position else None
        confidence = combined.combined_confidence

        if current_dir == Direction.LONG:
            # 롱 보유 중: SELL 시그널이면 청산
            if (
                combined.action == SignalType.SELL
                and confidence >= self._min_confidence
            ):
                self.set_cooldown(symbol, Direction.LONG)
                return DirectionDecision(
                    action="close",
                    direction=None,
                    confidence=confidence,
                    sizing_factor=0.0,
                    stop_loss_atr=0.0,
                    take_profit_atr=0.0,
                    reason=f"spot_sell: {combined.final_reason}",
                    strategy_name=self._top_strategy(combined),
                    indicators={"close": last_close, "atr": last_atr},
                )
            return _hold_decision(
                f"spot_long_hold: {combined.final_reason}",
                self._top_strategy(combined),
                {"close": last_close, "atr": last_atr},
            )

        if current_dir == Direction.SHORT:
            # 숏 보유 중: BUY 시그널이면 청산
            if combined.action == SignalType.BUY and confidence >= self._min_confidence:
                self.set_cooldown(symbol, Direction.SHORT)
                return DirectionDecision(
                    action="close",
                    direction=None,
                    confidence=confidence,
                    sizing_factor=0.0,
                    stop_loss_atr=0.0,
                    take_profit_atr=0.0,
                    reason=f"spot_buy_close_short: {combined.final_reason}",
                    strategy_name=self._top_strategy(combined),
                    indicators={"close": last_close, "atr": last_atr},
                )
            return _hold_decision(
                f"spot_short_hold: {combined.final_reason}",
                self._top_strategy(combined),
                {"close": last_close, "atr": last_atr},
            )

        if current_position is None:
            # 포지션 없음: BUY 시그널이면 롱 진입
            if combined.action == SignalType.BUY and confidence >= self._min_confidence:
                if self._in_cooldown(symbol, Direction.LONG):
                    logger.debug(
                        "spot_long_cooldown",
                        symbol=symbol,
                        confidence=confidence,
                    )
                    return _hold_decision(
                        f"spot_long_cooldown: {combined.final_reason}",
                        self._top_strategy(combined),
                        {"close": last_close, "atr": last_atr},
                    )
                return DirectionDecision(
                    action="open",
                    direction=Direction.LONG,
                    confidence=confidence,
                    sizing_factor=min(confidence, 1.0),
                    stop_loss_atr=self._sl_atr_mult,
                    take_profit_atr=self._tp_atr_mult,
                    reason=f"spot_buy: {combined.final_reason}",
                    strategy_name=self._top_strategy(combined),
                    indicators={
                        "close": last_close,
                        "atr": last_atr,
                        "trailing_activation_atr": self._trail_activation_atr_mult,
                        "trailing_stop_atr": self._trail_stop_atr_mult,
                        "_signals": signals,
                        "_candle_row": df_4h.iloc[-1]
                        if df_4h is not None and len(df_4h) > 0
                        else None,
                        "_combined_confidence": confidence,
                    },
                )

            # 포지션 없음: SELL 시그널이면 숏 진입
            if (
                combined.action == SignalType.SELL
                and confidence >= self._min_confidence
            ):
                if self._in_cooldown(symbol, Direction.SHORT):
                    logger.debug(
                        "spot_short_cooldown",
                        symbol=symbol,
                        confidence=confidence,
                    )
                    return _hold_decision(
                        f"spot_short_cooldown: {combined.final_reason}",
                        self._top_strategy(combined),
                        {"close": last_close, "atr": last_atr},
                    )
                return DirectionDecision(
                    action="open",
                    direction=Direction.SHORT,
                    confidence=confidence,
                    sizing_factor=min(confidence, 1.0),
                    stop_loss_atr=self._sl_atr_mult,
                    take_profit_atr=self._tp_atr_mult,
                    reason=f"spot_sell_short: {combined.final_reason}",
                    strategy_name=self._top_strategy(combined),
                    indicators={
                        "close": last_close,
                        "atr": last_atr,
                        "trailing_activation_atr": self._trail_activation_atr_mult,
                        "trailing_stop_atr": self._trail_stop_atr_mult,
                        "_signals": signals,
                        "_candle_row": df_4h.iloc[-1]
                        if df_4h is not None and len(df_4h) > 0
                        else None,
                        "_combined_confidence": confidence,
                    },
                )

        # 시그널 미달 → hold
        return _hold_decision(
            f"spot_no_action: {combined.final_reason}",
            self._top_strategy(combined),
            {"close": last_close, "atr": last_atr},
        )

    def set_cooldown(self, symbol: str, direction: Direction | None = None) -> None:
        """종목별 방향별 쿨다운 설정 (외부에서 포지션 청산 시 호출).

        direction이 None이면 양방향 모두 쿨다운 설정 (후방 호환).
        """
        now = time.time()
        if direction is None or direction == Direction.LONG:
            self._long_cooldowns[symbol] = now
        if direction is None or direction == Direction.SHORT:
            self._short_cooldowns[symbol] = now

    def _in_cooldown(self, symbol: str, direction: Direction) -> bool:
        """종목별 방향별 쿨다운 체크."""
        cooldowns = (
            self._long_cooldowns
            if direction == Direction.LONG
            else self._short_cooldowns
        )
        last_exit = cooldowns.get(symbol, 0)
        elapsed = time.time() - last_exit
        if elapsed < self._cooldown_sec:
            remaining_h = (self._cooldown_sec - elapsed) / 3600
            logger.debug(
                "spot_eval_cooldown_active",
                symbol=symbol,
                direction=direction.value,
                remaining_h=f"{remaining_h:.1f}",
            )
            return True
        return False

    async def _fetch_candles(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
    ) -> pd.DataFrame | None:
        try:
            return await self._market_data.get_ohlcv_df(symbol, timeframe, limit)
        except Exception as e:
            logger.warning(
                "spot_eval_candle_error",
                symbol=symbol,
                tf=timeframe,
                error=str(e),
            )
            return None

    async def _fetch_ticker(self, symbol: str) -> Ticker | None:
        try:
            return await self._market_data.get_ticker(symbol)
        except Exception as e:
            logger.warning(
                "spot_eval_ticker_error",
                symbol=symbol,
                error=str(e),
            )
            return None

    @staticmethod
    def _extract_close_atr(
        df_5m: pd.DataFrame | None,
        df_4h: pd.DataFrame | None,
    ) -> tuple[float, float]:
        """5m 캔들에서 close/atr 추출. 없으면 4h 캔들에서 fallback.

        Tier1Manager._open_position_from_decision()가 indicators에서
        close/atr를 재사용하므로 반드시 유효한 값을 제공해야 한다.
        """
        # 5m 캔들 우선 (Tier1Manager가 SL/TP 체크에 5m close를 사용)
        if df_5m is not None and len(df_5m) > 0:
            close = (
                float(df_5m["close"].iloc[-1])
                if "close" in df_5m.columns and pd.notna(df_5m["close"].iloc[-1])
                else 0.0
            )
            atr = (
                float(df_5m["atr_14"].iloc[-1])
                if "atr_14" in df_5m.columns and pd.notna(df_5m["atr_14"].iloc[-1])
                else 0.0
            )
            if close > 0 and atr > 0:
                return close, atr

        # 4h 캔들 fallback
        if df_4h is not None and len(df_4h) > 0:
            close = (
                float(df_4h["close"].iloc[-1])
                if "close" in df_4h.columns and pd.notna(df_4h["close"].iloc[-1])
                else 0.0
            )
            atr = (
                float(df_4h["atr_14"].iloc[-1])
                if "atr_14" in df_4h.columns and pd.notna(df_4h["atr_14"].iloc[-1])
                else 0.0
            )
            if close > 0 and atr > 0:
                return close, atr

        return 0.0, 0.0

    @staticmethod
    def _top_strategy(combined) -> str:
        """CombinedDecision에서 최고 신뢰도 전략 이름 추출."""
        if combined.contributing_signals:
            top = max(
                combined.contributing_signals,
                key=lambda s: s.confidence,
            )
            return top.strategy_name
        return "spot_eval"


# 후방 호환 alias
SpotLongEvaluator = SpotEvaluator


def _hold_decision(
    reason: str,
    strategy_name: str,
    indicators: dict | None = None,
) -> DirectionDecision:
    """HOLD 결정 생성 헬퍼."""
    return DirectionDecision(
        action="hold",
        direction=None,
        confidence=0.0,
        sizing_factor=0.0,
        stop_loss_atr=0.0,
        take_profit_atr=0.0,
        reason=reason,
        strategy_name=strategy_name,
        indicators=indicators or {},
    )
