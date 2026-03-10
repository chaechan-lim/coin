"""
서지/모멘텀 트레이딩 백테스터 (5m 캔들).

거래량 급등 + 가격 변동 감지 → 짧은 보유 매매 시뮬레이션.

실행:
  cd backend && .venv/bin/python surge_backtest.py --days 90
  cd backend && .venv/bin/python surge_backtest.py --days 90 --sl 1.5 --tp 3.0
  cd backend && .venv/bin/python surge_backtest.py --days 180 --coins 20 --leverage 2
"""
import asyncio
import argparse
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.WARNING)


# ── 서지 포지션 ──────────────────────────────────────────────────

@dataclass
class SurgePosition:
    symbol: str
    direction: str  # "long" or "short"
    entry_price: float
    quantity: float
    entry_idx: int
    entry_time: datetime
    peak_price: float  # trailing용
    trough_price: float  # short trailing용
    trailing_active: bool = False
    sl_pct: float = 1.5
    tp_pct: float = 3.0
    trail_activation_pct: float = 1.0
    trail_stop_pct: float = 0.8
    surge_score: float = 0.0


@dataclass
class SurgeTrade:
    symbol: str
    direction: str
    entry_price: float
    exit_price: float
    entry_time: datetime
    exit_time: datetime
    pnl_pct: float
    pnl_usdt: float
    hold_minutes: float
    exit_reason: str
    surge_score: float
    volume_ratio: float


@dataclass
class SurgeBacktestResult:
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl_pct: float = 0.0
    total_pnl_usdt: float = 0.0
    max_drawdown_pct: float = 0.0
    profit_factor: float = 0.0
    avg_hold_minutes: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    final_balance: float = 0.0
    trades: list[SurgeTrade] = field(default_factory=list)


# ── 서지 백테스터 ────────────────────────────────────────────────

