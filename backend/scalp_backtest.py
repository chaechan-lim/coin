"""
스캘핑 백테스트 — BB/RSI 기반 횡보장 단기 매매 검증

Usage:
    cd backend
    .venv/bin/python scalp_backtest.py --days 180 --leverage 3
    .venv/bin/python scalp_backtest.py --days 540 --leverage 3 --param-sweep
"""
import asyncio
import argparse
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import numpy as np

# backtest_v2의 데이터 로딩 재사용
sys.path.insert(0, os.path.dirname(__file__))

COINS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT"]
TIMEFRAME = "5m"
TF_MINUTES = 5

FUTURES_FEE = 0.0004   # 0.04%
SLIPPAGE = 0.0002      # 0.02%
ROUND_TRIP_COST = (FUTURES_FEE + SLIPPAGE) * 2  # 0.12%


@dataclass
class ScalpConfig:
    """스캘핑 파라미터"""
    # BB 진입 조건
    bb_period: int = 20
    bb_std: float = 2.0
    bb_entry_low: float = 0.20    # BB position < this → 롱
    bb_entry_high: float = 0.80   # BB position > this → 숏

    # RSI 필터
    rsi_period: int = 14
    rsi_oversold: float = 38      # RSI < this → 롱 허용
    rsi_overbought: float = 62    # RSI > this → 숏 허용

    # SL/TP (% 기반, 레버리지 전)
    sl_pct: float = 0.4           # 0.4% 손절
    tp_pct: float = 0.8           # 0.8% 익절

    # 트레일링 (레버리지 전 %)
    trail_activation_pct: float = 0.5  # 0.5% 수익 시 트레일링 활성화
    trail_stop_pct: float = 0.2        # 고점 대비 0.2% 하락 시 청산

    # 거래 제어
    cooldown_candles: int = 6     # 30분 (6 × 5m)
    max_hold_candles: int = 36    # 3시간 최대 보유 (36 × 5m)
    position_pct: float = 0.05   # 잔고 5%씩
    leverage: int = 3
    max_concurrent: int = 3       # 최대 동시 포지션

    # EMA 필터 (옵션)
    use_ema_filter: bool = False  # EMA 방향 확인
    ema_fast: int = 9
    ema_slow: int = 21

    # 1h RSI 방향 필터 (V2 검증된 핵심)
    use_1h_rsi_filter: bool = False
    # ATR 기반 SL/TP (고정% 대신)
    use_atr_sltp: bool = False
    atr_sl_mult: float = 1.0   # SL = 1.0 × ATR
    atr_tp_mult: float = 2.0   # TP = 2.0 × ATR
    # BB 밴드폭 필터 (좁을 때만 진입 = 횡보 확인)
    use_bbw_filter: bool = False
    bbw_max: float = 0.04      # 밴드폭 < 4%일 때만


@dataclass
class ScalpPosition:
    symbol: str
    direction: str  # "long" or "short"
    entry_price: float
    quantity: float
    margin: float
    entry_idx: int
    sl_price: float
    tp_price: float
    trail_activation_price: float
    trail_stop_pct: float
    extreme_price: float
    trailing_active: bool = False
    trail_stop_price: float = 0.0


@dataclass
class ScalpTrade:
    symbol: str
    direction: str
    entry_price: float
    exit_price: float
    quantity: float
    margin: float
    pnl: float
    pnl_pct: float
    fee: float
    hold_candles: int
    exit_reason: str


