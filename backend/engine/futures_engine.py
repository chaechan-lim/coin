"""
바이낸스 USDM 선물 트레이딩 엔진
=================================
TradingEngine 서브클래스 — 70% 코드 재사용, 선물 전용 로직만 오버라이드.
- 롱/숏 양방향 매매
- 레버리지 포지션 사이징
- 청산가 근접 긴급 청산
- 숏 SL/TP 반전 로직
"""
import asyncio
import math
import structlog
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from config import AppConfig
from core.enums import SignalType, MarketState
from core.models import Position
from exchange.base import ExchangeAdapter
from services.market_data import MarketDataService
from strategies.base import Signal
from strategies.combiner import SignalCombiner, CombinedDecision
from engine.order_manager import OrderManager
from engine.portfolio_manager import PortfolioManager
from engine.trading_engine import TradingEngine, PositionTracker
from core.event_bus import emit_event

logger = structlog.get_logger(__name__)

# 선물 전용 SL/TP: 레버리지에 따라 자동 축소
_FUTURES_DEFAULT_SL_PCT = 5.0
_FUTURES_DEFAULT_TP_PCT = 10.0
_FUTURES_TRAILING_ACTIVATION = 3.0
_FUTURES_TRAILING_STOP = 3.0


class BinanceFuturesEngine(TradingEngine):
    """Binance USDM 선물 전용 엔진 (TradingEngine 서브클래스)."""

    def __init__(
        self,
        config: AppConfig,
        exchange: ExchangeAdapter,
        market_data: MarketDataService,
        order_manager: OrderManager,
        portfolio_manager: PortfolioManager,
        combiner: SignalCombiner,
        agent_coordinator=None,
    ):
        super().__init__(
            config=config,
            exchange=exchange,
            market_data=market_data,
            order_manager=order_manager,
            portfolio_manager=portfolio_manager,
            combiner=combiner,
            agent_coordinator=agent_coordinator,
            exchange_name="binance_futures",
        )
        self._leverage = config.binance.default_leverage
        self._max_leverage = config.binance.max_leverage
        self._futures_fee = config.binance.futures_fee
        self._funding_rates: dict[str, float] = {}
        self._last_funding_update: datetime | None = None

    async def initialize(self) -> None:
        """Initialize strategies + 추적 심볼 레버리지 설정."""
        await super().initialize()

        # 바이낸스 선물 전용 tracked_coins 사용
        tracked = self._config.binance.tracked_coins
        for symbol in tracked:
            try:
                await self._exchange.set_leverage(symbol, self._leverage)
                logger.info("futures_leverage_set", symbol=symbol, leverage=self._leverage)
            except Exception as e:
                logger.warning("leverage_set_failed", symbol=symbol, error=str(e))

    @property
    def tracked_coins(self) -> list[str]:
        """선물용 tracked_coins (BTC/USDT, ETH/USDT 등)."""
        return self._config.binance.tracked_coins

    async def _evaluation_cycle(self) -> None:
        """선물 평가 루프 — 로테이션(서지) 로직 제외, 펀딩비 업데이트 추가."""
        from db.session import get_session_factory

        self._reset_daily_counter()

        session_factory = get_session_factory()
        async with session_factory() as session:
            try:
                # 시장 상태 업데이트 (BTC/USDT 기준)
                await self._maybe_update_market_state(session)

                # 현금 잔고 보정
                await self._portfolio_manager.reconcile_cash_from_db(session)

                # 추적 코인 + 보유 중인 포지션 합집합
                tracked = set(self.tracked_coins)
                result = await session.execute(
                    select(Position.symbol).where(
                        Position.quantity > 0,
                        Position.exchange == self._exchange_name,
                    )
                )
                held = {r[0] for r in result.all()}
                coins_to_eval = tracked | held

                for symbol in coins_to_eval:
                    try:
                        await self._evaluate_futures_coin(session, symbol)
                    except Exception as e:
                        logger.error("futures_eval_error", symbol=symbol, error=str(e))

                # 펀딩비 업데이트 (30분마다)
                await self._maybe_update_funding_rates()

                # 스냅샷 (DB locked 재시도)
                for _attempt in range(3):
                    try:
                        await self._portfolio_manager.take_snapshot(session)
                        await session.commit()
                        break
                    except Exception as snap_err:
                        if "database is locked" in str(snap_err) and _attempt < 2:
                            await session.rollback()
                            await asyncio.sleep(1)
                        else:
                            raise

                # WebSocket broadcast
                if self._broadcast_callback:
                    summary = await self._portfolio_manager.get_portfolio_summary(session)
                    await self._broadcast_callback({
                        "event": "portfolio_update",
                        "exchange": "binance_futures",
                        "data": summary,
                    })

            except Exception as e:
                logger.error("futures_cycle_error", error=str(e), exc_info=True)
                await session.rollback()

    async def _evaluate_futures_coin(self, session: AsyncSession, symbol: str) -> None:
        """선물 코인 평가: SL/TP + 청산가 체크 + 양방향 매매."""
        # 포지션 조회
        result = await session.execute(
            select(Position).where(
                Position.symbol == symbol,
                Position.quantity > 0,
                Position.exchange == self._exchange_name,
            )
        )
        position = result.scalar_one_or_none()

        # 포지션 있으면 SL/TP/청산가 체크
        if position:
            sold = await self._check_futures_stop_conditions(session, symbol, position)
            if sold:
                return

        # 전략 시그널 수집 + 결합
        signals = await self._collect_signals(symbol)
        if not signals:
            return

        decision = self._combiner.combine(signals, self._market_state)
        if decision.action == SignalType.HOLD:
            return

        await self._process_futures_decision(session, symbol, decision, signals, position)

    async def _check_futures_stop_conditions(
        self, session: AsyncSession, symbol: str, position: Position
    ) -> bool:
        """선물 SL/TP/청산가 체크 — 숏은 방향 반전."""
        tracker = self._position_trackers.get(symbol)
        if not tracker:
            # 트래커 복원
            lev = position.leverage or self._leverage
            sqrt_lev = math.sqrt(lev)
            sl_pct = _FUTURES_DEFAULT_SL_PCT / sqrt_lev
            tp_pct = _FUTURES_DEFAULT_TP_PCT / sqrt_lev
            trail_act = _FUTURES_TRAILING_ACTIVATION / sqrt_lev
            trail_stop = _FUTURES_TRAILING_STOP / sqrt_lev
            tracker = PositionTracker(
                entry_price=position.average_buy_price,
                highest_price=position.average_buy_price,
                stop_loss_pct=sl_pct,
                take_profit_pct=tp_pct,
                trailing_activation_pct=trail_act,
                trailing_stop_pct=trail_stop,
            )
            if position.entered_at:
                ea = position.entered_at
                if ea.tzinfo is None:
                    ea = ea.replace(tzinfo=timezone.utc)
                tracker.entered_at = ea
            self._position_trackers[symbol] = tracker

        try:
            price = await self._market_data.get_current_price(symbol)
        except Exception:
            return False

        direction = position.direction or "long"
        entry = tracker.entry_price

        # 1. 청산가 근접 체크 (2% 이내 → 긴급 청산)
        if position.liquidation_price and position.liquidation_price > 0:
            liq = position.liquidation_price
            if direction == "long" and price <= liq * 1.02:
                await self._close_position(session, symbol, position, price,
                                           f"긴급 청산: 롱 청산가 근접 (청산가 {liq:.2f}, 현재 {price:.2f})")
                return True
            elif direction == "short" and price >= liq * 0.98:
                await self._close_position(session, symbol, position, price,
                                           f"긴급 청산: 숏 청산가 근접 (청산가 {liq:.2f}, 현재 {price:.2f})")
                return True

        # PnL 계산 (방향별)
        if direction == "long":
            pnl_pct = (price - entry) / entry * 100
            if price > tracker.highest_price:
                tracker.highest_price = price
        else:  # short
            pnl_pct = (entry - price) / entry * 100
            # 숏은 lowest_price 추적 (highest_price 변수를 lowest로 재활용)
            if price < tracker.highest_price:
                tracker.highest_price = price  # lowest price for short

        sell_reason = None

        # 트레일링 활성화
        if (tracker.trailing_activation_pct > 0
                and not tracker.trailing_active
                and pnl_pct >= tracker.trailing_activation_pct):
            tracker.trailing_active = True

        # 트레일링 스탑
        if tracker.trailing_active and tracker.trailing_stop_pct > 0:
            if direction == "long":
                drawdown = (tracker.highest_price - price) / tracker.highest_price * 100
            else:
                # 숏: lowest에서 올라간 비율
                drawdown = (price - tracker.highest_price) / tracker.highest_price * 100
            if drawdown >= tracker.trailing_stop_pct:
                sell_reason = f"Trailing Stop: {drawdown:.2f}% (수익 {pnl_pct:+.1f}%)"

        # SL
        if not sell_reason and pnl_pct <= -tracker.stop_loss_pct:
            sell_reason = f"SL: {pnl_pct:.2f}% (한도 -{tracker.stop_loss_pct:.1f}%)"

        # TP (트레일링 미활성 시)
        if (not sell_reason and not tracker.trailing_active
                and tracker.take_profit_pct > 0
                and pnl_pct >= tracker.take_profit_pct):
            sell_reason = f"TP: +{pnl_pct:.2f}% (목표 +{tracker.take_profit_pct:.1f}%)"

        if sell_reason:
            await self._close_position(session, symbol, position, price, sell_reason)
            return True

        return False

    async def _close_position(
        self, session: AsyncSession, symbol: str, position: Position,
        price: float, reason: str,
    ) -> None:
        """포지션 청산 (롱/숏 공통)."""
        direction = position.direction or "long"
        side = "sell" if direction == "long" else "buy"

        signal = Signal(
            strategy_name="futures_stop",
            signal_type=SignalType.SELL if direction == "long" else SignalType.BUY,
            confidence=1.0,
            reason=reason,
        )

        order = await self._order_manager.create_order(
            session, symbol, side, position.quantity, price, signal,
            order_type="market",
        )

        if order.status == "filled":
            await self._portfolio_manager.update_position_on_sell(
                session, symbol, position.quantity, price,
                position.quantity * price, order.fee
            )
            self._position_trackers.pop(symbol, None)
            logger.info("futures_position_closed", symbol=symbol, direction=direction, reason=reason)
            await emit_event("info", "trade",
                             f"선물 {direction} 청산: {symbol}",
                             metadata={"price": price, "reason": reason})

    async def _process_futures_decision(
        self, session: AsyncSession, symbol: str,
        decision: CombinedDecision, signals: list[Signal],
        position: Position | None,
    ) -> None:
        """선물 양방향 매매 처리."""
        if not self._can_trade(symbol, decision.action.value):
            return

        primary_signal = next(
            (s for s in signals if s.signal_type == decision.action), None
        )
        if not primary_signal:
            return

        try:
            ticker = await self._market_data.get_ticker(symbol)
            price = ticker.last
        except Exception:
            return

        # 최소 확신도
        bt = self._config.binance_trading
        min_conf = bt.min_combined_confidence
        if self._market_confidence < 0.35:
            min_conf += 0.10

        if decision.combined_confidence < min_conf:
            return

        direction = position.direction if position else None

        if decision.action == SignalType.BUY:
            if position and direction == "short":
                # 숏 → BUY 시그널 = 숏 청산
                await self._close_position(session, symbol, position, price,
                                           f"전략 BUY → 숏 청산 (conf={decision.combined_confidence:.2f})")
            elif not position:
                # 롱 진입
                await self._open_long(session, symbol, price, primary_signal, decision)

        elif decision.action == SignalType.SELL:
            if position and direction == "long":
                # 롱 → SELL 시그널 = 롱 청산
                await self._close_position(session, symbol, position, price,
                                           f"전략 SELL → 롱 청산 (conf={decision.combined_confidence:.2f})")
            elif not position:
                # 숏 진입 (downtrend/crash만 허용)
                if self._market_state in (MarketState.DOWNTREND.value, MarketState.CRASH.value):
                    await self._open_short(session, symbol, price, primary_signal, decision)

    async def _open_long(
        self, session: AsyncSession, symbol: str, price: float,
        signal: Signal, decision: CombinedDecision,
    ) -> None:
        """롱 포지션 진입."""
        cash = self._portfolio_manager.cash_balance
        bt = self._config.binance_trading
        size_pct = bt.max_trade_size_pct

        # 시장 상태별 사이징
        if self._market_state == MarketState.CRASH.value:
            size_pct *= 0.25
        elif self._market_state == MarketState.DOWNTREND.value:
            size_pct *= 0.5

        margin = cash * size_pct
        notional = margin * self._leverage
        amount = notional / price

        # 수수료 마진 (0.04%)
        margin_with_fee = margin * (1 + self._futures_fee)
        if margin_with_fee > cash or margin < 1.0:
            return

        order = await self._order_manager.create_order(
            session, symbol, "buy", amount, price, signal, decision,
            order_type="market",
        )

        if order.status != "filled":
            if order.exchange_order_id:
                try:
                    await self._order_manager.cancel_order_by_id(session, order.id)
                except Exception:
                    pass
            return

        await self._portfolio_manager.update_position_on_buy(
            session, symbol, amount, price, margin, order.fee
        )

        # 선물 전용 필드 업데이트
        result = await session.execute(
            select(Position).where(
                Position.symbol == symbol,
                Position.exchange == self._exchange_name,
            )
        )
        pos = result.scalar_one_or_none()
        if pos:
            pos.direction = "long"
            pos.leverage = self._leverage
            pos.liquidation_price = price * (1 - 1 / self._leverage + self._futures_fee)
            pos.margin_used = margin

        # SL/TP 트래커 — 레버리지 축소
        sqrt_lev = math.sqrt(self._leverage)
        self._position_trackers[symbol] = PositionTracker(
            entry_price=price,
            highest_price=price,
            stop_loss_pct=_FUTURES_DEFAULT_SL_PCT / sqrt_lev,
            take_profit_pct=_FUTURES_DEFAULT_TP_PCT / sqrt_lev,
            trailing_activation_pct=_FUTURES_TRAILING_ACTIVATION / sqrt_lev,
            trailing_stop_pct=_FUTURES_TRAILING_STOP / sqrt_lev,
        )

        self._daily_buy_count += 1
        self._daily_coin_buy_count[symbol] = self._daily_coin_buy_count.get(symbol, 0) + 1

        logger.info(
            "futures_long_opened", symbol=symbol, price=price,
            leverage=self._leverage, margin=round(margin, 2),
        )
        await emit_event("info", "trade", f"선물 롱: {symbol}",
                         metadata={"price": price, "leverage": self._leverage})

    async def _open_short(
        self, session: AsyncSession, symbol: str, price: float,
        signal: Signal, decision: CombinedDecision,
    ) -> None:
        """숏 포지션 진입."""
        cash = self._portfolio_manager.cash_balance
        bt = self._config.binance_trading
        size_pct = bt.max_trade_size_pct

        margin = cash * size_pct
        notional = margin * self._leverage
        amount = notional / price

        margin_with_fee = margin * (1 + self._futures_fee)
        if margin_with_fee > cash or margin < 1.0:
            return

        order = await self._order_manager.create_order(
            session, symbol, "sell", amount, price, signal, decision,
            order_type="market",
        )

        if order.status != "filled":
            if order.exchange_order_id:
                try:
                    await self._order_manager.cancel_order_by_id(session, order.id)
                except Exception:
                    pass
            return

        # 숏 매도 → 포지션 생성 (margin을 투자금으로 기록)
        await self._portfolio_manager.update_position_on_buy(
            session, symbol, amount, price, margin, order.fee
        )

        result = await session.execute(
            select(Position).where(
                Position.symbol == symbol,
                Position.exchange == self._exchange_name,
            )
        )
        pos = result.scalar_one_or_none()
        if pos:
            pos.direction = "short"
            pos.leverage = self._leverage
            pos.liquidation_price = price * (1 + 1 / self._leverage - self._futures_fee)
            pos.margin_used = margin

        # 숏 트래커 — highest_price를 lowest로 사용
        sqrt_lev = math.sqrt(self._leverage)
        self._position_trackers[symbol] = PositionTracker(
            entry_price=price,
            highest_price=price,  # Will track lowest
            stop_loss_pct=_FUTURES_DEFAULT_SL_PCT / sqrt_lev,
            take_profit_pct=_FUTURES_DEFAULT_TP_PCT / sqrt_lev,
            trailing_activation_pct=_FUTURES_TRAILING_ACTIVATION / sqrt_lev,
            trailing_stop_pct=_FUTURES_TRAILING_STOP / sqrt_lev,
        )

        self._daily_buy_count += 1
        self._daily_coin_buy_count[symbol] = self._daily_coin_buy_count.get(symbol, 0) + 1

        logger.info(
            "futures_short_opened", symbol=symbol, price=price,
            leverage=self._leverage, margin=round(margin, 2),
        )
        await emit_event("info", "trade", f"선물 숏: {symbol}",
                         metadata={"price": price, "leverage": self._leverage})

    async def _maybe_update_funding_rates(self) -> None:
        """펀딩비 조회 (30분 간격)."""
        now = datetime.now(timezone.utc)
        if self._last_funding_update and (now - self._last_funding_update).total_seconds() < 1800:
            return
        try:
            for symbol in self.tracked_coins:
                rate = await self._exchange.fetch_funding_rate(symbol)
                self._funding_rates[symbol] = rate
            self._last_funding_update = now
            logger.info("funding_rates_updated", rates=self._funding_rates)
        except Exception as e:
            logger.warning("funding_rate_fetch_failed", error=str(e))

    async def _maybe_update_market_state(self, session: AsyncSession) -> None:
        """BTC/USDT 기준 시장 상태 감지."""
        now = datetime.now(timezone.utc)
        if self._market_state_updated and (now - self._market_state_updated).total_seconds() < 1800:
            return
        try:
            # BTC/USDT 기준
            df = await self._market_data.get_candles("BTC/USDT", "4h", 200)
            state, conf = self._detect_market_state(df)
            self._market_state = state
            self._market_confidence = conf
            self._market_state_updated = now
            logger.info("futures_market_state", state=state, confidence=round(conf, 3))
        except Exception as e:
            logger.warning("futures_market_state_failed", error=str(e))

    async def _collect_signals(self, symbol: str) -> list[Signal]:
        """전략 시그널 수집."""
        signals = []
        for name, strategy in self._strategies.items():
            try:
                timeframe = getattr(strategy, "required_timeframe", "1h")
                df = await self._market_data.get_candles(symbol, timeframe, 200)
                if df is None or len(df) < 20:
                    continue
                signal = strategy.generate_signal(df, symbol)
                if signal:
                    signals.append(signal)
            except Exception as e:
                logger.debug("strategy_signal_error", strategy=name, symbol=symbol, error=str(e))
        return signals

    @property
    def rotation_status(self) -> dict:
        """선물 엔진에서는 로테이션 미지원 — 빈 상태 반환."""
        return {
            "rotation_enabled": False,
            "surge_threshold": 0,
            "market_state": self._market_state,
            "current_surge_symbol": None,
            "last_rotation_time": None,
            "last_scan_time": None,
            "rotation_cooldown_sec": 0,
            "tracked_coins": self.tracked_coins,
            "rotation_coins": [],
            "all_surge_scores": {},
        }
