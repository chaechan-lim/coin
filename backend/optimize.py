"""
현물 전략 Optuna 최적화 (다중 기간 로버스트니스 검증)
=====================================================
과적합 방지: 180d/365d/540d 세 기간 모두에서 좋은 파라미터 탐색.
단일 기간에만 좋은 파라미터는 페널티.

실행:
  cd backend && .venv/bin/python optimize.py --trials 50
  cd backend && .venv/bin/python optimize.py --trials 100 --simple --days 540
  cd backend && .venv/bin/python optimize.py --db optuna.db  # 이어하기 지원
"""

import asyncio
import argparse
import logging
import sys
from datetime import datetime

logging.basicConfig(level=logging.WARNING)
import structlog
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
)

# 전략 등록 import (backtest.py와 동일)
from strategies.bnf_deviation import BNFDeviationStrategy      # noqa: F401
from strategies.cis_momentum import CISMomentumStrategy        # noqa: F401
from strategies.larry_williams import LarryWilliamsStrategy     # noqa: F401
from strategies.donchian_channel import DonchianChannelStrategy # noqa: F401
from strategies.registry import StrategyRegistry
from strategies.combiner import SignalCombiner
from exchange.bithumb_adapter import BithumbAdapter

import optuna
from optuna.samplers import TPESampler

from backtest import PortfolioBacktester

STRATEGY_NAMES = ["bnf_deviation", "cis_momentum", "larry_williams", "donchian_channel"]
SYMBOLS_KRW = ["BTC/KRW", "ETH/KRW", "XRP/KRW", "SOL/KRW", "ADA/KRW"]
SYMBOLS_USDT = ["BTC/USDT", "ETH/USDT", "XRP/USDT", "SOL/USDT", "ADA/USDT"]
TIMEFRAME = "4h"
MULTI_PERIODS = [180, 365, 540]  # 다중 기간 검증

USE_BINANCE = False  # CLI에서 설정
SYMBOLS = SYMBOLS_KRW  # CLI에서 변경


async def create_exchange():
    """백테스트용 거래소 어댑터 (캐시 데이터만 사용)."""
    if USE_BINANCE:
        from exchange.binance_spot_adapter import BinanceSpotAdapter
        ex = BinanceSpotAdapter(api_key="", api_secret="", testnet=False)
        await ex.initialize()
        return ex
    return BithumbAdapter(api_key="", api_secret="")


def suggest_params(trial: optuna.Trial) -> dict:
    """Optuna trial에서 파라미터 샘플링."""
    raw_w = {
        "bnf_deviation": trial.suggest_float("w_bnf", 0.03, 0.40),
        "cis_momentum": trial.suggest_float("w_cis", 0.10, 0.50),
        "larry_williams": trial.suggest_float("w_larry", 0.10, 0.50),
        "donchian_channel": trial.suggest_float("w_donchian", 0.05, 0.45),
    }
    total = sum(raw_w.values())
    weights = {k: v / total for k, v in raw_w.items()}

    return {
        "weights": weights,
        "min_confidence": trial.suggest_float("min_confidence", 0.35, 0.55, step=0.05),
        "stop_loss_pct": trial.suggest_float("stop_loss_pct", 3.0, 8.0, step=0.5),
        "take_profit_pct": trial.suggest_float("take_profit_pct", 6.0, 15.0, step=1.0),
        "trailing_activation": trial.suggest_float("trailing_activation", 2.0, 5.0, step=0.5),
        "trailing_stop": trial.suggest_float("trailing_stop", 1.5, 4.0, step=0.5),
        "trade_cooldown": trial.suggest_int("trade_cooldown", 6, 18, step=3),
        "max_trade_size_pct": trial.suggest_float("max_trade_size_pct", 0.15, 0.30, step=0.05),
    }