async def fetch_data(coins: list[str], days: int) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    """캐시 CSV 우선 로드. 5m + 1h 데이터 반환."""
    from backtest_v2 import compute_v2_indicators

    cache_dir = Path(__file__).parent / ".cache"
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    data_5m = {}
    data_1h = {}
    need_fetch = {"5m": [], "1h": []}

    for tf_label, tf_str, target in [("5m", TIMEFRAME, data_5m), ("1h", "1h", data_1h)]:
        for coin in coins:
            safe = coin.replace("/", "_")
            cache_path = cache_dir / f"{safe}_{tf_str}.csv"
            if cache_path.exists():
                try:
                    df = pd.read_csv(cache_path, parse_dates=["timestamp"], index_col="timestamp")
                    df = df[df.index >= cutoff]
                    if len(df) > 50:
                        df = compute_v2_indicators(df)
                        target[coin] = df
                        if tf_label == "5m":
                            print(f"  {coin}: {len(df)} candles (cache)")
                        continue
                except Exception:
                    pass
            need_fetch[tf_label].append(coin)

    # API fetch for missing
    all_need = set(need_fetch["5m"] + need_fetch["1h"])
    if all_need:
        from backtest_v2 import fetch_ohlcv_cached
        import ccxt.async_support as ccxt
        exchange = ccxt.binanceusdm({"enableRateLimit": True})
        await exchange.load_markets()
        for coin in all_need:
            for tf_str, target in [(TIMEFRAME, data_5m), ("1h", data_1h)]:
                if coin in target:
                    continue
                try:
                    df = await fetch_ohlcv_cached(exchange, coin, tf_str, days)
                    if df is not None and len(df) > 50:
                        df = compute_v2_indicators(df)
                        target[coin] = df
                except Exception as e:
                    print(f"  {coin} {tf_str}: error - {e}")
        await exchange.close()

    return data_5m, data_1h


def get_1h_rsi_direction(data_1h: dict[str, pd.DataFrame], symbol: str, ts) -> tuple[bool, bool]:
    """1h RSI 방향 (상승중, 하락중) 반환"""
    if symbol not in data_1h:
        return False, False
    df = data_1h[symbol]
    # ts 이전의 가장 가까운 1h 캔들 찾기
    mask = df.index <= ts
    if mask.sum() < 3:
        return False, False
    recent = df[mask].iloc[-3:]
    rsi_col = "rsi_14"
    if rsi_col not in recent.columns:
        return False, False
    rsi_now = recent[rsi_col].iloc[-1]
    rsi_prev = recent[rsi_col].iloc[-2]
    if pd.isna(rsi_now) or pd.isna(rsi_prev):
        return False, False
    return rsi_now > rsi_prev, rsi_now < rsi_prev


def compute_bb_position(close: float, bb_upper: float, bb_lower: float) -> float:
    """BB 내 위치 (0=하단, 1=상단)"""
    if bb_upper == bb_lower:
        return 0.5
    return (close - bb_lower) / (bb_upper - bb_lower)