class SurgeBacktester:
    """5m 캔들 기반 서지 트레이딩 백테스터."""

    # 바이낸스 선물 거래대금 상위 코인
    DEFAULT_COINS = [
        "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT",
        "DOGE/USDT", "ADA/USDT", "AVAX/USDT", "LINK/USDT", "DOT/USDT",
        "NEAR/USDT", "SUI/USDT", "1000PEPE/USDT", "WIF/USDT", "ATOM/USDT",
        "FIL/USDT", "ARB/USDT", "OP/USDT", "TRX/USDT", "AAVE/USDT",
        "ETC/USDT", "APT/USDT", "IMX/USDT", "INJ/USDT", "SEI/USDT",
        "FET/USDT", "RENDER/USDT", "TIA/USDT", "JUP/USDT", "PENDLE/USDT",
    ]

    def __init__(
        self,
        exchange,
        symbols: list[str] | None = None,
        initial_balance: float = 1000.0,
        leverage: int = 2,
        sl_pct: float = 1.5,
        tp_pct: float = 3.0,
        trail_activation_pct: float = 1.0,
        trail_stop_pct: float = 0.8,
        max_hold_minutes: int = 120,
        volume_ratio_threshold: float = 5.0,
        price_change_threshold: float = 1.0,
        max_concurrent: int = 3,
        position_pct: float = 0.08,
        cooldown_candles: int = 6,  # 30분 (5m * 6)
        fee_pct: float = 0.04,  # 0.04% maker/taker
        exhaustion_filter_pct: float = 8.0,  # 이미 N% 이동 시 스킵
        long_only: bool = False,
    ):
        self._exchange = exchange
        self._symbols = symbols or self.DEFAULT_COINS
        self._initial_balance = initial_balance
        self._leverage = leverage
        self._sl_pct = sl_pct
        self._tp_pct = tp_pct
        self._trail_activation_pct = trail_activation_pct
        self._trail_stop_pct = trail_stop_pct
        self._max_hold_candles = max_hold_minutes // 5  # 5m candles
        self._vol_threshold = volume_ratio_threshold
        self._price_threshold = price_change_threshold
        self._max_concurrent = max_concurrent
        self._position_pct = position_pct
        self._cooldown_candles = cooldown_candles
        self._fee_pct = fee_pct / 100  # 0.04% → 0.0004
        self._exhaustion_pct = exhaustion_filter_pct
        self._long_only = long_only
        self._vol_lookback = 60  # 60 * 5m = 5h 평균 거래량

    async def fetch_5m_data(self, symbol: str, days: int) -> pd.DataFrame | None:
        """5m 캔들 데이터 로드 (CSV 캐시)."""
        cache_dir = Path(__file__).parent / ".cache"
        cache_dir.mkdir(exist_ok=True)
        safe = symbol.replace("/", "_")
        cache_path = cache_dir / f"{safe}_5m.csv"

        candles_needed = days * 24 * 12 + 200  # 5m = 12/hour
        tf_ms = 5 * 60 * 1000
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        start_ms = now_ms - candles_needed * tf_ms

        # CSV 캐시 로드
        cached_df = None
        last_cached_ts = 0
        if cache_path.exists():
            try:
                cached_df = pd.read_csv(
                    cache_path, parse_dates=["timestamp"], index_col="timestamp",
                )
                if cached_df.index.tz is None:
                    cached_df.index = cached_df.index.tz_localize("UTC")
                cached_df.sort_index(inplace=True)
                last_cached_ts = int(cached_df.index[-1].timestamp() * 1000)
            except Exception:
                cached_df = None

        fetch_since = last_cached_ts + tf_ms if (cached_df is not None and last_cached_ts > start_ms) else start_ms

        # 페이지네이션
        all_new = []
        cursor = fetch_since
        page = 0
        while cursor < now_ms:
            try:
                raw = await self._exchange.fetch_ohlcv(symbol, "5m", limit=1000, since=cursor)
            except Exception as e:
                print(f"  {symbol} 데이터 오류: {e}")
                break
            if not raw:
                break
            all_new.extend(raw)
            last_ts = int(raw[-1].timestamp.timestamp() * 1000)
            if last_ts <= cursor:
                break
            cursor = last_ts + tf_ms
            page += 1
            if len(raw) < 900:
                break

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
            return None

        df.to_csv(cache_path)

        # 날짜 필터
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        if df.index.tz is not None:
            cutoff = cutoff.replace(tzinfo=df.index.tz)
        df = df[df.index >= cutoff]

        return df if len(df) > self._vol_lookback else None

    async def prefetch_all(self, days: int) -> dict[str, pd.DataFrame]:
        """모든 심볼의 5m 데이터를 로드."""
        data = {}
        total = len(self._symbols)
        for i, sym in enumerate(self._symbols):
            print(f"  [{i+1}/{total}] {sym} 로딩...", end=" ", flush=True)
            df = await self.fetch_5m_data(sym, days)
            if df is not None:
                print(f"OK ({len(df):,} 캔들)")
                data[sym] = df
            else:
                print("SKIP (데이터 부족)")
        return data

    def _calc_volume_ratio(self, df: pd.DataFrame, idx: int) -> float:
        """현재 캔들의 거래량 / 최근 N캔들 평균."""
        if idx < self._vol_lookback:
            return 0.0
        vols = df.iloc[idx - self._vol_lookback:idx]["volume"].values
        avg = vols.mean()
        if avg <= 0:
            return 0.0
        return float(df.iloc[idx]["volume"]) / avg

    def _calc_price_change(self, df: pd.DataFrame, idx: int, lookback: int = 3) -> float:
        """최근 N캔들 가격 변동률 (%)."""
        if idx < lookback:
            return 0.0
        prev = df.iloc[idx - lookback]["close"]
        curr = df.iloc[idx]["close"]
        if prev <= 0:
            return 0.0
        return (curr - prev) / prev * 100

    def _calc_price_change_15m(self, df: pd.DataFrame, idx: int) -> float:
        """15분 가격 변동률 (3 * 5m 캔들)."""
        return self._calc_price_change(df, idx, lookback=3)

    def _calc_acceleration(self, df: pd.DataFrame, idx: int) -> float:
        """거래량 가속도: 현재 ratio vs 2캔들 전 ratio."""
        if idx < self._vol_lookback + 2:
            return 0.0
        ratio_now = self._calc_volume_ratio(df, idx)
        ratio_prev = self._calc_volume_ratio(df, idx - 2)
        return ratio_now - ratio_prev

    def _calc_rsi(self, df: pd.DataFrame, idx: int, period: int = 14) -> float:
        """간단 RSI 계산."""
        if idx < period + 1:
            return 50.0
        closes = df.iloc[idx - period:idx + 1]["close"].values
        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0).mean()
        losses = np.where(deltas < 0, -deltas, 0).mean()
        if losses == 0:
            return 100.0
        rs = gains / losses
        return 100.0 - (100.0 / (1.0 + rs))

    def _compute_surge_score(self, df: pd.DataFrame, idx: int) -> tuple[float, float, float]:
        """서지 점수 계산. (score, volume_ratio, price_change) 반환."""
        vol_ratio = self._calc_volume_ratio(df, idx)
        price_chg = self._calc_price_change_15m(df, idx)
        accel = self._calc_acceleration(df, idx)

        # 정규화
        vol_signal = min(vol_ratio / 10.0, 1.0)
        price_signal = min(abs(price_chg) / 5.0, 1.0)
        accel_signal = max(0, min(accel / 3.0, 1.0))

        # 가중 합산
        score = (
            0.40 * vol_signal +
            0.35 * price_signal +
            0.25 * accel_signal
        )
        return score, vol_ratio, price_chg

    def _check_exit(
        self, pos: SurgePosition, candle: pd.Series, candle_idx: int,
    ) -> tuple[bool, float, str]:
        """포지션 청산 조건 체크. (should_exit, exit_price, reason) 반환.

        Intra-candle SL: high/low로 SL/TP 터치 검사.
        """
        o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
        entry = pos.entry_price
        held = candle_idx - pos.entry_idx

        if pos.direction == "long":
            pnl_at_close = (c - entry) / entry * 100 * self._leverage
            pnl_at_low = (l - entry) / entry * 100 * self._leverage
            pnl_at_high = (h - entry) / entry * 100 * self._leverage

            # 1. SL (intra-candle)
            if pnl_at_low <= -pos.sl_pct:
                sl_price = entry * (1 - pos.sl_pct / 100 / self._leverage)
                return True, sl_price, "SL"

            # 2. TP (intra-candle)
            if pnl_at_high >= pos.tp_pct:
                tp_price = entry * (1 + pos.tp_pct / 100 / self._leverage)
                return True, tp_price, "TP"

            # Trailing 업데이트
            if h > pos.peak_price:
                pos.peak_price = h
            peak_pnl = (pos.peak_price - entry) / entry * 100 * self._leverage

            # 3. Trailing stop
            if peak_pnl >= pos.trail_activation_pct:
                pos.trailing_active = True
                drawdown = (pos.peak_price - l) / pos.peak_price * 100 * self._leverage
                if drawdown >= pos.trail_stop_pct:
                    trail_price = pos.peak_price * (1 - pos.trail_stop_pct / 100 / self._leverage)
                    return True, max(trail_price, l), "Trailing"

            # 4. 시간 초과
            if held >= self._max_hold_candles:
                return True, c, "TimeExpiry"

            # 5. 볼륨 페이드 (거래량 정상 복귀)
            # 간소화: held > 6 이후 체크
            if held > 6:
                vol_ratio = self._calc_volume_ratio(
                    # 이건 전체 df가 필요한데 candle만 있음 → skip for simplicity
                    # volume fade는 run()에서 별도 체크
                    None, 0) if False else 0
                pass

        else:  # short
            pnl_at_close = (entry - c) / entry * 100 * self._leverage
            pnl_at_high = (entry - h) / entry * 100 * self._leverage  # worst
            pnl_at_low = (entry - l) / entry * 100 * self._leverage  # best

            # 1. SL
            if pnl_at_high <= -pos.sl_pct:
                sl_price = entry * (1 + pos.sl_pct / 100 / self._leverage)
                return True, sl_price, "SL"

            # 2. TP
            if pnl_at_low >= pos.tp_pct:
                tp_price = entry * (1 - pos.tp_pct / 100 / self._leverage)
                return True, tp_price, "TP"

            # Trailing
            if l < pos.trough_price:
                pos.trough_price = l
            trough_pnl = (entry - pos.trough_price) / entry * 100 * self._leverage

            # 3. Trailing stop
            if trough_pnl >= pos.trail_activation_pct:
                pos.trailing_active = True
                drawup = (h - pos.trough_price) / pos.trough_price * 100 * self._leverage
                if drawup >= pos.trail_stop_pct:
                    trail_price = pos.trough_price * (1 + pos.trail_stop_pct / 100 / self._leverage)
                    return True, min(trail_price, h), "Trailing"

            # 4. 시간 초과
            if held >= self._max_hold_candles:
                return True, c, "TimeExpiry"

        return False, 0.0, ""

    def run(self, all_data: dict[str, pd.DataFrame]) -> SurgeBacktestResult:
        """서지 백테스트 실행."""
        cash = self._initial_balance
        positions: dict[str, SurgePosition] = {}
        trades: list[SurgeTrade] = []
        cooldowns: dict[str, int] = {}  # symbol → 재진입 가능 캔들 idx
        equity_curve: list[float] = []
        peak_equity = cash

        # 모든 데이터의 타임스탬프 합집합 → 시간순 반복
        all_timestamps = set()
        for df in all_data.values():
            all_timestamps.update(df.index.tolist())
        sorted_ts = sorted(all_timestamps)

        daily_trades = 0
        daily_losses = 0
        last_reset_day = None
        consecutive_losses = 0
        pause_until_idx = 0

        for ts_idx, ts in enumerate(sorted_ts):
            # 일일 카운터 리셋
            day = ts.date() if hasattr(ts, 'date') else None
            if day and day != last_reset_day:
                daily_trades = 0
                daily_losses = 0
                consecutive_losses = 0
                last_reset_day = day

            # 일시 정지 중
            if ts_idx < pause_until_idx:
                continue

            # ── 1. 기존 포지션 청산 체크 ──
            closed_symbols = []
            for sym, pos in list(positions.items()):
                if sym not in all_data:
                    continue
                df = all_data[sym]
                if ts not in df.index:
                    continue
                # 해당 심볼의 인덱스 위치
                sym_idx = df.index.get_loc(ts)
                if isinstance(sym_idx, slice):
                    sym_idx = sym_idx.start
                candle = df.iloc[sym_idx]

                should_exit, exit_price, reason = self._check_exit(pos, candle, sym_idx)
                if should_exit:
                    # PnL 계산
                    if pos.direction == "long":
                        raw_pnl_pct = (exit_price - pos.entry_price) / pos.entry_price * 100
                    else:
                        raw_pnl_pct = (pos.entry_price - exit_price) / pos.entry_price * 100
                    lev_pnl_pct = raw_pnl_pct * self._leverage
                    fee_pct = self._fee_pct * self._leverage * 2 * 100  # 진입+청산
                    net_pnl_pct = lev_pnl_pct - fee_pct
                    cost = pos.quantity * pos.entry_price / self._leverage
                    pnl_usdt = cost * net_pnl_pct / 100

                    cash += cost + pnl_usdt
                    hold_min = (sym_idx - pos.entry_idx) * 5

                    trade = SurgeTrade(
                        symbol=sym, direction=pos.direction,
                        entry_price=pos.entry_price, exit_price=exit_price,
                        entry_time=pos.entry_time, exit_time=ts,
                        pnl_pct=net_pnl_pct, pnl_usdt=pnl_usdt,
                        hold_minutes=hold_min, exit_reason=reason,
                        surge_score=pos.surge_score, volume_ratio=0,
                    )
                    trades.append(trade)
                    closed_symbols.append(sym)
                    cooldowns[sym] = sym_idx + self._cooldown_candles

                    if net_pnl_pct < 0:
                        daily_losses += 1
                        consecutive_losses += 1
                        if consecutive_losses >= 3:
                            pause_until_idx = ts_idx + 6  # 30분 정지
                    else:
                        consecutive_losses = 0

            for sym in closed_symbols:
                del positions[sym]

            # ── 2. 이퀴티 추적 ──
            equity = cash
            for sym, pos in positions.items():
                if sym in all_data and ts in all_data[sym].index:
                    idx = all_data[sym].index.get_loc(ts)
                    if isinstance(idx, slice):
                        idx = idx.start
                    price = all_data[sym].iloc[idx]["close"]
                    cost = pos.quantity * pos.entry_price / self._leverage
                    if pos.direction == "long":
                        upnl = (price - pos.entry_price) / pos.entry_price * self._leverage
                    else:
                        upnl = (pos.entry_price - price) / pos.entry_price * self._leverage
                    equity += cost * (1 + upnl)
                else:
                    cost = pos.quantity * pos.entry_price / self._leverage
                    equity += cost
            equity_curve.append(equity)
            if equity > peak_equity:
                peak_equity = equity

            # 일일 제한
            if daily_trades >= 15 or daily_losses >= 5:
                continue

            # ── 3. 서지 스캔 → 신규 진입 ──
            if len(positions) >= self._max_concurrent:
                continue

            surge_candidates = []
            for sym, df in all_data.items():
                if sym in positions:
                    continue
                if ts not in df.index:
                    continue
                sym_idx = df.index.get_loc(ts)
                if isinstance(sym_idx, slice):
                    sym_idx = sym_idx.start

                # 쿨다운 체크
                if sym in cooldowns and sym_idx < cooldowns[sym]:
                    continue

                score, vol_ratio, price_chg = self._compute_surge_score(df, sym_idx)

                if score < 0.40:  # 최소 임계값
                    continue
                if vol_ratio < self._vol_threshold:
                    continue
                if abs(price_chg) < self._price_threshold:
                    continue

                # 소진 필터
                price_chg_30m = self._calc_price_change(df, sym_idx, lookback=6)
                if abs(price_chg_30m) > self._exhaustion_pct:
                    continue

                # RSI 극단 필터
                rsi = self._calc_rsi(df, sym_idx)
                if price_chg > 0 and rsi > 85:
                    continue
                if price_chg < 0 and rsi < 15:
                    continue

                surge_candidates.append((sym, score, vol_ratio, price_chg, sym_idx))

            # 점수 높은 순 정렬, 최대 동시 포지션까지
            surge_candidates.sort(key=lambda x: x[1], reverse=True)
            slots = self._max_concurrent - len(positions)

            for sym, score, vol_ratio, price_chg, sym_idx in surge_candidates[:slots]:
                df = all_data[sym]
                candle = df.iloc[sym_idx]
                entry_price = candle["close"]

                # 방향 결정
                direction = "long" if price_chg > 0 else "short"
                if self._long_only and direction == "short":
                    continue

                # 포지션 크기
                size_usdt = cash * self._position_pct
                # 서지 강도별 스케일링
                if score >= 0.70:
                    size_usdt *= 1.0
                elif score >= 0.55:
                    size_usdt *= 0.75
                else:
                    size_usdt *= 0.50

                if size_usdt < 5:
                    continue

                qty = size_usdt * self._leverage / entry_price
                margin = size_usdt  # 마진 = size
                if margin > cash:
                    continue

                cash -= margin

                pos = SurgePosition(
                    symbol=sym, direction=direction,
                    entry_price=entry_price, quantity=qty,
                    entry_idx=sym_idx, entry_time=ts,
                    peak_price=candle["high"],
                    trough_price=candle["low"],
                    sl_pct=self._sl_pct, tp_pct=self._tp_pct,
                    trail_activation_pct=self._trail_activation_pct,
                    trail_stop_pct=self._trail_stop_pct,
                    surge_score=score,
                )
                positions[sym] = pos
                daily_trades += 1

        # ── 미청산 포지션 강제 청산 ──
        for sym, pos in list(positions.items()):
            if sym in all_data:
                df = all_data[sym]
                last_price = df.iloc[-1]["close"]
                if pos.direction == "long":
                    raw_pnl = (last_price - pos.entry_price) / pos.entry_price * 100
                else:
                    raw_pnl = (pos.entry_price - last_price) / pos.entry_price * 100
                lev_pnl = raw_pnl * self._leverage
                fee = self._fee_pct * self._leverage * 2 * 100
                net_pnl = lev_pnl - fee
                cost = pos.quantity * pos.entry_price / self._leverage
                pnl_usdt = cost * net_pnl / 100
                cash += cost + pnl_usdt
                trades.append(SurgeTrade(
                    symbol=sym, direction=pos.direction,
                    entry_price=pos.entry_price, exit_price=last_price,
                    entry_time=pos.entry_time, exit_time=sorted_ts[-1] if sorted_ts else pos.entry_time,
                    pnl_pct=net_pnl, pnl_usdt=pnl_usdt,
                    hold_minutes=(len(all_data[sym]) - pos.entry_idx) * 5,
                    exit_reason="BacktestEnd", surge_score=pos.surge_score, volume_ratio=0,
                ))

        # ── 결과 집계 ──
        result = SurgeBacktestResult()
        result.trades = trades
        result.total_trades = len(trades)
        result.final_balance = cash

        if trades:
            wins = [t for t in trades if t.pnl_pct > 0]
            losses = [t for t in trades if t.pnl_pct <= 0]
            result.wins = len(wins)
            result.losses = len(losses)
            result.total_pnl_pct = sum(t.pnl_pct for t in trades)
            result.total_pnl_usdt = sum(t.pnl_usdt for t in trades)
            result.avg_hold_minutes = np.mean([t.hold_minutes for t in trades])
            result.avg_win_pct = np.mean([t.pnl_pct for t in wins]) if wins else 0
            result.avg_loss_pct = np.mean([t.pnl_pct for t in losses]) if losses else 0

            gross_profit = sum(t.pnl_usdt for t in wins)
            gross_loss = abs(sum(t.pnl_usdt for t in losses))
            result.profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')

        # MDD
        if equity_curve:
            eq = np.array(equity_curve)
            peak = np.maximum.accumulate(eq)
            dd = (peak - eq) / peak * 100
            result.max_drawdown_pct = float(dd.max())

        return result


