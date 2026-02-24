"""
코인 자동 매매 시스템 — 백테스터
====================================
실행 예시:
  python backtest.py --symbol BTC/KRW --days 90 --balance 500000
  python backtest.py --symbol SOL/KRW --days 30 --strategies rsi bollinger_rsi
  python backtest.py --symbol ETH/KRW --days 60 --timeframe 4h
  python backtest.py --all-coins --days 30
  python backtest.py --symbol BTC/KRW --days 30 --stop-loss 5 --take-profit 8
  python backtest.py --all-coins --days 540 --timeframe 4h
  python backtest.py --all-coins --days 30 --no-trend-filter
  python backtest.py --symbol BTC/KRW --days 90 --trailing-activation 0

로테이션 모드:
  python backtest.py --rotation --days 180 --timeframe 4h
  python backtest.py --rotation --days 180 --surge-threshold 2.0
  python backtest.py --rotation --days 180 --no-strategy-confirm
"""

import asyncio
import argparse
import csv
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import pandas_ta as ta

# structlog 로그 레벨을 WARNING으로 올려서 백테스트 중 불필요한 출력 제거
logging.basicConfig(level=logging.WARNING)
import structlog
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
)

# ── 전략 등록을 위해 import ──────────────────────────────────
from strategies.volatility_breakout import VolatilityBreakoutStrategy
from strategies.ma_crossover import MACrossoverStrategy
from strategies.rsi_strategy import RSIStrategy
from strategies.macd_crossover import MACDCrossoverStrategy
from strategies.bollinger_rsi import BollingerRSIStrategy
from strategies.registry import StrategyRegistry
from strategies.combiner import SignalCombiner
from strategies.base import Signal
from core.enums import SignalType
from exchange.bithumb_adapter import BithumbAdapter
from exchange.data_models import Candle, Ticker


# ── 설정 ──────────────────────────────────────────────────────
TAKER_FEE = 0.0025      # 0.25% 빗썸 테이커 수수료
SLIPPAGE  = 0.001       # 0.1% 슬리피지
MIN_TRADE_KRW = 5_000   # 최소 거래대금


def _tf_hours(tf: str) -> float:
    """타임프레임 문자열 → 시간 단위 변환."""
    map_ = {"1m": 1/60, "5m": 5/60, "15m": 15/60, "1h": 1, "4h": 4, "1d": 24}
    return map_.get(tf, 1)

ALL_STRATEGIES = [
    "volatility_breakout", "ma_crossover", "rsi",
    "macd_crossover", "bollinger_rsi",
]

# 역발상 전략(RSI/Bollinger) 중심 가중치 — 백테스트 실적 기반
DEFAULT_WEIGHTS = {
    "volatility_breakout": 0.10,
    "ma_crossover":        0.10,
    "rsi":                 0.30,
    "macd_crossover":      0.15,
    "bollinger_rsi":       0.35,
}


# ── 데이터 클래스 ──────────────────────────────────────────────
@dataclass
class BacktestTrade:
    timestamp: datetime
    side: str          # "buy" / "sell" / "sell(sl)" / "sell(tp)" / "sell(close)"
    symbol: str
    price: float
    quantity: float
    cost: float
    fee: float
    strategy: str
    confidence: float
    reason: str
    pnl: float = 0.0
    pnl_pct: float = 0.0

@dataclass
class BacktestResult:
    symbol: str
    days: int
    initial_balance: float
    final_balance: float
    total_pnl: float
    total_pnl_pct: float
    max_drawdown_pct: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    profit_factor: float = 0.0
    buy_hold_pnl_pct: float = 0.0   # 단순 매수 후 보유 대비 비교용
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[tuple] = field(default_factory=list)
    strategy_stats: dict = field(default_factory=dict)


# ── 데이터 수집 (모듈 레벨 — Backtester / RotationBacktester 공유) ──
async def fetch_history(
    exchange: BithumbAdapter,
    symbol: str,
    timeframe: str,
    days: int,
) -> pd.DataFrame:
    """과거 OHLCV 데이터를 가져와 DataFrame으로 반환.

    페이지네이션으로 1500캔들 제한 우회, CSV 캐싱으로 재실행 가속.
    """
    candles_needed = int(days * 24 / _tf_hours(timeframe)) + 200
    tf_ms = int(_tf_hours(timeframe) * 3600 * 1000)

    # ── CSV 캐시 ──────────────────────────────────────────────
    cache_dir = Path(__file__).parent / ".cache"
    cache_dir.mkdir(exist_ok=True)
    safe_symbol = symbol.replace("/", "_")
    cache_path = cache_dir / f"{safe_symbol}_{timeframe}.csv"

    cached_df = None
    last_cached_ts = 0
    if cache_path.exists():
        cached_df = pd.read_csv(
            cache_path, parse_dates=["timestamp"], index_col="timestamp",
        )
        if cached_df.index.tz is None:
            cached_df.index = cached_df.index.tz_localize("UTC")
        cached_df.sort_index(inplace=True)
        last_cached_ts = int(cached_df.index[-1].timestamp() * 1000)

    # ── 필요한 시작 시점 ──────────────────────────────────────
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - candles_needed * tf_ms

    # 캐시가 충분하면 새 데이터만 추가 요청
    if cached_df is not None and last_cached_ts > start_ms:
        fetch_since = last_cached_ts + tf_ms  # 다음 캔들부터
    else:
        fetch_since = start_ms

    # ── 페이지네이션 루프 ─────────────────────────────────────
    all_new: list[Candle] = []
    page_limit = 1500
    cursor = fetch_since

    while cursor < now_ms:
        raw = await exchange.fetch_ohlcv(
            symbol, timeframe, limit=page_limit, since=cursor,
        )
        if not raw:
            break
        all_new.extend(raw)
        last_ts = int(raw[-1].timestamp.timestamp() * 1000)
        if last_ts <= cursor:
            break  # 더 이상 새 데이터 없음
        cursor = last_ts + tf_ms
        if len(raw) < page_limit:
            break  # 마지막 페이지

    # ── 새 데이터를 DataFrame으로 ─────────────────────────────
    if all_new:
        new_df = pd.DataFrame([{
            "timestamp": c.timestamp,
            "open": c.open, "high": c.high,
            "low": c.low, "close": c.close, "volume": c.volume,
        } for c in all_new])
        new_df.set_index("timestamp", inplace=True)
        new_df.sort_index(inplace=True)

        if cached_df is not None:
            df = pd.concat([cached_df, new_df])
            df = df[~df.index.duplicated(keep="last")]
            df.sort_index(inplace=True)
        else:
            df = new_df
    elif cached_df is not None:
        df = cached_df
    else:
        raise ValueError(f"{symbol} 데이터 없음")

    # ── 캐시 저장 ────────────────────────────────────────────
    df.to_csv(cache_path)

    # ── 기술적 지표 계산 (pandas_ta) ─────────────────────────
    df.ta.sma(length=5,  append=True)
    df.ta.sma(length=20, append=True)
    df.ta.sma(length=50, append=True)
    df.ta.sma(length=60, append=True)
    df.ta.ema(length=12, append=True)
    df.ta.ema(length=26, append=True)
    df.ta.rsi(length=14, append=True)
    df.ta.macd(fast=12, slow=26, signal=9, append=True)
    df.ta.bbands(length=20, std=2, append=True)
    df.ta.atr(length=14,  append=True)
    df.ta.adx(length=14,  append=True)

    df.dropna(inplace=True)

    # 날짜 필터: 최근 N일
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    if df.index.tz is not None:
        cutoff = cutoff.replace(tzinfo=df.index.tz)
    df = df[df.index >= cutoff]

    return df