def run_scalp_backtest(
    data: dict[str, pd.DataFrame],
    config: ScalpConfig,
    initial_cash: float = 1000.0,
    verbose: bool = False,
    data_1h: dict[str, pd.DataFrame] | None = None,
) -> dict:
    """스캘핑 백테스트 실행"""
    cash = initial_cash
    peak_equity = initial_cash
    max_dd = 0.0
    positions: dict[str, ScalpPosition] = {}
    trades: list[ScalpTrade] = []
    last_exit: dict[str, int] = {}

    # 모든 코인의 타임스탬프 합집합
    all_ts = sorted(set().union(*(df.index.tolist() for df in data.values())))

    for candle_idx, ts in enumerate(all_ts):
        # --- 1. 기존 포지션 SL/TP/trailing/시간초과 체크 ---
        closed_symbols = []
        for sym, pos in list(positions.items()):
            if sym not in data or ts not in data[sym].index:
                continue

            row = data[sym].loc[ts]
            high = row["high"]
            low = row["low"]
            close = row["close"]

            exit_reason = None
            exit_price = close

            # SL 체크 (high/low 기반)
            if pos.direction == "long" and low <= pos.sl_price:
                exit_reason = "stop_loss"
                exit_price = pos.sl_price
            elif pos.direction == "short" and high >= pos.sl_price:
                exit_reason = "stop_loss"
                exit_price = pos.sl_price

            # TP 체크
            if not exit_reason:
                if pos.direction == "long" and high >= pos.tp_price:
                    exit_reason = "take_profit"
                    exit_price = pos.tp_price
                elif pos.direction == "short" and low <= pos.tp_price:
                    exit_reason = "take_profit"
                    exit_price = pos.tp_price

            # 트레일링 업데이트
            if not exit_reason:
                if pos.direction == "long":
                    if high > pos.extreme_price:
                        pos.extreme_price = high
                    if not pos.trailing_active and high >= pos.trail_activation_price:
                        pos.trailing_active = True
                    if pos.trailing_active:
                        pos.trail_stop_price = pos.extreme_price * (1 - pos.trail_stop_pct / 100)
                        if low <= pos.trail_stop_price:
                            exit_reason = "trailing_stop"
                            exit_price = pos.trail_stop_price
                else:
                    if low < pos.extreme_price:
                        pos.extreme_price = low
                    if not pos.trailing_active and low <= pos.trail_activation_price:
                        pos.trailing_active = True
                    if pos.trailing_active:
                        pos.trail_stop_price = pos.extreme_price * (1 + pos.trail_stop_pct / 100)
                        if high >= pos.trail_stop_price:
                            exit_reason = "trailing_stop"
                            exit_price = pos.trail_stop_price

            # 시간초과
            if not exit_reason and (candle_idx - pos.entry_idx) >= config.max_hold_candles:
                exit_reason = "timeout"
                exit_price = close

            if exit_reason:
                # 청산
                if pos.direction == "long":
                    pnl = (exit_price - pos.entry_price) * pos.quantity
                else:
                    pnl = (pos.entry_price - exit_price) * pos.quantity

                fee = pos.quantity * exit_price * (FUTURES_FEE + SLIPPAGE)
                pnl -= fee
                pnl_pct = pnl / pos.margin * 100

                cash += pos.margin + pnl
                trades.append(ScalpTrade(
                    symbol=sym, direction=pos.direction,
                    entry_price=pos.entry_price, exit_price=exit_price,
                    quantity=pos.quantity, margin=pos.margin,
                    pnl=pnl, pnl_pct=pnl_pct, fee=fee,
                    hold_candles=candle_idx - pos.entry_idx,
                    exit_reason=exit_reason,
                ))
                last_exit[sym] = candle_idx
                closed_symbols.append(sym)

        for sym in closed_symbols:
            del positions[sym]

        # --- 2. 신규 진입 ---
        if len(positions) >= config.max_concurrent:
            continue

        for sym, df in data.items():
            if sym in positions:
                continue
            if ts not in df.index:
                continue
            if len(positions) >= config.max_concurrent:
                break

            # 쿨다운
            if sym in last_exit and (candle_idx - last_exit[sym]) < config.cooldown_candles:
                continue

            row = df.loc[ts]
            close = row["close"]

            # 필요 지표 확인
            sma_col = "sma_20"
            rsi_col = "rsi_14"
            bb_upper_col = None
            bb_lower_col = None

            # BB 컬럼 찾기
            for col in df.columns:
                if "bbu" in col.lower() or "bb_upper" in col.lower() or col == "BBU_20_2.0":
                    bb_upper_col = col
                elif "bbl" in col.lower() or "bb_lower" in col.lower() or col == "BBL_20_2.0":
                    bb_lower_col = col

            if bb_upper_col is None or bb_lower_col is None or rsi_col not in df.columns:
                continue

            bb_upper = row.get(bb_upper_col, np.nan)
            bb_lower = row.get(bb_lower_col, np.nan)
            rsi = row.get(rsi_col, np.nan)

            if pd.isna(bb_upper) or pd.isna(bb_lower) or pd.isna(rsi):
                continue

            bb_pos = compute_bb_position(close, bb_upper, bb_lower)

            # EMA 필터
            if config.use_ema_filter:
                ema_f = row.get(f"ema_{config.ema_fast}", np.nan)
                ema_s = row.get(f"ema_{config.ema_slow}", np.nan)
                if pd.isna(ema_f) or pd.isna(ema_s):
                    continue
            else:
                ema_f = ema_s = None

            # BB 밴드폭 필터 (횡보 확인)
            if config.use_bbw_filter:
                sma_val = row.get("sma_20", np.nan)
                if pd.isna(sma_val) or sma_val == 0:
                    continue
                bbw = (bb_upper - bb_lower) / sma_val
                if bbw > config.bbw_max:
                    continue  # 밴드 넓음 = 변동성 높음 → 스킵

            direction = None

            # 롱 시그널: BB 하단 + RSI 과매도
            if bb_pos < config.bb_entry_low and rsi < config.rsi_oversold:
                if not config.use_ema_filter or ema_f > ema_s:
                    direction = "long"

            # 숏 시그널: BB 상단 + RSI 과매수
            elif bb_pos > config.bb_entry_high and rsi > config.rsi_overbought:
                if not config.use_ema_filter or ema_f < ema_s:
                    direction = "short"

            if direction is None:
                continue

            # 1h RSI 방향 필터 (V2 검증)
            if config.use_1h_rsi_filter and data_1h:
                rsi_rising, rsi_falling = get_1h_rsi_direction(data_1h, sym, ts)
                if direction == "long" and not rsi_rising:
                    continue  # 1h RSI 하락 중이면 롱 스킵
                if direction == "short" and not rsi_falling:
                    continue  # 1h RSI 상승 중이면 숏 스킵

            # 포지션 사이징
            margin = cash * config.position_pct
            if margin < 5:  # 최소 5 USDT
                continue

            quantity = margin * config.leverage / close
            entry_fee = quantity * close * (FUTURES_FEE + SLIPPAGE)

            if cash < margin + entry_fee:
                continue

            # SL/TP 계산
            if config.use_atr_sltp:
                atr_val = row.get("atr_14", np.nan)
                if pd.isna(atr_val) or atr_val == 0:
                    continue
                sl_dist = atr_val * config.atr_sl_mult
                tp_dist = atr_val * config.atr_tp_mult
                trail_dist = atr_val * config.atr_tp_mult * 0.5
            else:
                sl_dist = close * config.sl_pct / 100
                tp_dist = close * config.tp_pct / 100
                trail_dist = close * config.trail_activation_pct / 100

            if direction == "long":
                sl_price = close - sl_dist
                tp_price = close + tp_dist
                trail_act = close + trail_dist
                extreme = close
            else:
                sl_price = close + sl_dist
                tp_price = close - tp_dist
                trail_act = close - trail_dist
                extreme = close

            cash -= margin + entry_fee
            positions[sym] = ScalpPosition(
                symbol=sym, direction=direction,
                entry_price=close, quantity=quantity,
                margin=margin, entry_idx=candle_idx,
                sl_price=sl_price, tp_price=tp_price,
                trail_activation_price=trail_act,
                trail_stop_pct=config.trail_stop_pct,
                extreme_price=extreme,
            )

        # --- 3. Equity / Drawdown ---
        unrealized = 0.0
        for sym, pos in positions.items():
            if sym in data and ts in data[sym].index:
                price = data[sym].loc[ts]["close"]
                if pos.direction == "long":
                    unrealized += (price - pos.entry_price) * pos.quantity
                else:
                    unrealized += (pos.entry_price - price) * pos.quantity

        equity = cash + sum(p.margin for p in positions.values()) + unrealized
        if equity > peak_equity:
            peak_equity = equity
        dd = (peak_equity - equity) / peak_equity * 100
        if dd > max_dd:
            max_dd = dd

    # 남은 포지션 강제 청산
    for sym, pos in list(positions.items()):
        if sym in data:
            close = data[sym].iloc[-1]["close"]
            if pos.direction == "long":
                pnl = (close - pos.entry_price) * pos.quantity
            else:
                pnl = (pos.entry_price - close) * pos.quantity
            fee = pos.quantity * close * (FUTURES_FEE + SLIPPAGE)
            pnl -= fee
            cash += pos.margin + pnl
            trades.append(ScalpTrade(
                symbol=sym, direction=pos.direction,
                entry_price=pos.entry_price, exit_price=close,
                quantity=pos.quantity, margin=pos.margin,
                pnl=pnl, pnl_pct=pnl / pos.margin * 100, fee=fee,
                hold_candles=len(all_ts) - pos.entry_idx,
                exit_reason="force_close",
            ))

    return analyze_results(trades, initial_cash, cash, max_dd, config)