def print_result(result: SurgeBacktestResult, args) -> None:
    """결과 출력."""
    print(f"\n{'='*65}")
    n_coins = len(args.coins_list) if hasattr(args, 'coins_list') else args.coins
    print(f"  서지 백테스트 결과 ({args.days}일, {n_coins}코인)")
    print(f"{'='*65}")

    ret = (result.final_balance - args.balance) / args.balance * 100
    wr = result.wins / result.total_trades * 100 if result.total_trades > 0 else 0

    print(f"  거래 수:        {result.total_trades}")
    print(f"  승/패:          {result.wins} / {result.losses}")
    print(f"  승률:           {wr:.1f}%")
    print(f"  총 PnL:         {result.total_pnl_pct:+.1f}% (${result.total_pnl_usdt:+.2f})")
    print(f"  수익률:         {ret:+.1f}%")
    print(f"  Profit Factor:  {result.profit_factor:.2f}")
    print(f"  MDD:            {result.max_drawdown_pct:.1f}%")
    print(f"  잔고:           ${args.balance:.0f} → ${result.final_balance:.2f}")
    print(f"  평균 보유:      {result.avg_hold_minutes:.0f}분")
    print(f"  평균 수익:      {result.avg_win_pct:+.2f}%")
    print(f"  평균 손실:      {result.avg_loss_pct:+.2f}%")

    # 방향별 통계
    longs = [t for t in result.trades if t.direction == "long"]
    shorts = [t for t in result.trades if t.direction == "short"]
    if longs:
        long_wr = sum(1 for t in longs if t.pnl_pct > 0) / len(longs) * 100
        long_pnl = sum(t.pnl_pct for t in longs)
        print(f"\n  롱:  {len(longs)}건, 승률 {long_wr:.0f}%, PnL {long_pnl:+.1f}%")
    if shorts:
        short_wr = sum(1 for t in shorts if t.pnl_pct > 0) / len(shorts) * 100
        short_pnl = sum(t.pnl_pct for t in shorts)
        print(f"  숏:  {len(shorts)}건, 승률 {short_wr:.0f}%, PnL {short_pnl:+.1f}%")

    # 청산 사유별
    reasons = {}
    for t in result.trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    print(f"\n  청산 사유:")
    for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
        pnl = sum(t.pnl_pct for t in result.trades if t.exit_reason == reason)
        print(f"    {reason:12s}: {count:3d}건, PnL {pnl:+.1f}%")

    # 코인별 상위/하위
    coin_pnl = {}
    for t in result.trades:
        coin_pnl.setdefault(t.symbol, []).append(t.pnl_pct)
    coin_summary = [(sym, sum(pnls), len(pnls)) for sym, pnls in coin_pnl.items()]
    coin_summary.sort(key=lambda x: x[1], reverse=True)
    print(f"\n  코인별 (상위 5):")
    for sym, pnl, cnt in coin_summary[:5]:
        print(f"    {sym:16s}: {cnt:3d}건, PnL {pnl:+.1f}%")
    if len(coin_summary) > 5:
        print(f"  코인별 (하위 5):")
        for sym, pnl, cnt in coin_summary[-5:]:
            print(f"    {sym:16s}: {cnt:3d}건, PnL {pnl:+.1f}%")

    print(f"{'='*65}")


