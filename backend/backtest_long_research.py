"""
롱 전략 하락장 방어 연구 백테스트
================================
실험 G: 하락장(crash+downtrend) 롱 완전 차단
실험 H: crash만 롱 차단
실험 I: 하락장 롱 사이징 축소 (현행 라이브와 유사)
실험 J: 하락장 롱 차단 + 숏 포지션 확대
실험 K: 하락장 롱 차단 + 횡보 롱 사이징 50%

Usage:
  cd backend && .venv/bin/python backtest_long_research.py
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


# ── 실험 설정 ─────────────────────────────────────────────
DAYS = 540
TIMEFRAME = "4h"
LEVERAGE = 3
BALANCE = 10_000

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
        strategy_names=ALL_STRATEGIES_6,
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

    # ── 0. Baseline: 현재 라이브 설정 (롱 제한 없음) ──────
    results["baseline"] = await run_experiment(
        "0. BASELINE (현행 — 롱 제한 없음)",
        exchange,
        **common,
    )

    # ── G. 하락장 롱 완전 차단 (crash + downtrend) ────────
    results["exp_g"] = await run_experiment(
        "G. 하락장 롱 완전 차단 (crash+downtrend)",
        exchange,
        long_block_states={"crash", "downtrend"},
        **common,
    )

    # ── H. crash만 롱 차단 ──────────────────────────────
    results["exp_h"] = await run_experiment(
        "H. crash만 롱 차단",
        exchange,
        long_block_states={"crash"},
        **common,
    )

    # ── I. 하락장 롱 사이징 축소 (라이브와 동일) ──────────
    results["exp_i"] = await run_experiment(
        "I. 하락장 롱 사이징 축소 (crash=25%, downtrend=50%)",
        exchange,
        long_sizing_states={"crash": 0.25, "downtrend": 0.50},
        **common,
    )

    # ── J. 하락장 롱 차단 + 포지션 확대 (45%) ────────────
    results["exp_j"] = await run_experiment(
        "J. 하락장 롱 차단 + 포지션 확대 (45%)",
        exchange,
        long_block_states={"crash", "downtrend"},
        position_pct=0.45,
        **{k: v for k, v in common.items() if k != "position_pct"},
    )

    # ── K. 하락장 롱 차단 + 횡보 사이징 50% ──────────────
    results["exp_k"] = await run_experiment(
        "K. 하락장 롱 차단 + 횡보 롱 50%",
        exchange,
        long_block_states={"crash", "downtrend"},
        long_sizing_states={"sideways": 0.50},
        **common,
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
            "baseline": "0. BASELINE (롱 제한 없음)",
            "exp_g": "G. 하락장 롱 차단 (crash+downtrend)",
            "exp_h": "H. crash만 롱 차단",
            "exp_i": "I. 사이징 축소 (crash=25%, down=50%)",
            "exp_j": "J. 롱 차단 + 포지션 45%",
            "exp_k": "K. 롱 차단 + 횡보 50%",
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