def analyze_results(
    trades: list[ScalpTrade],
    initial_cash: float,
    final_cash: float,
    max_dd: float,
    config: ScalpConfig,
) -> dict:
    """결과 분석"""
    if not trades:
        return {"total_trades": 0, "pf": 0, "win_rate": 0, "total_return": 0, "max_dd": max_dd}

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]

    gross_profit = sum(t.pnl for t in wins) if wins else 0
    gross_loss = abs(sum(t.pnl for t in losses)) if losses else 0.001
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    total_fees = sum(t.fee for t in trades)
    avg_hold = np.mean([t.hold_candles for t in trades]) * TF_MINUTES

    # 청산 사유별
    reasons = {}
    for t in trades:
        r = t.exit_reason
        if r not in reasons:
            reasons[r] = {"count": 0, "wins": 0, "pnl": 0.0}
        reasons[r]["count"] += 1
        reasons[r]["pnl"] += t.pnl
        if t.pnl > 0:
            reasons[r]["wins"] += 1

    # 코인별
    coins = {}
    for t in trades:
        if t.symbol not in coins:
            coins[t.symbol] = {"count": 0, "wins": 0, "pnl": 0.0}
        coins[t.symbol]["count"] += 1
        coins[t.symbol]["pnl"] += t.pnl
        if t.pnl > 0:
            coins[t.symbol]["wins"] += 1

    # 방향별
    longs = [t for t in trades if t.direction == "long"]
    shorts = [t for t in trades if t.direction == "short"]

    return {
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trades) * 100,
        "pf": round(pf, 2),
        "total_return": round((final_cash - initial_cash) / initial_cash * 100, 1),
        "total_pnl": round(final_cash - initial_cash, 2),
        "max_dd": round(max_dd, 2),
        "avg_hold_min": round(avg_hold, 1),
        "total_fees": round(total_fees, 2),
        "avg_pnl": round(np.mean([t.pnl for t in trades]), 2),
        "avg_win_pct": round(np.mean([t.pnl_pct for t in wins]), 2) if wins else 0,
        "avg_loss_pct": round(np.mean([t.pnl_pct for t in losses]), 2) if losses else 0,
        "longs": len(longs),
        "shorts": len(shorts),
        "long_wr": round(sum(1 for t in longs if t.pnl > 0) / len(longs) * 100, 1) if longs else 0,
        "short_wr": round(sum(1 for t in shorts if t.pnl > 0) / len(shorts) * 100, 1) if shorts else 0,
        "by_reason": reasons,
        "by_coin": coins,
        "config_summary": f"BB {config.bb_entry_low}/{config.bb_entry_high} RSI {config.rsi_oversold}/{config.rsi_overbought} SL{config.sl_pct}/TP{config.tp_pct} cd{config.cooldown_candles} hold{config.max_hold_candles}",
    }


