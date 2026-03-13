"""
전략 Optuna 최적화 (현물 + 선물)
=================================
과적합 방지: 180d/365d/540d 세 기간 모두에서 좋은 파라미터 탐색.
단일 기간에만 좋은 파라미터는 페널티.

실행 (현물):
  cd backend && .venv/bin/python optimize.py --use-binance --trials 50
  cd backend && .venv/bin/python optimize.py --use-binance --simple --days 540

실행 (선물):
  cd backend && .venv/bin/python optimize.py --futures --simple --days 540 --trials 50
  cd backend && .venv/bin/python optimize.py --futures --trials 100
  cd backend && .venv/bin/python optimize.py --futures --db optuna_futures.db  # 이어하기
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

# 전략 등록 import — 현물 + 선물 모두 (backtest.py와 동일)
from strategies.bnf_deviation import BNFDeviationStrategy      # noqa: F401
from strategies.cis_momentum import CISMomentumStrategy        # noqa: F401
from strategies.larry_williams import LarryWilliamsStrategy     # noqa: F401
from strategies.donchian_channel import DonchianChannelStrategy # noqa: F401
from strategies.ma_crossover import MACrossoverStrategy         # noqa: F401
from strategies.rsi_strategy import RSIStrategy                 # noqa: F401
from strategies.macd_crossover import MACDCrossoverStrategy     # noqa: F401
from strategies.bollinger_rsi import BollingerRSIStrategy       # noqa: F401
from strategies.stochastic_rsi import StochasticRSIStrategy     # noqa: F401
from strategies.obv_divergence import OBVDivergenceStrategy     # noqa: F401
from strategies.bb_squeeze import BBSqueezeStrategy             # noqa: F401
from strategies.registry import StrategyRegistry
from strategies.combiner import SignalCombiner
from exchange.bithumb_adapter import BithumbAdapter

import optuna
from optuna.samplers import TPESampler

from backtest import PortfolioBacktester, FuturesPortfolioBacktester

# ── 현물 설정 ──
SPOT_STRATEGY_NAMES = ["bnf_deviation", "cis_momentum", "larry_williams", "donchian_channel"]
SYMBOLS_KRW = ["BTC/KRW", "ETH/KRW", "XRP/KRW", "SOL/KRW", "ADA/KRW"]
SYMBOLS_USDT_SPOT = ["BTC/USDT", "ETH/USDT", "XRP/USDT", "SOL/USDT", "ADA/USDT"]

# ── 선물 설정 ──
FUTURES_STRATEGY_NAMES = [
    "ma_crossover", "rsi", "macd_crossover", "bollinger_rsi",
    "stochastic_rsi", "obv_divergence", "bb_squeeze",
]
FUTURES_SYMBOLS = ["BTC/USDT", "ETH/USDT", "XRP/USDT", "SOL/USDT", "BNB/USDT"]

TIMEFRAME = "4h"
MULTI_PERIODS = [180, 365, 540]

USE_BINANCE = False
SYMBOLS = SYMBOLS_KRW


# ══════════════════════════════════════════════════════════════
#  거래소 어댑터
# ══════════════════════════════════════════════════════════════

async def create_spot_exchange():
    """현물 백테스트용 거래소 어댑터."""
    if USE_BINANCE:
        from exchange.binance_spot_adapter import BinanceSpotAdapter
        ex = BinanceSpotAdapter(api_key="", api_secret="", testnet=False)
        await ex.initialize()
        return ex
    return BithumbAdapter(api_key="", api_secret="")


async def create_futures_exchange():
    """선물 백테스트용 거래소 어댑터."""
    from exchange.binance_usdm_adapter import BinanceUSDMAdapter
    ex = BinanceUSDMAdapter(api_key="", api_secret="", testnet=False)
    await ex.initialize()
    return ex


# ══════════════════════════════════════════════════════════════
#  현물 최적화
# ══════════════════════════════════════════════════════════════

def suggest_spot_params(trial: optuna.Trial) -> dict:
    """현물 파라미터 샘플링."""
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


async def run_spot_backtest(exchange, params: dict, days: int) -> dict:
    """현물 포트폴리오 백테스트."""
    bt = PortfolioBacktester(
        exchange=exchange,
        strategy_names=SPOT_STRATEGY_NAMES,
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


# ══════════════════════════════════════════════════════════════
#  선물 최적화
# ══════════════════════════════════════════════════════════════

def suggest_futures_params(trial: optuna.Trial) -> dict:
    """선물 7전략 파라미터 샘플링."""
    raw_w = {
        "bollinger_rsi":   trial.suggest_float("w_boll", 0.10, 0.40),
        "rsi":             trial.suggest_float("w_rsi", 0.08, 0.35),
        "bb_squeeze":      trial.suggest_float("w_bbsq", 0.05, 0.25),
        "stochastic_rsi":  trial.suggest_float("w_stoch", 0.05, 0.25),
        "obv_divergence":  trial.suggest_float("w_obv", 0.03, 0.20),
        "ma_crossover":    trial.suggest_float("w_ma", 0.03, 0.15),
        "macd_crossover":  trial.suggest_float("w_macd", 0.03, 0.15),
    }
    total = sum(raw_w.values())
    weights = {k: v / total for k, v in raw_w.items()}

    return {
        "weights": weights,
        "min_confidence": trial.suggest_float("min_confidence", 0.40, 0.65, step=0.05),
        "stop_loss_pct": trial.suggest_float("stop_loss_pct", 5.0, 12.0, step=0.5),
        "take_profit_pct": trial.suggest_float("take_profit_pct", 8.0, 25.0, step=1.0),
        "trailing_activation": trial.suggest_float("trailing_activation", 3.0, 8.0, step=0.5),
        "trailing_stop": trial.suggest_float("trailing_stop", 2.0, 5.0, step=0.5),
        "trade_cooldown": trial.suggest_int("trade_cooldown", 3, 12, step=3),
        "position_pct": trial.suggest_float("position_pct", 0.25, 0.50, step=0.05),
        "min_sell_weight": trial.suggest_float("min_sell_weight", 0.10, 0.30, step=0.05),
    }


async def run_futures_backtest(exchange, params: dict, days: int) -> dict:
    """선물 포트폴리오 백테스트."""
    bt = FuturesPortfolioBacktester(
        exchange=exchange,
        strategy_names=FUTURES_STRATEGY_NAMES,
        symbols=FUTURES_SYMBOLS,
        initial_balance=10_000,
        min_confidence=params["min_confidence"],
        stop_loss_pct=params["stop_loss_pct"],
        take_profit_pct=params["take_profit_pct"],
        trailing_activation=params["trailing_activation"],
        trailing_stop=params["trailing_stop"],
        adaptive_weights=True,
        dynamic_sl=True,
        trade_cooldown=params["trade_cooldown"],
        leverage=3,
        position_pct=params["position_pct"],
        short_all=True,
    )
    # 가중치 오버라이드
    bt._combiner = SignalCombiner(
        strategy_weights=params["weights"],
        min_confidence=params["min_confidence"],
    )
    bt._combiner.MIN_SELL_ACTIVE_WEIGHT = params["min_sell_weight"]

    result = await bt.run(timeframe=TIMEFRAME, days=days)
    short_wr = result.short_wins / result.short_trades if result.short_trades > 0 else 0.0
    long_wr = result.long_wins / result.long_trades if result.long_trades > 0 else 0.0
    return {
        "pnl_pct": result.total_pnl_pct,
        "max_dd": result.max_drawdown_pct,
        "profit_factor": result.profit_factor,
        "trades": result.total_trades,
        "win_rate": result.win_rate,
        "buy_hold": result.buy_hold_pnl_pct,
        "long_trades": result.long_trades,
        "short_trades": result.short_trades,
        "long_pnl": result.long_pnl,
        "short_pnl": result.short_pnl,
        "long_wr": long_wr,
        "short_wr": short_wr,
        "liquidations": result.liquidations,
        "total_funding": result.total_funding,
    }


# ══════════════════════════════════════════════════════════════
#  Objective 함수
# ══════════════════════════════════════════════════════════════

def _score_spot(results: list[dict], periods: list[int]) -> float:
    """현물 다중 기간 스코어."""
    annualized = [r["pnl_pct"] * (365 / d) for r, d in zip(results, periods)]
    avg_ann = sum(annualized) / len(annualized)
    min_pnl = min(r["pnl_pct"] for r in results)
    max_dd = max(r["max_dd"] for r in results)
    min_trades = min(r["trades"] for r in results)

    dd_penalty = max(0, max_dd - 30) * 0.5
    trade_penalty = max(0, 20 - min_trades) * 0.5
    consistency = 5.0 if all(r["pnl_pct"] > 0 for r in results) else 0.0
    worst_penalty = max(0, -min_pnl - 15) * 0.3

    return avg_ann + consistency - dd_penalty - trade_penalty - worst_penalty


def _score_futures(results: list[dict], periods: list[int]) -> float:
    """선물 다중 기간 스코어 (롱/숏 밸런스 + 청산 페널티)."""
    annualized = [r["pnl_pct"] * (365 / d) for r, d in zip(results, periods)]
    avg_ann = sum(annualized) / len(annualized)
    min_pnl = min(r["pnl_pct"] for r in results)
    max_dd = max(r["max_dd"] for r in results)
    min_trades = min(r["trades"] for r in results)
    total_liquidations = sum(r["liquidations"] for r in results)

    # 선물: MDD 25% 초과 페널티 (3x 레버리지)
    dd_penalty = max(0, max_dd - 25) * 0.8
    # 최소 거래 15건 (선물은 쿨다운이 김)
    trade_penalty = max(0, 15 - min_trades) * 0.5
    # 일관성 보너스
    consistency = 8.0 if all(r["pnl_pct"] > 0 for r in results) else 0.0
    # 최악 기간 페널티
    worst_penalty = max(0, -min_pnl - 10) * 0.5
    # 청산 페널티 (치명적)
    liq_penalty = total_liquidations * 5.0
    # 숏 승률 < 25% 페널티 (숏이 아예 작동 안 하면)
    avg_short_wr = sum(r["short_wr"] for r in results) / len(results)
    short_penalty = max(0, 0.25 - avg_short_wr) * 15.0
    # PF 바닥 페널티
    min_pf = min(r["profit_factor"] for r in results)
    pf_penalty = max(0, 1.0 - min_pf) * 20.0

    return (avg_ann + consistency
            - dd_penalty - trade_penalty - worst_penalty
            - liq_penalty - short_penalty - pf_penalty)


def _score_futures_simple(r: dict) -> float:
    """선물 단일 기간 스코어."""
    pnl = r["pnl_pct"]
    dd = r["max_dd"]
    trades = r["trades"]

    dd_penalty = max(0, dd - 25) * 0.8
    trade_penalty = max(0, 15 - trades) * 0.5
    liq_penalty = r["liquidations"] * 5.0
    short_penalty = max(0, 0.25 - r["short_wr"]) * 15.0
    pf_penalty = max(0, 1.0 - r["profit_factor"]) * 20.0

    return pnl - dd_penalty - trade_penalty - liq_penalty - short_penalty - pf_penalty


async def multi_period_objective(
    trial: optuna.Trial,
    periods: list[int],
    is_futures: bool = False,
) -> float:
    """다중 기간 로버스트니스 objective."""
    if is_futures:
        params = suggest_futures_params(trial)
        exchange = await create_futures_exchange()
        run_fn = run_futures_backtest
    else:
        params = suggest_spot_params(trial)
        exchange = await create_spot_exchange()
        run_fn = run_spot_backtest

    results = []
    for days in periods:
        try:
            r = await run_fn(exchange, params, days=days)
            results.append(r)
            trial.set_user_attr(f"pnl_{days}d", round(r["pnl_pct"], 2))
            trial.set_user_attr(f"dd_{days}d", round(r["max_dd"], 2))
            trial.set_user_attr(f"pf_{days}d", round(r["profit_factor"], 2))
            trial.set_user_attr(f"trades_{days}d", r["trades"])
            if is_futures:
                trial.set_user_attr(f"L_pnl_{days}d", round(r["long_pnl"], 1))
                trial.set_user_attr(f"S_pnl_{days}d", round(r["short_pnl"], 1))
        except Exception as e:
            print(f"    {days}d 실패: {e}")
            fail = {"pnl_pct": -50, "max_dd": 50, "profit_factor": 0,
                     "trades": 0, "win_rate": 0, "buy_hold": 0}
            if is_futures:
                fail.update({"long_trades": 0, "short_trades": 0,
                             "long_pnl": 0, "short_pnl": 0,
                             "long_wr": 0, "short_wr": 0,
                             "liquidations": 0, "total_funding": 0})
            results.append(fail)

    score = _score_futures(results, periods) if is_futures else _score_spot(results, periods)

    trial.set_user_attr("score", round(score, 2))
    trial.set_user_attr("weights", params["weights"])

    return score


async def simple_objective(
    trial: optuna.Trial,
    total_days: int,
    is_futures: bool = False,
) -> float:
    """단순 전체 기간 백테스트 objective."""
    if is_futures:
        params = suggest_futures_params(trial)
        exchange = await create_futures_exchange()
        run_fn = run_futures_backtest
    else:
        params = suggest_spot_params(trial)
        exchange = await create_spot_exchange()
        run_fn = run_spot_backtest

    try:
        result = await run_fn(exchange, params, days=total_days)
    except Exception as e:
        print(f"  Trial {trial.number} 실패: {e}")
        return -100.0

    pnl = result["pnl_pct"]
    dd = result["max_dd"]
    trades = result["trades"]

    if is_futures:
        score = _score_futures_simple(result)
        trial.set_user_attr("long_pnl", round(result["long_pnl"], 1))
        trial.set_user_attr("short_pnl", round(result["short_pnl"], 1))
        trial.set_user_attr("long_trades", result["long_trades"])
        trial.set_user_attr("short_trades", result["short_trades"])
        trial.set_user_attr("liquidations", result["liquidations"])
    else:
        dd_penalty = max(0, dd - 30) * 0.3
        trade_penalty = max(0, 20 - trades) * 0.5
        score = pnl - dd_penalty - trade_penalty

    trial.set_user_attr("pnl_pct", round(pnl, 2))
    trial.set_user_attr("max_dd", round(dd, 2))
    trial.set_user_attr("profit_factor", round(result["profit_factor"], 2))
    trial.set_user_attr("trades", trades)
    trial.set_user_attr("win_rate", round(result["win_rate"], 1))
    trial.set_user_attr("weights", params["weights"])

    return score


# ══════════════════════════════════════════════════════════════
#  결과 출력
# ══════════════════════════════════════════════════════════════

def print_best(study: optuna.Study, is_futures: bool = False):
    """최적 결과 출력."""
    best = study.best_trial
    print(f"\n{'='*60}")
    mode_str = "선물 7전략" if is_futures else "현물 4전략"
    print(f"  {mode_str} 최적화 완료 — {len(study.trials)} trials")
    print(f"{'='*60}")
    print(f"  Best Score: {best.value:.2f}")

    print(f"\n  가중치:")
    weights = best.user_attrs.get("weights", {})
    for name, w in sorted(weights.items(), key=lambda x: -x[1]):
        print(f"    {name:20s}: {w:.4f}")

    print(f"\n  파라미터:")
    param_keys = ["min_confidence", "stop_loss_pct", "take_profit_pct",
                   "trailing_activation", "trailing_stop", "trade_cooldown",
                   "max_trade_size_pct", "position_pct", "min_sell_weight"]
    for key in param_keys:
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

    # Top 5
    trials_sorted = sorted(
        study.trials,
        key=lambda t: t.value if t.value is not None else -999,
        reverse=True,
    )
    print(f"\n  Top 5 trials:")
    for i, t in enumerate(trials_sorted[:5]):
        pnl = t.user_attrs.get("pnl_pct", t.user_attrs.get("pnl_540d", "?"))
        pf = t.user_attrs.get("profit_factor", t.user_attrs.get("pf_540d", "?"))
        dd = t.user_attrs.get("max_dd", t.user_attrs.get("dd_540d", "?"))
        print(f"    #{t.number:3d}  score={t.value:+7.2f}  PnL={pnl:+.1f}%  PF={pf}  MDD={dd}%")

    # 적용 코드
    if is_futures:
        weight_name = "DEFAULT_WEIGHTS"
    else:
        weight_name = "SPOT_WEIGHTS"
    print(f"\n  적용 코드 (combiner.py {weight_name}):")
    print(f"    {weight_name} = {{")
    for name, w in sorted(weights.items()):
        print(f"        \"{name}\": {w:.2f},")
    print(f"    }}")
    print(f"{'='*60}")


# ══════════════════════════════════════════════════════════════
#  메인
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="전략 Optuna 최적화 (현물/선물)")
    parser.add_argument("--trials", type=int, default=50, help="시행 횟수 (기본 50)")
    parser.add_argument("--days", type=int, default=540, help="단순 모드 백테스트 기간")
    parser.add_argument("--simple", action="store_true", help="단순 모드 (단일 기간)")
    parser.add_argument("--db", type=str, default=None, help="Optuna DB 경로 (이어하기)")
    parser.add_argument("--use-binance", action="store_true", help="바이낸스 USDT 데이터 사용 (현물)")
    parser.add_argument("--futures", action="store_true", help="선물 7전략 최적화")
    args = parser.parse_args()

    is_futures = args.futures

    global USE_BINANCE, SYMBOLS
    if args.use_binance or is_futures:
        USE_BINANCE = True
        SYMBOLS = SYMBOLS_USDT_SPOT

    if args.simple:
        mode_str = f"단순 ({args.days}일)"
    else:
        mode_str = f"다중 기간 ({'/'.join(str(d) for d in MULTI_PERIODS)}일)"

    if is_futures:
        strats = FUTURES_STRATEGY_NAMES
        syms = FUTURES_SYMBOLS
        title = "선물 7전략"
    else:
        strats = SPOT_STRATEGY_NAMES
        syms = SYMBOLS
        title = "현물 4전략"

    print(f"\n{title} Optuna 최적화")
    print(f"  시행: {args.trials}회, 모드: {mode_str}")
    print(f"  전략: {', '.join(strats)}")
    print(f"  코인: {', '.join(syms)}")
    if is_futures:
        print(f"  레버리지: 3x, short-all, dynamic_sl")
    if not args.simple:
        est_min = args.trials * len(MULTI_PERIODS) * (3 if is_futures else 2)
        print(f"  예상 시간: ~{est_min}분 ({len(MULTI_PERIODS)}기간 x {args.trials}회)")
    print()

    storage = f"sqlite:///{args.db}" if args.db else None
    study_name = "futures_7strategy" if is_futures else f"spot_4strategy_{'binance' if USE_BINANCE else 'bithumb'}"
    study = optuna.create_study(
        study_name=study_name,
        direction="maximize",
        sampler=TPESampler(seed=42),
        storage=storage,
        load_if_exists=True,
    )

    loop = asyncio.new_event_loop()

    if args.simple:
        def objective(trial):
            return loop.run_until_complete(
                simple_objective(trial, args.days, is_futures=is_futures)
            )
    else:
        def objective(trial):
            return loop.run_until_complete(
                multi_period_objective(trial, MULTI_PERIODS, is_futures=is_futures)
            )

    try:
        study.optimize(objective, n_trials=args.trials, show_progress_bar=True)
    except KeyboardInterrupt:
        print("\n  중단됨 (Ctrl+C) — 현재까지 결과 출력")

    if study.best_trial:
        print_best(study, is_futures=is_futures)
    loop.close()


if __name__ == "__main__":
    main()