async def run_backtest_with_params(
    exchange,
    params: dict,
    days: int,
) -> dict:
    """주어진 파라미터로 포트폴리오 백테스트 실행."""
    bt = PortfolioBacktester(
        exchange=exchange,
        strategy_names=STRATEGY_NAMES,
        symbols=SYMBOLS,
        initial_balance=500_000,
        min_confidence=params["min_confidence"],
        stop_loss_pct=params["stop_loss_pct"],
        take_profit_pct=params["take_profit_pct"],
        trailing_activation=params["trailing_activation"],
        trailing_stop=params["trailing_stop"],
        adaptive_weights=False,
        trade_cooldown=params["trade_cooldown"],
        asymmetric=True,
        max_trade_size_pct=params["max_trade_size_pct"],
        dynamic_sl=True,
        strategy_sell_mode="paired",
    )
    bt._combiner = SignalCombiner(
        strategy_weights=params["weights"],
        min_confidence=params["min_confidence"],
    )

    result = await bt.run(timeframe=TIMEFRAME, days=days)
    return {
        "pnl_pct": result.total_pnl_pct,
        "max_dd": result.max_drawdown_pct,
        "profit_factor": result.profit_factor,
        "trades": result.total_trades,
        "win_rate": result.win_rate,
        "buy_hold": result.buy_hold_pnl_pct,
    }


async def multi_period_objective(
    trial: optuna.Trial,
    periods: list[int],
) -> float:
    """다중 기간 로버스트니스 objective.

    180d/365d/540d 모두에서 좋은 파라미터만 높은 점수.
    """
    params = suggest_params(trial)
    exchange = await create_exchange()

    results = []
    for days in periods:
        try:
            r = await run_backtest_with_params(exchange, params, days=days)
            results.append(r)
            trial.set_user_attr(f"pnl_{days}d", round(r["pnl_pct"], 2))
            trial.set_user_attr(f"dd_{days}d", round(r["max_dd"], 2))
            trial.set_user_attr(f"pf_{days}d", round(r["profit_factor"], 2))
            trial.set_user_attr(f"trades_{days}d", r["trades"])
        except Exception as e:
            print(f"    {days}d 실패: {e}")
            results.append({"pnl_pct": -50, "max_dd": 50, "profit_factor": 0, "trades": 0})

    # 수익률 계산 (일수 보정: 연간화)
    annualized = []
    for r, days in zip(results, periods):
        ann = r["pnl_pct"] * (365 / days)
        annualized.append(ann)

    avg_ann = sum(annualized) / len(annualized)
    min_pnl = min(r["pnl_pct"] for r in results)
    max_dd = max(r["max_dd"] for r in results)
    min_trades = min(r["trades"] for r in results)

    # MDD 페널티: 30% 초과분
    dd_penalty = max(0, max_dd - 30) * 0.5
    # 거래 수 페널티: 20건 미만
    trade_penalty = max(0, 20 - min_trades) * 0.5
    # 일관성 보너스: 모든 기간 수익이면 +5
    consistency = 5.0 if all(r["pnl_pct"] > 0 for r in results) else 0.0
    # 최악 기간 페널티: -15% 이하
    worst_penalty = max(0, -min_pnl - 15) * 0.3

    score = avg_ann + consistency - dd_penalty - trade_penalty - worst_penalty

    trial.set_user_attr("avg_ann_pnl", round(avg_ann, 2))
    trial.set_user_attr("max_dd", round(max_dd, 2))
    trial.set_user_attr("consistency", all(r["pnl_pct"] > 0 for r in results))
    trial.set_user_attr("weights", params["weights"])

    return score


async def simple_objective(
    trial: optuna.Trial,
    total_days: int,
) -> float:
    """단순 전체 기간 백테스트 objective (빠른 탐색용)."""
    params = suggest_params(trial)
    exchange = await create_exchange()

    try:
        result = await run_backtest_with_params(exchange, params, days=total_days)
    except Exception as e:
        print(f"  Trial {trial.number} 실패: {e}")
        return -100.0

    pnl = result["pnl_pct"]
    dd = result["max_dd"]
    trades = result["trades"]

    dd_penalty = max(0, dd - 30) * 0.3
    trade_penalty = max(0, 20 - trades) * 0.5
    score = pnl - dd_penalty - trade_penalty

    trial.set_user_attr("pnl_pct", round(pnl, 2))
    trial.set_user_attr("max_dd", round(dd, 2))
    trial.set_user_attr("profit_factor", round(result["profit_factor"], 2))
    trial.set_user_attr("trades", trades)
    trial.set_user_attr("weights", params["weights"])

    return score