def print_results(results: dict, label: str = ""):
    """결과 출력"""
    if results["total_trades"] == 0:
        print(f"  {label}: 거래 없음")
        return

    r = results
    print(f"\n{'=' * 60}")
    if label:
        print(f"  {label}")
        print(f"  Config: {r['config_summary']}")
    print(f"{'=' * 60}")
    print(f"  총 거래:         {r['total_trades']:>6}")
    print(f"    롱:            {r['longs']:>6}  (승률 {r['long_wr']}%)")
    print(f"    숏:            {r['shorts']:>6}  (승률 {r['short_wr']}%)")
    print(f"  승: {r['wins']:>3}  패: {r['losses']:>3}  승률: {r['win_rate']:.1f}%")
    print(f"  Profit Factor:   {r['pf']:>6}")
    print(f"  총 수익:         {r['total_return']:>+6.1f}%  ({r['total_pnl']:+.2f} USDT)")
    print(f"  최대 낙폭:       {r['max_dd']:>6.2f}%")
    print(f"  평균 보유:       {r['avg_hold_min']:>6.1f}분")
    print(f"  총 수수료:       {r['total_fees']:>8.2f} USDT")
    print(f"  평균 승:         {r['avg_win_pct']:>+6.2f}%  평균 패: {r['avg_loss_pct']:>+6.2f}%")

    print(f"\n  청산 사유별:")
    for reason, stats in sorted(r["by_reason"].items(), key=lambda x: -x[1]["pnl"]):
        wr = stats["wins"] / stats["count"] * 100 if stats["count"] > 0 else 0
        print(f"    {reason:<20} {stats['count']:>4}건  승률 {wr:>5.1f}%  PnL {stats['pnl']:>+8.2f}")

    print(f"\n  코인별:")
    for coin, stats in sorted(r["by_coin"].items(), key=lambda x: -x[1]["pnl"]):
        wr = stats["wins"] / stats["count"] * 100 if stats["count"] > 0 else 0
        print(f"    {coin:<12} {stats['count']:>4}건  승률 {wr:>5.1f}%  PnL {stats['pnl']:>+8.2f}")

    # 타겟 확인
    print(f"\n  ── 타겟 확인 ──")
    pf_ok = r["pf"] >= 1.3
    dd_ok = r["max_dd"] <= 10
    wr_ok = r["win_rate"] >= 55
    print(f"  PF >= 1.3:    {'✓' if pf_ok else '✗'} ({r['pf']})")
    print(f"  MDD <= 10%:   {'✓' if dd_ok else '✗'} ({r['max_dd']}%)")
    print(f"  Win% >= 55%:  {'✓' if wr_ok else '✗'} ({r['win_rate']:.1f}%)")
    all_pass = pf_ok and dd_ok and wr_ok
    print(f"  종합:         {'✓ PASS' if all_pass else '✗ FAIL'}")
    print(f"{'=' * 60}")


