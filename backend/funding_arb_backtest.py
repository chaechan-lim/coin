"""
Funding Rate Arbitrage 백테스트.

전략:
- Delta-neutral: 현물 매수 + 동일 notional 선물 숏 (1x leverage)
- 가격 변동은 상쇄 → 시장 방향 무관
- Funding rate 양수 → short position이 받음 → 수익
- Funding rate 음수 → short position이 지불 → 손실

비용:
- 진입: 현물 매수(0.10%) + 선물 숏(0.04%) = notional × 0.14%
- 청산: 같음 → round-trip 0.28%
- 슬리피지: 0.02% × 2 = 0.04%

학술 검증 (Sharpe 3-6, 연 12-25%) 재현 가능한지 확인.
"""
from __future__ import annotations
import asyncio
import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pandas as pd
import numpy as np

# 비용 상수
SPOT_FEE = 0.001       # 바이낸스 현물 0.10%
FUTURES_FEE = 0.0004   # 바이낸스 USDM 선물 0.04%
SPOT_SLIPPAGE = 0.0002    # 0.02%
FUTURES_SLIPPAGE = 0.0001  # 0.01%

CACHE_DIR = Path(__file__).parent / ".cache"


@dataclass
class FundingPosition:
    """Delta-neutral 포지션 (현물 long + 선물 short)."""
    coin: str
    entry_price: float
    quantity: float          # spot/futures 동일 수량
    notional: float          # entry_price × quantity (각 사이드)
    entered_at: pd.Timestamp
    entry_cost: float        # 수수료 + 슬리피지 합계


@dataclass
class FundingArbResult:
    initial_capital: float
    final_capital: float
    funding_received: float
    total_fees: float
    return_pct: float
    sharpe: float
    max_drawdown: float
    days: int
    n_funding_events: int
    n_negative_events: int  # funding<0 발생 횟수
    coins: list[str]
    equity_curve: list[tuple[pd.Timestamp, float]] = field(default_factory=list)


def _ensure_tz(df: pd.DataFrame) -> pd.DataFrame:
    """DatetimeIndex로 변환 + UTC 보장."""
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, errors="coerce", utc=True)
    elif df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df = df[df.index.notna()]
    df.sort_index(inplace=True)
    return df


def load_funding_history(coin: str) -> pd.DataFrame:
    """funding_BTC_USDT_USDT.csv 형태 로드."""
    path = CACHE_DIR / f"funding_{coin}_USDT_USDT.csv"
    if not path.exists():
        raise FileNotFoundError(f"{path} 없음 (먼저 fetch 필요)")
    df = pd.read_csv(path, parse_dates=["timestamp"], index_col="timestamp")
    return _ensure_tz(df)


def load_price_history(coin: str, timeframe: str = "1h") -> pd.DataFrame:
    """1h 또는 5m 가격 캐시 로드."""
    path = CACHE_DIR / f"{coin}_USDT_{timeframe}.csv"
    if not path.exists():
        raise FileNotFoundError(f"{path} 없음")
    df = pd.read_csv(path, parse_dates=["timestamp"], index_col="timestamp")
    return _ensure_tz(df)


def find_price_at(df: pd.DataFrame, ts: pd.Timestamp) -> float | None:
    """ts 시점의 가장 가까운 가격."""
    if len(df) == 0:
        return None
    idx = df.index.searchsorted(ts, side="right") - 1
    if idx < 0:
        return None
    return float(df["close"].iloc[idx])