def print_best(study: optuna.Study):
    """최적 결과 출력."""
    best = study.best_trial
    print(f"\n{'='*60}")
    print(f"  최적화 완료 — {len(study.trials)} trials")
    print(f"{'='*60}")
    print(f"  Best Score: {best.value:.2f}")

    print(f"\n  가중치:")
    weights = best.user_attrs.get("weights", {})
    for name, w in sorted(weights.items(), key=lambda x: -x[1]):
        print(f"    {name:20s}: {w:.4f}")

    print(f"\n  파라미터:")
    for key in ["min_confidence", "stop_loss_pct", "take_profit_pct",
                 "trailing_activation", "trailing_stop", "trade_cooldown",
                 "max_trade_size_pct"]:
        if key in best.params:
            print(f"    {key:25s}: {best.params[key]}")

    print(f"\n  성과:")
    for key in sorted(best.user_attrs.keys()):
        if key == "weights":
            continue
        val = best.user_attrs[key]
        if isinstance(val, float):
            print(f"    {key:25s}: {val:.2f}")
        else:
            print(f"    {key:25s}: {val}")

    # Top 5 출력
    trials_sorted = sorted(study.trials, key=lambda t: t.value if t.value is not None else -999, reverse=True)
    print(f"\n  Top 5 trials:")
    for i, t in enumerate(trials_sorted[:5]):
        w = t.user_attrs.get("weights", {})
        w_str = " / ".join(f"{k[:3]}={v:.2f}" for k, v in sorted(w.items(), key=lambda x: -x[1]))
        print(f"    #{t.number:3d}  score={t.value:+7.2f}  {w_str}")

    # 적용 코드
    print(f"\n  적용 코드 (combiner.py SPOT_WEIGHTS):")
    print(f"    SPOT_WEIGHTS = {{")
    for name, w in sorted(weights.items()):
        print(f"        \"{name}\": {w:.2f},")
    print(f"    }}")
    print(f"{'='*60}")


def main():
    parser = argparse.ArgumentParser(description="현물 전략 Optuna 최적화")
    parser.add_argument("--trials", type=int, default=50, help="시행 횟수 (기본 50)")
    parser.add_argument("--days", type=int, default=540, help="단순 모드 백테스트 기간")
    parser.add_argument("--simple", action="store_true", help="단순 모드 (단일 기간)")
    parser.add_argument("--db", type=str, default=None, help="Optuna DB 경로 (이어하기)")
    parser.add_argument("--use-binance", action="store_true", help="바이낸스 USDT 데이터 사용")
    args = parser.parse_args()

    global USE_BINANCE, SYMBOLS
    if args.use_binance:
        USE_BINANCE = True
        SYMBOLS = SYMBOLS_USDT

    if args.simple:
        mode_str = f"단순 ({args.days}일)"
    else:
        mode_str = f"다중 기간 ({'/'.join(str(d) for d in MULTI_PERIODS)}일)"

    print(f"\n현물 4전략 Optuna 최적화")
    print(f"  시행: {args.trials}회, 모드: {mode_str}")
    print(f"  전략: {', '.join(STRATEGY_NAMES)}")
    print(f"  코인: {', '.join(SYMBOLS)}")
    if not args.simple:
        est_min = args.trials * len(MULTI_PERIODS) * 2
        print(f"  예상 시간: ~{est_min}분 ({len(MULTI_PERIODS)}기간 x {args.trials}회 x ~2분)")
    print()

    storage = f"sqlite:///{args.db}" if args.db else None
    exch_tag = "binance" if USE_BINANCE else "bithumb"
    study = optuna.create_study(
        study_name=f"spot_4strategy_{exch_tag}",
        direction="maximize",
        sampler=TPESampler(seed=42),
        storage=storage,
        load_if_exists=True,
    )

    loop = asyncio.new_event_loop()

    if args.simple:
        def objective(trial):
            return loop.run_until_complete(simple_objective(trial, args.days))
    else:
        def objective(trial):
            return loop.run_until_complete(
                multi_period_objective(trial, MULTI_PERIODS)
            )

    try:
        study.optimize(objective, n_trials=args.trials, show_progress_bar=True)
    except KeyboardInterrupt:
        print("\n  중단됨 (Ctrl+C) — 현재까지 결과 출력")

    if study.best_trial:
        print_best(study)
    loop.close()


if __name__ == "__main__":
    main()
