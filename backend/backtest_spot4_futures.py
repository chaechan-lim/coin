"""
현물 4전략만으로 선물 백테스트
==============================
기존 6전략 vs 현물 4전략 (bnf_deviation, cis_momentum, larry_williams, donchian_channel)
선물에서 현물 추세전략만 사용하면 어떤지 비교.

Usage:
  cd backend && .venv/bin/python backtest_spot4_futures.py
"""
import asyncio
import logging

logging.basicConfig(level=logging.WARNING)
import structlog
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
)

from backtest import (
    FuturesPortfolioBacktester,
    ALL_STRATEGIES_6,
    FUTURES_FEE, FUNDING_RATE,
    print_futures_portfolio_result,
)
from exchange.binance_usdm_adapter import BinanceUSDMAdapter


DAYS = 540
TIMEFRAME = "4h"
LEVERAGE = 3
BALANCE = 10_000

COINS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT"]

# 현물 4전략
SPOT_4 = ["bnf_deviation", "cis_momentum", "larry_williams", "donchian_channel"]
SPOT_4_WEIGHTS = {
    "bnf_deviation": 0.23,
    "cis_momentum": 0.22,
    "larry_williams": 0.31,
    "donchian_channel": 0.24,
}


async def run_experiment(name, exchange, **kwargs):
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

    # ── 0. Baseline: 기존 6전략 ─────────────────────────
    results["baseline"] = await run_experiment(
        "0. BASELINE (기존 6전략)",
        exchange,
        strategy_names=ALL_STRATEGIES_6,
        **common,
    )

    # ── A. 현물 4전략만 (Optuna 가중치) ─────────────────
    results["spot4"] = await run_experiment(
        "A. 현물 4전략만 (Optuna SPOT_WEIGHTS)",
        exchange,
        strategy_names=SPOT_4,
        **common,
    )

    # ── B. 현물 4전략 균등 가중치 ───────────────────────
    results["spot4_equal"] = await run_experiment(
        "B. 현물 4전략만 (균등 가중치)",
        exchange,
        strategy_names=SPOT_4,
        adaptive_weights=False,
        **{k: v for k, v in common.items() if k != "adaptive_weights"},
    )

    # ── C. 현물 4전략 + 낮은 신뢰도 (0.50) ─────────────
    results["spot4_low"] = await run_experiment(
        "C. 현물 4전략 + 낮은 신뢰도 (0.50)",
        exchange,
        strategy_names=SPOT_4,
        min_confidence=0.50,
        **{k: v for k, v in common.items() if k != "min_confidence"},
    )

    # ── D. 현물 4전략 + 포지션 확대 (45%) ──────────────
    results["spot4_big"] = await run_experiment(
        "D. 현물 4전략 + 포지션 확대 (45%)",
        exchange,
        strategy_names=SPOT_4,
        position_pct=0.45,
        **{k: v for k, v in common.items() if k != "position_pct"},
    )

    await exchange.close()

    # ── 결과 비교 요약 ────────────────────────────────────
    print(f"\n{'='*70}")
    print("  결과 비교 요약")
    print(f"{'='*70}")
    print(f"{'실험':<50} {'PF':>6} {'수익률':>8} {'알파':>8} {'MDD':>7} {'매매':>5} {'L PnL':>8} {'S PnL':>8}")
    print("-" * 125)

    for key, r in results.items():
        name_map = {
            "baseline": "0. BASELINE (6전략)",
            "spot4": "A. 현물 4전략 (Optuna)",
            "spot4_equal": "B. 현물 4전략 (균등)",
            "spot4_low": "C. 현물 4전략 + conf 0.50",
            "spot4_big": "D. 현물 4전략 + pos 45%",
        }
        name = name_map.get(key, key)

        pf = r.profit_factor
        ret = r.total_pnl_pct
        alpha = r.total_pnl_pct - r.buy_hold_pnl_pct
        mdd = r.max_drawdown_pct
        trades = r.total_trades
        l_pnl = r.long_pnl
        s_pnl = r.short_pnl

        print(f"{name:<50} {pf:>6.2f} {ret:>+7.1f}% {alpha:>+7.1f}% {mdd:>6.1f}% {trades:>5} {l_pnl:>+8.1f} {s_pnl:>+8.1f}")


if __name__ == "__main__":
    asyncio.run(main())
