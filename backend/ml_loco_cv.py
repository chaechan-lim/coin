"""
Leave-One-Coin-Out (LOCO) 교차검증으로 ML 과적합 검증.

4코인 학습 → 1코인 테스트 × 5회.
진짜 out-of-sample 성능 측정.

실행:
  cd backend && .venv/bin/python ml_loco_cv.py --days 540
"""
import asyncio
import argparse
import logging
import numpy as np
import pandas as pd

logging.basicConfig(level=logging.WARNING)
import structlog
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))

from ml_backtest import collect_training_data
from strategies.ml_filter import MLSignalFilter
from backtest import (
    DEFAULT_FUTURES_PORTFOLIO_COINS, WEIGHTS_6, ALL_STRATEGIES_6,
)


async def main():
    parser = argparse.ArgumentParser(description="LOCO 교차검증")
    parser.add_argument("--days", type=int, default=540)
    parser.add_argument("--timeframe", default="4h")
    parser.add_argument("--min-win-prob", type=float, default=0.60)
    args = parser.parse_args()

    from exchange.binance_usdm_adapter import BinanceUSDMAdapter
    print("바이낸스 연결 중...")
    exchange = BinanceUSDMAdapter(api_key="", api_secret="", testnet=False)
    await exchange.initialize()

    coins = DEFAULT_FUTURES_PORTFOLIO_COINS
    print(f"코인: {coins}")

    try:
        # 전체 데이터 수집
        df = await collect_training_data(
            exchange=exchange,
            symbols=coins,
            strategy_names=ALL_STRATEGIES_6,
            timeframe=args.timeframe,
            days=args.days,
            leverage=3,
            min_confidence=0.55,
        )

        if len(df) < 30:
            print(f"샘플 부족: {len(df)}")
            return

        # 데이터 저장 (재사용)
        df.to_csv("data/ml_training_data.csv", index=False)
        print(f"\n학습 데이터 저장: data/ml_training_data.csv ({len(df)}건)")

        feature_names = MLSignalFilter.FEATURE_NAMES

        # ── LOCO 교차검증 ──
        print(f"\n{'='*60}")
        print(f"  Leave-One-Coin-Out 교차검증")
        print(f"{'='*60}")

        results = []
        for test_coin in coins:
            train_coins = [c for c in coins if c != test_coin]
            train_mask = df["symbol"].isin(train_coins)
            test_mask = df["symbol"] == test_coin

            X_train = df[train_mask][feature_names].values
            y_train = df[train_mask]["label"].values
            X_test = df[test_mask][feature_names].values
            y_test = df[test_mask]["label"].values
            pnl_test = df[test_mask]["net_pnl_pct"].values

            if len(X_test) < 5:
                print(f"  {test_coin}: 테스트 샘플 부족 ({len(X_test)}건)")
                continue

            ml = MLSignalFilter(min_win_prob=args.min_win_prob)
            ml.train(X_train, y_train, n_splits=3)
            probs = ml._model.predict_proba(X_test)[:, 1]

            ml_pass = probs >= args.min_win_prob
            pass_count = ml_pass.sum()
            total = len(y_test)

            total_wr = y_test.mean() * 100
            pass_wr = y_test[ml_pass].mean() * 100 if pass_count > 0 else 0
            pass_pnl = pnl_test[ml_pass].sum() if pass_count > 0 else 0
            total_pnl = pnl_test.sum()

            print(f"\n  {test_coin} (학습: {train_coins})")
            print(f"    전체 {total}건, ML통과 {int(pass_count)}건 ({pass_count/total*100:.0f}%)")
            print(f"    승률: {total_wr:.1f}% → {pass_wr:.1f}% (ML필터)")
            print(f"    PnL:  {total_pnl:+.1f}% → {pass_pnl:+.1f}% (ML필터)")

            results.append({
                "coin": test_coin,
                "total": total,
                "ml_pass": int(pass_count),
                "total_wr": total_wr,
                "ml_wr": pass_wr,
                "total_pnl": total_pnl,
                "ml_pnl": pass_pnl,
            })

        # ── 시간 기반 검증 (기존) ──
        print(f"\n{'='*60}")
        print(f"  시간 기반 Walk-Forward (70/30)")
        print(f"{'='*60}")

        X_all = df[feature_names].values
        y_all = df["label"].values
        pnl_all = df["net_pnl_pct"].values

        split_idx = int(len(X_all) * 0.7)
        X_tr, X_te = X_all[:split_idx], X_all[split_idx:]
        y_te = y_all[split_idx:]
        pnl_te = pnl_all[split_idx:]

        ml_wf = MLSignalFilter(min_win_prob=args.min_win_prob)
        ml_wf.train(X_tr, y_all[:split_idx], n_splits=3)
        probs_wf = ml_wf._model.predict_proba(X_te)[:, 1]
        wf_pass = probs_wf >= args.min_win_prob

        wf_total = len(y_te)
        wf_pass_count = wf_pass.sum()
        wf_total_wr = y_te.mean() * 100
        wf_pass_wr = y_te[wf_pass].mean() * 100 if wf_pass_count > 0 else 0
        wf_total_pnl = pnl_te.sum()
        wf_pass_pnl = pnl_te[wf_pass].sum() if wf_pass_count > 0 else 0

        print(f"  전체 {wf_total}건, ML통과 {int(wf_pass_count)}건")
        print(f"  승률: {wf_total_wr:.1f}% → {wf_pass_wr:.1f}%")
        print(f"  PnL:  {wf_total_pnl:+.1f}% → {wf_pass_pnl:+.1f}%")

        # ── 비교 요약 ──
        print(f"\n{'='*60}")
        print(f"  LOCO vs Walk-Forward 비교")
        print(f"{'='*60}")
        print(f"  {'검증':15s} | {'승률개선':>10s} | {'PnL개선':>10s}")
        print(f"  {'-'*15}-+-{'-'*10}-+-{'-'*10}")

        if results:
            loco_wr_improve = np.mean([r["ml_wr"] - r["total_wr"] for r in results])
            loco_pnl_improve = np.mean([r["ml_pnl"] - r["total_pnl"] for r in results])
            print(f"  {'LOCO (OOS)':15s} | {loco_wr_improve:+9.1f}% | {loco_pnl_improve:+9.1f}%")

        wf_wr_improve = wf_pass_wr - wf_total_wr
        wf_pnl_improve = wf_pass_pnl - wf_total_pnl
        print(f"  {'Walk-Forward':15s} | {wf_wr_improve:+9.1f}% | {wf_pnl_improve:+9.1f}%")

        print(f"\n  LOCO 개선 < Walk-Forward 개선 → 과적합 존재")
        print(f"  LOCO 개선 ≈ Walk-Forward 개선 → 일반화 양호")
        print(f"{'='*60}")

    finally:
        await exchange.close()


if __name__ == "__main__":
    asyncio.run(main())
