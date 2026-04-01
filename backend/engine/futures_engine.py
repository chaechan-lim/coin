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
import pandas as pd
from datetime import datetime, timezone
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
from engine.trading_engine import TradingEngine, PositionTracker, _effective_direction
from core.event_bus import emit_event

logger = structlog.get_logger(__name__)

# 선물 전용 SL/TP: 레버리지에 따라 자동 축소 (P1 최적화: 4h 기반)
_FUTURES_DEFAULT_SL_PCT = 8.0
_FUTURES_DEFAULT_TP_PCT = 16.0
_FUTURES_TRAILING_ACTIVATION = 5.0
_FUTURES_TRAILING_STOP = 3.5

# 동적 SL 프로필 (ATR 기반): (atr_multiplier, floor_pct, cap_pct)
_DYNAMIC_SL_PROFILES = {
    "strong_uptrend": (2.5, 4.0, 12.0),
    "uptrend":        (2.0, 4.0, 10.0),
    "sideways":       (2.0, 4.0,  7.0),
    "downtrend":      (2.0, 4.0,  7.0),
    "crash":          (1.5, 3.0,  5.0),
}
_FUTURES_TIMEFRAME = "4h"  # 전략 평가 타임프레임 (4h: P1 최적화, 1h→4h 복원 — 1h PF 0.76 vs 4h PF 0.98)