# ── 백테스터 엔진 ──────────────────────────────────────────────
class Backtester:

    def __init__(
        self,
        exchange: BithumbAdapter,
        strategy_names: list[str],
        initial_balance: float = 500_000,
        min_confidence: float = 0.35,
        stop_loss_pct: float = 5.0,       # 고정 손절 퍼센트 (0이면 비활성)
        take_profit_pct: float = 10.0,     # 익절 퍼센트 (0이면 비활성)
        trend_filter: bool = True,          # 글로벌 추세 필터
        trailing_activation: float = 3.0,   # 트레일링 활성화 수익% (0이면 비활성)
        trailing_stop: float = 3.0,         # 고점 대비 하락 % (트레일링)
        adaptive_weights: bool = True,      # 적응형 가중치
        dynamic_sl: bool = False,           # ATR+시장상태 동적 손절
    ):
        self._exchange = exchange
        self._initial_balance = initial_balance
        self._min_confidence = min_confidence
        self._stop_loss_pct = stop_loss_pct
        self._take_profit_pct = take_profit_pct
        self._trend_filter = trend_filter
        self._trailing_activation = trailing_activation
        self._trailing_stop = trailing_stop
        self._adaptive_weights = adaptive_weights
        self._dynamic_sl = dynamic_sl

        # 전략 로드 (인스턴스 생성)
        all_strats = StrategyRegistry.create_all()
        self._strategies = {
            name: strat for name, strat in all_strats.items()
            if name in strategy_names
        }
        weights = {k: v for k, v in DEFAULT_WEIGHTS.items() if k in strategy_names}
        self._combiner = SignalCombiner(
            strategy_weights=weights,
            min_confidence=min_confidence,
        )

    async def fetch_history(
        self, symbol: str, timeframe: str, days: int
    ) -> pd.DataFrame:
        """Backtester 인스턴스 메서드 — 모듈 레벨 함수에 위임."""
        return await fetch_history(self._exchange, symbol, timeframe, days)

    def _execute_sell(
        self, ts, current_price, holdings, avg_buy_price,
        side_label, strategy_name, confidence, reason,
    ) -> tuple[float, float, BacktestTrade]:
        """매도 실행 → (proceeds, pnl, trade)"""
        exec_price = current_price * (1 - SLIPPAGE)
        cost = holdings * exec_price
        fee = cost * TAKER_FEE
        proceeds = cost - fee

        buy_cost = avg_buy_price * holdings
        pnl = proceeds - buy_cost
        pnl_pct = pnl / buy_cost * 100 if buy_cost > 0 else 0

        t = BacktestTrade(
            timestamp=ts, side=side_label, symbol="",
            price=exec_price, quantity=holdings,
            cost=cost, fee=fee,
            strategy=strategy_name,
            confidence=confidence,
            reason=reason,
            pnl=pnl, pnl_pct=round(pnl_pct, 2),
        )
        return proceeds, pnl, t

    async def run(self, symbol: str, timeframe: str = "1h", days: int = 30) -> BacktestResult:
        """심볼 하나에 대해 백테스트를 실행한다."""
        print(f"\n{'='*60}")
        print(f"  백테스트: {symbol} | {timeframe} | {days}일")
        print(f"  전략: {', '.join(self._strategies.keys())}")
        sl_str = "동적(ATR+시장)" if self._dynamic_sl else (
            f"고정 {self._stop_loss_pct}%" if self._stop_loss_pct > 0 else "OFF")
        tp_str = f"{self._take_profit_pct}%" if self._take_profit_pct > 0 else "OFF"
        tf_str = "ON" if self._trend_filter else "OFF"
        trail_str = (f"활성 +{self._trailing_activation}% / 스탑 -{self._trailing_stop}%"
                     if self._trailing_activation > 0 else "OFF")
        aw_str = "ON" if self._adaptive_weights else "OFF"
        print(f"  손절: {sl_str} | 익절: {tp_str} | 최소 신뢰도: {self._min_confidence}")
        print(f"  추세 필터: {tf_str} | 트레일링: {trail_str} | 적응형 가중치: {aw_str}")
        print(f"{'='*60}")

        df = await self.fetch_history(symbol, timeframe, days)
        print(f"  데이터: {len(df)}개 캔들 ({df.index[0].date()} ~ {df.index[-1].date()})")

        # Buy & Hold 기준 (첫 캔들 종가 → 마지막 캔들 종가)
        first_close = float(df.iloc[0]["close"])
        last_close = float(df.iloc[-1]["close"])
        buy_hold_pnl_pct = (last_close - first_close) / first_close * 100

        # ── 시뮬레이션 상태 ─────────────────────────────────────
        cash = self._initial_balance
        holdings = 0.0
        avg_buy_price = 0.0
        dynamic_sl_pct = self._stop_loss_pct  # 동적 손절 (매수/시장전환 시 갱신)
        current_market_state = "sideways"
        peak_price_since_entry = 0.0      # 트레일링 스탑용
        trailing_active = False            # 트레일링 활성 여부
        trades: list[BacktestTrade] = []
        equity_curve: list[tuple] = []
        peak_equity = self._initial_balance
        max_drawdown = 0.0
        last_trade_idx = -9999

        strategy_wins   = {name: 0 for name in self._strategies}
        strategy_losses = {name: 0 for name in self._strategies}
        strategy_trades = {name: 0 for name in self._strategies}

        win_count  = 0
        loss_count = 0
        total_win_pct = 0.0
        total_loss_pct = 0.0

        # 적응형 가중치: 마지막 재평가 인덱스
        last_weight_eval_idx = -9999

        # ── 캔들 루프 ───────────────────────────────────────────
        rows = list(df.iterrows())
        for i, (ts, row) in enumerate(rows):
            current_price = float(row["close"])
            current_equity = cash + holdings * current_price

            # 에쿼티 곡선 / 낙폭 계산
            equity_curve.append((ts, current_equity))
            if current_equity > peak_equity:
                peak_equity = current_equity
            drawdown = (peak_equity - current_equity) / peak_equity * 100
            if drawdown > max_drawdown:
                max_drawdown = drawdown

            if i < 60:  # 지표 안정화 구간 스킵
                continue

            # ── 24캔들(≈1일)마다 시장 상태 재평가 ─────────────────
            if i - last_weight_eval_idx >= 24:
                current_market_state = _detect_market_state(row)
                if self._adaptive_weights:
                    new_weights = _get_adaptive_weights(current_market_state)
                    filtered = {k: v for k, v in new_weights.items() if k in self._strategies}
                    self._combiner.update_weights(filtered)
                # 보유 중이면 동적 손절도 시장 상태에 맞게 갱신
                if self._dynamic_sl and holdings > 0:
                    dynamic_sl_pct = _calc_dynamic_sl(row, current_price, current_market_state)
                last_weight_eval_idx = i

            # ── 손절/익절/트레일링 체크 (보유 중일 때) ────────────
            if holdings > 0 and avg_buy_price > 0:
                unrealized_pct = (current_price - avg_buy_price) / avg_buy_price * 100

                # 트레일링 스탑: 고점 추적
                if current_price > peak_price_since_entry:
                    peak_price_since_entry = current_price

                # 트레일링 활성화 체크
                if (self._trailing_activation > 0
                        and not trailing_active
                        and unrealized_pct >= self._trailing_activation):
                    trailing_active = True

                # 트레일링 스탑 발동
                if trailing_active and self._trailing_stop > 0:
                    drop_from_peak = (peak_price_since_entry - current_price) / peak_price_since_entry * 100
                    if drop_from_peak >= self._trailing_stop:
                        actual_pnl_pct = unrealized_pct
                        proceeds, pnl, t = self._execute_sell(
                            ts, current_price, holdings, avg_buy_price,
                            "sell(trail)", "trailing_stop", 0,
                            f"트레일링 스탑: 고점 {peak_price_since_entry:,.0f} 대비 "
                            f"-{drop_from_peak:.1f}% (수익 {actual_pnl_pct:+.1f}%)",
                        )
                        t.symbol = symbol
                        cash += proceeds
                        trades.append(t)
                        if pnl > 0:
                            win_count += 1
                            total_win_pct += t.pnl_pct
                        else:
                            loss_count += 1
                            total_loss_pct += abs(t.pnl_pct)
                        holdings = 0
                        avg_buy_price = 0
                        peak_price_since_entry = 0
                        trailing_active = False
                        last_trade_idx = i
                        continue

                # 손절 (동적 ATR 또는 고정 %)
                if dynamic_sl_pct > 0 and unrealized_pct <= -dynamic_sl_pct:
                    proceeds, pnl, t = self._execute_sell(
                        ts, current_price, holdings, avg_buy_price,
                        "sell(sl)", "stop_loss", 0,
                        f"손절: {unrealized_pct:.1f}% (한도: -{dynamic_sl_pct:.1f}%)",
                    )
                    t.symbol = symbol
                    cash += proceeds
                    trades.append(t)
                    loss_count += 1
                    total_loss_pct += abs(t.pnl_pct)
                    holdings = 0
                    avg_buy_price = 0
                    peak_price_since_entry = 0
                    trailing_active = False
                    last_trade_idx = i
                    continue

                # 익절 (트레일링 미활성 시에만)
                if (not trailing_active
                        and self._take_profit_pct > 0
                        and unrealized_pct >= self._take_profit_pct):
                    proceeds, pnl, t = self._execute_sell(
                        ts, current_price, holdings, avg_buy_price,
                        "sell(tp)", "take_profit", 0,
                        f"익절: +{unrealized_pct:.1f}% (목표: +{self._take_profit_pct}%)",
                    )
                    t.symbol = symbol
                    cash += proceeds
                    trades.append(t)
                    win_count += 1
                    total_win_pct += t.pnl_pct
                    holdings = 0
                    avg_buy_price = 0
                    peak_price_since_entry = 0
                    trailing_active = False
                    last_trade_idx = i
                    continue

            # 쿨다운: 마지막 매매로부터 최소 3캔들 후
            if i - last_trade_idx < 3:
                continue

            # ── 전략 신호 수집 ─────────────────────────────────
            slice_df = df.iloc[max(0, i-200):i+1]  # 최근 200캔들만 전달 (성능)
            ticker = Ticker(
                symbol=symbol,
                last=current_price,
                bid=current_price * 0.9995,
                ask=current_price * 1.0005,
                high=float(row["high"]),
                low=float(row["low"]),
                volume=float(row.get("volume", 0)),
                timestamp=ts,
            )

            signals: list[Signal] = []
            for name, strategy in self._strategies.items():
                try:
                    sig = await strategy.analyze(slice_df.copy(), ticker)
                    signals.append(sig)
                except Exception:
                    pass

            if not signals:
                continue

            decision = self._combiner.combine(signals)

            # ── 글로벌 추세 필터: 하락장에서 매수 차단 ────────────
            if (self._trend_filter
                    and decision.action == SignalType.BUY
                    and _is_downtrend(row)):
                continue  # 매수 차단, 매도는 허용

            # ── 매수 ──────────────────────────────────────────
            if decision.action == SignalType.BUY and holdings == 0:
                if decision.combined_confidence < self._min_confidence:
                    continue

                trade_size = cash * 0.95
                if trade_size < MIN_TRADE_KRW:
                    continue

                exec_price = current_price * (1 + SLIPPAGE)
                fee = trade_size * TAKER_FEE
                qty = (trade_size - fee) / exec_price
                cost = qty * exec_price

                cash -= (cost + fee)
                holdings = qty
                avg_buy_price = exec_price
                peak_price_since_entry = current_price
                trailing_active = False

                # 동적 손절 계산
                if self._dynamic_sl:
                    dynamic_sl_pct = _calc_dynamic_sl(row, current_price, current_market_state)
                else:
                    dynamic_sl_pct = self._stop_loss_pct

                # BUY 신호를 낸 전략 중 최고 신뢰도
                buy_signals = [s for s in signals if s.signal_type == SignalType.BUY]
                best_signal = max(buy_signals, key=lambda s: s.confidence) if buy_signals else signals[0]

                t = BacktestTrade(
                    timestamp=ts, side="buy", symbol=symbol,
                    price=exec_price, quantity=qty, cost=cost, fee=fee,
                    strategy=best_signal.strategy_name,
                    confidence=float(decision.combined_confidence),
                    reason=best_signal.reason,
                )
                trades.append(t)
                strategy_trades[best_signal.strategy_name] = strategy_trades.get(best_signal.strategy_name, 0) + 1
                last_trade_idx = i

            # ── 매도 (전략 신호) ──────────────────────────────
            elif decision.action == SignalType.SELL and holdings > 0:
                proceeds, pnl, t = self._execute_sell(
                    ts, current_price, holdings, avg_buy_price,
                    "sell", "", float(decision.combined_confidence), "",
                )
                t.symbol = symbol

                sell_signals = [s for s in signals if s.signal_type == SignalType.SELL]
                best_signal = max(sell_signals, key=lambda s: s.confidence) if sell_signals else signals[0]
                t.strategy = best_signal.strategy_name
                t.reason = best_signal.reason

                cash += proceeds
                trades.append(t)
                strategy_trades[best_signal.strategy_name] = strategy_trades.get(best_signal.strategy_name, 0) + 1
                last_trade_idx = i

                if pnl > 0:
                    win_count += 1
                    total_win_pct += t.pnl_pct
                    strategy_wins[best_signal.strategy_name] = strategy_wins.get(best_signal.strategy_name, 0) + 1
                else:
                    loss_count += 1
                    total_loss_pct += abs(t.pnl_pct)
                    strategy_losses[best_signal.strategy_name] = strategy_losses.get(best_signal.strategy_name, 0) + 1

                holdings = 0
                avg_buy_price = 0
                peak_price_since_entry = 0
                trailing_active = False

        # ── 미청산 포지션 강제 청산 ────────────────────────────
        if holdings > 0:
            proceeds, pnl, t = self._execute_sell(
                df.index[-1], last_close, holdings, avg_buy_price,
                "sell(close)", "forced_close", 0, "백테스트 종료 강제 청산",
            )
            t.symbol = symbol
            cash += proceeds
            trades.append(t)
            if pnl > 0:
                win_count += 1
                total_win_pct += t.pnl_pct
            else:
                loss_count += 1
                total_loss_pct += abs(t.pnl_pct)

        final_balance = cash
        total_pnl = final_balance - self._initial_balance
        total_pnl_pct = total_pnl / self._initial_balance * 100
        total_sell_trades = len([t for t in trades if "sell" in t.side])
        win_rate = win_count / (win_count + loss_count) * 100 if (win_count + loss_count) > 0 else 0

        avg_win = total_win_pct / win_count if win_count > 0 else 0
        avg_loss = total_loss_pct / loss_count if loss_count > 0 else 0
        profit_factor = (total_win_pct / total_loss_pct) if total_loss_pct > 0 else float("inf") if total_win_pct > 0 else 0

        # 전략별 통계
        strategy_stats = {}
        for name in self._strategies:
            n = strategy_trades.get(name, 0)
            w = strategy_wins.get(name, 0)
            l = strategy_losses.get(name, 0)
            strategy_stats[name] = {
                "trades": n,
                "wins": w,
                "losses": l,
                "win_rate": round(w / (w + l) * 100, 1) if (w + l) > 0 else 0,
            }

        return BacktestResult(
            symbol=symbol,
            days=days,
            initial_balance=self._initial_balance,
            final_balance=round(final_balance, 0),
            total_pnl=round(total_pnl, 0),
            total_pnl_pct=round(total_pnl_pct, 2),
            max_drawdown_pct=round(max_drawdown, 2),
            total_trades=total_sell_trades,
            winning_trades=win_count,
            losing_trades=loss_count,
            win_rate=round(win_rate, 1),
            avg_win_pct=round(avg_win, 2),
            avg_loss_pct=round(avg_loss, 2),
            profit_factor=round(profit_factor, 2),
            buy_hold_pnl_pct=round(buy_hold_pnl_pct, 2),
            trades=trades,
            equity_curve=equity_curve,
            strategy_stats=strategy_stats,
        )


