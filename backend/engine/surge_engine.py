"""
SurgeEngine — 거래량 급등 기반 단기 모멘텀 매매 엔진
====================================================
기존 TradingEngine/BinanceFuturesEngine과 완전 독립.
WebSocket 티커 스트림으로 실시간 서지 감지, 시장가 진입/청산.

DB 격리: exchange="binance_surge"
"""
import asyncio
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

import numpy as np
import structlog

from sqlalchemy import select

from config import AppConfig, SurgeTradingConfig
from core.enums import SignalType
from core.models import Position
from core.event_bus import emit_event
from db.session import get_session_factory
from exchange.base import ExchangeAdapter
from strategies.base import Signal

logger = structlog.get_logger(__name__)

EXCHANGE_NAME = "binance_surge"
FEE_PCT = 0.0004  # 0.04% per side


# ── Data structures ──────────────────────────────────────────────

@dataclass
class SurgePositionState:
    """In-memory position tracking for fast exit checks."""
    symbol: str
    direction: str  # "long" or "short"
    entry_price: float
    quantity: float
    margin: float
    entry_time: datetime
    peak_price: float
    trough_price: float
    trailing_active: bool = False
    surge_score: float = 0.0


@dataclass
class SymbolState:
    """Rolling window state per symbol for surge detection."""
    volume_1m: deque = field(default_factory=lambda: deque(maxlen=60))
    prices: deque = field(default_factory=lambda: deque(maxlen=60))
    last_price: float = 0.0
    last_volume: float = 0.0
    last_update: float = 0.0  # monotonic timestamp
    rsi_closes: deque = field(default_factory=lambda: deque(maxlen=20))


# ── SurgeEngine ──────────────────────────────────────────────────

