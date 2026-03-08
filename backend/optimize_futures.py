"""
선물 전략 Optuna 최적화 (다중 기간 로버스트니스 검증)
=====================================================
과적합 방지: 180d/365d/540d 세 기간 모두에서 좋은 파라미터 탐색.
단일 기간에만 좋은 파라미터는 페널티.

FuturesPortfolioBacktester 사용, leverage=3, 5코인 포트폴리오.

실행:
  cd backend && .venv/bin/python optimize_futures.py --trials 50
  cd backend && .venv/bin/python optimize_futures.py --trials 100 --simple --days 540
  cd backend && .venv/bin/python optimize_futures.py --db optuna_futures.db  # 이어하기 지원
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

# 선물 6전략 등록 import (backtest.py와 동일)
from strategies.ma_crossover import MACrossoverStrategy          # noqa: F401
from strategies.rsi_strategy import RSIStrategy                  # noqa: F401
from strategies.macd_crossover import MACDCrossoverStrategy      # noqa: F401
from strategies.bollinger_rsi import BollingerRSIStrategy        # noqa: F401
from strategies.stochastic_rsi import StochasticRSIStrategy      # noqa: F401
from strategies.obv_divergence import OBVDivergenceStrategy      # noqa: F401
from strategies.registry import StrategyRegistry
from strategies.combiner import SignalCombiner

import optuna
from optuna.samplers import TPESampler

from backtest import FuturesPortfolioBacktester

STRATEGY_NAMES = [
    "ma_crossover", "rsi", "macd_crossover",
    "bollinger_rsi", "stochastic_rsi", "obv_divergence",
]
SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "ADA/USDT"]
TIMEFRAME = "4h"
LEVERAGE = 3
INITIAL_BALANCE = 10_000  # USDT
MULTI_PERIODS = [180, 365, 540]  # 다중 기간 검증


async def create_exchange():
    """백테스트용 바이낸스 선물 어댑터 (public API만 사용)."""
    from exchange.binance_usdm_adapter import BinanceUSDMAdapter
    exchange = BinanceUSDMAdapter(api_key="", api_secret="", testnet=False)
    await exchange.initialize()
    return exchange


def suggest_params(trial: optuna.Trial) -> dict:
    """Optuna trial에서 선물 파라미터 샘플링."""
    # 전략 가중치 (6개)
    raw_w = {
        "ma_crossover":   trial.suggest_float("w_ma",         0.03, 0.20),
        "rsi":            trial.suggest_float("w_rsi",        0.10, 0.40),
        "macd_crossover": trial.suggest_float("w_macd",       0.03, 0.20),
        "bollinger_rsi":  trial.suggest_float("w_bollinger",  0.10, 0.45),
        "stochastic_rsi": trial.suggest_float("w_stoch",      0.05, 0.30),
        "obv_divergence": trial.suggest_float("w_obv",        0.05, 0.25),
    }
    total = sum(raw_w.values())
    weights = {k: v / total for k, v in raw_w.items()}

    return {
        "weights": weights,
        # 트레이딩 파라미터 (레버리지 적응 전 원본값)
        "min_confidence":       trial.suggest_float("min_confidence",       0.40, 0.65, step=0.05),
        "stop_loss_pct":        trial.suggest_float("stop_loss_pct",        5.0, 12.0, step=0.5),
        "take_profit_pct":      trial.suggest_float("take_profit_pct",      10.0, 25.0, step=1.0),
        "trailing_activation":  trial.suggest_float("trailing_activation",  3.0, 10.0, step=0.5),
        "trailing_stop":        trial.suggest_float("trailing_stop",        2.0, 7.0, step=0.5),
        "position_pct":         trial.suggest_float("position_pct",         0.20, 0.45, step=0.05),
        "trade_cooldown":       trial.suggest_int("trade_cooldown",         4, 18, step=2),
    }


async def run_backtest_with_params(
    exchange,
    params: dict,
    days: int,
) -> dict:
    """주어진 파라미터로 선물 포트폴리오 백테스트 실행."""
    bt = FuturesPortfolioBacktester(
        exchange=exchange,
        strategy_names=STRATEGY_NAMES,
        symbols=SYMBOLS,
        initial_balance=INITIAL_BALANCE,
        min_confidence=params["min_confidence"],
        stop_loss_pct=params["stop_loss_pct"],
        take_profit_pct=params["take_profit_pct"],
        trailing_activation=params["trailing_activation"],
        trailing_stop=params["trailing_stop"],
        adaptive_weights=True,
        trade_cooldown=params["trade_cooldown"],
        leverage=LEVERAGE,
        position_pct=params["position_pct"],
    )
    # 가중치 오버라이드
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
        "long_trades": result.long_trades,
        "short_trades": result.short_trades,
        "liquidations": result.liquidations,
        "total_funding": result.total_funding,
        "total_fees": result.total_fees,
        "buy_hold": result.buy_hold_pnl_pct,
    }


async def multi_period_objective(
    trial: optuna.Trial,
    exchange,
    periods: list[int],
) -> float:
    """다중 기간 로버스트니스 objective.

    180d/365d/540d 모두에서 좋은 파라미터만 높은 점수.
    선물 특성 반영: 청산 페널티, 펀딩비 포함.
    """
    params = suggest_params(trial)

    results = []
    for days in periods:
        try:
            r = await run_backtest_with_params(exchange, params, days=days)
            results.append(r)
            trial.set_user_attr(f"pnl_{days}d", round(r["pnl_pct"], 2))
            trial.set_user_attr(f"dd_{days}d", round(r["max_dd"], 2))
            trial.set_user_attr(f"pf_{days}d", round(r["profit_factor"], 2))
            trial.set_user_attr(f"trades_{days}d", r["trades"])
            trial.set_user_attr(f"liq_{days}d", r["liquidations"])
        except Exception as e:
            print(f"    {days}d 실패: {e}")
            results.append({
                "pnl_pct": -80, "max_dd": 80, "profit_factor": 0,
                "trades": 0, "liquidations": 5, "win_rate": 0,
            })

    # 수익률 계산 (일수 보정: 연간화)
    annualized = []
    for r, days in zip(results, periods):
        ann = r["pnl_pct"] * (365 / days)
        annualized.append(ann)

    avg_ann = sum(annualized) / len(annualized)
    min_pnl = min(r["pnl_pct"] for r in results)
    max_dd = max(r["max_dd"] for r in results)
    min_trades = min(r["trades"] for r in results)
    total_liq = sum(r.get("liquidations", 0) for r in results)

    # MDD 페널티: 선물은 40% 초과분 (레버리지 고려 완화)
    dd_penalty = max(0, max_dd - 40) * 0.5
    # 거래 수 페널티: 15건 미만
    trade_penalty = max(0, 15 - min_trades) * 0.5
    # 일관성 보너스: 모든 기간 수익이면 +5
    consistency = 5.0 if all(r["pnl_pct"] > 0 for r in results) else 0.0
    # 최악 기간 페널티: -20% 이하
    worst_penalty = max(0, -min_pnl - 20) * 0.3
    # 청산 페널티: 청산 1건당 -10점
    liq_penalty = total_liq * 10.0

    score = avg_ann + consistency - dd_penalty - trade_penalty - worst_penalty - liq_penalty

    trial.set_user_attr("avg_ann_pnl", round(avg_ann, 2))
    trial.set_user_attr("max_dd", round(max_dd, 2))
    trial.set_user_attr("consistency", all(r["pnl_pct"] > 0 for r in results))
    trial.set_user_attr("total_liquidations", total_liq)
    trial.set_user_attr("weights", params["weights"])

    return score


async def simple_objective(
    trial: optuna.Trial,
    exchange,
    total_days: int,
) -> float:
    """단순 전체 기간 백테스트 objective (빠른 탐색용)."""
    params = suggest_params(trial)

    try:
        result = await run_backtest_with_params(exchange, params, days=total_days)
    except Exception as e:
        print(f"  Trial {trial.number} 실패: {e}")
        return -100.0

    pnl = result["pnl_pct"]
    dd = result["max_dd"]
    trades = result["trades"]
    liq = result.get("liquidations", 0)

    dd_penalty = max(0, dd - 40) * 0.3
    trade_penalty = max(0, 15 - trades) * 0.5
    liq_penalty = liq * 10.0
    score = pnl - dd_penalty - trade_penalty - liq_penalty

    trial.set_user_attr("pnl_pct", round(pnl, 2))
    trial.set_user_attr("max_dd", round(dd, 2))
    trial.set_user_attr("profit_factor", round(result["profit_factor"], 2))
    trial.set_user_attr("trades", trades)
    trial.set_user_attr("win_rate", round(result["win_rate"], 2))
    trial.set_user_attr("long_trades", result.get("long_trades", 0))
    trial.set_user_attr("short_trades", result.get("short_trades", 0))
    trial.set_user_attr("liquidations", liq)
    trial.set_user_attr("weights", params["weights"])

    return score


def print_best(study: optuna.Study):
    """최적 결과 출력."""
    best = study.best_trial
    print(f"\n{'='*60}")
    print(f"  선물 최적화 완료 -- {len(study.trials)} trials")
    print(f"{'='*60}")
    print(f"  Best Score: {best.value:.2f}")

    print(f"\n  가중치 (DEFAULT_WEIGHTS / combiner.py):")
    weights = best.user_attrs.get("weights", {})
    for name, w in sorted(weights.items(), key=lambda x: -x[1]):
        print(f"    {name:20s}: {w:.4f}")

    print(f"\n  파라미터:")
    for key in ["min_confidence", "stop_loss_pct", "take_profit_pct",
                 "trailing_activation", "trailing_stop", "position_pct",
                 "trade_cooldown"]:
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
    trials_sorted = sorted(
        study.trials,
        key=lambda t: t.value if t.value is not None else -999,
        reverse=True,
    )
    print(f"\n  Top 5 trials:")
    for i, t in enumerate(trials_sorted[:5]):
        w = t.user_attrs.get("weights", {})
        w_str = " / ".join(
            f"{k[:3]}={v:.2f}" for k, v in sorted(w.items(), key=lambda x: -x[1])
        )
        liq = t.user_attrs.get("total_liquidations", t.user_attrs.get("liquidations", "?"))
        print(f"    #{t.number:3d}  score={t.value:+7.2f}  liq={liq}  {w_str}")

    # 적용 코드
    print(f"\n  적용 코드 (combiner.py DEFAULT_WEIGHTS):")
    print(f"    DEFAULT_WEIGHTS = {{")
    for name, w in sorted(weights.items()):
        print(f'        "{name}": {w:.2f},')
    print(f"    }}")
    print()
    print(f"  적용 코드 (config.py / engine):")
    for key in ["stop_loss_pct", "take_profit_pct", "trailing_activation",
                 "trailing_stop", "position_pct", "min_confidence"]:
        if key in best.params:
            print(f"    {key}: {best.params[key]}")
    print(f"{'='*60}")


def main():
    parser = argparse.ArgumentParser(description="선물 전략 Optuna 최적화")
    parser.add_argument("--trials", type=int, default=50, help="시행 횟수 (기본 50)")
    parser.add_argument("--days", type=int, default=540, help="단순 모드 백테스트 기간")
    parser.add_argument("--simple", action="store_true", help="단순 모드 (단일 기간)")
    parser.add_argument("--db", type=str, default=None, help="Optuna DB 경로 (이어하기)")
    args = parser.parse_args()

    if args.simple:
        mode_str = f"단순 ({args.days}일)"
    else:
        mode_str = f"다중 기간 ({'/'.join(str(d) for d in MULTI_PERIODS)}일)"

    print(f"\n선물 6전략 Optuna 최적화 (leverage={LEVERAGE}x)")
    print(f"  시행: {args.trials}회, 모드: {mode_str}")
    print(f"  전략: {', '.join(STRATEGY_NAMES)}")
    print(f"  코인: {', '.join(SYMBOLS)}")
    print(f"  초기 잔액: {INITIAL_BALANCE:,.0f} USDT")
    if not args.simple:
        est_min = args.trials * len(MULTI_PERIODS) * 3
        print(f"  예상 시간: ~{est_min}분 ({len(MULTI_PERIODS)}기간 x {args.trials}회 x ~3분)")
    print()

    storage = f"sqlite:///{args.db}" if args.db else None
    study = optuna.create_study(
        study_name="futures_6strategy_optimize",
        direction="maximize",
        sampler=TPESampler(seed=42),
        storage=storage,
        load_if_exists=True,
    )

    loop = asyncio.new_event_loop()

    # 거래소 어댑터 한 번만 생성 (재사용)
    exchange = loop.run_until_complete(create_exchange())

    if args.simple:
        def objective(trial):
            return loop.run_until_complete(
                simple_objective(trial, exchange, args.days)
            )
    else:
        def objective(trial):
            return loop.run_until_complete(
                multi_period_objective(trial, exchange, MULTI_PERIODS)
            )

    try:
        study.optimize(objective, n_trials=args.trials, show_progress_bar=True)
    except KeyboardInterrupt:
        print("\n  중단됨 (Ctrl+C) -- 현재까지 결과 출력")

    if study.best_trial:
        print_best(study)
    loop.close()


if __name__ == "__main__":
    main()