def _is_downtrend(row) -> bool:
    """SMA20 < SMA60이면 하락 추세로 판단."""
    sma20 = row.get("SMA_20")
    sma60 = row.get("SMA_60")
    if sma20 is None or sma60 is None or pd.isna(sma20) or pd.isna(sma60):
        return False
    return float(sma20) < float(sma60)


def _detect_market_state(row) -> str:
    """SMA20/SMA60 + ADX + RSI로 시장 상태 감지.

    Returns: 'strong_uptrend', 'uptrend', 'sideways', 'downtrend', 'crash'
    """
    from core.enums import MarketState

    sma20 = row.get("SMA_20")
    sma60 = row.get("SMA_60")
    adx = row.get("ADX_14")
    rsi = row.get("RSI_14")

    # 기본값
    if any(v is None or (isinstance(v, float) and pd.isna(v))
           for v in [sma20, sma60, adx, rsi]):
        return MarketState.SIDEWAYS.value

    sma20, sma60, adx, rsi = float(sma20), float(sma60), float(adx), float(rsi)

    uptrend = sma20 > sma60
    strong_trend = adx > 25

    if uptrend and strong_trend and rsi > 55:
        return MarketState.STRONG_UPTREND.value
    elif uptrend:
        return MarketState.UPTREND.value
    elif not uptrend and strong_trend and rsi < 35:
        return MarketState.CRASH.value
    elif not uptrend and (strong_trend or rsi < 45):
        return MarketState.DOWNTREND.value
    else:
        return MarketState.SIDEWAYS.value