def param_sweep(data: dict[str, pd.DataFrame], leverage: int = 3):
    """파라미터 스윕"""
    configs = []

    # BB 진입 레벨
    for bb_low, bb_high in [(0.15, 0.85), (0.20, 0.80), (0.25, 0.75), (0.30, 0.70)]:
        # RSI 필터
        for rsi_os, rsi_ob in [(35, 65), (38, 62), (40, 60)]:
            # SL/TP
            for sl, tp in [(0.3, 0.6), (0.4, 0.8), (0.5, 1.0), (0.3, 0.9)]:
                # 쿨다운
                for cd in [3, 6, 12]:
                    configs.append(ScalpConfig(
                        bb_entry_low=bb_low, bb_entry_high=bb_high,
                        rsi_oversold=rsi_os, rsi_overbought=rsi_ob,
                        sl_pct=sl, tp_pct=tp,
                        cooldown_candles=cd,
                        leverage=leverage,
                    ))

    print(f"\n파라미터 스윕: {len(configs)}개 조합 테스트")
    print(f"{'=' * 100}")
    print(f"{'BB':>10} {'RSI':>10} {'SL/TP':>10} {'CD':>4} | {'거래':>5} {'승률':>6} {'PF':>6} {'수익%':>7} {'MDD':>6} {'평균보유':>7}")
    print(f"{'-' * 100}")

    results = []
    for i, cfg in enumerate(configs):
        r = run_scalp_backtest(data, cfg)
        results.append((cfg, r))

        if r["total_trades"] >= 10:  # 최소 거래 수
            mark = " ★" if r["pf"] >= 1.3 and r["win_rate"] >= 55 else ""
            print(f"  {cfg.bb_entry_low}/{cfg.bb_entry_high:>4} "
                  f" {cfg.rsi_oversold}/{cfg.rsi_overbought:>4} "
                  f" {cfg.sl_pct}/{cfg.tp_pct:>4} "
                  f" {cfg.cooldown_candles:>3} "
                  f"| {r['total_trades']:>5} {r['win_rate']:>5.1f}% {r['pf']:>5.2f} "
                  f"{r['total_return']:>+6.1f}% {r['max_dd']:>5.2f}% "
                  f"{r['avg_hold_min']:>5.0f}m{mark}")

        if (i + 1) % 36 == 0:
            print(f"  ... {i + 1}/{len(configs)} 완료")

    # 상위 5개
    valid = [(c, r) for c, r in results if r["total_trades"] >= 20 and r["pf"] >= 1.0]
    valid.sort(key=lambda x: x[1]["pf"], reverse=True)

    print(f"\n{'=' * 60}")
    print(f"  상위 5개 (PF 기준, 최소 20거래)")
    print(f"{'=' * 60}")

    for cfg, r in valid[:5]:
        print_results(r, label=f"PF {r['pf']}")

    return valid


