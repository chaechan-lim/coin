"""
ML Signal Filter 백테스트 파이프라인.

1단계: 기존 백테스트에서 feature+label 데이터 수집
2단계: Walk-forward 방식으로 ML 모델 학습 + 필터링 효과 측정

실행:
  cd backend && .venv/bin/python ml_backtest.py --days 540
  cd backend && .venv/bin/python ml_backtest.py --days 540 --min-win-prob 0.60
"""
import asyncio
import argparse
import sys
import logging
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

# structlog 조용하게
logging.basicConfig(level=logging.WARNING)
import structlog
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))

from strategies.volatility_breakout import VolatilityBreakoutStrategy
from strategies.ma_crossover import MACrossoverStrategy
from strategies.rsi_strategy import RSIStrategy
from strategies.macd_crossover import MACDCrossoverStrategy
from strategies.bollinger_rsi import BollingerRSIStrategy
from strategies.stochastic_rsi import StochasticRSIStrategy
from strategies.obv_divergence import OBVDivergenceStrategy
from strategies.bnf_deviation import BNFDeviationStrategy
from strategies.cis_momentum import CISMomentumStrategy
from strategies.larry_williams import LarryWilliamsStrategy
from strategies.donchian_channel import DonchianChannelStrategy
from strategies.volatility_regime import VolatilityRegimeStrategy
from strategies.registry import StrategyRegistry
from strategies.combiner import SignalCombiner
from strategies.ml_filter import MLSignalFilter
from strategies.base import Signal
from core.enums import SignalType
from exchange.data_models import Ticker

from backtest import (
    fetch_history, _detect_market_state,
    DEFAULT_FUTURES_PORTFOLIO_COINS, WEIGHTS_7_LIVE, ALL_STRATEGIES_7_LIVE,
    FUTURES_FEE,
)


async def collect_training_data(
    exchange,
    symbols: list[str],
    strategy_names: list[str],
    timeframe: str,
    days: int,
    leverage: int = 3,
    min_confidence: float = 0.55,
) -> pd.DataFrame:
    """백테스트 데이터에서 ML 학습용 feature+label 수집.

    각 캔들마다:
    - 6전략 시그널 수집
    - combiner 결과 확인
    - BUY/SELL 결정 시 feature 추출
    - N캔들 후 수익률 계산 → label (1=수익, 0=손실)
    """
    print("=" * 60)
    print(f"  ML 학습 데이터 수집 | {timeframe} | {days}일")
    print("=" * 60)

    # 데이터 로드
    all_data = {}
    all_syms = list(dict.fromkeys(["BTC/USDT"] + list(symbols)))
    for sym in all_syms:
        try:
            print(f"  {sym} 로딩...", end="", flush=True)
            df = await fetch_history(exchange, sym, timeframe, days)
            all_data[sym] = df
            print(f" {len(df)}캔들")
        except Exception as e:
            print(f" 실패({e})")

    # 전략 로드
    all_strats = StrategyRegistry.create_all()
    strategies = {name: strat for name, strat in all_strats.items() if name in strategy_names}

    weights = {k: v for k, v in WEIGHTS_7_LIVE.items() if k in strategy_names}
    total_w = sum(weights.values())
    if total_w > 0:
        weights = {k: v / total_w for k, v in weights.items()}
    combiner = SignalCombiner(strategy_weights=weights, min_confidence=min_confidence)

    # 유니온 타임스탬프
    all_timestamps = sorted(set().union(*(df.index for df in all_data.values())))
    print(f"  타임스탬프: {len(all_timestamps)}개")

    # BTC 시장 상태용
    btc_df = all_data.get("BTC/USDT")

    # Feature + Label 수집
    LOOKAHEAD = 6  # 6캔들(24h@4h) 후 수익 확인
    records = []
    total_checked = 0

    for i, ts in enumerate(all_timestamps):
        if i < 60 or i + LOOKAHEAD >= len(all_timestamps):
            continue  # 앞뒤 여유

        # 시장 상태 감지
        market_state = "sideways"
        if btc_df is not None and ts in btc_df.index:
            btc_iloc = btc_df.index.get_loc(ts)
            if isinstance(btc_iloc, slice):
                btc_iloc = btc_iloc.start
            market_state, _ = _detect_market_state(btc_df.loc[ts], btc_df, btc_iloc)

        future_ts = all_timestamps[i + LOOKAHEAD]

        for sym in symbols:
            if sym not in all_data or ts not in all_data[sym].index:
                continue
            if future_ts not in all_data[sym].index:
                continue

            sym_df = all_data[sym]
            sym_iloc = sym_df.index.get_loc(ts)
            if isinstance(sym_iloc, slice):
                sym_iloc = sym_iloc.start

            row = sym_df.iloc[sym_iloc]
            cur_price = float(row["close"])
            future_price = float(all_data[sym].loc[future_ts, "close"])

            # 전략 신호 수집
            slice_df = sym_df.iloc[max(0, sym_iloc - 200):sym_iloc + 1]
            ticker = Ticker(
                symbol=sym, last=cur_price,
                bid=cur_price * 0.9999, ask=cur_price * 1.0001,
                high=float(row["high"]), low=float(row["low"]),
                volume=float(row.get("volume", 0)), timestamp=ts,
            )

            signals = []
            for name, strategy in strategies.items():
                try:
                    sig = await strategy.analyze(slice_df.copy(), ticker)
                    signals.append(sig)
                except Exception:
                    pass

            if not signals:
                continue

            decision = combiner.combine(signals, market_state=market_state)

            # BUY 또는 SELL 결정일 때만 feature 수집
            if decision.action == SignalType.HOLD:
                continue

            total_checked += 1

            # Feature 추출
            features = MLSignalFilter.extract_features(
                signals=signals,
                row=row,
                price=cur_price,
                market_state=market_state,
                combined_confidence=decision.combined_confidence,
            )

            # Label: 수익이면 1, 손실이면 0
            if decision.action == SignalType.BUY:
                # 롱: 가격 상승이 수익
                pnl_pct = (future_price - cur_price) / cur_price * 100 * leverage
                fee_pct = FUTURES_FEE * leverage * 100 * 2  # 왕복
                net_pnl = pnl_pct - fee_pct
            else:
                # 숏: 가격 하락이 수익
                pnl_pct = (cur_price - future_price) / cur_price * 100 * leverage
                fee_pct = FUTURES_FEE * leverage * 100 * 2
                net_pnl = pnl_pct - fee_pct

            features["label"] = 1 if net_pnl > 0 else 0
            features["net_pnl_pct"] = net_pnl
            features["side"] = "long" if decision.action == SignalType.BUY else "short"
            features["symbol"] = sym
            features["timestamp"] = ts

            records.append(features)

    df_records = pd.DataFrame(records)
    print(f"\n  총 시그널 체크: {total_checked}")
    print(f"  수집된 샘플: {len(records)}")
    if len(records) > 0:
        win_rate = df_records["label"].mean() * 100
        print(f"  승률: {win_rate:.1f}%")
        print(f"  평균 수익: {df_records[df_records['label']==1]['net_pnl_pct'].mean():.2f}%")
        print(f"  평균 손실: {df_records[df_records['label']==0]['net_pnl_pct'].mean():.2f}%")
        print(f"  롱: {(df_records['side']=='long').sum()}, 숏: {(df_records['side']=='short').sum()}")

    return df_records