# 시장 상태별 적응형 가중치 프로필 (역발상 전략 중심)
_ADAPTIVE_WEIGHT_PROFILES = {
    "strong_uptrend": {
        "volatility_breakout": 0.15,
        "ma_crossover":        0.15,
        "rsi":                 0.20,
        "macd_crossover":      0.20,
        "bollinger_rsi":       0.30,
    },
    "uptrend": {
        "volatility_breakout": 0.10,
        "ma_crossover":        0.15,
        "rsi":                 0.25,
        "macd_crossover":      0.20,
        "bollinger_rsi":       0.30,
    },
    "sideways": {
        "volatility_breakout": 0.05,
        "ma_crossover":        0.05,
        "rsi":                 0.35,
        "macd_crossover":      0.15,
        "bollinger_rsi":       0.40,
    },
    "downtrend": {
        "volatility_breakout": 0.00,
        "ma_crossover":        0.10,
        "rsi":                 0.35,
        "macd_crossover":      0.15,
        "bollinger_rsi":       0.40,
    },
    "crash": {
        "volatility_breakout": 0.00,
        "ma_crossover":        0.05,
        "rsi":                 0.40,
        "macd_crossover":      0.10,
        "bollinger_rsi":       0.45,
    },
}


def _get_adaptive_weights(market_state: str) -> dict[str, float]:
    """시장 상태에 맞는 가중치 반환."""
    return _ADAPTIVE_WEIGHT_PROFILES.get(market_state, DEFAULT_WEIGHTS).copy()


# ── 시장 상태별 동적 손절 프로필 (하이브리드) ─────────────────────
# (atr_multiplier, floor_pct, cap_pct)
# floor: 최소 손절폭. BTC처럼 ATR이 작은 코인이 floor에 걸림
#   → 너무 낮으면 상승장 조정(-3~5%)에서 조기 탈출
#   → crash만 타이트(3%), 나머지는 4% 이상으로 여유
_DYNAMIC_SL_PROFILES = {
    "strong_uptrend": (2.5, 4.0, 12.0),  # 넓게 — 수익 구간 오래 버팀
    "uptrend":        (2.0, 4.0, 10.0),
    "sideways":       (2.0, 4.0,  7.0),
    "downtrend":      (2.0, 4.0,  7.0),  # 조정일 수 있으므로 moderate
    "crash":          (1.5, 3.0,  5.0),  # 진짜 폭락만 타이트
}
_DEFAULT_SL_PROFILE = (2.0, 4.0, 7.0)


def _calc_dynamic_sl(row, price: float, market_state: str) -> float:
    """ATR + 시장 상태 기반 동적 손절 % 계산.

    Returns: 손절 퍼센트 (예: 5.0 → -5% 시 손절)
    """
    atr_mult, floor_pct, cap_pct = _DYNAMIC_SL_PROFILES.get(
        market_state, _DEFAULT_SL_PROFILE,
    )
    atr_val = row.get("ATRr_14")
    if atr_val is None or (isinstance(atr_val, float) and pd.isna(atr_val)) or price <= 0:
        return cap_pct  # ATR 없으면 캡으로 폴백

    atr_pct = float(atr_val) / price * 100
    raw_sl = atr_pct * atr_mult
    return max(floor_pct, min(raw_sl, cap_pct))


def print_result(r: BacktestResult):
    """백테스트 결과를 보기 좋게 출력."""
    pnl_sign = "+" if r.total_pnl >= 0 else ""
    dd_warn  = " !!!" if r.max_drawdown_pct > 15 else ""
    bh_sign  = "+" if r.buy_hold_pnl_pct >= 0 else ""
    alpha    = r.total_pnl_pct - r.buy_hold_pnl_pct
    alpha_sign = "+" if alpha >= 0 else ""

    print(f"\n{'='*60}")
    print(f"  {r.symbol} 백테스트 결과 ({r.days}일)")
    print(f"{'='*60}")
    print(f"  초기 자산    : {r.initial_balance:>12,.0f} 원")
    print(f"  최종 자산    : {r.final_balance:>12,.0f} 원")
    print(f"  총 수익      : {pnl_sign}{r.total_pnl:>10,.0f} 원  ({pnl_sign}{r.total_pnl_pct:.2f}%)")
    print(f"  최대 낙폭    : {r.max_drawdown_pct:.2f}%{dd_warn}")
    print(f"{'─'*60}")
    print(f"  단순 보유(B&H): {bh_sign}{r.buy_hold_pnl_pct:.2f}%")
    print(f"  초과 수익(a) : {alpha_sign}{alpha:.2f}%")
    print(f"{'─'*60}")
    print(f"  총 매매 횟수 : {r.total_trades}회")
    print(f"  승리 / 패배  : {r.winning_trades}승 / {r.losing_trades}패")
    print(f"  승률         : {r.win_rate:.1f}%")
    print(f"  평균 수익    : +{r.avg_win_pct:.2f}% | 평균 손실: -{r.avg_loss_pct:.2f}%")
    print(f"  Profit Factor: {r.profit_factor:.2f}")
    print(f"{'─'*60}")
    print(f"  전략별 기여:")
    for name, stat in r.strategy_stats.items():
        if stat["trades"] > 0:
            print(f"    {name:<22}: {stat['trades']:>3}회  승률 {stat['win_rate']:>5.1f}%")
    print(f"{'─'*60}")

    # 모든 매도 거래 출력
    sell_trades = [t for t in r.trades if "sell" in t.side]
    if sell_trades:
        print(f"  매매 내역 (매도 기준, 최근 10건):")
        for t in sell_trades[-10:]:
            arrow = "+" if t.pnl >= 0 else ""
            tag = {"sell(sl)": "손절", "sell(tp)": "익절", "sell(close)": "강제청산"}.get(t.side, "신호")
            print(f"    {t.timestamp.strftime('%m/%d %H:%M')}  {arrow}{t.pnl_pct:>+6.1f}%  "
                  f"[{tag}] {t.reason[:50]}")
    print(f"{'='*60}\n")


# ── 거래량 서지 스코어 ────────────────────────────────────────────
def _calc_surge_score(df: pd.DataFrame, index: int, lookback: int = 20) -> float:
    """현재 캔들의 거래량 / 최근 lookback 캔들 평균 거래량."""
    if index < lookback:
        return 0.0
    vols = df.iloc[index - lookback:index]["volume"].values
    avg_vol = vols.mean()
    if avg_vol <= 0:
        return 0.0
    return float(df.iloc[index]["volume"]) / avg_vol


# ── 로테이션 백테스트 결과 ────────────────────────────────────────
@dataclass
class RotationResult:
    days: int
    initial_balance: float
    final_balance: float
    total_pnl: float
    total_pnl_pct: float
    max_drawdown_pct: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    profit_factor: float = 0.0
    buy_hold_btc_pnl_pct: float = 0.0
    total_rotations: int = 0
    symbols_traded: list[str] = field(default_factory=list)
    avg_hold_candles: float = 0.0
    rotation_log: list[dict] = field(default_factory=list)
    trades: list[BacktestTrade] = field(default_factory=list)