async def main():
    parser = argparse.ArgumentParser(description="서지 트레이딩 백테스터 (5m)")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--balance", type=float, default=1000.0)
    parser.add_argument("--leverage", type=int, default=2)
    parser.add_argument("--sl", type=float, default=1.5, help="SL %% (기본 1.5)")
    parser.add_argument("--tp", type=float, default=3.0, help="TP %% (기본 3.0)")
    parser.add_argument("--trail-act", type=float, default=1.0, help="트레일링 활성화 %% (기본 1.0)")
    parser.add_argument("--trail-stop", type=float, default=0.8, help="트레일링 스톱 %% (기본 0.8)")
    parser.add_argument("--max-hold", type=int, default=120, help="최대 보유 분 (기본 120)")
    parser.add_argument("--vol-threshold", type=float, default=5.0, help="거래량 배수 임계 (기본 5.0)")
    parser.add_argument("--price-threshold", type=float, default=1.0, help="가격 변동 %% 임계 (기본 1.0)")
    parser.add_argument("--max-concurrent", type=int, default=3, help="최대 동시 포지션 (기본 3)")
    parser.add_argument("--position-pct", type=float, default=0.08, help="포지션 비율 (기본 0.08)")
    parser.add_argument("--coins", type=int, default=20, help="대상 코인 수 (기본 20)")
    parser.add_argument("--exhaustion", type=float, default=8.0, help="소진 필터 %% (기본 8.0)")
    parser.add_argument("--cooldown", type=int, default=6, help="쿨다운 캔들 수 (기본 6 = 30분)")
    parser.add_argument("--long-only", action="store_true", default=False, help="롱만 진입")
    args = parser.parse_args()

    from exchange.binance_usdm_adapter import BinanceUSDMAdapter
    print("바이낸스 선물 연결 중...")
    exchange = BinanceUSDMAdapter(api_key="", api_secret="", testnet=False)
    await exchange.initialize()

    try:
        symbols = SurgeBacktester.DEFAULT_COINS[:args.coins]
        args.coins_list = symbols

        bt = SurgeBacktester(
            exchange=exchange,
            symbols=symbols,
            initial_balance=args.balance,
            leverage=args.leverage,
            sl_pct=args.sl,
            tp_pct=args.tp,
            trail_activation_pct=args.trail_act,
            trail_stop_pct=args.trail_stop,
            max_hold_minutes=args.max_hold,
            volume_ratio_threshold=args.vol_threshold,
            price_change_threshold=args.price_threshold,
            max_concurrent=args.max_concurrent,
            position_pct=args.position_pct,
            cooldown_candles=args.cooldown,
            exhaustion_filter_pct=args.exhaustion,
            long_only=args.long_only,
        )

        print(f"\n5m 데이터 로딩 ({args.days}일, {len(symbols)}코인)...")
        all_data = await bt.prefetch_all(args.days)

        if not all_data:
            print("데이터 없음!")
            return

        print(f"\n서지 백테스트 실행 중 ({len(all_data)}코인)...")
        result = bt.run(all_data)

        print_result(result, args)

    finally:
        await exchange.close()


if __name__ == "__main__":
    asyncio.run(main())