class SurgeEngine:
    """Standalone surge/momentum trading engine for Binance USDM futures.

    Does NOT subclass TradingEngine. Shares the BinanceUSDMAdapter.
    잔고 통합: 선물 PM의 cash를 직접 조정하고, DB positions/orders로 서지 PnL 추적.
    """

    # Top-30 USDT perpetual contracts for scanning
    DEFAULT_SCAN_SYMBOLS = [
        "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT",
        "DOGE/USDT", "ADA/USDT", "AVAX/USDT", "LINK/USDT", "DOT/USDT",
        "NEAR/USDT", "SUI/USDT", "1000PEPE/USDT", "WIF/USDT", "ATOM/USDT",
        "FIL/USDT", "ARB/USDT", "OP/USDT", "TRX/USDT", "AAVE/USDT",
        "ETC/USDT", "APT/USDT", "IMX/USDT", "INJ/USDT", "SEI/USDT",
        "FET/USDT", "RENDER/USDT", "TIA/USDT", "JUP/USDT", "PENDLE/USDT",
    ]

    def __init__(
        self,
        config: AppConfig,
        exchange: ExchangeAdapter,
        futures_pm,
        order_manager,
        *,
        engine_registry=None,
    ):
        self._config = config
        sc: SurgeTradingConfig = config.surge_trading
        self._exchange = exchange
        self._futures_pm = futures_pm  # 선물 PM 공유 (cash 조정용)
        self._order_manager = order_manager
        self._engine_registry = engine_registry

        # Config params
        self._leverage = sc.leverage
        self._max_concurrent = sc.max_concurrent
        self._position_pct = sc.position_pct
        self._sl_pct = sc.sl_pct
        self._tp_pct = sc.tp_pct
        self._trail_activation_pct = sc.trail_activation_pct
        self._trail_stop_pct = sc.trail_stop_pct
        self._max_hold_minutes = sc.max_hold_minutes
        self._vol_threshold = sc.vol_threshold
        self._price_threshold = sc.price_threshold
        self._long_only = sc.long_only
        self._daily_trade_limit = sc.daily_trade_limit
        self._cooldown_sec = sc.cooldown_per_symbol_sec
        self._scan_interval = sc.scan_interval_sec
        self._mode = sc.mode
        self._scan_symbols = self.DEFAULT_SCAN_SYMBOLS[:sc.scan_symbols_count]

        # COIN-20: 진입 필터 강화
        self._min_score = sc.min_score
        self._rsi_overbought = sc.rsi_overbought
        self._rsi_oversold = sc.rsi_oversold
        self._consecutive_sl_cooldown_sec = sc.consecutive_sl_cooldown_sec
        self._min_atr_pct = sc.min_atr_pct

        # Runtime state
        self._running = False
        self._main_task: asyncio.Task | None = None
        self._positions: dict[str, SurgePositionState] = {}
        self._symbol_states: dict[str, SymbolState] = {}
        self._cooldowns: dict[str, datetime] = {}  # symbol -> next allowed time

        # 캔들 기반 거래량 데이터 (5m OHLCV, 60초마다 갱신)
        self._candle_vol_ratios: dict[str, float] = {}
        self._candle_price_chgs: dict[str, float] = {}
        self._candle_vol_accel: dict[str, float] = {}
        self._candle_atr_pct: dict[str, float] = {}  # COIN-20: ATR% per symbol
        self._last_candle_update: float = 0.0
        self._CANDLE_UPDATE_INTERVAL = 60  # 캔들 데이터 갱신 간격 (초)
        self._last_scan_time: datetime | None = None

        # Daily counters (reset at 00:00 UTC)
        self._daily_trades = 0
        self._daily_losses = 0
        self._consecutive_losses = 0
        self._pause_until: datetime | None = None
        self._last_reset_date: datetime | None = None

        # COIN-20: 심볼별 연속 SL 카운터 (장기 쿨다운용)
        self._consecutive_sl_count: dict[str, int] = {}  # symbol -> count

        # Exchange name for DB isolation
        self._exchange_name = EXCHANGE_NAME

    # ── Public interface (EngineRegistry compatible) ─────────────

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def tracked_coins(self) -> list[str]:
        return list(self._scan_symbols)

    @property
    def exchange_name(self) -> str:
        return self._exchange_name

    def set_engine_registry(self, registry) -> None:
        self._engine_registry = registry

    async def start(self) -> None:
        if self._running:
            logger.warning("surge_engine_already_running")
            return
        self._running = True
        self._main_task = asyncio.create_task(self._main_loop(), name="surge_engine_loop")
        logger.info("surge_engine_started",
                     mode=self._mode,
                     leverage=self._leverage,
                     symbols=len(self._scan_symbols))
        await emit_event("info", "system", "서지 엔진 시작",
                         detail=f"모드={self._mode}, 레버리지={self._leverage}x, 심볼={len(self._scan_symbols)}개")

    async def stop(self) -> None:
        self._running = False
        if self._main_task and not self._main_task.done():
            self._main_task.cancel()
            try:
                await self._main_task
            except asyncio.CancelledError:
                pass
        self._main_task = None
        logger.info("surge_engine_stopped")

    def status(self) -> dict:
        return {
            "running": self._running,
            "mode": self._mode,
            "leverage": self._leverage,
            "open_positions": len(self._positions),
            "daily_trades": self._daily_trades,
            "daily_losses": self._daily_losses,
            "consecutive_losses": self._consecutive_losses,
            "paused": self._pause_until is not None and datetime.now(timezone.utc) < self._pause_until,
            "scan_symbols": len(self._scan_symbols),
        }

    def scan_status(self) -> dict:
        """서지 스캔 상태 — 심볼별 점수 + 포지션 정보."""
        scores = []
        for sym in self._scan_symbols:
            score, vol_ratio, price_chg = self.compute_surge_score(sym)
            state = self._symbol_states.get(sym)
            pos = self._positions.get(sym)
            scores.append({
                "symbol": sym,
                "score": round(score, 4),
                "vol_ratio": round(vol_ratio, 2),
                "price_chg": round(price_chg, 3),
                "rsi": round(self.compute_rsi(sym), 1),
                "atr_pct": round(self._candle_atr_pct.get(sym, 0.0), 3),
                "consecutive_sl": self._consecutive_sl_count.get(sym, 0),
                "last_price": round(state.last_price, 4) if state else 0,
                "has_position": pos is not None,
                "direction": pos.direction if pos else None,
                "pnl_pct": self._calc_position_pnl_pct(pos) if pos else None,
            })
        scores.sort(key=lambda x: x["score"], reverse=True)

        return {
            "scan_symbols_count": len(self._scan_symbols),
            "open_positions": len(self._positions),
            "daily_trades": self._daily_trades,
            "daily_limit": self._daily_trade_limit,
            "daily_losses": self._daily_losses,
            "consecutive_losses": self._consecutive_losses,
            "paused": self._pause_until is not None and datetime.now(timezone.utc) < self._pause_until,
            "scan_interval_sec": self._scan_interval,
            "leverage": self._leverage,
            "last_scan_time": self._last_scan_time.isoformat() if self._last_scan_time else None,
            "min_score": self._min_score,
            "min_atr_pct": self._min_atr_pct,
            "rsi_overbought": self._rsi_overbought,
            "rsi_oversold": self._rsi_oversold,
            "scores": scores,
        }

    def _calc_position_pnl_pct(self, pos: SurgePositionState) -> float:
        """인메모리 포지션의 현재 PnL% 계산."""
        state = self._symbol_states.get(pos.symbol)
        if not state or state.last_price <= 0 or pos.entry_price <= 0:
            return 0.0
        if pos.direction == "long":
            return (state.last_price - pos.entry_price) / pos.entry_price * 100 * self._leverage
        else:
            return (pos.entry_price - state.last_price) / pos.entry_price * 100 * self._leverage

    # ── Main loop ────────────────────────────────────────────────

    async def _main_loop(self) -> None:
        """Continuous scan loop with configurable interval."""
        logger.info("surge_main_loop_start", interval=self._scan_interval)
        while self._running:
            try:
                await self._scan_cycle()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("surge_scan_error", error=str(e), exc_info=True)
            await asyncio.sleep(self._scan_interval)

    async def _scan_cycle(self) -> None:
        """One scan cycle: update state, check exits, find entries."""
        self._reset_daily_counters_if_needed()

        # 1. Fetch tickers for all scan symbols (prices + exit checks)
        tickers = await self._fetch_tickers()
        if not tickers:
            return

        # 2. Update price state
        now = asyncio.get_event_loop().time()
        for sym, ticker_data in tickers.items():
            self._update_symbol_state(sym, ticker_data, now)

        # 3. Check exits for open positions
        await self._check_all_exits(tickers)

        # 4. Check if we are paused
        if self._pause_until and datetime.now(timezone.utc) < self._pause_until:
            return

        # 5. Daily limit
        if self._daily_trades >= self._daily_trade_limit:
            return

        # 6. 캔들 기반 거래량 데이터 갱신 (60초마다)
        if now - self._last_candle_update >= self._CANDLE_UPDATE_INTERVAL:
            await self._update_candle_volume_data()
            self._last_candle_update = now

        # 7. Scan for new entries
        await self._scan_for_entries(tickers)

        self._last_scan_time = datetime.now(timezone.utc)

    # ── Ticker fetching ──────────────────────────────────────────

    async def _fetch_tickers(self) -> dict[str, dict]:
        """Fetch current tickers for scan symbols (batch API call)."""
        tickers = {}
        try:
            # 배치 fetch — 개별 30 API 콜 대신 1회 호출
            all_tickers = await self._exchange.fetch_tickers()
            # USDM 선물: 키가 "BTC/USDT:USDT" 형식 → "BTC/USDT"로 정규화
            scan_set = set(self._scan_symbols)
            for raw_sym, data in all_tickers.items():
                sym = raw_sym.replace(":USDT", "")
                if sym in scan_set:
                    last = float(data.get("last", 0) or 0)
                    if last <= 0:
                        continue
                    tickers[sym] = {
                        "last": last,
                        "bid": float(data.get("bid", 0) or 0),
                        "ask": float(data.get("ask", 0) or 0),
                        "volume": float(data.get("quoteVolume", 0) or 0),
                    }
        except Exception as e:
            logger.warning("surge_ticker_batch_failed_fallback", error=str(e))
        # 배치 실패 또는 심볼 누락 시 → 개별 fetch 폴백
        if len(tickers) < len(self._scan_symbols) // 2:
            for sym in self._scan_symbols:
                if sym in tickers:
                    continue
                try:
                    ticker = await self._exchange.fetch_ticker(sym)
                    tickers[sym] = {
                        "last": ticker.last,
                        "bid": ticker.bid,
                        "ask": ticker.ask,
                        "volume": ticker.volume,
                    }
                except Exception:
                    continue
        return tickers

    # ── Rolling window updates ───────────────────────────────────

    def _update_symbol_state(self, symbol: str, ticker: dict, now: float) -> None:
        """Update rolling window state for a symbol."""
        if symbol not in self._symbol_states:
            self._symbol_states[symbol] = SymbolState()

        state = self._symbol_states[symbol]
        price = ticker["last"]
        volume = ticker.get("volume", 0)

        state.last_price = price
        state.last_volume = volume
        state.prices.append(price)
        state.volume_1m.append(volume)
        state.rsi_closes.append(price)
        state.last_update = now

    # ── Candle-based volume data ─────────────────────────────────

    # 백테스트 동일 lookback: 60 × 5m = 5시간 baseline
    _VOL_LOOKBACK = 60

    async def _update_candle_volume_data(self) -> None:
        """5m 캔들 OHLCV로 거래량 비율 갱신 (백테스트와 동일한 5시간 baseline).

        주의: fetch_ohlcv 마지막 캔들은 진행 중(미완성)이므로 제외.
        [-2]가 가장 최근 완성된 캔들.
        """
        updated = 0
        # baseline(60) + current(1) + 진행중(1) = 62
        need = self._VOL_LOOKBACK + 2
        for sym in self._scan_symbols:
            try:
                candles = await self._exchange.fetch_ohlcv(sym, "5m", limit=need)
                if not candles or len(candles) < 10:
                    continue

                # 마지막 캔들(진행 중) 제외 → [-1]이 최근 완성 캔들
                completed = candles[:-1]
                volumes = [c.volume for c in completed]
                current_vol = volumes[-1]
                # baseline: 현재 완성 캔들 제외, 최대 _VOL_LOOKBACK개 평균
                baseline = volumes[:-1][-self._VOL_LOOKBACK:]
                avg_vol = np.mean(baseline) if baseline else 0.0
                if avg_vol > 0:
                    self._candle_vol_ratios[sym] = current_vol / avg_vol
                else:
                    self._candle_vol_ratios[sym] = 0.0

                # 가격 변동 (최근 15분 = 3 × 5m 완성 캔들)
                lookback = min(3, len(completed) - 1)
                old_close = completed[-lookback - 1].close
                new_close = completed[-1].close
                if old_close > 0:
                    self._candle_price_chgs[sym] = (new_close - old_close) / old_close * 100
                else:
                    self._candle_price_chgs[sym] = 0.0

                # 가속도: 현재 vol_ratio vs 2캔들 전 vol_ratio
                if len(volumes) >= self._VOL_LOOKBACK + 3:
                    prev_baseline = volumes[:-3][-self._VOL_LOOKBACK:]
                    prev_avg = np.mean(prev_baseline) if prev_baseline else avg_vol
                    prev_ratio = volumes[-3] / prev_avg if prev_avg > 0 else 0.0
                    self._candle_vol_accel[sym] = self._candle_vol_ratios[sym] - prev_ratio
                else:
                    self._candle_vol_accel[sym] = 0.0

                # COIN-20: ATR% 계산 (최근 14캔들 ATR / close)
                atr_lookback = min(14, len(completed) - 1)
                if atr_lookback >= 2:
                    recent = completed[-atr_lookback - 1:]
                    tr_sum = 0.0
                    for j in range(1, len(recent)):
                        hi = recent[j].high
                        lo = recent[j].low
                        prev_c = recent[j - 1].close
                        tr = max(hi - lo, abs(hi - prev_c), abs(lo - prev_c))
                        tr_sum += tr
                    atr = tr_sum / atr_lookback
                    close_price = completed[-1].close
                    self._candle_atr_pct[sym] = (atr / close_price * 100) if close_price > 0 else 0.0
                else:
                    self._candle_atr_pct[sym] = 0.0

                updated += 1
            except Exception:
                continue

        # 상위 서지 로그 (데이터 없으면 warning)
        if updated > 0:
            top = sorted(self._candle_vol_ratios.items(), key=lambda x: x[1], reverse=True)[:3]
            logger.info("surge_candle_volume_updated",
                        updated=updated,
                        top=[(s, round(v, 1)) for s, v in top if v >= 2.0])
        else:
            logger.warning("surge_candle_volume_no_data", symbols=len(self._scan_symbols))

    # ── Surge scoring ────────────────────────────────────────────

    def compute_surge_score(
        self, symbol: str
    ) -> tuple[float, float, float]:
        """Compute surge score for a symbol.

        Returns (score, volume_ratio, price_change_pct).
        Uses 5m candle OHLCV data for volume comparison (ticker 24h volume은 변동 없어 사용 불가).
        """
        # 캔들 기반 거래량 데이터 사용
        vol_ratio = self._candle_vol_ratios.get(symbol, 0.0)
        price_chg = self._candle_price_chgs.get(symbol, 0.0)
        accel = self._candle_vol_accel.get(symbol, 0.0)

        if vol_ratio <= 0:
            return 0.0, 0.0, 0.0

        # Normalize signals
        vol_signal = min(vol_ratio / 10.0, 1.0)
        price_signal = min(abs(price_chg) / 5.0, 1.0)
        accel_signal = max(0, min(accel / 3.0, 1.0))

        # Weighted composite (matches backtest weights)
        score = (
            0.40 * vol_signal +
            0.35 * price_signal +
            0.25 * accel_signal
        )

        return score, vol_ratio, price_chg

    def compute_rsi(self, symbol: str, period: int = 14) -> float:
        """Compute RSI from rolling close prices."""
        state = self._symbol_states.get(symbol)
        if not state or len(state.rsi_closes) < period + 1:
            return 50.0

        closes = list(state.rsi_closes)
        closes = closes[-(period + 1):]
        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0).mean()
        losses_val = np.where(deltas < 0, -deltas, 0).mean()
        if losses_val == 0:
            return 100.0
        rs = gains / losses_val
        return 100.0 - (100.0 / (1.0 + rs))

    # ── Entry logic ──────────────────────────────────────────────

    async def _scan_for_entries(self, tickers: dict[str, dict]) -> None:
        """Scan all symbols for surge entry opportunities."""
        if len(self._positions) >= self._max_concurrent:
            return

        candidates = []
        now = datetime.now(timezone.utc)

        for sym in self._scan_symbols:
            if sym in self._positions:
                continue
            if sym not in tickers:
                continue

            # Cooldown check
            if sym in self._cooldowns and now < self._cooldowns[sym]:
                continue

            score, vol_ratio, price_chg = self.compute_surge_score(sym)

            # COIN-20: Configurable min score filter (was hardcoded 0.40)
            if score < self._min_score:
                continue
            if vol_ratio < self._vol_threshold:
                continue
            if abs(price_chg) < self._price_threshold:
                continue

            # COIN-20: ATR volatility filter (횡보장 fake surge 차단)
            atr_pct = self._candle_atr_pct.get(sym, 0.0)
            if atr_pct > 0 and atr_pct < self._min_atr_pct:
                continue

            # Exhaustion filter: if already moved >8% skip
            prices = list(self._symbol_states[sym].prices)
            if len(prices) >= 6:
                old_price = prices[-6]
                if old_price > 0 and abs((prices[-1] - old_price) / old_price * 100) > 8.0:
                    continue

            # COIN-20: Configurable RSI filter (was hardcoded 85/15)
            rsi = self.compute_rsi(sym)
            if price_chg > 0 and rsi > self._rsi_overbought:
                continue
            if price_chg < 0 and rsi < self._rsi_oversold:
                continue

            # Spread filter
            bid = tickers[sym].get("bid", 0)
            ask = tickers[sym].get("ask", 0)
            if bid > 0 and ask > 0:
                spread_pct = (ask - bid) / ask * 100
                if spread_pct > 0.15:
                    continue

            candidates.append((sym, score, vol_ratio, price_chg))

        # Sort by score descending
        candidates.sort(key=lambda x: x[1], reverse=True)
        slots = self._max_concurrent - len(self._positions)

        for sym, score, vol_ratio, price_chg in candidates[:slots]:
            # Direction
            direction = "long" if price_chg > 0 else "short"
            if self._long_only and direction == "short":
                continue

            # Cross-engine conflict check
            if self._check_cross_engine_conflict(sym, direction):
                continue

            await self._enter_position(sym, direction, score, tickers[sym])

    def _check_cross_engine_conflict(self, symbol: str, direction: str) -> bool:
        """Check if main futures engine has an opposite position."""
        if not self._engine_registry:
            return False
        main_engine = self._engine_registry.get_engine("binance_futures")
        if not main_engine:
            return False

        # Check position trackers
        trackers = getattr(main_engine, "_position_trackers", {})
        if symbol in trackers:
            tracker = trackers[symbol]
            main_dir = getattr(tracker, "direction", "long")
            if main_dir != direction:
                logger.debug("surge_cross_conflict_blocked",
                             symbol=symbol, surge_dir=direction, main_dir=main_dir)
                return True
        return False

    async def _enter_position(
        self, symbol: str, direction: str, score: float, ticker: dict,
    ) -> None:
        """Execute a surge entry."""
        price = ticker["last"]
        cash = self._futures_pm.cash_balance

        # Position sizing with surge strength scaling
        size_usdt = cash * self._position_pct
        if score >= 0.70:
            size_usdt *= 1.0
        elif score >= 0.55:
            size_usdt *= 0.75
        else:
            size_usdt *= 0.50

        if size_usdt < 5:
            return

        margin = size_usdt
        if margin > cash:
            return

        qty = size_usdt * self._leverage / price
        now = datetime.now(timezone.utc)

        # Execute market order
        try:
            sf = get_session_factory()
            async with sf() as session:
                # Set leverage
                try:
                    await self._exchange.set_leverage(symbol, self._leverage)
                except Exception:
                    pass  # may already be set

                side = "buy" if direction == "long" else "sell"
                signal = Signal(
                    strategy_name="surge_detector",
                    signal_type=SignalType.BUY if direction == "long" else SignalType.SELL,
                    confidence=score,
                    reason=f"Surge score={score:.2f}",
                )
                order = await self._order_manager.create_order(
                    session=session,
                    symbol=symbol,
                    side=side,
                    amount=qty,
                    price=price,
                    signal=signal,
                    order_type="market",
                    direction=direction,
                    leverage=self._leverage,
                    margin_used=margin,
                )

                exec_price = order.executed_price or price
                exec_qty = order.executed_quantity or qty
                fee = order.fee or (exec_price * exec_qty * FEE_PCT)
                actual_margin = exec_qty * exec_price / self._leverage

                # DB Position 직접 관리 (PM 거치지 않음)
                pos_result = await session.execute(
                    select(Position).where(
                        Position.symbol == symbol,
                        Position.exchange == EXCHANGE_NAME,
                    )
                )
                db_pos = pos_result.scalar_one_or_none()
                if db_pos:
                    total_cost = db_pos.average_buy_price * db_pos.quantity + exec_price * exec_qty
                    db_pos.quantity += exec_qty
                    db_pos.average_buy_price = total_cost / db_pos.quantity if db_pos.quantity > 0 else 0
                    db_pos.total_invested += actual_margin + fee
                    db_pos.is_surge = True
                    if not db_pos.entered_at:
                        db_pos.entered_at = now
                    db_pos.last_trade_at = now
                else:
                    db_pos = Position(
                        exchange=EXCHANGE_NAME,
                        symbol=symbol,
                        quantity=exec_qty,
                        average_buy_price=exec_price,
                        total_invested=actual_margin + fee,
                        is_paper=self._mode == "paper",
                        is_surge=True,
                        entered_at=now,
                        last_trade_at=now,
                    )
                    session.add(db_pos)

                # SL/TP/trailing 설정
                db_pos.direction = direction
                db_pos.leverage = self._leverage
                db_pos.stop_loss_pct = self._sl_pct
                db_pos.take_profit_pct = self._tp_pct
                db_pos.trailing_activation_pct = self._trail_activation_pct
                db_pos.trailing_stop_pct = self._trail_stop_pct
                db_pos.trailing_active = False
                db_pos.highest_price = exec_price
                db_pos.max_hold_hours = self._max_hold_minutes / 60.0

                # 선물 PM cash 조정 (commit 전에 반영 — 예외 시 rollback과 함께 원복)
                self._futures_pm.cash_balance -= (actual_margin + fee)

                await session.commit()

            # Track in memory
            self._positions[symbol] = SurgePositionState(
                symbol=symbol,
                direction=direction,
                entry_price=exec_price,
                quantity=exec_qty,
                margin=actual_margin,
                entry_time=now,
                peak_price=exec_price,
                trough_price=exec_price,
                surge_score=score,
            )

            self._daily_trades += 1
            self._cooldowns[symbol] = now + timedelta(seconds=self._cooldown_sec)

            logger.info("surge_entry",
                        symbol=symbol, direction=direction,
                        price=exec_price, qty=exec_qty,
                        score=round(score, 3), margin=round(actual_margin, 2))
            await emit_event(
                "info", "surge_trade",
                f"[Surge] {direction.upper()} {symbol} @ {exec_price:.2f}",
                detail=f"Score={score:.2f} | Size={actual_margin:.1f} USDT ({self._leverage}x)",
            )

        except Exception as e:
            logger.error("surge_entry_failed", symbol=symbol, error=str(e), exc_info=True)

    # ── Exit logic ───────────────────────────────────────────────

    async def _check_all_exits(self, tickers: dict[str, dict]) -> None:
        """Check exit conditions for all open positions."""
        to_close = []
        now = datetime.now(timezone.utc)

        for sym, pos in list(self._positions.items()):
            if sym not in tickers:
                continue
            price = tickers[sym]["last"]
            should_exit, exit_reason = self._check_exit_conditions(pos, price, now)
            if should_exit:
                to_close.append((sym, pos, price, exit_reason))

        for sym, pos, price, reason in to_close:
            await self._exit_position(sym, pos, price, reason)

    def _check_exit_conditions(
        self, pos: SurgePositionState, current_price: float, now: datetime,
    ) -> tuple[bool, str]:
        """Check all exit conditions for a position.

        Returns (should_exit, reason).
        """
        entry = pos.entry_price
        if entry <= 0:
            return False, ""

        if pos.direction == "long":
            pnl_pct = (current_price - entry) / entry * 100 * self._leverage

            # 1. Stop Loss
            if pnl_pct <= -self._sl_pct:
                return True, "SL"

            # 2. Take Profit
            if pnl_pct >= self._tp_pct:
                return True, "TP"

            # Update peak
            if current_price > pos.peak_price:
                pos.peak_price = current_price

            # 3. Trailing stop
            peak_pnl = (pos.peak_price - entry) / entry * 100 * self._leverage
            if peak_pnl >= self._trail_activation_pct:
                pos.trailing_active = True
                drawdown = (pos.peak_price - current_price) / pos.peak_price * 100 * self._leverage
                if drawdown >= self._trail_stop_pct:
                    return True, "Trailing"

        else:  # short
            pnl_pct = (entry - current_price) / entry * 100 * self._leverage

            # 1. Stop Loss
            if pnl_pct <= -self._sl_pct:
                return True, "SL"

            # 2. Take Profit
            if pnl_pct >= self._tp_pct:
                return True, "TP"

            # Update trough
            if current_price < pos.trough_price:
                pos.trough_price = current_price

            # 3. Trailing stop
            trough_pnl = (entry - pos.trough_price) / entry * 100 * self._leverage
            if trough_pnl >= self._trail_activation_pct:
                pos.trailing_active = True
                drawup = (current_price - pos.trough_price) / pos.trough_price * 100 * self._leverage
                if drawup >= self._trail_stop_pct:
                    return True, "Trailing"

        # 4. Time-based exit
        hold_minutes = (now - pos.entry_time).total_seconds() / 60
        if hold_minutes >= self._max_hold_minutes:
            return True, "TimeExpiry"

        return False, ""

    async def _exit_position(
        self, symbol: str, pos: SurgePositionState, price: float, reason: str,
    ) -> None:
        """Execute position exit."""
        try:
            sf = get_session_factory()
            async with sf() as session:
                side = "sell" if pos.direction == "long" else "buy"
                signal = Signal(
                    strategy_name="surge_detector",
                    signal_type=SignalType.SELL if pos.direction == "long" else SignalType.BUY,
                    confidence=0.99,
                    reason=f"Surge exit: {reason}",
                )

                order = await self._order_manager.create_order(
                    session=session,
                    symbol=symbol,
                    side=side,
                    amount=pos.quantity,
                    price=price,
                    signal=signal,
                    order_type="market",
                    direction=pos.direction,
                    leverage=self._leverage,
                    entry_price=pos.entry_price,
                )

                exec_price = order.executed_price or price
                exec_qty = order.executed_quantity or pos.quantity
                fee = order.fee or (exec_price * exec_qty * FEE_PCT)

                # Calculate PnL
                if pos.direction == "long":
                    raw_pnl_pct = (exec_price - pos.entry_price) / pos.entry_price * 100
                else:
                    raw_pnl_pct = (pos.entry_price - exec_price) / pos.entry_price * 100
                lev_pnl_pct = raw_pnl_pct * self._leverage
                fee_pct = FEE_PCT * self._leverage * 2 * 100
                net_pnl_pct = lev_pnl_pct - fee_pct
                pnl_usdt = pos.margin * net_pnl_pct / 100

                cost_return = pos.margin + pnl_usdt

                # DB Position 직접 업데이트 (PM 거치지 않음)
                pos_result = await session.execute(
                    select(Position).where(
                        Position.symbol == symbol,
                        Position.exchange == EXCHANGE_NAME,
                    )
                )
                db_pos = pos_result.scalar_one_or_none()
                now = datetime.now(timezone.utc)
                if db_pos:
                    db_pos.quantity = 0
                    db_pos.average_buy_price = 0
                    db_pos.total_invested = 0
                    db_pos.is_surge = False
                    db_pos.entered_at = None
                    db_pos.last_trade_at = now
                    db_pos.last_sell_at = now

                # 선물 PM cash 조정 (commit 전에 반영)
                self._futures_pm.cash_balance += cost_return

                await session.commit()

            # Update counters
            if net_pnl_pct < 0:
                self._daily_losses += 1
                self._consecutive_losses += 1
                if self._consecutive_losses >= 3:
                    self._pause_until = datetime.now(timezone.utc) + timedelta(minutes=30)
                    logger.warning("surge_consecutive_loss_pause",
                                   losses=self._consecutive_losses)

                # COIN-20: 심볼별 연속 SL 추적 + 장기 쿨다운
                if reason == "SL":
                    sl_count = self._consecutive_sl_count.get(symbol, 0) + 1
                    self._consecutive_sl_count[symbol] = sl_count
                    if sl_count >= 2:
                        # 2+ 연속 SL → 장기 쿨다운 (180분 기본)
                        extended_cooldown = timedelta(seconds=self._consecutive_sl_cooldown_sec)
                        self._cooldowns[symbol] = datetime.now(timezone.utc) + extended_cooldown
                        logger.warning("surge_consecutive_sl_extended_cooldown",
                                       symbol=symbol, sl_count=sl_count,
                                       cooldown_min=self._consecutive_sl_cooldown_sec // 60)
            else:
                self._consecutive_losses = 0
                # COIN-20: 수익 시 연속 SL 카운터 리셋
                self._consecutive_sl_count.pop(symbol, None)

            # Remove from memory
            del self._positions[symbol]

            hold_min = (datetime.now(timezone.utc) - pos.entry_time).total_seconds() / 60

            logger.info("surge_exit",
                        symbol=symbol, direction=pos.direction,
                        reason=reason, pnl_pct=round(net_pnl_pct, 2),
                        pnl_usdt=round(pnl_usdt, 2),
                        hold_min=round(hold_min, 1))
            await emit_event(
                "info", "surge_trade",
                f"[Surge] CLOSED {symbol} | {net_pnl_pct:+.1f}% | {reason}",
                detail=f"PnL={pnl_usdt:+.2f} USDT | Hold={hold_min:.0f}min",
            )

        except Exception as e:
            logger.error("surge_exit_failed", symbol=symbol, error=str(e), exc_info=True)

    # ── Daily counter management ─────────────────────────────────

    def _reset_daily_counters_if_needed(self) -> None:
        """Reset daily counters at 00:00 UTC."""
        today = datetime.now(timezone.utc).date()
        if self._last_reset_date != today:
            self._daily_trades = 0
            self._daily_losses = 0
            self._consecutive_losses = 0
            self._pause_until = None
            self._last_reset_date = today

    # ── Recovery placeholder (EngineRegistry compatibility) ──────

    def set_recovery_manager(self, recovery) -> None:
        """Placeholder for EngineRegistry compatibility."""
        pass

    def set_broadcast_callback(self, callback) -> None:
        """Placeholder for EngineRegistry compatibility."""
        pass

    async def initialize(self) -> None:
        """Initialize engine state — restore open positions from DB."""
        try:
            sf = get_session_factory()
            async with sf() as session:
                result = await session.execute(
                    select(Position).where(
                        Position.exchange == EXCHANGE_NAME,
                        Position.quantity > 0,
                    )
                )
                positions = result.scalars().all()
                for pos in positions:
                    self._positions[pos.symbol] = SurgePositionState(
                        symbol=pos.symbol,
                        direction=pos.direction or "long",
                        entry_price=pos.average_buy_price,
                        quantity=pos.quantity,
                        margin=pos.total_invested,
                        entry_time=pos.entered_at or datetime.now(timezone.utc),
                        peak_price=pos.highest_price or pos.average_buy_price,
                        trough_price=pos.average_buy_price,
                        trailing_active=pos.trailing_active or False,
                        surge_score=0.0,
                    )
                if positions:
                    logger.info("surge_positions_restored", count=len(positions))
        except Exception as e:
            logger.warning("surge_init_restore_failed", error=str(e))