def train_and_evaluate(
    df: pd.DataFrame,
    min_win_prob: float = 0.55,
) -> dict:
    """Walk-forward 학습 + 필터링 효과 측정."""
    from strategies.ml_filter import MLSignalFilter

    feature_names = MLSignalFilter.FEATURE_NAMES
    X = df[feature_names].values
    y = df["label"].values

    print(f"\n{'='*60}")
    print(f"  ML 모델 학습 | 샘플 {len(y)} | 승률 {y.mean()*100:.1f}%")
    print(f"{'='*60}")

    ml_filter = MLSignalFilter(min_win_prob=min_win_prob)
    metrics = ml_filter.train(X, y, n_splits=3)

    print(f"  CV Accuracy: {metrics['accuracy_mean']:.3f}")
    print(f"  CV Precision: {metrics['precision_mean']:.3f}")
    print(f"  CV F1: {metrics['f1_mean']:.3f}")
    print(f"  Positive rate: {metrics['positive_rate']:.3f}")

    # Feature importance top 10
    importance = metrics["feature_importance"]
    sorted_imp = sorted(importance.items(), key=lambda x: x[1], reverse=True)
    print(f"\n  Feature Importance Top 10:")
    for name, imp in sorted_imp[:10]:
        print(f"    {name:30s}: {imp}")

    # Walk-forward 시뮬레이션: 앞 70% 학습 → 뒤 30% 필터링 효과
    split_idx = int(len(X) * 0.7)
    X_train, X_test = X[:split_idx], X[split_idx:]
    y_test = y[split_idx:]
    df_test = df.iloc[split_idx:].copy()

    ml_test = MLSignalFilter(min_win_prob=min_win_prob)
    ml_test.train(X_train, y[:split_idx])

    # 필터링 전후 비교
    probs = ml_test._model.predict_proba(X_test)[:, 1]
    df_test = df_test.copy()
    df_test["win_prob"] = probs
    df_test["ml_pass"] = probs >= min_win_prob

    print(f"\n  Walk-Forward 검증 (뒤 30%: {len(df_test)}건):")
    print(f"  {'':30s} | {'전체':>8s} | {'ML통과':>8s} | {'ML차단':>8s}")
    print(f"  {'-'*30}-+-{'-'*8}-+-{'-'*8}-+-{'-'*8}")

    total = len(df_test)
    passed = df_test["ml_pass"].sum()
    blocked = total - passed

    total_wr = df_test["label"].mean() * 100
    passed_wr = df_test[df_test["ml_pass"]]["label"].mean() * 100 if passed > 0 else 0
    blocked_wr = df_test[~df_test["ml_pass"]]["label"].mean() * 100 if blocked > 0 else 0

    total_pnl = df_test["net_pnl_pct"].sum()
    passed_pnl = df_test[df_test["ml_pass"]]["net_pnl_pct"].sum()
    blocked_pnl = df_test[~df_test["ml_pass"]]["net_pnl_pct"].sum()

    print(f"  {'거래 수':30s} | {total:>8d} | {int(passed):>8d} | {int(blocked):>8d}")
    print(f"  {'승률':30s} | {total_wr:>7.1f}% | {passed_wr:>7.1f}% | {blocked_wr:>7.1f}%")
    print(f"  {'총 PnL%':30s} | {total_pnl:>+7.1f}% | {passed_pnl:>+7.1f}% | {blocked_pnl:>+7.1f}%")

    avg_pnl_all = df_test["net_pnl_pct"].mean()
    avg_pnl_pass = df_test[df_test["ml_pass"]]["net_pnl_pct"].mean() if passed > 0 else 0
    print(f"  {'평균 PnL%':30s} | {avg_pnl_all:>+7.2f}% | {avg_pnl_pass:>+7.2f}%")

    # Walk-forward 모델 저장 (앞 70%로만 학습 — 백테스트용)
    from strategies.ml_filter import MODEL_DIR as _MD
    _MD.mkdir(parents=True, exist_ok=True)
    ml_wf = MLSignalFilter(min_win_prob=min_win_prob)
    ml_wf.train(X_train, y[:split_idx])
    ml_wf.save(str(_MD / "signal_filter_wf.pkl"))
    print(f"\n  Walk-forward 모델 저장: data/ml_models/signal_filter_wf.pkl")

    # 전체 학습 모델도 저장 (라이브용)
    ml_filter.save()
    print(f"  전체 학습 모델 저장: data/ml_models/signal_filter.pkl")

    return {
        "total_samples": total,
        "passed": int(passed),
        "blocked": int(blocked),
        "total_win_rate": total_wr,
        "passed_win_rate": passed_wr,
        "blocked_win_rate": blocked_wr,
        "total_pnl": total_pnl,
        "passed_pnl": passed_pnl,
        "metrics": metrics,
    }


