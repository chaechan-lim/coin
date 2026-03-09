"""
숏 전략 강화 연구 백테스트
==========================
실험 A: 하락장 SELL 가중치 (추세추종 강화)
실험 B: 하락장 숏 신뢰도 임계값 하향
실험 C: 하락장 숏 사이징 1.5배
실험 D: 10전략 (기존 6 + 현물 4)
실험 E: 최적 조합

Usage:
  cd backend && .venv/bin/python backtest_short_research.py
"""
import asyncio
import sys
import os
import logging

logging.basicConfig(level=logging.WARNING)
import structlog
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
)

from backtest import (
    FuturesPortfolioBacktester,
    ALL_STRATEGIES_6, ALL_STRATEGIES_10,
    WEIGHTS_6, FUTURES_FEE, FUNDING_RATE,
    print_futures_portfolio_result,
)
from exchange.binance_usdm_adapter import BinanceUSDMAdapter


# ── 실험 설정 ─────────────────────────────────────────────
DAYS = 540
TIMEFRAME = "4h"
LEVERAGE = 3
BALANCE = 10_000

# 기존 6전략 가중치 (현재 라이브)
W6 = WEIGHTS_6.copy()

# 10전략 가중치 (기존 6 + 현물 4)
# 현물 전략 SELL 신호 강점 반영: larry_williams, donchian, cis에 적정 배분
W10 = {
    "ma_crossover":        0.06,
    "rsi":                 0.18,
    "macd_crossover":      0.06,
    "bollinger_rsi":       0.22,
    "stochastic_rsi":      0.11,
    "obv_divergence":      0.09,
    # 신규 4전략 (현물에서 검증된 추세/채널 전략)
    "bnf_deviation":       0.06,
    "cis_momentum":        0.07,
    "larry_williams":      0.08,
    "donchian_channel":    0.07,
}


COINS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT"]


async def run_experiment(name, exchange, **kwargs):
    """단일 실험 실행."""
    print(f"\n{'='*70}")
    print(f"  실험: {name}")
    print(f"{'='*70}")

    bt = FuturesPortfolioBacktester(exchange=exchange, **kwargs)
    result = await bt.run(TIMEFRAME, DAYS)
    print_futures_portfolio_result(result)
    return result


async def main():
    print("바이낸스 연결 중...")
    exchange = BinanceUSDMAdapter(api_key="", api_secret="", testnet=False)
    await exchange.initialize()

    results = {}

    common = dict(
        symbols=COINS,
        initial_balance=BALANCE,
        min_confidence=0.55,
        stop_loss_pct=8.0,
        take_profit_pct=16.0,
        trailing_activation=5.0,
        trailing_stop=3.5,
        adaptive_weights=True,
        dynamic_sl=True,
        agent_market=True,
        trade_cooldown=36,
        leverage=LEVERAGE,
        futures_fee=FUTURES_FEE,
        funding_rate=FUNDING_RATE,
        position_pct=0.35,
        short_all=True,
        dynamic_position=False,
        dual_timeframe=False,
        max_positions=5,
        risk_enabled=True,
        trade_limit_enabled=True,
        risk_max_drawdown=0.30,
        risk_daily_loss=0.05,
        risk_max_concentration=0.40,
        trade_daily_buy_limit=20,
        trade_max_coin_buys=3,
    )

    # ── 0. Baseline: 현재 라이브 설정 ──────────────────────
    results["baseline"] = await run_experiment(
        "0. BASELINE (현재 라이브 — 6전략, short-all, BNB)",
        exchange,
        strategy_names=ALL_STRATEGIES_6,
        **common,
    )

    # ── A. 방향별 가중치 (하락장 SELL에 추세추종 강화) ──────
    results["exp_a"] = await run_experiment(
        "A. 방향별 가중치 (BUY=추세추종, SELL=평균회귀) — 재검증",
        exchange,
        strategy_names=ALL_STRATEGIES_6,
        directional_weights=True,
        **common,
    )

    # ── B. 숏 신뢰도 하향 (0.55 → 0.45) ──────────────────
    results["exp_b"] = await run_experiment(
        "B. 낮은 신뢰도 (0.55 → 0.45)",
        exchange,
        strategy_names=ALL_STRATEGIES_6,
        min_confidence=0.45,
        **{k: v for k, v in common.items() if k != "min_confidence"},
    )

    # ── C. 숏 사이징 강화 (position_pct 35% → 45%) ────────
    results["exp_c"] = await run_experiment(
        "C. 포지션 확대 (35% → 45%)",
        exchange,
        strategy_names=ALL_STRATEGIES_6,
        position_pct=0.45,
        **{k: v for k, v in common.items() if k != "position_pct"},
    )

    # ── D. 10전략 (기존 6 + 현물 4전략 추가) ──────────────
    common_d = {k: v for k, v in common.items()}
    results["exp_d"] = await run_experiment(
        "D. 10전략 (기존 6 + 현물 4전략)",
        exchange,
        strategy_names=ALL_STRATEGIES_10,
        **common_d,
    )

    # ── E. D + 방향별 가중치 조합 ─────────────────────────
    results["exp_e"] = await run_experiment(
        "E. 10전략 + 방향별 가중치",
        exchange,
        strategy_names=ALL_STRATEGIES_10,
        directional_weights=True,
        **common_d,
    )

    # ── F. 짧은 쿨다운 (cd36 → cd24) ─────────────────────
    results["exp_f"] = await run_experiment(
        "F. 쿨다운 단축 (cd36 → cd24)",
        exchange,
        strategy_names=ALL_STRATEGIES_6,
        trade_cooldown=24,
        **{k: v for k, v in common.items() if k != "trade_cooldown"},
    )

    await exchange.close()

    # ── 결과 비교 요약 ────────────────────────────────────
    print(f"\n{'='*70}")
    print("  결과 비교 요약")
    print(f"{'='*70}")
    print(f"{'실험':<45} {'PF':>6} {'수익률':>8} {'알파':>8} {'MDD':>7} {'매매':>5} {'L PnL':>8} {'S PnL':>8}")
    print("-" * 120)

    for key, r in results.items():
        name_map = {
            "baseline": "0. BASELINE (6전략)",
            "exp_a": "A. 방향별 가중치",
            "exp_b": "B. 낮은 신뢰도 (0.45)",
            "exp_c": "C. 포지션 확대 (45%)",
            "exp_d": "D. 10전략",
            "exp_e": "E. 10전략 + 방향별",
            "exp_f": "F. 쿨다운 cd24",
        }
        name = name_map.get(key, key)

        pf = r.profit_factor
        ret = r.total_pnl_pct
        alpha = r.total_pnl_pct - r.buy_hold_pnl_pct
        mdd = r.max_drawdown_pct
        trades = r.total_trades
        l_pnl = r.long_pnl
        s_pnl = r.short_pnl

        print(f"{name:<45} {pf:>6.2f} {ret:>+7.1f}% {alpha:>+7.1f}% {mdd:>6.1f}% {trades:>5} {l_pnl:>+8.1f} {s_pnl:>+8.1f}")


if __name__ == "__main__":
    asyncio.run(main())