# ── 기본 로테이션 대상 코인 (빗썸 거래대금 상위) ──────────────────
DEFAULT_ROTATION_COINS = [
    "BTC/KRW", "ETH/KRW", "XRP/KRW", "SOL/KRW", "DOGE/KRW",
    "WLD/KRW", "AVAX/KRW", "DOT/KRW", "LINK/KRW", "ATOM/KRW",
    "BCH/KRW", "ETC/KRW", "ADA/KRW", "AAVE/KRW", "UNI/KRW",
    "SAND/KRW", "AXS/KRW", "STEEM/KRW", "MANA/KRW", "VIRTUAL/KRW",
]


# ── 로테이션 백테스터 ─────────────────────────────────────────────
class RotationBacktester:

    def __init__(
        self,
        exchange: BithumbAdapter,
        strategy_names: list[str],
        symbols: list[str] | None = None,
        initial_balance: float = 500_000,
        min_confidence: float = 0.35,
        stop_loss_pct: float = 5.0,
        take_profit_pct: float = 10.0,
        trend_filter: bool = True,
        trailing_activation: float = 3.0,
        trailing_stop: float = 3.0,
        adaptive_weights: bool = True,
        dynamic_sl: bool = False,
        surge_threshold: float = 3.0,
        rotation_cooldown: int = 6,
        require_strategy_confirm: bool = True,
    ):
        self._exchange = exchange
        self._initial_balance = initial_balance
        self._min_confidence = min_confidence
        self._stop_loss_pct = stop_loss_pct
        self._take_profit_pct = take_profit_pct
        self._trend_filter = trend_filter
        self._trailing_activation = trailing_activation
        self._trailing_stop = trailing_stop
        self._adaptive_weights = adaptive_weights
        self._dynamic_sl = dynamic_sl
        self._surge_threshold = surge_threshold
        self._rotation_cooldown = rotation_cooldown
        self._require_strategy_confirm = require_strategy_confirm
        self._symbols = symbols or DEFAULT_ROTATION_COINS

        all_strats = StrategyRegistry.create_all()
        self._strategies = {
            name: strat for name, strat in all_strats.items()
            if name in strategy_names
        }
        weights = {k: v for k, v in DEFAULT_WEIGHTS.items() if k in strategy_names}
        self._combiner = SignalCombiner(
            strategy_weights=weights,
            min_confidence=min_confidence,
        )

    async def prefetch_all(
        self, timeframe: str, days: int,
    ) -> dict[str, pd.DataFrame]:
        """전 코인 데이터 프리페치. 실패 코인 건너뜀."""
        all_data: dict[str, pd.DataFrame] = {}
        total = len(self._symbols)
        for idx, sym in enumerate(self._symbols, 1):
            try:
                print(f"  [{idx}/{total}] {sym} 데이터 로딩...", end="", flush=True)
                df = await fetch_history(self._exchange, sym, timeframe, days)
                all_data[sym] = df
                print(f" {len(df)}캔들")
            except Exception as e:
                print(f" 실패({e})")
        return all_data

    def _scan_surges(
        self, all_data: dict[str, pd.DataFrame], ts,
    ) -> list[tuple[str, float]]:
        """전 코인 서지 스코어 계산. threshold 이상만 (symbol, score) 반환."""
        surges = []
        for sym, df in all_data.items():
            if ts not in df.index:
                continue
            idx = df.index.get_loc(ts)
            if isinstance(idx, slice):
                idx = idx.start
            score = _calc_surge_score(df, idx)
            if score >= self._surge_threshold:
                surges.append((sym, score))
        surges.sort(key=lambda x: x[1], reverse=True)
        return surges

    async def _get_strategy_confirmation(
        self, df: pd.DataFrame, ticker: Ticker, i: int,
    ) -> tuple[bool, float]:
        """기존 전략 파이프라인으로 BUY 신호 확인.

        Returns: (confirmed, confidence)
        """
        row = df.iloc[i]

        # 추세 필터
        if self._trend_filter and _is_downtrend(row):
            return False, 0.0

        slice_df = df.iloc[max(0, i - 200):i + 1]
        signals: list[Signal] = []
        for name, strategy in self._strategies.items():
            try:
                sig = await strategy.analyze(slice_df.copy(), ticker)
                signals.append(sig)
            except Exception:
                pass

        if not signals:
            return False, 0.0

        decision = self._combiner.combine(signals)
        if decision.action == SignalType.BUY and decision.combined_confidence >= self._min_confidence:
            return True, float(decision.combined_confidence)
        return False, 0.0

    async def run(self, timeframe: str = "4h", days: int = 180) -> RotationResult:
        """로테이션 백테스트 실행."""
        print(f"\n{'='*60}")
        print(f"  로테이션 백테스트 | {timeframe} | {days}일")
        print(f"  대상: {len(self._symbols)}개 코인")
        print(f"  서지 임계: {self._surge_threshold}x | 쿨다운: {self._rotation_cooldown}캔들")
        print(f"  전략 확인: {'ON' if self._require_strategy_confirm else 'OFF'}")
        sl_str = "동적(ATR+시장)" if self._dynamic_sl else (
            f"고정 {self._stop_loss_pct}%" if self._stop_loss_pct > 0 else "OFF")
        tp_str = f"{self._take_profit_pct}%" if self._take_profit_pct > 0 else "OFF"
        trail_str = (f"+{self._trailing_activation}%/-{self._trailing_stop}%"
                     if self._trailing_activation > 0 else "OFF")
        print(f"  손절: {sl_str} | 익절: {tp_str} | 트레일링: {trail_str}")
        print(f"{'='*60}")

        # 1. 데이터 프리페치
        all_data = await self.prefetch_all(timeframe, days)
        if not all_data:
            raise ValueError("사용 가능한 코인 데이터 없음")
        print(f"\n  {len(all_data)}개 코인 로딩 완료")

        # BTC B&H 기준
        btc_df = all_data.get("BTC/KRW")
        btc_bh_pct = 0.0
        if btc_df is not None and len(btc_df) > 1:
            btc_bh_pct = (float(btc_df.iloc[-1]["close"]) - float(btc_df.iloc[0]["close"])) / float(btc_df.iloc[0]["close"]) * 100

        # 2. 유니온 타임스탬프 인덱스
        all_timestamps = sorted(set().union(*(df.index for df in all_data.values())))
        print(f"  타임라인: {len(all_timestamps)}개 캔들 ({all_timestamps[0].date()} ~ {all_timestamps[-1].date()})")

        # 3. 시뮬레이션 상태
        cash = self._initial_balance
        current_symbol: str | None = None
        holdings = 0.0
        avg_buy_price = 0.0
        dynamic_sl_pct = self._stop_loss_pct
        current_market_state = "sideways"
        peak_price_since_entry = 0.0
        trailing_active = False
        entry_candle_idx = 0

        trades: list[BacktestTrade] = []
        rotation_log: list[dict] = []
        symbols_traded: set[str] = set()
        total_rotations = 0
        total_hold_candles = 0
        num_completed_trades = 0

        peak_equity = self._initial_balance
        max_drawdown = 0.0

        win_count = 0
        loss_count = 0
        total_win_pct = 0.0
        total_loss_pct = 0.0

        last_rotation_idx = -9999
        last_weight_eval_idx = -9999

        # 4. 캔들 루프
        for candle_idx, ts in enumerate(all_timestamps):
            # 에쿼티 계산
            if current_symbol and holdings > 0 and current_symbol in all_data:
                sym_df = all_data[current_symbol]
                if ts in sym_df.index:
                    cur_price = float(sym_df.loc[ts, "close"])
                else:
                    cur_price = avg_buy_price  # 해당 캔들 없으면 매입가 유지
                current_equity = cash + holdings * cur_price
            else:
                cur_price = 0.0
                current_equity = cash

            if current_equity > peak_equity:
                peak_equity = current_equity
            drawdown = (peak_equity - current_equity) / peak_equity * 100
            if drawdown > max_drawdown:
                max_drawdown = drawdown

            if candle_idx < 60:
                continue

            # ── 24캔들마다 시장 상태 재평가 ─────────────────────
            if candle_idx - last_weight_eval_idx >= 24:
                # BTC 기준으로 시장 상태 판단
                if btc_df is not None and ts in btc_df.index:
                    current_market_state = _detect_market_state(btc_df.loc[ts])
                    if self._adaptive_weights:
                        new_weights = _get_adaptive_weights(current_market_state)
                        filtered = {k: v for k, v in new_weights.items() if k in self._strategies}
                        self._combiner.update_weights(filtered)
                    # 보유 중이면 동적 손절도 시장 상태에 맞게 갱신
                    if self._dynamic_sl and current_symbol and holdings > 0:
                        if current_symbol in all_data and ts in all_data[current_symbol].index:
                            sym_row = all_data[current_symbol].loc[ts]
                            cur_p = float(sym_row["close"])
                            dynamic_sl_pct = _calc_dynamic_sl(sym_row, cur_p, current_market_state)
                last_weight_eval_idx = candle_idx

            # ── 보유 중: SL/TP/트레일링 체크 ──────────────────
            if current_symbol and holdings > 0 and current_symbol in all_data:
                sym_df = all_data[current_symbol]
                if ts not in sym_df.index:
                    continue  # 해당 코인 캔들 없음

                cur_price = float(sym_df.loc[ts, "close"])
                unrealized_pct = (cur_price - avg_buy_price) / avg_buy_price * 100

                if cur_price > peak_price_since_entry:
                    peak_price_since_entry = cur_price

                # 트레일링 활성화
                if (self._trailing_activation > 0
                        and not trailing_active
                        and unrealized_pct >= self._trailing_activation):
                    trailing_active = True

                # 트레일링 스탑 발동
                if trailing_active and self._trailing_stop > 0:
                    drop_from_peak = (peak_price_since_entry - cur_price) / peak_price_since_entry * 100
                    if drop_from_peak >= self._trailing_stop:
                        exec_price = cur_price * (1 - SLIPPAGE)
                        cost = holdings * exec_price
                        fee = cost * TAKER_FEE
                        proceeds = cost - fee
                        pnl = proceeds - (avg_buy_price * holdings)
                        pnl_pct = pnl / (avg_buy_price * holdings) * 100

                        t = BacktestTrade(
                            timestamp=ts, side="sell(trail)", symbol=current_symbol,
                            price=exec_price, quantity=holdings, cost=cost, fee=fee,
                            strategy="trailing_stop", confidence=0,
                            reason=f"트레일링 스탑 ({current_symbol}) "
                                   f"고점 대비 -{drop_from_peak:.1f}%",
                            pnl=pnl, pnl_pct=round(pnl_pct, 2),
                        )
                        trades.append(t)
                        cash += proceeds
                        total_hold_candles += candle_idx - entry_candle_idx
                        num_completed_trades += 1
                        if pnl > 0:
                            win_count += 1; total_win_pct += abs(pnl_pct)
                        else:
                            loss_count += 1; total_loss_pct += abs(pnl_pct)
                        current_symbol = None; holdings = 0; avg_buy_price = 0
                        peak_price_since_entry = 0; trailing_active = False
                        last_rotation_idx = candle_idx
                        continue

                # 손절 (동적 ATR 또는 고정 %)
                if dynamic_sl_pct > 0 and unrealized_pct <= -dynamic_sl_pct:
                    exec_price = cur_price * (1 - SLIPPAGE)
                    cost = holdings * exec_price
                    fee = cost * TAKER_FEE
                    proceeds = cost - fee
                    pnl = proceeds - (avg_buy_price * holdings)
                    pnl_pct = pnl / (avg_buy_price * holdings) * 100

                    t = BacktestTrade(
                        timestamp=ts, side="sell(sl)", symbol=current_symbol,
                        price=exec_price, quantity=holdings, cost=cost, fee=fee,
                        strategy="stop_loss", confidence=0,
                        reason=f"손절 ({current_symbol}) {unrealized_pct:.1f}% (한도 -{dynamic_sl_pct:.1f}%)",
                        pnl=pnl, pnl_pct=round(pnl_pct, 2),
                    )
                    trades.append(t)
                    cash += proceeds
                    total_hold_candles += candle_idx - entry_candle_idx
                    num_completed_trades += 1
                    loss_count += 1; total_loss_pct += abs(pnl_pct)
                    current_symbol = None; holdings = 0; avg_buy_price = 0
                    peak_price_since_entry = 0; trailing_active = False
                    last_rotation_idx = candle_idx
                    continue

                # 익절
                if (not trailing_active
                        and self._take_profit_pct > 0
                        and unrealized_pct >= self._take_profit_pct):
                    exec_price = cur_price * (1 - SLIPPAGE)
                    cost = holdings * exec_price
                    fee = cost * TAKER_FEE
                    proceeds = cost - fee
                    pnl = proceeds - (avg_buy_price * holdings)
                    pnl_pct = pnl / (avg_buy_price * holdings) * 100

                    t = BacktestTrade(
                        timestamp=ts, side="sell(tp)", symbol=current_symbol,
                        price=exec_price, quantity=holdings, cost=cost, fee=fee,
                        strategy="take_profit", confidence=0,
                        reason=f"익절 ({current_symbol}) +{unrealized_pct:.1f}%",
                        pnl=pnl, pnl_pct=round(pnl_pct, 2),
                    )
                    trades.append(t)
                    cash += proceeds
                    total_hold_candles += candle_idx - entry_candle_idx
                    num_completed_trades += 1
                    win_count += 1; total_win_pct += abs(pnl_pct)
                    current_symbol = None; holdings = 0; avg_buy_price = 0
                    peak_price_since_entry = 0; trailing_active = False
                    last_rotation_idx = candle_idx
                    continue

            # ── 서지 스캔 ─────────────────────────────────────
            surges = self._scan_surges(all_data, ts)
            if not surges:
                continue

            # 쿨다운 체크
            if candle_idx - last_rotation_idx < self._rotation_cooldown:
                continue

            for surge_sym, surge_score in surges:
                if surge_sym == current_symbol:
                    continue  # 현재 보유 코인은 스킵

                sym_df = all_data[surge_sym]
                if ts not in sym_df.index:
                    continue
                sym_idx = sym_df.index.get_loc(ts)
                if isinstance(sym_idx, slice):
                    sym_idx = sym_idx.start

                sym_row = sym_df.iloc[sym_idx]
                surge_price = float(sym_row["close"])

                # 전략 확인
                if self._require_strategy_confirm:
                    ticker = Ticker(
                        symbol=surge_sym,
                        last=surge_price,
                        bid=surge_price * 0.9995,
                        ask=surge_price * 1.0005,
                        high=float(sym_row["high"]),
                        low=float(sym_row["low"]),
                        volume=float(sym_row.get("volume", 0)),
                        timestamp=ts,
                    )
                    confirmed, confidence = await self._get_strategy_confirmation(
                        sym_df, ticker, sym_idx,
                    )
                    if not confirmed:
                        continue
                else:
                    confidence = surge_score / 10.0  # 서지만으로 신뢰도 추정

                # ── 기존 포지션 매도 (로테이션) ──────────────
                prev_sym = current_symbol
                if current_symbol and holdings > 0:
                    sell_df = all_data[current_symbol]
                    if ts in sell_df.index:
                        sell_price = float(sell_df.loc[ts, "close"])
                    else:
                        sell_price = avg_buy_price

                    exec_price = sell_price * (1 - SLIPPAGE)
                    cost = holdings * exec_price
                    fee = cost * TAKER_FEE
                    proceeds = cost - fee
                    pnl = proceeds - (avg_buy_price * holdings)
                    pnl_pct = pnl / (avg_buy_price * holdings) * 100

                    t = BacktestTrade(
                        timestamp=ts, side="sell(rot)", symbol=current_symbol,
                        price=exec_price, quantity=holdings, cost=cost, fee=fee,
                        strategy="rotation", confidence=0,
                        reason=f"로테이션 → {surge_sym} (서지 {surge_score:.1f}x)",
                        pnl=pnl, pnl_pct=round(pnl_pct, 2),
                    )
                    trades.append(t)
                    cash += proceeds
                    total_hold_candles += candle_idx - entry_candle_idx
                    num_completed_trades += 1
                    if pnl > 0:
                        win_count += 1; total_win_pct += abs(pnl_pct)
                    else:
                        loss_count += 1; total_loss_pct += abs(pnl_pct)
                    total_rotations += 1

                # ── 서지 코인 매수 ────────────────────────────
                trade_size = cash * 0.95
                if trade_size < MIN_TRADE_KRW:
                    continue

                exec_price = surge_price * (1 + SLIPPAGE)
                fee = trade_size * TAKER_FEE
                qty = (trade_size - fee) / exec_price
                cost = qty * exec_price

                cash -= (cost + fee)
                holdings = qty
                avg_buy_price = exec_price
                peak_price_since_entry = surge_price
                trailing_active = False
                current_symbol = surge_sym
                entry_candle_idx = candle_idx
                symbols_traded.add(surge_sym)

                # 동적 손절 계산
                if self._dynamic_sl:
                    dynamic_sl_pct = _calc_dynamic_sl(sym_row, surge_price, current_market_state)
                else:
                    dynamic_sl_pct = self._stop_loss_pct

                t = BacktestTrade(
                    timestamp=ts, side="buy", symbol=surge_sym,
                    price=exec_price, quantity=qty, cost=cost, fee=fee,
                    strategy="surge_rotation", confidence=confidence,
                    reason=f"서지 {surge_score:.1f}x"
                           + (f" (← {prev_sym})" if prev_sym else ""),
                )
                trades.append(t)

                rotation_log.append({
                    "timestamp": ts,
                    "from": prev_sym,
                    "to": surge_sym,
                    "surge_score": round(surge_score, 1),
                    "confidence": round(confidence, 2),
                })
                last_rotation_idx = candle_idx
                break  # 최고 서지 코인 1개만

        # 5. 종료 시 강제 청산
        if current_symbol and holdings > 0:
            sym_df = all_data[current_symbol]
            last_price = float(sym_df.iloc[-1]["close"])
            exec_price = last_price * (1 - SLIPPAGE)
            cost = holdings * exec_price
            fee = cost * TAKER_FEE
            proceeds = cost - fee
            pnl = proceeds - (avg_buy_price * holdings)
            pnl_pct = pnl / (avg_buy_price * holdings) * 100 if avg_buy_price * holdings > 0 else 0

            t = BacktestTrade(
                timestamp=all_timestamps[-1], side="sell(close)", symbol=current_symbol,
                price=exec_price, quantity=holdings, cost=cost, fee=fee,
                strategy="forced_close", confidence=0,
                reason=f"백테스트 종료 강제 청산 ({current_symbol})",
                pnl=pnl, pnl_pct=round(pnl_pct, 2),
            )
            trades.append(t)
            cash += proceeds
            total_hold_candles += len(all_timestamps) - 1 - entry_candle_idx
            num_completed_trades += 1
            if pnl > 0:
                win_count += 1; total_win_pct += abs(pnl_pct)
            else:
                loss_count += 1; total_loss_pct += abs(pnl_pct)

        # 6. 결과 통계
        final_balance = cash
        total_pnl = final_balance - self._initial_balance
        total_pnl_pct = total_pnl / self._initial_balance * 100
        total_sell_trades = len([t for t in trades if "sell" in t.side])
        win_rate = win_count / (win_count + loss_count) * 100 if (win_count + loss_count) > 0 else 0
        avg_win = total_win_pct / win_count if win_count > 0 else 0
        avg_loss = total_loss_pct / loss_count if loss_count > 0 else 0
        profit_factor = (total_win_pct / total_loss_pct) if total_loss_pct > 0 else float("inf") if total_win_pct > 0 else 0
        avg_hold = total_hold_candles / num_completed_trades if num_completed_trades > 0 else 0

        return RotationResult(
            days=days,
            initial_balance=self._initial_balance,
            final_balance=round(final_balance, 0),
            total_pnl=round(total_pnl, 0),
            total_pnl_pct=round(total_pnl_pct, 2),
            max_drawdown_pct=round(max_drawdown, 2),
            total_trades=total_sell_trades,
            winning_trades=win_count,
            losing_trades=loss_count,
            win_rate=round(win_rate, 1),
            avg_win_pct=round(avg_win, 2),
            avg_loss_pct=round(avg_loss, 2),
            profit_factor=round(profit_factor, 2),
            buy_hold_btc_pnl_pct=round(btc_bh_pct, 2),
            total_rotations=total_rotations,
            symbols_traded=sorted(symbols_traded),
            avg_hold_candles=round(avg_hold, 1),
            rotation_log=rotation_log,
            trades=trades,
        )