async def main():
    parser = argparse.ArgumentParser(description="ML Signal Filter 백테스트")
    parser.add_argument("--days", type=int, default=540)
    parser.add_argument("--timeframe", default="4h")
    parser.add_argument("--leverage", type=int, default=3)
    parser.add_argument("--min-confidence", type=float, default=0.55)
    parser.add_argument("--min-win-prob", type=float, default=0.55)
    parser.add_argument("--portfolio-coins", nargs="+", default=None)
    args = parser.parse_args()

    from exchange.binance_usdm_adapter import BinanceUSDMAdapter
    print("바이낸스 USDM 선물 연결 중...")
    exchange = BinanceUSDMAdapter(api_key="", api_secret="", testnet=False)
    await exchange.initialize()

    coins = args.portfolio_coins
    if coins:
        coins = [c if "/" in c else f"{c}/USDT" for c in coins]
    else:
        coins = DEFAULT_FUTURES_PORTFOLIO_COINS

    try:
        # 1단계: 데이터 수집
        df = await collect_training_data(
            exchange=exchange,
            symbols=coins,
            strategy_names=ALL_STRATEGIES_7_LIVE,
            timeframe=args.timeframe,
            days=args.days,
            leverage=args.leverage,
            min_confidence=args.min_confidence,
        )

        if len(df) < 30:
            print(f"\n  ⚠️ 샘플 부족 ({len(df)}개). ML 학습 불가.")
            return

        # 2단계: 학습 + 평가
        results = train_and_evaluate(df, min_win_prob=args.min_win_prob)

        print(f"\n{'='*60}")
        if results["passed_win_rate"] > results["total_win_rate"]:
            improvement = results["passed_win_rate"] - results["total_win_rate"]
            print(f"  ✓ ML 필터로 승률 {improvement:.1f}%p 개선 가능")
            print(f"    ({results['total_win_rate']:.1f}% → {results['passed_win_rate']:.1f}%)")
            print(f"    거래 {results['total_samples']} → {results['passed']}건 ({results['blocked']}건 차단)")
        else:
            print(f"  ✗ ML 필터 효과 없음 (승률 {results['total_win_rate']:.1f}% → {results['passed_win_rate']:.1f}%)")
        print(f"{'='*60}")

    finally:
        await exchange.close()


if __name__ == "__main__":
    asyncio.run(main())