def simulate_static_arb(
    coins: list[str],
    days: int,
    initial_capital: float = 1000.0,
    use_negative_fallback: bool = False,
    rebalance_days: int = 0,
) -> FundingArbResult:
    """가장 단순한 버전: 진입 → funding 수령 → 종료 시 청산.

    Args:
        coins: 거래 코인 (예: ["BTC", "ETH", "SOL", "XRP"])
        days: 백테스트 기간
        initial_capital: 초기 자본
        use_negative_fallback: True면 funding 음수 시 청산 (TODO)
        rebalance_days: 리밸런싱 주기 (0=리밸런싱 없음)
    """
    # 데이터 로드
    funding_data = {c: load_funding_history(c) for c in coins}
    price_data = {c: load_price_history(c, "1h") for c in coins}

    # 시작 시점
    end_ts = datetime.now(timezone.utc)
    start_ts = end_ts - timedelta(days=days)
    start_ts = pd.Timestamp(start_ts).tz_convert("UTC")

    # 코인당 자본 (delta-neutral이라 절반은 spot, 절반은 futures margin)
    capital_per_coin = initial_capital / len(coins)
    notional_per_coin = capital_per_coin / 2  # 한쪽

    # 진입
    positions: dict[str, FundingPosition] = {}
    cash = initial_capital
    total_fees = 0.0

    for coin in coins:
        price = find_price_at(price_data[coin], start_ts)
        if price is None:
            print(f"  ⚠️ {coin} 시작 시점 가격 없음, 스킵")
            continue
        quantity = notional_per_coin / price
        entry_cost = notional_per_coin * (SPOT_FEE + FUTURES_FEE + SPOT_SLIPPAGE + FUTURES_SLIPPAGE)
        cash -= entry_cost
        total_fees += entry_cost
        positions[coin] = FundingPosition(
            coin=coin,
            entry_price=price,
            quantity=quantity,
            notional=notional_per_coin,
            entered_at=start_ts,
            entry_cost=entry_cost,
        )

    # 시뮬레이션: 모든 funding 이벤트를 시간순으로 처리
    all_events: list[tuple[pd.Timestamp, str, float]] = []
    for coin, fdf in funding_data.items():
        if coin not in positions:
            continue
        events = fdf[fdf.index >= start_ts]
        events = events[events.index <= pd.Timestamp(end_ts).tz_convert("UTC")]
        for ts, row in events.iterrows():
            all_events.append((ts, coin, float(row["fundingRate"])))
    all_events.sort(key=lambda x: x[0])

    funding_received = 0.0
    n_negative = 0
    equity_curve = [(start_ts, initial_capital)]
    funding_per_coin: dict[str, float] = {c: 0.0 for c in positions}

    for ts, coin, rate in all_events:
        pos = positions[coin]
        # short position이 funding 받음 (양수면 +)
        payment = pos.notional * rate
        funding_received += payment
        funding_per_coin[coin] += payment
        if rate < 0:
            n_negative += 1
        # equity 업데이트 (가격 변동 무시 — delta neutral)
        current_equity = initial_capital - total_fees + funding_received
        equity_curve.append((ts, current_equity))

    # 종료 시 청산 비용
    exit_cost = sum(p.notional * (SPOT_FEE + FUTURES_FEE + SPOT_SLIPPAGE + FUTURES_SLIPPAGE)
                    for p in positions.values())
    cash -= exit_cost
    total_fees += exit_cost

    final_capital = initial_capital - total_fees + funding_received
    return_pct = (final_capital - initial_capital) / initial_capital * 100

    # Sharpe 계산 (8시간 단위 → 연 환산)
    if len(equity_curve) >= 2:
        equities = np.array([e[1] for e in equity_curve])
        returns = np.diff(equities) / equities[:-1]
        if len(returns) > 0 and returns.std() > 0:
            # 8시간 = 1년 / (365*3) periods
            sharpe = returns.mean() / returns.std() * np.sqrt(365 * 3)
        else:
            sharpe = 0.0
    else:
        sharpe = 0.0

    # MDD
    if len(equity_curve) >= 2:
        equities = np.array([e[1] for e in equity_curve])
        peak = np.maximum.accumulate(equities)
        dd = (peak - equities) / peak
        max_drawdown = float(dd.max() * 100)
    else:
        max_drawdown = 0.0

    return FundingArbResult(
        initial_capital=initial_capital,
        final_capital=final_capital,
        funding_received=funding_received,
        total_fees=total_fees,
        return_pct=return_pct,
        sharpe=sharpe,
        max_drawdown=max_drawdown,
        days=days,
        n_funding_events=len(all_events),
        n_negative_events=n_negative,
        coins=list(positions.keys()),
        equity_curve=equity_curve,
    )


def print_result(r: FundingArbResult, label: str = ""):
    print(f"\n{'='*60}")
    print(f"  Funding Rate Arbitrage 백테스트 {label}")
    print(f"{'='*60}")
    print(f"  코인:           {', '.join(r.coins)}")
    print(f"  기간:           {r.days}일")
    print(f"  초기 자본:      {r.initial_capital:>12,.2f} USDT")
    print(f"  최종 자본:      {r.final_capital:>12,.2f} USDT")
    print(f"  순수익:         {r.final_capital - r.initial_capital:>+12,.2f} USDT ({r.return_pct:+.2f}%)")
    print(f"  연환산 수익률:  {r.return_pct * 365 / r.days:>+12.2f}%")
    print(f"")
    print(f"  Funding 수령:   {r.funding_received:>+12,.2f} USDT")
    print(f"  총 수수료:      {r.total_fees:>12,.2f} USDT")
    print(f"  Funding events: {r.n_funding_events:,} (음수 {r.n_negative_events}회 = {r.n_negative_events/max(r.n_funding_events,1)*100:.1f}%)")
    print(f"")
    print(f"  Sharpe Ratio:   {r.sharpe:>12.2f}")
    print(f"  Max Drawdown:   {r.max_drawdown:>12.2f}%")
    print(f"{'='*60}")