# ATR 적응형 리스크 조절: 차단 대신 레버리지/마진 축소
# ATR% 구간별: ~5% 기본, 5~10% 마진축소, 10~20% 레버리지↓, 20%+ 레버리지1x+마진축소
_ATR_RISK_TIERS = (
    # (threshold, margin_mult, lev_override)  — 양방향 적응형 v2 (보수적)
    (2.0,  1.2, None),   # ATR ≤ 2%: 초안정 → 마진 120%, 레버리지 유지
    (3.0,  1.1, None),   # ATR 2~3%: 안정 → 마진 110%, 레버리지 유지
    (5.0,  1.0, None),   # ATR 3~5%: 기본 (마진 100%, 레버리지 유지)
    (10.0, 0.7, None),   # ATR 5~10%: 변동 → 마진 70%, 레버리지 유지
    (20.0, 0.5, 2),      # ATR 10~20%: 고변동 → 마진 50%, 레버리지 2x
    (999,  0.3, 1),      # ATR 20%+: 극단 → 마진 30%, 레버리지 1x
)


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
        self._maintenance_margin_rate = config.binance.maintenance_margin_rate
        self._funding_rates: dict[str, float] = {}
        self._last_funding_update: datetime | None = None
        # 동적 종목 선정 (비활성 — 추적 코인만 사용)
        self._dynamic_coins: list[str] = []

        # 연속 평가 오류 카운터 — N회 연속 실패 시 포지션 강제 청산
        self._eval_error_counts: dict[str, int] = {}
        self._MAX_EVAL_ERRORS = 3  # 3회 연속 (~15분) → 강제 청산

        # 마지막 평가 시각 (rotation_status에서 last_scan_time으로 노출)
        self._last_eval_time: datetime | None = None

        # ML Signal Filter용 캔들 캐시
        self._latest_candle_rows: dict[str, pd.Series] = {}

        # ML Signal Filter (선택적 — 모델 파일 존재 시 활성)
        self._ml_filter = None
        try:
            from strategies.ml_filter import MLSignalFilter, MODEL_DIR
            model_path = MODEL_DIR / "signal_filter.pkl"
            if model_path.exists():
                self._ml_filter = MLSignalFilter(min_win_prob=0.52)
                self._ml_filter.load(str(model_path))
                logger.info("ml_filter_loaded", model_path=str(model_path))
        except Exception as e:
            logger.warning("ml_filter_load_failed", error=str(e))

        # WebSocket 가격 모니터
        self._monitor_task: asyncio.Task | None = None
        self._eval_task: asyncio.Task | None = None
        self._close_lock = asyncio.Lock()  # 모니터/평가 동시 청산 방지

    async def initialize(self) -> None:
        """Initialize strategies + 추적 심볼 레버리지 설정."""
        await super().initialize()

        # 바이낸스 선물 전용 tracked_coins 레버리지 설정
        tracked = self._ec.tracked_coins
        for symbol in tracked:
            try:
                await self._exchange.set_leverage(symbol, self._leverage)
                logger.info("futures_leverage_set", symbol=symbol, leverage=self._leverage)
            except Exception as e:
                logger.warning("leverage_set_failed", symbol=symbol, error=str(e))


    # ── 좀비 포지션 정리 ────────────────────────────────────────

    async def _close_zombie_positions(self) -> None:
        """비추적 심볼의 잔여 포지션 자동 청산 (동적 종목 선정 잔여물 등)."""
        from db.session import get_session_factory
        tracked = set(self.tracked_coins)
        try:
            session_factory = get_session_factory()
            async with session_factory() as session:
                result = await session.execute(
                    select(Position).where(
                        Position.quantity > 0,
                        Position.exchange == self._exchange_name,
                    )
                )
                positions = result.scalars().all()
                for pos in positions:
                    if pos.symbol in tracked or pos.is_surge:
                        continue
                    await self._close_single_zombie(session, pos)
        except Exception as e:
            logger.warning("zombie_cleanup_skipped", error=str(e))

    async def _close_single_zombie(self, session, pos: Position) -> None:
        """단일 좀비 포지션 시장가 청산."""
        logger.warning("zombie_position_closing",
                       symbol=pos.symbol, direction=pos.direction,
                       quantity=pos.quantity, margin=round(pos.total_invested, 2))
        try:
            direction = pos.direction or "long"
            side = "sell" if direction == "long" else "buy"
            signal = Signal(
                strategy_name="zombie_cleanup",
                signal_type=SignalType.SELL if direction == "long" else SignalType.BUY,
                confidence=0.99,
                reason=f"비추적 심볼 자동 청산: {pos.symbol}",
            )
            order = await self._order_manager.create_order(
                session=session,
                symbol=pos.symbol,
                side=side,
                amount=pos.quantity,
                price=0,
                signal=signal,
                order_type="market",
                direction=direction,
                leverage=pos.leverage or self._leverage,
                entry_price=pos.average_buy_price,
            )
            exec_price = order.executed_price or pos.average_buy_price
            if direction == "long":
                pnl_pct = (exec_price - pos.average_buy_price) / pos.average_buy_price * 100
            else:
                pnl_pct = (pos.average_buy_price - exec_price) / pos.average_buy_price * 100
            lev = pos.leverage or self._leverage
            pnl_pct *= lev

            pos.quantity = 0
            pos.last_sell_at = datetime.now(timezone.utc)
            margin = pos.total_invested or pos.margin_used or 0
            fee = (exec_price * (order.executed_quantity or 0)) * 0.0004
            cost_return = margin * (1 + pnl_pct / 100) - fee
            async with self._portfolio_manager.cash_lock:
                self._portfolio_manager.cash_balance += cost_return
            pos.total_invested = 0
            pos.margin_used = 0

            await session.commit()
            logger.info("zombie_position_closed",
                        symbol=pos.symbol, pnl_pct=round(pnl_pct, 2))
            await emit_event(
                "info", "engine",
                f"좀비 포지션 청산: {pos.symbol} (비추적) PnL={pnl_pct:+.1f}%",
                metadata={"symbol": pos.symbol, "pnl_pct": round(pnl_pct, 2)},
            )
        except Exception as e:
            logger.error("zombie_close_failed",
                         symbol=pos.symbol, error=str(e))

    # ── 듀얼 루프 시작/중지 ─────────────────────────────────────

    _FAST_SL_INTERVAL = 30  # 선물 빠른 SL 체크 간격 (초) — WS 실패 시 폴백

    async def start(self) -> None:
        """Start futures engine: WebSocket price monitor + strategy eval loop."""
        self._is_running = True
        await self._restore_trade_timestamps()
        logger.info("engine_started", exchange=self._exchange_name)
        await emit_event("info", "engine", "선물 엔진 시작",
                         metadata={"mode": self._ec.mode})

        # 다운타임 중 SL/TP 초과 포지션 즉시 체크
        await self._check_downtime_stops()

        # 비추적 좀비 포지션 정리 (이전 동적 종목 선정 잔여)
        await self._close_zombie_positions()

        # WebSocket 가격 모니터 + 잔고 모니터 초기화
        ws_enabled = self._config.binance_trading.ws_price_monitor
        self._balance_task = None
        self._fast_sl_task = None
        if ws_enabled:
            try:
                await self._exchange.create_ws_exchange()
                self._monitor_task = asyncio.create_task(
                    self._price_monitor_loop(), name="futures_price_monitor"
                )
                self._balance_task = asyncio.create_task(
                    self._balance_monitor_loop(), name="futures_balance_monitor"
                )
                logger.info("price_monitor_started")
                logger.info("balance_monitor_started")
            except Exception as e:
                logger.warning("ws_init_failed_fallback_polling", error=str(e))
                self._monitor_task = None

        # WebSocket 미사용 또는 실패 시 → 30초 빠른 SL 폴백 루프
        if not self._monitor_task:
            self._fast_sl_task = asyncio.create_task(
                self._fast_stop_check_loop(), name="futures_fast_sl"
            )
            logger.info("futures_fast_sl_fallback_started")

        # 전략 평가 루프 (기존 5분 폴링)
        self._eval_task = asyncio.create_task(
            self._strategy_eval_loop(), name="futures_strategy_eval"
        )

        # Income API 펀딩비 폴링 (8시간 + 시작 시 즉시)
        self._income_task = asyncio.create_task(
            self._income_poll_loop(), name="futures_income_poll"
        )

        # 모든 태스크 대기
        tasks = [t for t in (self._monitor_task, self._eval_task,
                             self._balance_task, self._fast_sl_task,
                             self._income_task) if t]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _strategy_eval_loop(self) -> None:
        """기존 5분 주기 전략 평가 루프."""
        interval = self._ec.evaluation_interval_sec
        while self._is_running:
            try:
                await self._evaluation_cycle()
            except Exception as e:
                logger.error("engine_cycle_error", error=str(e), exc_info=True)
            await asyncio.sleep(interval)

    # WS 재연결 상수
    _WS_RECONNECT_MIN = 5      # 최소 대기 (초)
    _WS_RECONNECT_MAX = 300    # 최대 대기 (초)
    _WS_RECONNECT_FACTOR = 2   # 지수 배율

    async def _ws_reconnect(self, backoff: float) -> float:
        """WS 재연결 시도. 성공 시 backoff 리셋, 실패 시 증가된 backoff 반환."""
        wait = min(backoff, self._WS_RECONNECT_MAX)
        logger.info("ws_reconnect_attempt", wait_sec=wait)
        await asyncio.sleep(wait)
        try:
            await self._exchange.close_ws()
        except Exception:
            pass
        try:
            await self._exchange.create_ws_exchange()
            logger.info("ws_reconnected")
            return self._WS_RECONNECT_MIN  # 성공 → backoff 리셋
        except Exception as e:
            logger.warning("ws_reconnect_failed", error=str(e))
            return min(wait * self._WS_RECONNECT_FACTOR, self._WS_RECONNECT_MAX)

    async def _price_monitor_loop(self) -> None:
        """WebSocket으로 실시간 가격 수신 → 보유 포지션 SL/TP/청산가 체크.
        연결 끊김 시 자동 재연결 (지수 백오프)."""
        backoff = self._WS_RECONNECT_MIN
        consecutive_errors = 0
        while self._is_running:
            try:
                symbols = list(self._position_trackers.keys())
                if not symbols:
                    await asyncio.sleep(5)
                    continue
                tickers = await self._exchange.watch_tickers(symbols)
                consecutive_errors = 0  # 성공 → 에러 카운터 리셋
                backoff = self._WS_RECONNECT_MIN
                for symbol in symbols:
                    if symbol in tickers:
                        price = float(tickers[symbol].get("last", 0))
                        if price > 0:
                            await self._realtime_stop_check(symbol, price)
            except asyncio.CancelledError:
                break
            except Exception as e:
                consecutive_errors += 1
                logger.warning("price_monitor_error", error=str(e),
                               consecutive=consecutive_errors)

                # 3회 연속 실패 시 빠른 SL 폴백 시작
                if consecutive_errors >= 3:
                    if not getattr(self, '_fast_sl_task', None) or self._fast_sl_task.done():
                        self._fast_sl_task = asyncio.create_task(
                            self._fast_stop_check_loop(), name="futures_fast_sl"
                        )
                        logger.warning("futures_fast_sl_auto_started",
                                       reason="ws_error")

                    # WS 재연결 시도
                    backoff = await self._ws_reconnect(backoff)
                    if backoff == self._WS_RECONNECT_MIN:
                        # 재연결 성공 → 폴백 해제
                        if self._fast_sl_task and not self._fast_sl_task.done():
                            self._fast_sl_task.cancel()
                            try:
                                await self._fast_sl_task
                            except asyncio.CancelledError:
                                pass
                            self._fast_sl_task = None
                            logger.info("fast_sl_fallback_stopped", reason="ws_reconnected")
                        consecutive_errors = 0
                else:
                    await asyncio.sleep(3)

    async def _fast_stop_check_loop(self) -> None:
        """선물 빠른 SL/TP 체크 (30초 주기) — WebSocket 실패 시 폴백."""
        from db.session import get_session_factory
        while self._is_running:
            await asyncio.sleep(self._FAST_SL_INTERVAL)
            try:
                trackers = dict(self._position_trackers)
                if not trackers:
                    continue
                session_factory = get_session_factory()
                async with session_factory() as session:
                    for symbol in list(trackers.keys()):
                        try:
                            result = await session.execute(
                                select(Position).where(
                                    Position.symbol == symbol,
                                    Position.quantity > 0,
                                    Position.exchange == self._exchange_name,
                                )
                            )
                            position = result.scalar_one_or_none()
                            if not position:
                                continue
                            await self._check_futures_stop_conditions(
                                session, symbol, position)
                            await session.commit()
                        except Exception as e:
                            logger.debug("fast_stop_check_error",
                                         symbol=symbol, error=str(e))
            except Exception as e:
                logger.warning("futures_fast_stop_loop_error", error=str(e))

    _INCOME_POLL_INTERVAL = 8 * 3600  # 8시간마다 펀딩비 폴링

    async def _income_poll_loop(self) -> None:
        """Income API로 펀딩비를 주기적으로 가져와 내부 장부에 반영."""
        await asyncio.sleep(30)  # 엔진 초기화 대기
        try:
            await self._portfolio_manager.apply_income(self._exchange)
        except Exception as e:
            logger.warning("income_initial_fetch_error", error=str(e))
        while self._is_running:
            try:
                await asyncio.sleep(self._INCOME_POLL_INTERVAL)
                await self._portfolio_manager.apply_income(self._exchange)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("income_poll_error", error=str(e))

    async def _balance_monitor_loop(self) -> None:
        """WebSocket으로 선물 잔고+포지션 실시간 수신."""
        await asyncio.gather(
            self._ws_balance_loop(),
            self._ws_position_loop(),
            return_exceptions=True,
        )

    async def _ws_balance_loop(self) -> None:
        """잔고 실시간 감사 — 내부 장부 vs 거래소 잔고 차이 모니터링 (cash 갱신 안 함).
        연결 끊김 시 자동 재연결."""
        backoff = self._WS_RECONNECT_MIN
        consecutive_errors = 0
        while self._is_running:
            try:
                balance = await self._exchange.watch_balance()
                consecutive_errors = 0
                backoff = self._WS_RECONNECT_MIN
                usdt = balance.get("USDT", {})
                wallet_total = float(usdt.get("total", 0) or 0)
                margin_used = float(usdt.get("used", 0) or 0)
                exchange_cash = wallet_total - margin_used
                internal_cash = self._portfolio_manager.cash_balance

                if exchange_cash > 0:
                    diff = abs(exchange_cash - internal_cash)
                    if diff > 5.0 or (internal_cash > 0 and diff / internal_cash > 0.02):
                        logger.warning("ws_balance_discrepancy",
                                       internal=round(internal_cash, 2),
                                       exchange=round(exchange_cash, 2),
                                       diff=round(diff, 2))
            except asyncio.CancelledError:
                break
            except asyncio.TimeoutError:
                continue  # 잔고 변동 없음 — 정상 (watch_balance는 변동 시에만 반환)
            except Exception as e:
                consecutive_errors += 1
                logger.warning("ws_balance_audit_error", error=str(e),
                               consecutive=consecutive_errors)
                if consecutive_errors >= 3:
                    backoff = await self._ws_reconnect(backoff)
                    if backoff == self._WS_RECONNECT_MIN:
                        consecutive_errors = 0
                else:
                    await asyncio.sleep(5)

    async def _ws_position_loop(self) -> None:
        """포지션 실시간 수신 → DB 포지션 즉시 갱신 (margin, unrealizedPnl, 수량).
        연결 끊김 시 자동 재연결."""
        from db.session import get_session_factory
        backoff = self._WS_RECONNECT_MIN
        consecutive_errors = 0
        while self._is_running:
            try:
                positions = await self._exchange.watch_positions()
                if not positions:
                    continue
                session_factory = get_session_factory()
                async with session_factory() as session:
                    for fp in positions:
                        contracts = float(fp.get("contracts", 0) or 0)
                        sym = fp.get("symbol", "").replace(":USDT", "")
                        if not sym:
                            continue

                        result = await session.execute(
                            select(Position).where(
                                Position.symbol == sym,
                                Position.exchange == self._exchange_name,
                            )
                        )
                        db_pos = result.scalar_one_or_none()
                        if not db_pos:
                            continue

                        changed = False
                        margin = float(fp.get("initialMargin", 0) or 0)
                        entry = float(fp.get("entryPrice", 0) or 0)
                        liq = float(fp.get("liquidationPrice", 0) or 0) or None
                        unrealized = float(fp.get("unrealizedPnl", 0) or 0)

                        if contracts > 0 and abs(db_pos.quantity - contracts) / max(db_pos.quantity, 0.0001) > 0.01:
                            db_pos.quantity = contracts
                            changed = True
                        if margin > 0 and abs((db_pos.margin_used or 0) - margin) > 0.1:
                            db_pos.margin_used = margin
                            # total_invested는 진입 시 설정된 값 유지 (qty*entry_price)
                            # margin_used만 갱신 (실제 담보금)
                            changed = True
                        if entry > 0 and abs(db_pos.average_buy_price - entry) > 0.0001:
                            db_pos.average_buy_price = entry
                            changed = True
                        if liq and db_pos.liquidation_price != liq:
                            db_pos.liquidation_price = liq
                            changed = True
                        if contracts > 0 and entry > 0:
                            db_pos.current_price = float(fp.get("markPrice", 0) or entry)
                            db_pos.current_value = margin + unrealized

                        if changed:
                            await session.commit()
                            logger.debug("ws_position_updated", symbol=sym,
                                         margin=round(margin, 2), contracts=contracts)
                consecutive_errors = 0
                backoff = self._WS_RECONNECT_MIN
            except asyncio.CancelledError:
                break
            except asyncio.TimeoutError:
                continue  # 포지션 변동 없음 — 정상 (watch_positions는 변동 시에만 반환)
            except Exception as e:
                consecutive_errors += 1
                logger.warning("ws_position_error", error=str(e),
                               consecutive=consecutive_errors)
                if consecutive_errors >= 3:
                    backoff = await self._ws_reconnect(backoff)
                    if backoff == self._WS_RECONNECT_MIN:
                        consecutive_errors = 0
                else:
                    await asyncio.sleep(5)

    async def _realtime_stop_check(self, symbol: str, price: float) -> None:
        """경량 SL/TP/청산가 체크 (인메모리 트래커 기반, DB 조회 최소화)."""
        tracker = self._position_trackers.get(symbol)
        if not tracker:
            return

        # Position 방향 결정 — DB 없이 트래커에서 추론
        # 트래커가 있으면 반드시 포지션이 존재
        from db.session import get_session_factory

        # 빠른 판정: SL/TP 범위 밖인지 먼저 체크 (대부분 여기서 리턴)
        entry = tracker.entry_price
        if not entry or entry <= 0:
            return  # entry_price 미설정 — SL/TP 체크 불가
        # direction은 DB에서 가져와야 하지만, 비용을 줄이기 위해
        # _check_price_in_range로 빠른 필터링
        pnl_pct_long = (price - entry) / entry * 100
        # 롱 기준으로 SL/TP 범위 안이면 청산 불필요 (숏도 대칭이므로)
        if abs(pnl_pct_long) < tracker.stop_loss_pct and abs(pnl_pct_long) < tracker.take_profit_pct:
            # 트레일링/청산가는 체크해야 하지만, 대부분 해당 없음
            if not tracker.trailing_active:
                return

        # SL/TP 범위 진입 → DB 조회하여 정확한 청산 판정
        async with self._close_lock:
            session_factory = get_session_factory()
            async with session_factory() as session:
                result = await session.execute(
                    select(Position).where(
                        Position.symbol == symbol,
                        Position.quantity > 0,
                        Position.exchange == self._exchange_name,
                    )
                )
                position = result.scalar_one_or_none()
                if not position:
                    self._position_trackers.pop(symbol, None)
                    return

                direction = position.direction or "long"

                # 1. 청산가 근접 (2% 이내)
                if position.liquidation_price and position.liquidation_price > 0:
                    liq = position.liquidation_price
                    if direction == "long" and price <= liq * 1.02:
                        await self._close_position(
                            session, symbol, position, price,
                            f"[WS] 긴급 청산: 롱 청산가 근접 ({liq:.2f}, 현재 {price:.2f})"
                        )
                        await session.commit()
                        return
                    elif direction == "short" and price >= liq * 0.98:
                        await self._close_position(
                            session, symbol, position, price,
                            f"[WS] 긴급 청산: 숏 청산가 근접 ({liq:.2f}, 현재 {price:.2f})"
                        )
                        await session.commit()
                        return

                # 2. PnL 계산
                if direction == "long":
                    pnl_pct = (price - entry) / entry * 100
                    if price > tracker.extreme_price:
                        tracker.extreme_price = price
                else:
                    pnl_pct = (entry - price) / entry * 100
                    if price < tracker.extreme_price:
                        tracker.extreme_price = price

                sell_reason = None

                # 트레일링 활성화
                if (tracker.trailing_activation_pct > 0
                        and not tracker.trailing_active
                        and pnl_pct >= tracker.trailing_activation_pct):
                    tracker.trailing_active = True

                # 트레일링 스탑
                if tracker.trailing_active and tracker.trailing_stop_pct > 0:
                    if direction == "long":
                        drawdown = (tracker.extreme_price - price) / tracker.extreme_price * 100
                    else:
                        drawdown = (price - tracker.extreme_price) / tracker.extreme_price * 100
                    if drawdown >= tracker.trailing_stop_pct:
                        sell_reason = f"[WS] Trailing Stop: {drawdown:.2f}% (수익 {pnl_pct:+.1f}%)"

                # SL
                if not sell_reason and pnl_pct <= -tracker.stop_loss_pct:
                    sell_reason = f"[WS] SL: {pnl_pct:.2f}% (한도 -{tracker.stop_loss_pct:.1f}%)"

                # TP (트레일링 미활성 시)
                if (not sell_reason and not tracker.trailing_active
                        and tracker.take_profit_pct > 0
                        and pnl_pct >= tracker.take_profit_pct):
                    sell_reason = f"[WS] TP: +{pnl_pct:.2f}% (목표 +{tracker.take_profit_pct:.1f}%)"

                if sell_reason:
                    await self._close_position(session, symbol, position, price, sell_reason)
                    await session.commit()

    async def stop(self) -> None:
        """Graceful stop — 모니터/평가 태스크 종료 + WebSocket 정리."""
        # 모니터/평가 태스크 취소
        for task in (self._monitor_task, self._eval_task,
                     getattr(self, '_fast_sl_task', None),
                     getattr(self, '_balance_task', None),
                     getattr(self, '_income_task', None)):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._monitor_task = None
        self._eval_task = None
        self._fast_sl_task = None
        self._balance_task = None
        self._income_task = None

        # WebSocket 정리
        try:
            await self._exchange.close_ws()
        except Exception as e:
            logger.warning("ws_close_error", error=str(e))

        # 부모 stop (포지션 경고 + _is_running=False)
        from db.session import get_session_factory
        session_factory = get_session_factory()
        async with session_factory() as session:
            result = await session.execute(
                select(Position).where(
                    Position.quantity > 0,
                    Position.exchange == self._exchange_name,
                )
            )
            positions = result.scalars().all()

            if positions:
                for pos in positions:
                    direction = pos.direction or "long"
                    try:
                        price = await self._market_data.get_current_price(pos.symbol)
                        if direction == "long":
                            pnl_pct = (price - pos.average_buy_price) / pos.average_buy_price * 100
                        else:
                            pnl_pct = (pos.average_buy_price - price) / pos.average_buy_price * 100
                    except Exception:
                        price = 0
                        pnl_pct = 0

                    lev = pos.leverage or self._leverage
                    logger.warning(
                        "futures_stop_open_position",
                        symbol=pos.symbol, direction=direction, leverage=lev,
                        quantity=pos.quantity, entry=pos.average_buy_price,
                        current_price=price, pnl_pct=round(pnl_pct, 2),
                    )

                await emit_event(
                    "warning", "engine",
                    f"선물 엔진 중지: {len(positions)}개 포지션 보유 중 (레버리지 포지션 주의)",
                    metadata={
                        "positions": [
                            {"symbol": p.symbol, "direction": p.direction or "long",
                             "leverage": p.leverage or self._leverage}
                            for p in positions
                        ]
                    },
                )

        # 부모 TradingEngine.stop() (NOT BinanceFuturesEngine.stop() 재귀)
        self._is_running = False
        logger.info("engine_stopping", exchange=self._exchange_name)
        await emit_event("info", "engine", "선물 엔진 중지")

    @property
    def tracked_coins(self) -> list[str]:
        """설정 코인 목록."""
        return list(self._ec.tracked_coins)


    async def _evaluation_cycle(self) -> None:
        """선물 평가 루프 — 동적 종목 + 펀딩비 업데이트."""
        from db.session import get_session_factory

        self._reset_daily_counter()

        async with self._portfolio_manager._sync_lock:
            session_factory = get_session_factory()
            async with session_factory() as session:
                try:
                    # 시장 상태 업데이트 (BTC/USDT 기준)
                    await self._maybe_update_market_state(session)

                    # 현금 잔고 보정
                    await self._portfolio_manager.reconcile_cash_from_db(session)

                    # 포트폴리오 리밸런싱 (비중 초과 포지션 자동 일부 청산)
                    await self._check_and_rebalance(session)

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
                            self._eval_error_counts.pop(symbol, None)  # 성공 시 카운터 리셋
                        except Exception as e:
                            count = self._eval_error_counts.get(symbol, 0) + 1
                            self._eval_error_counts[symbol] = count
                            logger.error("futures_eval_error", symbol=symbol, error=str(e),
                                         consecutive_errors=count)
                            # 연속 N회 실패 + 보유 포지션 → 강제 청산
                            if count >= self._MAX_EVAL_ERRORS and symbol in held:
                                await self._force_close_stuck_position(session, symbol, str(e))
                            else:
                                await emit_event(
                                    "error", "engine",
                                    f"선물 평가 오류: {symbol} ({count}/{self._MAX_EVAL_ERRORS})",
                                    detail=str(e),
                                    metadata={"symbol": symbol, "consecutive_errors": count},
                                )

                    # 펀딩비 업데이트 (30분마다)
                    await self._maybe_update_funding_rates()

                    # 스냅샷 직전 현금 잔고 재보정 (eval 중 sync 인터리빙 방지)
                    await self._portfolio_manager.reconcile_cash_from_db(session)

                    # 매매 기록 먼저 커밋 (스냅샷 스킵과 무관하게 주문/포지션 영속화)
                    await session.commit()

                    # 스냅샷 (DB locked 재시도, 스파이크 시 자동 스킵)
                    for _attempt in range(3):
                        try:
                            snap = await self._portfolio_manager.take_snapshot(session)
                            if snap is not None:
                                await session.commit()
                            break
                        except Exception as snap_err:
                            if "database is locked" in str(snap_err) and _attempt < 2:
                                await session.rollback()
                                await asyncio.sleep(1)
                            else:
                                raise

                    self._last_eval_time = datetime.now(timezone.utc)

                    # WebSocket broadcast
                    if self._broadcast_callback:
                        summary = await self._portfolio_manager.get_portfolio_summary(session)
                        await self._broadcast_callback({
                            "event": "portfolio_update",
                            "exchange": self._exchange_name,
                            "data": summary,
                        })

                except Exception as e:
                    logger.error("futures_cycle_error", error=str(e), exc_info=True)
                    await session.rollback()

    async def _evaluate_futures_coin(self, session: AsyncSession, symbol: str) -> None:
        """선물 코인 평가: SL/TP + 청산가 체크 + 양방향 매매."""
        position = None
        # 포지션 조회 + SL/TP 체크 (모니터와 동시 청산 방지 — lock 내부에서 쿼리)
        async with self._close_lock:
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

        # 쿨다운 중이고 포지션 없으면 전략 평가 스킵 (CPU 절약)
        if not position and self._check_cooldown(symbol):
            return

        # 전략 시그널 수집 + 결합
        signals = await self._collect_signals(symbol)
        if not signals:
            return

        # 전략 신호 로그 저장 (빗썸 엔진과 동일)
        for signal in signals:
            await self._order_manager.log_signal_only(session, signal, symbol)

        decision = self._combiner.combine(signals, market_state=self._market_state, symbol=symbol)
        if decision.action == SignalType.HOLD:
            return
        await self._process_futures_decision(session, symbol, decision, signals, position)

    async def _check_futures_stop_conditions(
        self, session: AsyncSession, symbol: str, position: Position
    ) -> bool:
        """선물 SL/TP/청산가 체크 — 숏은 방향 반전."""
        tracker = self._position_trackers.get(symbol)
        if not tracker:
            if position.stop_loss_pct is not None:
                # DB에 저장된 트래커 값으로 복원 (방향별 extreme_price 컬럼 분기)
                direction = _effective_direction(position.direction)
                if direction == "short":
                    extreme = (
                        position.lowest_price if position.lowest_price is not None
                        else position.highest_price if position.highest_price is not None
                        else position.average_buy_price
                    )
                else:
                    extreme = (
                        position.highest_price if position.highest_price is not None
                        else position.average_buy_price
                    )
                tracker = PositionTracker(
                    entry_price=position.average_buy_price,
                    extreme_price=extreme,
                    stop_loss_pct=position.stop_loss_pct,
                    take_profit_pct=position.take_profit_pct or 10.0,
                    trailing_activation_pct=position.trailing_activation_pct or 5.0,
                    trailing_stop_pct=position.trailing_stop_pct or 4.0,
                    trailing_active=position.trailing_active or False,
                    is_surge=position.is_surge or False,
                    max_hold_hours=position.max_hold_hours or 0,
                )
                if position.entered_at:
                    ea = position.entered_at
                    if ea.tzinfo is None:
                        ea = ea.replace(tzinfo=timezone.utc)
                    tracker.entered_at = ea
                logger.info("tracker_restored_from_db", symbol=symbol,
                            sl=round(tracker.stop_loss_pct, 2),
                            trailing_active=tracker.trailing_active,
                            highest_price=round(tracker.extreme_price, 4))
            else:
                # 마이그레이션 전 포지션 → 기본값으로 복원
                lev = position.leverage or self._leverage
                sqrt_lev = math.sqrt(lev)
                sl_pct = _FUTURES_DEFAULT_SL_PCT / sqrt_lev
                tp_pct = _FUTURES_DEFAULT_TP_PCT / sqrt_lev
                trail_act = _FUTURES_TRAILING_ACTIVATION / sqrt_lev
                trail_stop = _FUTURES_TRAILING_STOP / sqrt_lev
                tracker = PositionTracker(
                    entry_price=position.average_buy_price,
                    extreme_price=position.average_buy_price,
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

        direction = _effective_direction(position.direction)
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
        if not entry or entry <= 0:
            logger.warning("futures_entry_price_zero", symbol=symbol, entry=entry)
            return False
        tracker_changed = False
        if direction == "long":
            pnl_pct = (price - entry) / entry * 100
            if price > tracker.extreme_price:
                tracker.extreme_price = price
                tracker_changed = True
        else:  # short
            pnl_pct = (entry - price) / entry * 100
            # 숏은 lowest_price 추적 (extreme_price = 최저가)
            if price < tracker.extreme_price:
                tracker.extreme_price = price  # lowest price for short
                tracker_changed = True

        sell_reason = None

        # 트레일링 활성화
        if (tracker.trailing_activation_pct > 0
                and not tracker.trailing_active
                and pnl_pct >= tracker.trailing_activation_pct):
            tracker.trailing_active = True
            tracker_changed = True

        # 트레일링 스탑
        if tracker.trailing_active and tracker.trailing_stop_pct > 0:
            if direction == "long":
                drawdown = (tracker.extreme_price - price) / tracker.extreme_price * 100
            else:
                # 숏: lowest에서 올라간 비율
                drawdown = (price - tracker.extreme_price) / tracker.extreme_price * 100
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

        # trailing_active/extreme_price 변경 시 DB 반영
        if tracker_changed and not sell_reason:
            await self._save_tracker_to_db(session, symbol, tracker)

        if sell_reason:
            # 스탑 경고 이벤트 — 5분 쿨다운으로 스팸 방지 (30초 루프에서 반복 발화 방지)
            now = datetime.now(timezone.utc)
            last_event = self._last_stop_event_time.get(symbol)
            if not last_event or (now - last_event).total_seconds() >= 300:
                lev_val = position.leverage or getattr(self, '_leverage', 3)
                leveraged_pnl = pnl_pct * lev_val
                loss_amount = abs(pnl_pct / 100 * entry * (position.quantity or 0))
                await emit_event(
                    "warning", "futures_trade",
                    f"선물 {direction} 스탑: {symbol}",
                    detail=sell_reason,
                    metadata={
                        "symbol": symbol,
                        "price": price,
                        "entry_price": entry,
                        "pnl_pct": round(pnl_pct, 2),
                        "leveraged_pnl_pct": round(leveraged_pnl, 2),
                        "loss_amount": round(loss_amount, 2),
                        "reason": sell_reason,
                        "direction": direction,
                        "leverage": lev_val,
                    },
                )
                self._last_stop_event_time[symbol] = now
            await self._close_position(session, symbol, position, price, sell_reason)
            return True

        return False

    async def _force_close_stuck_position(
        self, session: AsyncSession, symbol: str, last_error: str,
    ) -> None:
        """연속 평가 실패 포지션 강제 청산. 가격 조회 불가 시 DB에서 직접 제거."""
        result = await session.execute(
            select(Position).where(
                Position.symbol == symbol,
                Position.quantity > 0,
                Position.exchange == self._exchange_name,
            )
        )
        position = result.scalar_one_or_none()
        if not position:
            self._eval_error_counts.pop(symbol, None)
            return

        count = self._eval_error_counts.get(symbol, 0)
        logger.warning(
            "force_close_stuck_position",
            symbol=symbol,
            quantity=float(position.quantity),
            consecutive_errors=count,
            last_error=last_error,
        )

        # 1차 시도: 거래소에서 시장가 청산
        try:
            price = await self._market_data.get_current_price(symbol)
            async with self._close_lock:
                await session.refresh(position)
                if position.quantity > 0:
                    await self._close_position(
                        session, symbol, position, price,
                        f"강제 청산: 연속 {count}회 평가 실패 ({last_error})",
                    )
            self._eval_error_counts.pop(symbol, None)
            return
        except Exception as close_err:
            logger.warning("force_close_market_failed", symbol=symbol, error=str(close_err))

        # 2차: 가격도 못 가져오면 DB 포지션을 0으로 리셋 (손익 계산 불가 → 0 처리)
        entry = position.average_buy_price or 0
        position.quantity = 0
        position.current_price = entry  # PnL 0으로 정리
        position.current_value = 0
        await session.commit()
        self._position_trackers.pop(symbol, None)
        self._eval_error_counts.pop(symbol, None)
        self._last_sell_time.pop(symbol, None)  # 강제 청산은 쿨다운 면제

        logger.error(
            "force_close_db_cleanup",
            symbol=symbol,
            detail="거래소 청산 실패 → DB 포지션 강제 리셋",
        )
        await emit_event(
            "critical", "engine",
            f"강제 청산 (DB 리셋): {symbol}",
            detail=f"연속 {count}회 평가 실패, 거래소 청산 불가 → DB 포지션 0으로 리셋. "
                   f"수동으로 거래소에서 {symbol} 포지션을 확인하세요.",
            metadata={"symbol": symbol, "consecutive_errors": count},
        )

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

        lev = position.leverage or self._leverage
        margin_used = position.margin_used or 0
        ep = position.average_buy_price if position.average_buy_price and position.average_buy_price > 0 else None
        order = await self._order_manager.create_order(
            session, symbol, side, position.quantity, price, signal,
            order_type="market",
            direction=direction, leverage=lev, margin_used=position.margin_used,
            entry_price=ep,
        )

        if order.status == "filled":
            # 워시아웃 먼저 설정 — 후속 처리 실패해도 재진입 방지 보장
            now = datetime.now(timezone.utc)
            self._last_sell_time[symbol] = now
            self._position_trackers.pop(symbol, None)
            self._last_stop_event_time.pop(symbol, None)  # 청산 완료 시 알림 쿨다운 해제

            try:
                await self._portfolio_manager.update_position_on_sell(
                    session, symbol, position.quantity, price,
                    position.quantity * price, order.fee
                )
                # DB에 last_sell_at 기록 (재시작 시 쿨다운 복원용)
                result = await session.execute(
                    select(Position).where(
                        Position.symbol == symbol,
                        Position.exchange == self._exchange_name,
                    )
                )
                pos_record = result.scalar_one_or_none()
                if pos_record:
                    pos_record.last_sell_at = now
            except Exception as e:
                logger.error("futures_close_portfolio_update_failed",
                             symbol=symbol, error=str(e))

            entry_price = ep or price
            pnl_pct = ((price - entry_price) / entry_price * 100) if direction == "long" else ((entry_price - price) / entry_price * 100)
            lev_val = lev  # 이미 update 전에 저장한 값
            leveraged_pnl = pnl_pct * lev_val
            loss_amount = margin_used * leveraged_pnl / 100 if margin_used else 0
            logger.info("futures_position_closed", symbol=symbol, direction=direction, reason=reason, pnl_pct=round(pnl_pct, 2))
            await emit_event("info", "futures_trade",
                             f"선물 {direction} 청산: {symbol}",
                             metadata={
                                 "price": price, "reason": reason,
                                 "direction": direction,
                                 "entry_price": entry_price,
                                 "pnl_pct": round(pnl_pct, 2),
                                 "leveraged_pnl_pct": round(leveraged_pnl, 2),
                                 "loss_amount": round(loss_amount, 2),
                                 "leverage": lev_val,
                             })
            await self._on_sell_completed()

    async def close_position_for_cross_exchange(self, symbol: str, reason: str) -> bool:
        """다른 엔진의 요청으로 선물 포지션 청산. 자체 세션 사용."""
        from db.session import get_session_factory
        sf = get_session_factory()
        try:
            async with sf() as session:
                result = await session.execute(
                    select(Position).where(
                        Position.symbol == symbol,
                        Position.quantity > 0,
                        Position.exchange == self._exchange_name,
                    )
                )
                position = result.scalar_one_or_none()
                if not position:
                    return False

                price = await self._market_data.get_current_price(symbol)
                if price <= 0:
                    logger.warning("cross_close_no_price", symbol=symbol)
                    return False

                await self._close_position(session, symbol, position, price, reason)
                await session.commit()
                logger.info(
                    "cross_exchange_position_closed",
                    symbol=symbol, exchange=self._exchange_name,
                    price=price, reason=reason,
                )
                return True
        except Exception as e:
            logger.error("cross_exchange_close_failed", symbol=symbol, error=str(e))
            return False

    async def _execute_rebalancing_sell(
        self, session: AsyncSession, symbol: str, qty: float, price: float
    ) -> None:
        """선물 리밸런싱 부분 청산 (롱→sell, 숏→buy). close_lock 내에서 실행."""
        result = await session.execute(
            select(Position).where(
                Position.symbol == symbol,
                Position.quantity > 0,
                Position.exchange == self._exchange_name,
            )
        )
        position = result.scalar_one_or_none()
        if not position:
            return

        direction = position.direction or "long"
        side = "sell" if direction == "long" else "buy"

        signal = Signal(
            strategy_name="rebalancing",
            signal_type=SignalType.SELL if direction == "long" else SignalType.BUY,
            confidence=1.0,
            reason=f"선물 포트폴리오 리밸런싱: 비중 초과 부분 청산 ({direction})",
        )

        lev = position.leverage or self._leverage
        ep = position.average_buy_price if position.average_buy_price and position.average_buy_price > 0 else None
        async with self._close_lock:
            order = await self._order_manager.create_order(
                session, symbol, side, qty, price, signal,
                order_type="market",
                direction=direction, leverage=lev, margin_used=position.margin_used,
                entry_price=ep,
            )

            if order.status != "filled":
                logger.warning("futures_rebalancing_not_filled", symbol=symbol, status=order.status)
                if order.exchange_order_id:
                    try:
                        await self._order_manager.cancel_order_by_id(session, order.id)
                    except Exception:
                        pass
                return

            await self._portfolio_manager.update_position_on_sell(
                session, symbol, qty, price,
                qty * price, order.fee,
            )

        logger.info(
            "futures_rebalancing_executed",
            symbol=symbol, direction=direction, qty=round(qty, 8), price=price,
        )

    async def _process_futures_decision(
        self, session: AsyncSession, symbol: str,
        decision: CombinedDecision, signals: list[Signal],
        position: Position | None,
    ) -> None:
        """선물 양방향 매매 처리."""
        # 포지션 청산(반대 시그널)은 쿨다운/제한 면제
        direction = position.direction if position else None
        is_closing = (
            (decision.action == SignalType.BUY and direction == "short") or
            (decision.action == SignalType.SELL and direction == "long")
        )
        if not is_closing:
            can, reason = self._can_trade(symbol, decision.action.value.lower())
            if not can:
                logger.debug("futures_trade_blocked", symbol=symbol, reason=reason)
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
        min_conf = self._ec.min_combined_confidence
        if self._market_confidence < 0.35:
            min_conf += 0.10

        if decision.combined_confidence < min_conf:
            return

        # 변동성 게이트: ATR% 극단적이면 신규 진입 차단 (기존 포지션 청산은 허용)
        if not position:
            atr_pct = self._get_atr_pct(symbol)
            if atr_pct is not None and atr_pct > 12.0:
                logger.debug("volatility_gate_blocked", symbol=symbol,
                             atr_pct=round(atr_pct, 1))
                return

        # ML Signal Filter: 신규 진입만 필터링 (청산은 허용)
        if self._ml_filter and not position:
            _ml_row = self._latest_candle_rows.get(symbol)
            if _ml_row is not None:
                try:
                    from strategies.ml_filter import MLSignalFilter
                    _ml_features = MLSignalFilter.extract_features(
                        signals=signals,
                        row=_ml_row,
                        price=price,
                        market_state=self._market_state,
                        combined_confidence=decision.combined_confidence,
                    )
                    _ml_pred = self._ml_filter.predict(_ml_features)
                    if not _ml_pred.should_trade:
                        logger.info("ml_filter_blocked", symbol=symbol,
                                    win_prob=round(_ml_pred.win_probability, 3),
                                    action=decision.action.value)
                        return
                    logger.info("ml_filter_passed", symbol=symbol,
                                win_prob=round(_ml_pred.win_probability, 3))
                except Exception as e:
                    logger.warning("ml_filter_error", symbol=symbol, error=str(e))

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
                # 숏 진입 (전체 시장 허용 — P1 백테스트 결과)
                await self._open_short(session, symbol, price, primary_signal, decision)

    def _get_min_notional(self, symbol: str) -> float:
        """거래소에서 최소 notional 읽기. 없으면 5.0 USDT 기본값."""
        try:
            exchange = self._exchange
            if hasattr(exchange, '_exchange'):
                exchange = exchange._exchange
            market = exchange.market(symbol)
            min_cost = market.get("limits", {}).get("cost", {}).get("min")
            if min_cost and min_cost > 0:
                return float(min_cost)
        except Exception:
            pass
        return 100.0  # Binance USDM 최소 notional

    def _adjust_amount(self, symbol: str, amount: float) -> float | None:
        """거래소 최소 수량 정밀도에 맞게 수량 보정. 최소 미만이면 None."""
        try:
            exchange = self._exchange
            if hasattr(exchange, '_exchange'):
                exchange = exchange._exchange  # ccxt 인스턴스
            amount = float(exchange.amount_to_precision(symbol, amount))
            market = exchange.market(symbol)
            min_amount = market.get("limits", {}).get("amount", {}).get("min", 0)
            if min_amount and amount < min_amount:
                return None
            return amount
        except Exception:
            return amount

    def _get_atr_pct(self, symbol: str) -> float | None:
        """캐시된 4h 캔들에서 ATR% 계산. 없으면 None."""
        try:
            key = f"{symbol}:4h"
            if key in self._market_data._ohlcv_cache:
                _, df = self._market_data._ohlcv_cache[key]
                if "atr_14" in df.columns and len(df) > 0:
                    atr = df["atr_14"].iloc[-1]
                    close = df["close"].iloc[-1]
                    if atr > 0 and close > 0:
                        return (atr / close) * 100
        except Exception:
            pass
        return None

    def _atr_risk_adjust(self, symbol: str, atr_pct: float | None
                         ) -> tuple[float, int | None]:
        """ATR%에 따른 (마진 배수, 레버리지 오버라이드) 반환.
        레버리지 None이면 기본값 사용."""
        if atr_pct is None:
            return 1.0, None
        for threshold, margin_mult, lev_override in _ATR_RISK_TIERS:
            if atr_pct <= threshold:
                if margin_mult != 1.0 or lev_override is not None:
                    logger.info("atr_risk_adjusted", symbol=symbol,
                                atr_pct=round(atr_pct, 1),
                                margin_mult=margin_mult,
                                leverage_override=lev_override)
                return margin_mult, lev_override
        return 0.3, 1  # 극단 폴백

    def _check_cooldown(self, symbol: str) -> bool:
        """매매 후 쿨다운 체크. True이면 진입 차단."""
        last_sell = self._last_sell_time.get(symbol)
        if last_sell:
            elapsed = (datetime.now(timezone.utc) - last_sell).total_seconds()
            cooldown_sec = self._ec.min_trade_interval_sec
            if elapsed < cooldown_sec:
                remaining_h = (cooldown_sec - elapsed) / 3600
                logger.debug("futures_cooldown_blocked", symbol=symbol,
                             remaining_hours=round(remaining_h, 1))
                return True
        return False

    async def _open_long(
        self, session: AsyncSession, symbol: str, price: float,
        signal: Signal, decision: CombinedDecision,
    ) -> None:
        """롱 포지션 진입."""
        # 쿨다운 체크
        if self._check_cooldown(symbol):
            return

        # ATR 적응형 리스크 조절
        atr_pct = self._get_atr_pct(symbol)
        margin_mult, lev_override = self._atr_risk_adjust(symbol, atr_pct)
        effective_lev = lev_override if lev_override is not None else self._leverage

        cash = self._portfolio_manager.cash_balance
        size_pct = self._ec.max_trade_size_pct * margin_mult  # ATR에 따른 마진 축소

        # Confidence-proportional sizing: conf 0.55→0.7x, 0.70→1.0x, 0.85→1.5x, 1.0→2.0x
        conf = decision.combined_confidence
        conf_mult = min(2.0, max(0.5, 0.5 + (conf - 0.55) * (1.5 / 0.45)))
        size_pct *= conf_mult

        # 시장 상태별 사이징
        if self._market_state == MarketState.CRASH.value:
            size_pct *= 0.25
        elif self._market_state == MarketState.DOWNTREND.value:
            size_pct *= 0.5

        margin = cash * size_pct
        notional = margin * effective_lev
        amount = notional / price

        # 레버리지 오버라이드 시 거래소에 설정
        if lev_override is not None and lev_override != self._leverage:
            try:
                await self._exchange.set_leverage(symbol, effective_lev)
            except Exception as e:
                logger.warning("leverage_override_failed_long", symbol=symbol,
                               target=effective_lev, error=str(e))
                effective_lev = self._leverage
                notional = margin * effective_lev
                amount = notional / price

        # 거래소 최소 수량 보정
        amount = self._adjust_amount(symbol, amount)
        if amount is None:
            logger.debug("futures_amount_below_min", symbol=symbol, margin=round(margin, 2))
            return

        # min notional 체크
        actual_notional = amount * price
        min_notional = self._get_min_notional(symbol)
        if actual_notional < min_notional:
            logger.warning("below_min_notional", symbol=symbol,
                           notional=round(actual_notional, 2), min_notional=min_notional)
            return

        # 수수료 마진 (0.04%)
        margin_with_fee = margin * (1 + self._futures_fee)
        if margin_with_fee > cash or margin < 1.0:
            return

        try:
            order = await self._order_manager.create_order(
                session, symbol, "buy", amount, price, signal, decision,
                order_type="market",
                direction="long", leverage=effective_lev, margin_used=margin,
            )
        except Exception as e:
            logger.error("futures_long_order_failed", symbol=symbol, error=str(e))
            await emit_event("error", "trade", f"선물 롱 주문 실패: {symbol}", detail=str(e))
            return

        if order.status != "filled":
            await emit_event("warning", "trade", f"선물 롱 미체결: {symbol}", detail=f"status={order.status}")
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
            pos.leverage = effective_lev
            # Liquidation estimate: entry * (1 - 1/lev + mmr). WS sync keeps this accurate.
            pos.liquidation_price = price * (1 - 1 / effective_lev + self._maintenance_margin_rate)
            pos.margin_used = margin
            pos.highest_price = price   # initialise extreme_price for long
            pos.lowest_price = None     # clear any prior short-session value
            await session.flush()

        # SL/TP 트래커 — 레버리지 축소 + 동적 SL
        sqrt_lev = math.sqrt(effective_lev)
        sl_pct = self._compute_dynamic_sl(symbol, _FUTURES_DEFAULT_SL_PCT)
        self._position_trackers[symbol] = PositionTracker(
            entry_price=price,
            extreme_price=price,
            stop_loss_pct=sl_pct / sqrt_lev,
            take_profit_pct=_FUTURES_DEFAULT_TP_PCT / sqrt_lev,
            trailing_activation_pct=_FUTURES_TRAILING_ACTIVATION / sqrt_lev,
            trailing_stop_pct=_FUTURES_TRAILING_STOP / sqrt_lev,
        )
        await self._save_tracker_to_db(session, symbol, self._position_trackers[symbol])

        self._daily_buy_count += 1
        self._daily_coin_buy_count[symbol] = self._daily_coin_buy_count.get(symbol, 0) + 1

        logger.info(
            "futures_long_opened", symbol=symbol, price=price,
            leverage=effective_lev, margin=round(margin, 2),
            sl_pct=round(sl_pct / sqrt_lev, 2),
            atr_pct=round(atr_pct, 1) if atr_pct else None,
        )
        tracker = self._position_trackers[symbol]
        sl_price = round(price * (1 - tracker.stop_loss_pct / 100), 4)
        tp_price = round(price * (1 + tracker.take_profit_pct / 100), 4)
        await emit_event("info", "futures_trade", f"선물 롱: {symbol}", metadata={
            "price": price, "leverage": effective_lev,
            "margin": round(margin, 2),
            "strategy": signal.strategy_name,
            "confidence": round(decision.combined_confidence, 2),
            "sl_pct": round(sl_pct / sqrt_lev, 2),
            "sl_price": sl_price, "tp_price": tp_price,
            "market_state": self._market_state,
        })

    async def _open_short(
        self, session: AsyncSession, symbol: str, price: float,
        signal: Signal, decision: CombinedDecision,
    ) -> None:
        """숏 포지션 진입."""
        # 쿨다운 체크
        if self._check_cooldown(symbol):
            return

        # 교차 거래소 포지션 충돌 체크 (선물 숏 vs 현물 롱 — 빗썸/바이낸스 현물 모두)
        base = symbol.split("/")[0]
        cross_result = await session.execute(
            select(Position).where(
                Position.symbol.like(f"{base}/%"),
                Position.quantity > 0,
                Position.exchange != self._exchange_name,
                Position.direction != "short",
            )
        )
        cross_pos = cross_result.scalars().first()
        if cross_pos:
            # 높은 신뢰도면 현물 롱 청산 후 숏 진행 (포지션 방향 전환)
            flipped = False
            if (decision.combined_confidence >= self.CROSS_FLIP_MIN_CONFIDENCE
                    and self._engine_registry):
                cross_engine = self._engine_registry.get_engine(cross_pos.exchange)
                if cross_engine:
                    cross_symbol = f"{base}/{cross_engine._ec.quote_currency}"
                    flipped = await cross_engine.close_position_for_cross_exchange(
                        cross_symbol,
                        f"교차 전환: {self._exchange_name} SHORT(conf={decision.combined_confidence:.2f}) → 롱 청산",
                    )
                    if flipped:
                        await emit_event(
                            "info", "risk",
                            f"교차 포지션 전환: {cross_pos.exchange} {base} 롱 청산 → {self._exchange_name} 숏 진행",
                            metadata={"symbol": symbol, "confidence": round(decision.combined_confidence, 2)},
                        )
            if not flipped:
                logger.warning(
                    "cross_exchange_conflict_blocked",
                    symbol=symbol,
                    cross_exchange=cross_pos.exchange,
                    cross_direction="long",
                    cross_qty=cross_pos.quantity,
                )
                await emit_event(
                    "warning", "risk",
                    f"교차 거래소 충돌: {symbol} 숏 차단 (현물 롱 보유 중)",
                    metadata={"symbol": symbol, "cross_qty": cross_pos.quantity},
                )
                return

        # ATR 적응형 리스크 조절
        atr_pct = self._get_atr_pct(symbol)
        margin_mult, lev_override = self._atr_risk_adjust(symbol, atr_pct)
        effective_lev = lev_override if lev_override is not None else self._leverage

        cash = self._portfolio_manager.cash_balance
        size_pct = self._ec.max_trade_size_pct * margin_mult  # ATR에 따른 마진 축소

        # Confidence-proportional sizing: conf 0.55→0.7x, 0.70→1.0x, 0.85→1.5x, 1.0→2.0x
        conf = decision.combined_confidence
        conf_mult = min(2.0, max(0.5, 0.5 + (conf - 0.55) * (1.5 / 0.45)))
        size_pct *= conf_mult

        margin = cash * size_pct
        notional = margin * effective_lev
        amount = notional / price

        # 레버리지 오버라이드 시 거래소에 설정
        if lev_override is not None and lev_override != self._leverage:
            try:
                await self._exchange.set_leverage(symbol, effective_lev)
            except Exception as e:
                logger.warning("leverage_override_failed_short", symbol=symbol,
                               target=effective_lev, error=str(e))
                effective_lev = self._leverage
                notional = margin * effective_lev
                amount = notional / price

        # 거래소 최소 수량 보정
        amount = self._adjust_amount(symbol, amount)
        if amount is None:
            logger.debug("futures_amount_below_min", symbol=symbol, margin=round(margin, 2))
            return

        # min notional 체크
        actual_notional = amount * price
        min_notional = self._get_min_notional(symbol)
        if actual_notional < min_notional:
            logger.warning("below_min_notional", symbol=symbol,
                           notional=round(actual_notional, 2), min_notional=min_notional)
            return

        margin_with_fee = margin * (1 + self._futures_fee)
        if margin_with_fee > cash or margin < 1.0:
            return

        try:
            order = await self._order_manager.create_order(
                session, symbol, "sell", amount, price, signal, decision,
                order_type="market",
                direction="short", leverage=effective_lev, margin_used=margin,
            )
        except Exception as e:
            logger.error("futures_short_order_failed", symbol=symbol, error=str(e))
            await emit_event("error", "trade", f"선물 숏 주문 실패: {symbol}", detail=str(e))
            return

        if order.status != "filled":
            await emit_event("warning", "trade", f"선물 숏 미체결: {symbol}", detail=f"status={order.status}")
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
            pos.leverage = effective_lev
            # Liquidation estimate: entry * (1 + 1/lev - mmr). WS sync keeps this accurate.
            pos.liquidation_price = price * (1 + 1 / effective_lev - self._maintenance_margin_rate)
            pos.margin_used = margin
            pos.lowest_price = price   # initialise extreme_price for short (prevents stale long data)
            pos.highest_price = None   # clear any prior long-session value
            await session.flush()

        # 숏 트래커 — extreme_price = 최저가 추적 + 동적 SL
        sqrt_lev = math.sqrt(effective_lev)
        sl_pct = self._compute_dynamic_sl(symbol, _FUTURES_DEFAULT_SL_PCT)
        self._position_trackers[symbol] = PositionTracker(
            entry_price=price,
            extreme_price=price,  # 숏: 최저가 추적
            stop_loss_pct=sl_pct / sqrt_lev,
            take_profit_pct=_FUTURES_DEFAULT_TP_PCT / sqrt_lev,
            trailing_activation_pct=_FUTURES_TRAILING_ACTIVATION / sqrt_lev,
            trailing_stop_pct=_FUTURES_TRAILING_STOP / sqrt_lev,
        )
        await self._save_tracker_to_db(session, symbol, self._position_trackers[symbol])

        self._daily_buy_count += 1
        self._daily_coin_buy_count[symbol] = self._daily_coin_buy_count.get(symbol, 0) + 1

        logger.info(
            "futures_short_opened", symbol=symbol, price=price,
            leverage=effective_lev, margin=round(margin, 2),
            sl_pct=round(sl_pct / sqrt_lev, 2),
            atr_pct=round(atr_pct, 1) if atr_pct else None,
        )
        tracker = self._position_trackers[symbol]
        sl_price = round(price * (1 + tracker.stop_loss_pct / 100), 4)
        tp_price = round(price * (1 - tracker.take_profit_pct / 100), 4)
        await emit_event("info", "futures_trade", f"선물 숏: {symbol}", metadata={
            "price": price, "leverage": effective_lev,
            "margin": round(margin, 2),
            "strategy": signal.strategy_name,
            "confidence": round(decision.combined_confidence, 2),
            "sl_pct": round(sl_pct / sqrt_lev, 2),
            "sl_price": sl_price, "tp_price": tp_price,
            "market_state": self._market_state,
        })

    def _compute_dynamic_sl(self, symbol: str, default_sl: float) -> float:
        """ATR 기반 동적 손절 계산. 시장 상태별 프로필 적용."""
        try:
            # 캐시된 4h 캔들에서 ATR 가져오기
            key = f"{symbol}:{_FUTURES_TIMEFRAME}"
            if key in self._market_data._ohlcv_cache:
                _, df = self._market_data._ohlcv_cache[key]
                if "atr_14" in df.columns and len(df) > 0:
                    atr = df["atr_14"].iloc[-1]
                    close = df["close"].iloc[-1]
                    if atr > 0 and close > 0:
                        atr_pct = (atr / close) * 100
                        profile = _DYNAMIC_SL_PROFILES.get(
                            self._market_state, (2.0, 4.0, 10.0)
                        )
                        mult, floor_pct, cap_pct = profile
                        sl = max(floor_pct, min(atr_pct * mult, cap_pct))
                        logger.debug("dynamic_sl_computed", symbol=symbol,
                                     atr_pct=round(atr_pct, 2), sl=round(sl, 2),
                                     market=self._market_state)
                        return sl
        except Exception as e:
            logger.debug("dynamic_sl_fallback", symbol=symbol, error=str(e))
        return default_sl

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

    _MARKET_STATE_INTERVAL_SEC = 600  # 10분마다 갱신 (현물 30분 대비 3배 민감)

    async def _maybe_update_market_state(self, session: AsyncSession) -> None:
        """BTC/USDT 듀얼 타임프레임 시장 상태 감지 (4h 장기 + 1h 단기)."""
        now = datetime.now(timezone.utc)
        if (self._market_state_updated
                and (now - self._market_state_updated).total_seconds() < self._MARKET_STATE_INTERVAL_SEC):
            return
        try:
            # 장기 추세 (4h)
            df_4h = await self._market_data.get_candles("BTC/USDT", "4h", 200)
            state_4h, conf_4h = self._detect_market_state(df_4h)

            # 단기 추세 (1h) — 빠른 전환 감지
            df_1h = await self._market_data.get_candles("BTC/USDT", "1h", 200)
            state_1h, conf_1h = self._detect_market_state(df_1h)

            # 듀얼 타임프레임 결합:
            # - 4h와 1h가 일치하면 그대로 사용 (높은 확신)
            # - 1h가 더 약세면 한 단계 하향 (빠른 전환 반영)
            # - 1h가 더 강세면 4h 유지 (노이즈 방지, 보수적)
            _STATE_RANK = {
                "crash": 0, "downtrend": 1, "sideways": 2,
                "uptrend": 3, "strong_uptrend": 4,
            }
            _RANK_STATE = {v: k for k, v in _STATE_RANK.items()}

            rank_4h = _STATE_RANK.get(state_4h, 2)
            rank_1h = _STATE_RANK.get(state_1h, 2)

            if rank_1h < rank_4h:
                # 1h가 더 약세 → 한 단계 하향 (최대)
                final_rank = max(rank_4h - 1, rank_1h)
                final_state = _RANK_STATE.get(final_rank, state_4h)
                final_conf = (conf_4h + conf_1h) / 2
            else:
                # 일치 또는 1h가 더 강세 → 4h 유지
                final_state = state_4h
                final_conf = conf_4h

            old_state = self._market_state
            self._market_state = final_state
            self._market_confidence = round(final_conf, 2)
            self._market_state_updated = now

            if old_state != final_state:
                logger.info(
                    "futures_market_state_changed",
                    old=old_state, new=final_state,
                    state_4h=state_4h, state_1h=state_1h,
                    confidence=self._market_confidence,
                )
                await emit_event(
                    "info", "strategy",
                    f"선물 시장 상태: {old_state}→{final_state} (4h={state_4h}, 1h={state_1h}, 신뢰도 {int(final_conf*100)}%)",
                    metadata={"old": old_state, "new": final_state,
                              "state_4h": state_4h, "state_1h": state_1h},
                )
            else:
                logger.debug(
                    "futures_market_state",
                    state=final_state, state_4h=state_4h, state_1h=state_1h,
                    confidence=self._market_confidence,
                )
        except Exception as e:
            logger.warning("futures_market_state_failed", error=str(e))

    async def _collect_signals(self, symbol: str) -> list[Signal]:
        """전략 시그널 수집 (4h 타임프레임)."""
        signals = []
        try:
            ticker = await self._market_data.get_ticker(symbol)
        except Exception as e:
            logger.warning("ticker_fetch_failed", symbol=symbol, error=str(e))
            return signals  # 티커 조회 실패 시 빈 시그널 반환 (eval_error 대신 graceful)
        last_row = None
        for name, strategy in self._strategies.items():
            try:
                timeframe = _FUTURES_TIMEFRAME  # 4h 고정 (P1 최적화)
                candles = max(getattr(strategy, "min_candles_required", 50) + 50, 200)
                df = await self._market_data.get_candles(symbol, timeframe, candles)
                if df is None or len(df) < 20:
                    continue
                if last_row is None:
                    last_row = df.iloc[-1]
                signal = await strategy.analyze(df, ticker)
                if signal:
                    signals.append(signal)
            except Exception as e:
                logger.debug("strategy_signal_error", strategy=name, symbol=symbol, error=str(e))
        # ML 필터용 최신 캔들 저장
        if last_row is not None:
            self._latest_candle_rows[symbol] = last_row
        return signals

    @property
    def has_open_positions(self) -> bool:
        """동기적으로 트래커 기반 열린 포지션 존재 여부 확인."""
        return len(self._position_trackers) > 0

    @property
    def rotation_status(self) -> dict:
        """선물 엔진 상태."""
        return {
            "rotation_enabled": False,
            "surge_threshold": 0,
            "market_state": self._market_state,
            "current_surge_symbol": None,
            "last_rotation_time": None,
            "last_scan_time": self._last_eval_time.isoformat() if self._last_eval_time else None,
            "rotation_cooldown_sec": 0,
            "tracked_coins": self.tracked_coins,
            "rotation_coins": [],
            "all_surge_scores": {},
        }