def print_rotation_result(r: RotationResult):
    """로테이션 백테스트 결과 출력."""
    pnl_sign = "+" if r.total_pnl >= 0 else ""
    dd_warn  = " !!!" if r.max_drawdown_pct > 15 else ""
    bh_sign  = "+" if r.buy_hold_btc_pnl_pct >= 0 else ""
    alpha    = r.total_pnl_pct - r.buy_hold_btc_pnl_pct
    alpha_sign = "+" if alpha >= 0 else ""

    print(f"\n{'='*60}")
    print(f"  로테이션 백테스트 결과 ({r.days}일)")
    print(f"{'='*60}")
    print(f"  초기 자산    : {r.initial_balance:>12,.0f} 원")
    print(f"  최종 자산    : {r.final_balance:>12,.0f} 원")
    print(f"  총 수익      : {pnl_sign}{r.total_pnl:>10,.0f} 원  ({pnl_sign}{r.total_pnl_pct:.2f}%)")
    print(f"  최대 낙폭    : {r.max_drawdown_pct:.2f}%{dd_warn}")
    print(f"{'─'*60}")
    print(f"  BTC B&H      : {bh_sign}{r.buy_hold_btc_pnl_pct:.2f}%")
    print(f"  초과 수익(a) : {alpha_sign}{alpha:.2f}%")
    print(f"{'─'*60}")
    print(f"  총 매매      : {r.total_trades}회  (로테이션 {r.total_rotations}회)")
    print(f"  승리 / 패배  : {r.winning_trades}승 / {r.losing_trades}패")
    print(f"  승률         : {r.win_rate:.1f}%")
    print(f"  평균 수익    : +{r.avg_win_pct:.2f}% | 평균 손실: -{r.avg_loss_pct:.2f}%")
    print(f"  Profit Factor: {r.profit_factor:.2f}")
    print(f"  평균 보유    : {r.avg_hold_candles:.1f}캔들")
    print(f"  거래 코인    : {len(r.symbols_traded)}개 — {', '.join(r.symbols_traded)}")
    print(f"{'─'*60}")

    # 로테이션 로그
    if r.rotation_log:
        print(f"  로테이션 로그 (최근 15건):")
        for entry in r.rotation_log[-15:]:
            ts = entry["timestamp"]
            ts_str = ts.strftime("%m/%d %H:%M") if hasattr(ts, "strftime") else str(ts)[:16]
            from_sym = (entry["from"] or "현금").replace("/KRW", "")
            to_sym = entry["to"].replace("/KRW", "")
            print(f"    {ts_str}  {from_sym:>6} → {to_sym:<6}  "
                  f"서지 {entry['surge_score']:.1f}x  신뢰도 {entry['confidence']:.2f}")

    # 매도 내역
    sell_trades = [t for t in r.trades if "sell" in t.side]
    if sell_trades:
        print(f"{'─'*60}")
        print(f"  매매 내역 (최근 15건):")
        for t in sell_trades[-15:]:
            sym_short = t.symbol.replace("/KRW", "")
            tag = {"sell(sl)": "손절", "sell(tp)": "익절",
                   "sell(trail)": "트레일", "sell(rot)": "로테",
                   "sell(close)": "청산"}.get(t.side, "신호")
            print(f"    {t.timestamp.strftime('%m/%d %H:%M')}  {sym_short:>6}  "
                  f"{t.pnl_pct:>+6.1f}%  [{tag}] {t.reason[:45]}")
    print(f"{'='*60}\n")