def simulate_dynamic_arb(
    coins: list[str],
    days: int,
    initial_capital: float = 1000.0,
    funding_threshold: float = 0.0,
    rebalance_hours: int = 168,        # 매주 재평가 (168h = 7일)
    max_positions: int = 3,
) -> FundingArbResult:
    """동적 funding arb: 매 8시간마다 가장 높은 funding 코인만 보유.

    - funding_threshold 이상만 진입
    - 음수 또는 threshold 미달 시 청산
    - max_positions 만큼만 동시 보유 (높은 funding 우선)
    """
    funding_data = {c: load_funding_history(c) for c in coins}
    price_data = {c: load_price_history(c, "1h") for c in coins}

    end_ts = pd.Timestamp(datetime.now(timezone.utc))
    start_ts = end_ts - pd.Timedelta(days=days)

    # 모든 funding 이벤트를 시간순으로 정렬
    all_events: list[tuple[pd.Timestamp, str, float]] = []
    for coin, fdf in funding_data.items():
        events = fdf[(fdf.index >= start_ts) & (fdf.index <= end_ts)]
        for ts, row in events.iterrows():
            all_events.append((ts, coin, float(row["fundingRate"])))
    all_events.sort(key=lambda x: x[0])

    # 시간별로 그룹화 (같은 시점의 모든 코인 funding을 한 번에 평가)
    from collections import defaultdict
    events_by_time: dict[pd.Timestamp, dict[str, float]] = defaultdict(dict)
    for ts, coin, rate in all_events:
        # 8시간 단위로 정렬 (00, 08, 16)
        events_by_time[ts.floor("8h")][coin] = rate

    cash = initial_capital
    total_fees = 0.0
    funding_received = 0.0
    n_negative = 0
    n_events = 0
    n_rebalances = 0
    positions: dict[str, FundingPosition] = {}

    equity_curve = [(start_ts, initial_capital)]

    sorted_times = sorted(events_by_time.keys())
    last_rebalance_ts: pd.Timestamp | None = None
    # 최근 N개 funding의 평균으로 코인 ranking (단일 8h funding은 노이즈 큼)
    funding_ma_window = 21  # 7일 × 3 (3 events/day)
    funding_history_per_coin: dict[str, list[float]] = {c: [] for c in coins}

    for ts in sorted_times:
        coin_rates = events_by_time[ts]
        # 1. 기존 포지션 funding 수령 (보유 중이었던 것)
        for coin, pos in list(positions.items()):
            if coin in coin_rates:
                rate = coin_rates[coin]
                payment = pos.notional * rate
                funding_received += payment
                n_events += 1
                if rate < 0:
                    n_negative += 1

        # 2. funding history 업데이트 (이동 평균용)
        for coin, rate in coin_rates.items():
            funding_history_per_coin[coin].append(rate)
            if len(funding_history_per_coin[coin]) > funding_ma_window:
                funding_history_per_coin[coin].pop(0)

        # 3. 재평가 주기 체크 (rebalance_hours마다)
        if last_rebalance_ts is not None:
            elapsed = (ts - last_rebalance_ts).total_seconds() / 3600
            if elapsed < rebalance_hours:
                # equity 기록만 하고 다음 이벤트로
                current_equity = initial_capital - total_fees + funding_received
                equity_curve.append((ts, current_equity))
                continue
        last_rebalance_ts = ts

        # 4. 코인별 funding 이동 평균으로 ranking
        coin_ma = {}
        for coin, hist in funding_history_per_coin.items():
            if len(hist) >= 3:  # 최소 1일치
                coin_ma[coin] = np.mean(hist)
        ranked = sorted(coin_ma.items(), key=lambda x: -x[1])
        target_coins = [c for c, r in ranked if r > funding_threshold][:max_positions]

        # 3. 청산할 포지션 (target에 없거나 funding 미달)
        to_close = [c for c in positions if c not in target_coins]
        for coin in to_close:
            pos = positions.pop(coin)
            close_cost = pos.notional * (SPOT_FEE + FUTURES_FEE + SPOT_SLIPPAGE + FUTURES_SLIPPAGE)
            total_fees += close_cost
            n_rebalances += 1

        # 4. 신규 진입 (target 중 미보유)
        new_coins = [c for c in target_coins if c not in positions]
        if new_coins:
            free_capital = initial_capital - total_fees + funding_received
            # 보유 중인 코인 자본 제외
            available = free_capital - sum(p.notional * 2 for p in positions.values())
            per_coin = available / len(new_coins) if len(new_coins) > 0 else 0
            for coin in new_coins:
                if per_coin <= 0:
                    break
                price = find_price_at(price_data[coin], ts)
                if price is None:
                    continue
                notional = per_coin / 2  # spot + futures
                quantity = notional / price
                entry_cost = notional * (SPOT_FEE + FUTURES_FEE + SPOT_SLIPPAGE + FUTURES_SLIPPAGE)
                total_fees += entry_cost
                n_rebalances += 1
                positions[coin] = FundingPosition(
                    coin=coin, entry_price=price, quantity=quantity,
                    notional=notional, entered_at=ts, entry_cost=entry_cost,
                )

        # equity 기록
        current_equity = initial_capital - total_fees + funding_received
        equity_curve.append((ts, current_equity))

    # 최종 청산
    for pos in positions.values():
        total_fees += pos.notional * (SPOT_FEE + FUTURES_FEE + SPOT_SLIPPAGE + FUTURES_SLIPPAGE)

    final_capital = initial_capital - total_fees + funding_received
    return_pct = (final_capital - initial_capital) / initial_capital * 100

    if len(equity_curve) >= 2:
        equities = np.array([e[1] for e in equity_curve])
        returns = np.diff(equities) / equities[:-1]
        if len(returns) > 0 and returns.std() > 0:
            sharpe = returns.mean() / returns.std() * np.sqrt(365 * 3)
        else:
            sharpe = 0.0
        peak = np.maximum.accumulate(equities)
        dd = (peak - equities) / peak
        max_drawdown = float(dd.max() * 100)
    else:
        sharpe = 0.0
        max_drawdown = 0.0

    return FundingArbResult(
        initial_capital=initial_capital,
        final_capital=final_capital,
        funding_received=funding_received,
        total_fees=total_fees,
        return_pct=return_pct,
        sharpe=sharpe,
        max_drawdown=max_drawdown,
        days=days,
        n_funding_events=n_events,
        n_negative_events=n_negative,
        coins=list(coins),
        equity_curve=equity_curve,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--coins", nargs="+", default=["BTC", "ETH", "SOL", "XRP"],
                        help="대상 코인 (BNB는 funding 음수라 기본 제외)")
    parser.add_argument("--days", type=int, default=540, help="백테스트 기간 (일)")
    parser.add_argument("--capital", type=float, default=1000.0, help="초기 자본")
    parser.add_argument("--dynamic", action="store_true", help="동적 코인 선택 모드")
    parser.add_argument("--threshold", type=float, default=0.0, help="funding 진입 임계값 (8h 단위, 0=양수만)")
    parser.add_argument("--max-positions", type=int, default=3, help="동시 보유 코인 수")
    args = parser.parse_args()

    print(f"\n  Funding Rate Arbitrage 백테스트")
    print(f"  Strategy: Delta-neutral (spot long + futures short, 1x)")
    print(f"  Mode: {'동적 (top-N funding)' if args.dynamic else '정적 (균등)'}")
    if args.dynamic:
        print(f"  Threshold: {args.threshold*100:.4f}%/8h, Max positions: {args.max_positions}")
    print(f"  Cost: spot {SPOT_FEE*100:.2f}% + futures {FUTURES_FEE*100:.2f}% + slippage {(SPOT_SLIPPAGE+FUTURES_SLIPPAGE)*100:.2f}%")

    # 다양한 기간 비교
    for d in [90, 180, 360, 540, 1000]:
        if d > 1000:
            continue
        try:
            if args.dynamic:
                r = simulate_dynamic_arb(args.coins, d, args.capital,
                                         funding_threshold=args.threshold,
                                         max_positions=args.max_positions)
            else:
                r = simulate_static_arb(args.coins, d, args.capital)
            print_result(r, label=f"({d}d)")
        except Exception as e:
            print(f"  {d}d 실패: {e}")


if __name__ == "__main__":
    main()