async def main():
    parser = argparse.ArgumentParser(description="스캘핑 백테스트")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--leverage", type=int, default=3)
    parser.add_argument("--param-sweep", action="store_true", help="파라미터 스윕 모드")
    parser.add_argument("--cash", type=float, default=1000.0)
    parser.add_argument("--bb-low", type=float, default=0.20)
    parser.add_argument("--bb-high", type=float, default=0.80)
    parser.add_argument("--rsi-os", type=float, default=38)
    parser.add_argument("--rsi-ob", type=float, default=62)
    parser.add_argument("--sl", type=float, default=0.4)
    parser.add_argument("--tp", type=float, default=0.8)
    parser.add_argument("--cooldown", type=int, default=6)
    parser.add_argument("--max-hold", type=int, default=36)
    parser.add_argument("--pos-pct", type=float, default=0.05)
    parser.add_argument("--max-concurrent", type=int, default=3)
    parser.add_argument("--ema-filter", action="store_true")
    args = parser.parse_args()

    print(f"스캘핑 백테스트 — {args.days}일, {args.leverage}x 레버리지")
    print(f"수수료: {FUTURES_FEE*100:.2f}% + 슬리피지 {SLIPPAGE*100:.2f}% = 왕복 {ROUND_TRIP_COST*100:.2f}%")
    print(f"\n데이터 로딩...")
    data_5m, data_1h = await fetch_data(COINS, args.days)

    if not data_5m:
        print("데이터 로드 실패")
        return

    if args.param_sweep:
        param_sweep(data_5m, args.leverage, data_1h)
    else:
        config = ScalpConfig(
            bb_entry_low=args.bb_low, bb_entry_high=args.bb_high,
            rsi_oversold=args.rsi_os, rsi_overbought=args.rsi_ob,
            sl_pct=args.sl, tp_pct=args.tp,
            cooldown_candles=args.cooldown,
            max_hold_candles=args.max_hold,
            position_pct=args.pos_pct,
            leverage=args.leverage,
            max_concurrent=args.max_concurrent,
            use_ema_filter=args.ema_filter,
        )
        results = run_scalp_backtest(data_5m, config, args.cash, data_1h=data_1h)
        print_results(results, label="스캘핑 백테스트")


if __name__ == "__main__":
    asyncio.run(main())