# ── CLI 진입점 ─────────────────────────────────────────────────
async def main():
    parser = argparse.ArgumentParser(
        description="코인 자동 매매 시스템 백테스터",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  python backtest.py --symbol BTC/KRW --days 90
  python backtest.py --symbol SOL/KRW --days 30 --strategies rsi bollinger_rsi
  python backtest.py --all-coins --days 30 --balance 1000000
  python backtest.py --symbol ETH/KRW --days 60 --timeframe 4h
  python backtest.py --symbol BTC/KRW --days 30 --stop-loss 3 --take-profit 5
  python backtest.py --rotation --days 180 --timeframe 4h
  python backtest.py --rotation --days 180 --surge-threshold 2.0
        """,
    )
    parser.add_argument("--symbol",         default="BTC/KRW",  help="코인 심볼 (예: BTC/KRW)")
    parser.add_argument("--all-coins",      action="store_true", help="추적 중인 모든 코인 백테스트")
    parser.add_argument("--days",           type=int, default=30, help="백테스트 기간 (일, 기본 30)")
    parser.add_argument("--timeframe",      default="1h",        help="캔들 단위 (1h/4h/1d, 기본 1h)")
    parser.add_argument("--balance",        type=float, default=500_000, help="초기 잔액 (원, 기본 50만)")
    parser.add_argument("--strategies",     nargs="+", default=ALL_STRATEGIES,
                        help=f"사용할 전략 목록 (기본: 전체)\n선택: {', '.join(ALL_STRATEGIES)}")
    parser.add_argument("--min-confidence", type=float, default=0.35, help="최소 신뢰도 임계값 (기본 0.35)")
    parser.add_argument("--stop-loss",      type=float, default=5.0,  help="손절 %% (0=비활성, 기본 5)")
    parser.add_argument("--take-profit",    type=float, default=10.0, help="익절 %% (0=비활성, 기본 10)")
    # 추세 필터
    parser.add_argument("--trend-filter",    dest="trend_filter", action="store_true", default=True,
                        help="글로벌 추세 필터 ON (기본)")
    parser.add_argument("--no-trend-filter", dest="trend_filter", action="store_false",
                        help="글로벌 추세 필터 OFF")
    # 트레일링 스탑
    parser.add_argument("--trailing-activation", type=float, default=3.0,
                        help="트레일링 활성화 수익%% (0=비활성, 기본 3)")
    parser.add_argument("--trailing-stop",  type=float, default=3.0,
                        help="트레일링 스탑 고점 대비 하락%% (기본 3)")
    # 적응형 가중치
    parser.add_argument("--adaptive-weights",    dest="adaptive_weights", action="store_true", default=True,
                        help="적응형 가중치 ON (기본)")
    parser.add_argument("--no-adaptive-weights", dest="adaptive_weights", action="store_false",
                        help="적응형 가중치 OFF")
    # 동적 손절 (ATR + 시장 상태)
    parser.add_argument("--dynamic-sl",         dest="dynamic_sl", action="store_true", default=False,
                        help="ATR+시장상태 기반 동적 손절 ON")
    parser.add_argument("--no-dynamic-sl",      dest="dynamic_sl", action="store_false",
                        help="동적 손절 OFF (고정 %%사용, 기본)")
    # 로테이션 모드
    parser.add_argument("--rotation",       action="store_true",
                        help="거래량 서지 코인 로테이션 모드")
    parser.add_argument("--rotation-coins", nargs="+", default=None,
                        help=f"로테이션 대상 코인 (기본 {len(DEFAULT_ROTATION_COINS)}개)")
    parser.add_argument("--surge-threshold", type=float, default=3.0,
                        help="서지 임계 배수 (기본 3.0)")
    parser.add_argument("--rotation-cooldown", type=int, default=6,
                        help="로테이션 최소 간격 캔들 수 (기본 6)")
    parser.add_argument("--no-strategy-confirm", dest="strategy_confirm",
                        action="store_false", default=True,
                        help="전략 확인 없이 서지만으로 매매")

    args = parser.parse_args()

    # 전략 유효성 검사
    invalid = set(args.strategies) - set(ALL_STRATEGIES)
    if invalid:
        print(f"알 수 없는 전략: {invalid}")
        print(f"사용 가능: {ALL_STRATEGIES}")
        sys.exit(1)

    print("빗썸 연결 중...")
    exchange = BithumbAdapter(api_key="", api_secret="")
    await exchange.initialize()

    # ── 로테이션 모드 ─────────────────────────────────────────
    if args.rotation:
        rotation_coins = args.rotation_coins
        if rotation_coins:
            rotation_coins = [
                c if "/" in c else f"{c}/KRW" for c in rotation_coins
            ]

        rot = RotationBacktester(
            exchange=exchange,
            strategy_names=args.strategies,
            symbols=rotation_coins,
            initial_balance=args.balance,
            min_confidence=args.min_confidence,
            stop_loss_pct=args.stop_loss,
            take_profit_pct=args.take_profit,
            trend_filter=args.trend_filter,
            trailing_activation=args.trailing_activation,
            trailing_stop=args.trailing_stop,
            adaptive_weights=args.adaptive_weights,
            dynamic_sl=args.dynamic_sl,
            surge_threshold=args.surge_threshold,
            rotation_cooldown=args.rotation_cooldown,
            require_strategy_confirm=args.strategy_confirm,
        )
        result = await rot.run(args.timeframe, args.days)
        print_rotation_result(result)
        await exchange.close()
        return

    # ── 기존 단일/전체 코인 모드 ──────────────────────────────
    backtester = Backtester(
        exchange=exchange,
        strategy_names=args.strategies,
        initial_balance=args.balance,
        min_confidence=args.min_confidence,
        stop_loss_pct=args.stop_loss,
        take_profit_pct=args.take_profit,
        trend_filter=args.trend_filter,
        trailing_activation=args.trailing_activation,
        trailing_stop=args.trailing_stop,
        adaptive_weights=args.adaptive_weights,
        dynamic_sl=args.dynamic_sl,
    )

    symbols = (
        ["BTC/KRW", "ETH/KRW", "XRP/KRW", "SOL/KRW", "ADA/KRW"]
        if args.all_coins
        else [args.symbol]
    )

    results = []
    for symbol in symbols:
        try:
            r = await backtester.run(symbol, args.timeframe, args.days)
            print_result(r)
            results.append(r)
        except Exception as e:
            print(f"  {symbol} 실패: {e}")

    if len(results) > 1:
        print(f"\n{'='*60}")
        print(f"  전체 요약 ({args.days}일)")
        print(f"{'='*60}")
        total_pnl = sum(r.total_pnl for r in results)
        for r in results:
            sign = "+" if r.total_pnl_pct >= 0 else ""
            bh = "+" if r.buy_hold_pnl_pct >= 0 else ""
            print(f"  {r.symbol:<12}: {sign}{r.total_pnl_pct:>7.2f}%  "
                  f"B&H {bh}{r.buy_hold_pnl_pct:>6.2f}%  "
                  f"승률 {r.win_rate:>5.1f}%  낙폭 {r.max_drawdown_pct:.1f}%  "
                  f"({r.total_trades}회)")
        print(f"{'─'*60}")
        avg_pnl = sum(r.total_pnl_pct for r in results) / len(results)
        avg_bh  = sum(r.buy_hold_pnl_pct for r in results) / len(results)
        sign = "+" if avg_pnl >= 0 else ""
        bh_sign = "+" if avg_bh >= 0 else ""
        print(f"  평균 수익률  : {sign}{avg_pnl:.2f}%  (B&H: {bh_sign}{avg_bh:.2f}%)")
        print(f"  합산 손익    : {total_pnl:>+,.0f} 원")
        print(f"{'='*60}")

    await exchange.close()


if __name__ == "__main__":
    asyncio.run(main())
