"""방향별 가중치 최적화 스크립트 — 여러 BUY/SELL 가중치 조합을 자동 테스트."""
import asyncio
import sys
import json
from strategies.combiner import SignalCombiner

# 테스트할 BUY 가중치 변형 (롱 진입)
BUY_VARIANTS = {
    "B1_trend_heavy": {
        "ma_crossover": 0.18, "rsi": 0.10, "macd_crossover": 0.25,
        "bollinger_rsi": 0.15, "stochastic_rsi": 0.10, "obv_divergence": 0.22,
    },
    "B2_moderate": {
        "ma_crossover": 0.15, "rsi": 0.15, "macd_crossover": 0.15,
        "bollinger_rsi": 0.20, "stochastic_rsi": 0.10, "obv_divergence": 0.25,
    },
    "B3_macd_low": {
        "ma_crossover": 0.12, "rsi": 0.15, "macd_crossover": 0.12,
        "bollinger_rsi": 0.25, "stochastic_rsi": 0.10, "obv_divergence": 0.26,
    },
    "B4_obv_focus": {
        "ma_crossover": 0.15, "rsi": 0.10, "macd_crossover": 0.15,
        "bollinger_rsi": 0.15, "stochastic_rsi": 0.10, "obv_divergence": 0.35,
    },
}

# 테스트할 SELL 가중치 변형 (숏 진입)
SELL_VARIANTS = {
    "S1_meanrev": {
        "ma_crossover": 0.05, "rsi": 0.25, "macd_crossover": 0.10,
        "bollinger_rsi": 0.30, "stochastic_rsi": 0.20, "obv_divergence": 0.10,
    },
    "S2_balanced": {
        "ma_crossover": 0.08, "rsi": 0.22, "macd_crossover": 0.10,
        "bollinger_rsi": 0.28, "stochastic_rsi": 0.18, "obv_divergence": 0.14,
    },
}

# 조합: B1-S1, B2-S1, B3-S1, B4-S1, B2-S2, B3-S2
COMBOS = [
    ("B2_moderate", "S1_meanrev"),
    ("B3_macd_low", "S1_meanrev"),
    ("B4_obv_focus", "S1_meanrev"),
    ("B2_moderate", "S2_balanced"),
    ("B3_macd_low", "S2_balanced"),
]


async def run_combo(buy_name, sell_name, buy_w, sell_w):
    """하나의 가중치 조합으로 백테스트 실행."""
    # combiner의 클래스 변수를 임시 교체
    orig_buy = SignalCombiner.BUY_WEIGHTS.copy()
    orig_sell = SignalCombiner.SELL_WEIGHTS.copy()
    try:
        SignalCombiner.BUY_WEIGHTS = buy_w
        SignalCombiner.SELL_WEIGHTS = sell_w

        from backtest import (
            FuturesPortfolioBacktester, WEIGHTS_6,
            ALL_STRATEGIES_6, FUTURES_FEE, FUNDING_RATE,
        )
        from exchange.binance_usdm_adapter import BinanceUSDMAdapter

        exchange = BinanceUSDMAdapter(api_key="", api_secret="", testnet=False)
        await exchange.initialize()

        bt = FuturesPortfolioBacktester(
            exchange=exchange,
            strategy_names=ALL_STRATEGIES_6,
            initial_balance=10_000,
            min_confidence=0.50,
            stop_loss_pct=5.0,
            take_profit_pct=10.0,
            trailing_activation=7.0,
            trailing_stop=5.0,
            dynamic_sl=True,
            short_sideways=True,
            directional_weights=True,
            leverage=3,
            risk_enabled=True,
            trade_limit_enabled=True,
        )
        result = await bt.run("1h", 540)
        await exchange.close()

        long_wr = round(result.long_wins / result.long_trades * 100, 1) if result.long_trades > 0 else 0
        short_wr = round(result.short_wins / result.short_trades * 100, 1) if result.short_trades > 0 else 0
        return {
            "combo": f"{buy_name}+{sell_name}",
            "final": round(result.final_balance, 2),
            "pnl_pct": round(result.total_pnl_pct, 2),
            "pf": round(result.profit_factor, 2),
            "win_rate": round(result.win_rate, 1),
            "trades": result.total_trades,
            "long_trades": result.long_trades,
            "short_trades": result.short_trades,
            "long_win": long_wr,
            "short_win": short_wr,
            "mdd": round(result.max_drawdown_pct, 2),
            "fees": round(result.total_fees, 2),
        }
    finally:
        SignalCombiner.BUY_WEIGHTS = orig_buy
        SignalCombiner.SELL_WEIGHTS = orig_sell


async def main():
    results = []
    total = len(COMBOS)
    for i, (bn, sn) in enumerate(COMBOS, 1):
        bw = BUY_VARIANTS[bn]
        sw = SELL_VARIANTS[sn]
        print(f"[{i}/{total}] {bn}+{sn} ...", flush=True)
        r = await run_combo(bn, sn, bw, sw)
        results.append(r)
        print(f"  → PnL {r['pnl_pct']:+.2f}%, PF {r['pf']}, WR {r['win_rate']}%, trades {r['trades']}")

    print("\n=== 결과 요약 ===")
    results.sort(key=lambda x: x["pf"], reverse=True)
    for r in results:
        print(f"  {r['combo']:25s}  PnL {r['pnl_pct']:+6.2f}%  PF {r['pf']:.2f}  "
              f"WR {r['win_rate']:5.1f}%  거래 {r['trades']:3d}  "
              f"L승 {r['long_win']:5.1f}% S승 {r['short_win']:5.1f}%  "
              f"MDD {r['mdd']:.1f}%  수수료 {r['fees']:.0f}")


if __name__ == "__main__":
    asyncio.run(main())
