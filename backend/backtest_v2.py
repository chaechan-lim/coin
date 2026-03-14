"""
FuturesEngineV2 백테스터 — 레짐 적응형 선물 엔진 Walk-Forward 검증.

실행:
  python backtest_v2.py --days 540
  python backtest_v2.py --days 540 --walk-forward
  python backtest_v2.py --days 360 --coins BTC ETH SOL
  python backtest_v2.py --days 180 --leverage 5

Walk-Forward (240-day train + 60-day val + 60-day test):
  python backtest_v2.py --days 540 --walk-forward
"""
import asyncio
import argparse
import logging
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pandas_ta as ta

# structlog 로그 레벨을 WARNING으로 올려서 불필요한 출력 제거
logging.basicConfig(level=logging.WARNING)
import structlog
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
)

from core.enums import Direction, Regime, SignalType
from engine.regime_detector import RegimeDetector, RegimeState
from engine.strategy_selector import StrategySelector
from strategies.regime_base import RegimeStrategy, StrategyDecision
from strategies.base import Signal
from exchange.data_models import Candle, Ticker

# ── 상수 ──────────────────────────────────────────────────────
FUTURES_FEE = 0.0004      # 0.04% maker/taker (바이낸스 선물)
FUNDING_RATE = 0.0001     # 0.01%/8h 평균 펀딩비
SLIPPAGE = 0.0002         # 0.02% 슬리피지
MIN_MARGIN_USDT = 5.0     # 최소 마진
BASE_RISK_PCT = 0.02      # 기본 리스크: 자본의 2%
LOOKBACK_WINDOW = 60      # 전략 평가용 슬라이스 크기

COINS_DEFAULT = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT"]

# pandas_ta → v2 전략 컬럼 매핑
_RENAME_MAP = {
    "EMA_9": "ema_9",
    "EMA_21": "ema_21",
    "EMA_20": "ema_20",
    "EMA_50": "ema_50",
    "RSI_14": "rsi_14",
    "ATRr_14": "atr_14",
    "ADX_14": "adx_14",
    "BBU_20_2.0": "bb_upper_20",
    "BBL_20_2.0": "bb_lower_20",
    "BBM_20_2.0": "bb_mid_20",
}


# v1 전략 → v2 레짐 매핑 (미니 앙상블)
REGIME_STRATEGY_MAP: dict[Regime, list[tuple[str, float]]] = {
    # TRENDING: 추세추종 전략 (ma, macd, obv 중심)
    Regime.TRENDING_UP: [
        ("ma_crossover", 0.20),
        ("macd_crossover", 0.25),
        ("obv_divergence", 0.20),
        ("bollinger_rsi", 0.20),
        ("bb_squeeze", 0.15),
    ],
    Regime.TRENDING_DOWN: [
        ("ma_crossover", 0.20),
        ("macd_crossover", 0.25),
        ("obv_divergence", 0.20),
        ("bollinger_rsi", 0.20),
        ("bb_squeeze", 0.15),
    ],
    # RANGING: 평균회귀 전략 (bollinger_rsi, rsi, stochastic 중심)
    Regime.RANGING: [
        ("bollinger_rsi", 0.30),
        ("rsi", 0.25),
        ("stochastic_rsi", 0.20),
        ("bb_squeeze", 0.25),
    ],
    # VOLATILE: 돌파 전략 (bb_squeeze, obv 중심)
    Regime.VOLATILE: [
        ("bb_squeeze", 0.30),
        ("obv_divergence", 0.25),
        ("bollinger_rsi", 0.20),
        ("rsi", 0.15),
        ("stochastic_rsi", 0.10),
    ],
}


class V1StrategyAdapter(RegimeStrategy):
    """v1 BaseStrategy 래퍼 → v2 RegimeStrategy 인터페이스.

    v1 Signal(BUY/SELL/HOLD) → v2 StrategyDecision(LONG/SHORT/FLAT).
    레짐별 미니 앙상블: 2-5개 v1 전략 가중 투표.
    """

    def __init__(self, strategies: dict, regime_map: dict):
        self._strategies = strategies  # {name: BaseStrategy instance}
        self._regime_map = regime_map  # {Regime: [(name, weight), ...]}

    @property
    def name(self) -> str:
        return "v1_ensemble"

    @property
    def target_regimes(self) -> list[Regime]:
        return list(self._regime_map.keys())

    async def evaluate(
        self,
        df_5m: pd.DataFrame,
        df_1h: pd.DataFrame,
        regime: RegimeState,
        current_position: Direction | None,
    ) -> StrategyDecision:
        group = self._regime_map.get(regime.regime, [])
        if not group:
            return self._hold(current_position)

        # v1 전략은 4h 캔들용 → 1h 데이터 사용 (5m 노이즈 회피)
        df = df_1h if len(df_1h) >= 25 else df_5m
        close = float(df["close"].iloc[-1]) if "close" in df.columns else 0.0
        atr = float(df["atr_14"].iloc[-1]) if "atr_14" in df.columns else 0.0

        # 더미 Ticker (v1 전략의 analyze()에 필요)
        ticker = Ticker(
            symbol="X/USDT", last=close, bid=close * 0.999, ask=close * 1.001,
            high=close * 1.01, low=close * 0.99, volume=1000.0,
            timestamp=datetime.now(timezone.utc),
        )

        buy_score = 0.0
        sell_score = 0.0
        total_weight = 0.0
        active_signals: list[Signal] = []

        for strat_name, weight in group:
            strat = self._strategies.get(strat_name)
            if strat is None:
                continue
            try:
                signal = await strat.analyze(df, ticker)
            except Exception:
                continue

            if signal.signal_type == SignalType.HOLD:
                continue  # HOLD = 기권

            total_weight += weight
            if signal.signal_type == SignalType.BUY:
                buy_score += weight * signal.confidence
            elif signal.signal_type == SignalType.SELL:
                sell_score += weight * signal.confidence
            active_signals.append(signal)

        if total_weight < 0.10:  # 참여 전략 부족
            return self._hold(current_position)

        buy_norm = buy_score / total_weight if total_weight > 0 else 0
        sell_norm = sell_score / total_weight if total_weight > 0 else 0

        # BUY vs SELL 판정
        if buy_norm > sell_norm and buy_norm > 0.3:
            conf = min(1.0, buy_norm)
            sizing = self._calc_sizing(conf, atr, close) if atr > 0 and close > 0 else 0.5
            return StrategyDecision(
                direction=Direction.LONG,
                confidence=conf,
                sizing_factor=sizing,
                stop_loss_atr=1.5,
                take_profit_atr=3.0,
                reason=f"v1 ensemble BUY: {buy_norm:.2f} vs {sell_norm:.2f}",
                strategy_name="v1_ensemble",
                indicators={"buy_score": buy_norm, "sell_score": sell_norm,
                             "active": len(active_signals)},
            )
        elif sell_norm > buy_norm and sell_norm > 0.3:
            conf = min(1.0, sell_norm)
            sizing = self._calc_sizing(conf, atr, close) if atr > 0 and close > 0 else 0.5
            return StrategyDecision(
                direction=Direction.SHORT,
                confidence=conf,
                sizing_factor=sizing,
                stop_loss_atr=1.5,
                take_profit_atr=3.0,
                reason=f"v1 ensemble SELL: {sell_norm:.2f} vs {buy_norm:.2f}",
                strategy_name="v1_ensemble",
                indicators={"buy_score": buy_norm, "sell_score": sell_norm,
                             "active": len(active_signals)},
            )

        return self._hold(current_position)

    def _hold(self, current_position: Direction | None) -> StrategyDecision:
        return StrategyDecision(
            direction=current_position or Direction.FLAT,
            confidence=0.5,
            sizing_factor=0.0,
            stop_loss_atr=0,
            take_profit_atr=0,
            reason="v1_ensemble_hold",
            strategy_name="v1_ensemble",
        )


def create_v1_strategies() -> dict:
    """v1 전략 인스턴스 생성."""
    from strategies.bollinger_rsi import BollingerRSIStrategy
    from strategies.rsi_strategy import RSIStrategy
    from strategies.ma_crossover import MACrossoverStrategy
    from strategies.macd_crossover import MACDCrossoverStrategy
    from strategies.stochastic_rsi import StochasticRSIStrategy
    from strategies.obv_divergence import OBVDivergenceStrategy
    from strategies.bb_squeeze import BBSqueezeStrategy
    return {
        "bollinger_rsi": BollingerRSIStrategy(),
        "rsi": RSIStrategy(),
        "ma_crossover": MACrossoverStrategy(),
        "macd_crossover": MACDCrossoverStrategy(),
        "stochastic_rsi": StochasticRSIStrategy(),
        "obv_divergence": OBVDivergenceStrategy(),
        "bb_squeeze": BBSqueezeStrategy(),
    }


def _tf_hours(tf: str) -> float:
    return {"1m": 1/60, "5m": 5/60, "15m": 15/60, "1h": 1, "4h": 4, "1d": 24}.get(tf, 1)


# ── 데이터 수집 ──────────────────────────────────────────────
async def fetch_ohlcv_cached(
    exchange,
    symbol: str,
    timeframe: str,
    days: int,
) -> pd.DataFrame:
    """OHLCV 데이터를 가져와 CSV 캐싱. 기존 fetch_history 패턴 재사용."""
    candles_needed = int(days * 24 / _tf_hours(timeframe)) + 200
    tf_ms = int(_tf_hours(timeframe) * 3600 * 1000)

    cache_dir = Path(__file__).parent / ".cache"
    cache_dir.mkdir(exist_ok=True)
    safe_symbol = symbol.replace("/", "_")
    cache_path = cache_dir / f"{safe_symbol}_{timeframe}.csv"

    cached_df = None
    last_cached_ts = 0
    if cache_path.exists():
        cached_df = pd.read_csv(
            cache_path, parse_dates=["timestamp"], index_col="timestamp",
        )
        # 손상된 인덱스 행 제거 (파싱 실패 시 Index가 object dtype이 됨)
        if not isinstance(cached_df.index, pd.DatetimeIndex):
            cached_df.index = pd.to_datetime(cached_df.index, errors="coerce", utc=True)
            cached_df = cached_df[cached_df.index.notna()]
        if cached_df.index.tz is None:
            cached_df.index = cached_df.index.tz_localize("UTC")
        cached_df.sort_index(inplace=True)
        last_cached_ts = int(cached_df.index[-1].timestamp() * 1000)

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - candles_needed * tf_ms

    # 캐시가 충분히 오래된 데이터를 가지고 있는지 확인
    needs_old_data = True
    if cached_df is not None:
        first_cached_ts = int(cached_df.index[0].timestamp() * 1000)
        if first_cached_ts <= start_ms + tf_ms * 10:  # 10캔들 허용 오차
            needs_old_data = False

    if cached_df is not None and not needs_old_data and last_cached_ts > start_ms:
        fetch_since = last_cached_ts + tf_ms
    else:
        fetch_since = start_ms

    # 페이지네이션 루프
    all_new: list[Candle] = []
    page_limit = 1000
    cursor = fetch_since

    while cursor < now_ms:
        raw = await exchange.fetch_ohlcv(
            symbol, timeframe, limit=page_limit, since=cursor,
        )
        if not raw:
            break
        all_new.extend(raw)
        last_ts = int(raw[-1].timestamp.timestamp() * 1000)
        if last_ts <= cursor:
            break
        cursor = last_ts + tf_ms
        if len(raw) < page_limit * 0.9:
            break

    # 새 데이터를 DataFrame으로
    if all_new:
        new_df = pd.DataFrame([{
            "timestamp": c.timestamp,
            "open": c.open, "high": c.high,
            "low": c.low, "close": c.close, "volume": c.volume,
        } for c in all_new])
        new_df.set_index("timestamp", inplace=True)
        new_df.sort_index(inplace=True)

        if cached_df is not None:
            df = pd.concat([cached_df, new_df])
            df = df[~df.index.duplicated(keep="last")]
            df.sort_index(inplace=True)
        else:
            df = new_df
    elif cached_df is not None:
        df = cached_df
    else:
        raise ValueError(f"{symbol} {timeframe} 데이터 없음")

    df.to_csv(cache_path)
    return df


def compute_v2_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """v2 전략 + v1 전략에 필요한 기술적 지표 계산."""
    df = df.copy()

    # v2 전략용
    df.ta.ema(length=9, append=True)
    df.ta.ema(length=20, append=True)
    df.ta.ema(length=21, append=True)
    df.ta.ema(length=50, append=True)
    df.ta.rsi(length=14, append=True)
    df.ta.atr(length=14, append=True)
    df.ta.adx(length=14, append=True)
    df.ta.bbands(length=20, std=2, append=True)

    # v1 전략용 추가 지표
    df.ta.sma(length=5, append=True)
    df.ta.sma(length=20, append=True)
    df.ta.sma(length=50, append=True)
    df.ta.sma(length=60, append=True)
    df.ta.ema(length=12, append=True)
    df.ta.ema(length=26, append=True)
    df.ta.macd(fast=12, slow=26, signal=9, append=True)
    df["Volume_SMA_20"] = df["volume"].rolling(window=20).mean()

    # 컬럼 이름 변환: pandas_ta uppercase → v2 lowercase
    df.rename(columns=_RENAME_MAP, inplace=True)

    # BB 컬럼은 pandas_ta 버전에 따라 suffix가 다름 — 동적 매핑
    for col in df.columns:
        if col.startswith("BBU_20") and "bb_upper_20" not in df.columns:
            df.rename(columns={col: "bb_upper_20"}, inplace=True)
        elif col.startswith("BBL_20") and "bb_lower_20" not in df.columns:
            df.rename(columns={col: "bb_lower_20"}, inplace=True)
        elif col.startswith("BBM_20") and "bb_mid_20" not in df.columns:
            df.rename(columns={col: "bb_mid_20"}, inplace=True)

    df.dropna(inplace=True)

    # 날짜 필터
    cutoff = datetime.now(timezone.utc) - timedelta(days=1000)
    if df.index.tz is not None:
        cutoff = cutoff.replace(tzinfo=df.index.tz)
    df = df[df.index >= cutoff]

    return df


# ── 데이터 클래스 ────────────────────────────────────────────

@dataclass
class V2Position:
    symbol: str
    direction: Direction
    quantity: float
    entry_price: float
    margin: float
    leverage: int
    sl_price: float
    tp_price: float
    trail_activation_price: float
    trail_stop_atr: float
    extreme_price: float
    atr_at_entry: float
    entered_idx: int
    strategy_name: str
    trailing_active: bool = False
    trail_stop_price: float | None = None


@dataclass
class V2Trade:
    symbol: str
    direction: str       # "long" or "short"
    entry_price: float
    exit_price: float
    quantity: float
    margin: float
    pnl: float
    pnl_pct: float
    entry_time: datetime
    exit_time: datetime
    exit_reason: str
    strategy_name: str
    regime: str


@dataclass
class V2BacktestResult:
    coins: list[str]
    days: int
    initial_balance: float
    final_balance: float
    total_pnl: float
    total_pnl_pct: float
    max_drawdown_pct: float
    total_trades: int
    long_trades: int
    short_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    avg_win_pct: float
    avg_loss_pct: float
    profit_factor: float
    sharpe_ratio: float
    total_fees: float
    total_funding: float
    buy_hold_pnl_pct: float
    trades: list[V2Trade] = field(default_factory=list)
    equity_curve: list[tuple] = field(default_factory=list)
    regime_distribution: dict[str, int] = field(default_factory=dict)
    coin_stats: dict[str, dict] = field(default_factory=dict)


# ── 백테스터 ─────────────────────────────────────────────────

class V2Backtester:

    def __init__(
        self,
        exchange,
        coins: list[str],
        leverage: int = 3,
        initial_balance: float = 1000.0,
        max_position_pct: float = 0.15,
        base_risk_pct: float = BASE_RISK_PCT,
        cooldown_candles: int = 36,    # 3시간 (5m × 36)
        min_confidence: float = 0.5,   # 최소 신뢰도
        trending_only: bool = False,   # True = TRENDING 레짐에서만 거래
        eval_interval: int = 12,       # 전략 평가 주기 (5m 캔들 수, 12=1h)
    ):
        self._exchange = exchange
        self._coins = coins
        self._leverage = leverage
        self._initial_balance = initial_balance
        self._max_position_pct = max_position_pct
        self._base_risk_pct = base_risk_pct
        self._cooldown_candles = cooldown_candles
        self._min_confidence = min_confidence
        self._trending_only = trending_only
        self._eval_interval = eval_interval
        self._use_v1 = False
        self._v1_adapter: V1StrategyAdapter | None = None

        self._regime_detector = RegimeDetector()
        self._strategy_selector = StrategySelector()

    def enable_v1_strategies(self) -> None:
        """v1 전략 미니 앙상블 모드 활성화."""
        strategies = create_v1_strategies()
        self._v1_adapter = V1StrategyAdapter(strategies, REGIME_STRATEGY_MAP)
        self._use_v1 = True

    async def prefetch(self, days: int) -> dict[str, tuple[pd.DataFrame, pd.DataFrame]]:
        """모든 코인의 5m + 1h 데이터 프리페치."""
        result = {}
        for symbol in self._coins:
            try:
                print(f"  {symbol} 데이터 로딩...")
                df_5m_raw = await fetch_ohlcv_cached(self._exchange, symbol, "5m", days)
                df_1h_raw = await fetch_ohlcv_cached(self._exchange, symbol, "1h", days)

                df_5m = compute_v2_indicators(df_5m_raw)
                df_1h = compute_v2_indicators(df_1h_raw)

                print(f"    5m: {len(df_5m):,}캔들 | 1h: {len(df_1h):,}캔들")
                result[symbol] = (df_5m, df_1h)
            except Exception as e:
                print(f"    {symbol} 실패: {e}")
        return result

    async def run(self, days: int) -> V2BacktestResult:
        """v2 백테스트 실행."""
        print(f"\n{'='*60}")
        print(f"  FuturesEngine V2 백테스트 | 5m+1h | {days}일")
        print(f"  코인: {', '.join(self._coins)}")
        print(f"  레버리지: {self._leverage}x | 수수료: {FUTURES_FEE*100:.2f}%")
        print(f"  최대 포지션: {self._max_position_pct*100:.0f}% | 리스크: {self._base_risk_pct*100:.0f}%")
        print(f"  쿨다운: {self._cooldown_candles}캔들 ({self._cooldown_candles*5}분) | 최소 신뢰도: {self._min_confidence}")
        print(f"{'='*60}")

        all_data = await self.prefetch(days)
        if not all_data:
            raise ValueError("사용 가능한 데이터 없음")

        return await self._simulate(all_data, days)

    async def _simulate(
        self,
        all_data: dict[str, tuple[pd.DataFrame, pd.DataFrame]],
        days: int,
    ) -> V2BacktestResult:
        """메인 시뮬레이션 루프."""
        # 유니온 5m 타임스탬프 (데이터 전체 사용 — 이미 prefetch/WF에서 슬라이싱됨)
        all_ts = sorted(set().union(*(
            df5m.index for df5m, _ in all_data.values()
        )))
        if not all_ts:
            raise ValueError("날짜 범위에 데이터 없음")

        print(f"\n  타임라인: {len(all_ts):,}개 5m 캔들 ({all_ts[0].date()} ~ {all_ts[-1].date()})")

        # 1h 레짐 사전 계산 (코인별)
        regimes_per_coin: dict[str, list[tuple[datetime, RegimeState]]] = {}
        for sym, (_, df_1h) in all_data.items():
            regimes_per_coin[sym] = self._precompute_regimes(df_1h)
        # BTC 기준 글로벌 레짐 (에쿼티 보고용)
        btc_key = "BTC/USDT" if "BTC/USDT" in all_data else list(all_data.keys())[0]
        regimes_by_hour = regimes_per_coin[btc_key]

        # 초기 상태
        cash = self._initial_balance
        positions: dict[str, V2Position] = {}
        trades: list[V2Trade] = []
        equity_curve: list[tuple] = []
        peak_equity = self._initial_balance
        max_drawdown = 0.0
        total_fees = 0.0
        total_funding = 0.0
        regime_counts: dict[str, int] = {}

        # 코인별 통계
        coin_stats: dict[str, dict] = {
            sym: {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0,
                  "long_wins": 0, "long_losses": 0, "short_wins": 0, "short_losses": 0}
            for sym in all_data
        }

        # 쿨다운 추적 (코인별 마지막 청산 캔들 인덱스)
        last_exit_idx: dict[str, int] = {}

        # 진입 가격 기록 (B&H 비교용)
        first_prices: dict[str, float] = {}
        last_prices: dict[str, float] = {}

        candles_per_8h = 8 * 12  # 5m 캔들 96개 = 8h

        print("  시뮬레이션 진행 중...")

        for candle_idx, ts in enumerate(all_ts):
            # ─── 에쿼티 계산 ───
            equity = cash
            for sym, pos in positions.items():
                if sym in all_data:
                    df5m, _ = all_data[sym]
                    if ts in df5m.index:
                        price = float(df5m.loc[ts, "close"])
                        unrealized = self._calc_pnl(pos.direction, pos.entry_price, price, pos.quantity)
                        equity += pos.margin + unrealized

            equity_curve.append((ts, equity))
            if equity > peak_equity:
                peak_equity = equity
            dd = (peak_equity - equity) / peak_equity * 100 if peak_equity > 0 else 0
            if dd > max_drawdown:
                max_drawdown = dd

            # 워밍업 (60캔들 = 5시간)
            if candle_idx < LOOKBACK_WINDOW:
                continue

            # ─── 레짐 조회 ───
            regime = self._get_regime_at(regimes_by_hour, ts)
            if regime:
                regime_name = regime.regime.value
                regime_counts[regime_name] = regime_counts.get(regime_name, 0) + 1

            # ─── 펀딩비 (8시간마다) ───
            if candle_idx % candles_per_8h == 0:
                for sym, pos in positions.items():
                    if sym in all_data:
                        df5m, _ = all_data[sym]
                        if ts in df5m.index:
                            price = float(df5m.loc[ts, "close"])
                            notional = pos.quantity * price
                            if pos.direction == Direction.LONG:
                                funding = notional * FUNDING_RATE
                            else:
                                funding = -notional * FUNDING_RATE
                            cash -= funding
                            total_funding += funding

            # ─── 코인별 평가 ───
            for sym in all_data:
                df5m, df1h = all_data[sym]
                if ts not in df5m.index:
                    continue

                price = float(df5m.loc[ts, "close"])

                # B&H 가격 기록
                if sym not in first_prices:
                    first_prices[sym] = price
                last_prices[sym] = price

                has_position = sym in positions

                # ─── SL/TP/트레일링 체크 ───
                if has_position:
                    pos = positions[sym]
                    exit_reason = self._check_stops(pos, price)
                    if exit_reason:
                        pnl, fee = self._close_position(pos, price)
                        cash += pos.margin + pnl - fee
                        total_fees += fee
                        trade = self._record_trade(pos, price, ts, exit_reason, pnl, regime)
                        trades.append(trade)
                        self._update_coin_stats(coin_stats, trade)
                        del positions[sym]
                        last_exit_idx[sym] = candle_idx
                        has_position = False
                    else:
                        # 트레일링 업데이트
                        self._update_trailing(pos, price)

                # ─── 전략 평가 (eval_interval 주기로만) ───
                if candle_idx % self._eval_interval != 0:
                    continue

                # 코인별 레짐 조회
                coin_regime = self._get_regime_at(
                    regimes_per_coin.get(sym, regimes_by_hour), ts,
                )
                if coin_regime is None:
                    continue

                # trending-only 모드: 비추세 레짐에서 기존 포지션 청산, 신규 진입 차단
                if self._trending_only and coin_regime.regime not in (Regime.TRENDING_UP, Regime.TRENDING_DOWN):
                    if sym in positions:
                        pos = positions[sym]
                        pnl, fee = self._close_position(pos, price)
                        cash += pos.margin + pnl - fee
                        total_fees += fee
                        trade = self._record_trade(pos, price, ts, "regime_exit", pnl, coin_regime)
                        trades.append(trade)
                        self._update_coin_stats(coin_stats, trade)
                        del positions[sym]
                        last_exit_idx[sym] = candle_idx
                    continue

                strategy = self._v1_adapter if self._use_v1 else self._strategy_selector.select(coin_regime.regime)
                current_dir = positions[sym].direction if sym in positions else None

                # 5m 윈도우 슬라이스
                idx = df5m.index.get_loc(ts)
                if isinstance(idx, slice):
                    idx = idx.start
                start = max(0, idx - LOOKBACK_WINDOW + 1)
                window_5m = df5m.iloc[start:idx + 1]

                # 1h 윈도우 (가장 가까운 1h 캔들까지)
                h_idx = df1h.index.searchsorted(ts, side="right")
                h_start = max(0, h_idx - LOOKBACK_WINDOW)
                window_1h = df1h.iloc[h_start:h_idx]

                if len(window_5m) < 21 or len(window_1h) < 5:
                    continue

                decision = await strategy.evaluate(window_5m, window_1h, coin_regime, current_dir)

                # ─── 시그널 처리 ───
                if decision.is_hold:
                    continue

                if decision.is_exit and sym in positions:
                    # 전략이 FLAT 시그널
                    pos = positions[sym]
                    pnl, fee = self._close_position(pos, price)
                    cash += pos.margin + pnl - fee
                    total_fees += fee
                    trade = self._record_trade(pos, price, ts, "strategy_exit", pnl, coin_regime)
                    trades.append(trade)
                    self._update_coin_stats(coin_stats, trade)
                    del positions[sym]
                    last_exit_idx[sym] = candle_idx
                    continue

                if decision.is_entry:
                    # 최소 신뢰도 필터
                    if decision.confidence < self._min_confidence:
                        continue

                    # SAR: 현재 포지션과 다른 방향 → 청산 후 신규 진입
                    if sym in positions and positions[sym].direction != decision.direction:
                        pos = positions[sym]
                        pnl, fee = self._close_position(pos, price)
                        cash += pos.margin + pnl - fee
                        total_fees += fee
                        trade = self._record_trade(pos, price, ts, "SAR", pnl, coin_regime)
                        trades.append(trade)
                        self._update_coin_stats(coin_stats, trade)
                        del positions[sym]
                        last_exit_idx[sym] = candle_idx
                    elif sym not in positions:
                        # 신규 진입 시 쿨다운 체크 (SAR은 쿨다운 면제)
                        if sym in last_exit_idx:
                            elapsed = candle_idx - last_exit_idx[sym]
                            if elapsed < self._cooldown_candles:
                                continue

                    # 이미 같은 방향 포지션 있으면 스킵
                    if sym in positions:
                        continue

                    # 신규 진입
                    atr = float(window_5m["atr_14"].iloc[-1]) if "atr_14" in window_5m.columns else 0.0
                    if atr <= 0:
                        continue

                    margin = self._calc_margin(
                        decision, cash, price, atr,
                    )
                    if margin < MIN_MARGIN_USDT:
                        continue

                    quantity = margin * self._leverage / price
                    fee = quantity * price * FUTURES_FEE
                    cash -= margin + fee
                    total_fees += fee

                    sl_price, tp_price = self._calc_sl_tp(
                        decision.direction, price, atr,
                        decision.stop_loss_atr, decision.take_profit_atr,
                    )
                    trail_act = price * (1 + 2.0 * atr / price) if decision.direction == Direction.LONG \
                        else price * (1 - 2.0 * atr / price)

                    positions[sym] = V2Position(
                        symbol=sym,
                        direction=decision.direction,
                        quantity=quantity,
                        entry_price=price,
                        margin=margin,
                        leverage=self._leverage,
                        sl_price=sl_price,
                        tp_price=tp_price,
                        trail_activation_price=trail_act,
                        trail_stop_atr=1.0,
                        extreme_price=price,
                        atr_at_entry=atr,
                        entered_idx=candle_idx,
                        strategy_name=decision.strategy_name,
                    )

            # 10% 진행 출력
            if candle_idx > 0 and candle_idx % (len(all_ts) // 10 + 1) == 0:
                pct = candle_idx / len(all_ts) * 100
                print(f"    {pct:.0f}% ({candle_idx:,}/{len(all_ts):,}) 에쿼티: {equity:,.1f}")

        # ─── 미청산 포지션 정리 ───
        final_ts = all_ts[-1]
        for sym in list(positions.keys()):
            pos = positions[sym]
            if sym in all_data:
                df5m, _ = all_data[sym]
                if final_ts in df5m.index:
                    price = float(df5m.loc[final_ts, "close"])
                else:
                    price = pos.entry_price
            else:
                price = pos.entry_price
            pnl, fee = self._close_position(pos, price)
            cash += pos.margin + pnl - fee
            total_fees += fee
            trade = self._record_trade(pos, price, final_ts, "backtest_end", pnl, regime)
            trades.append(trade)
            self._update_coin_stats(coin_stats, trade)
        positions.clear()

        # ─── 결과 계산 ───
        final_balance = cash
        total_pnl = final_balance - self._initial_balance
        total_pnl_pct = total_pnl / self._initial_balance * 100

        long_trades = [t for t in trades if t.direction == "long"]
        short_trades = [t for t in trades if t.direction == "short"]
        winning = [t for t in trades if t.pnl > 0]
        losing = [t for t in trades if t.pnl <= 0]

        total_win_amt = sum(t.pnl for t in winning)
        total_loss_amt = abs(sum(t.pnl for t in losing))
        profit_factor = total_win_amt / total_loss_amt if total_loss_amt > 0 else float('inf')

        win_rate = len(winning) / len(trades) * 100 if trades else 0
        avg_win_pct = sum(t.pnl_pct for t in winning) / len(winning) if winning else 0
        avg_loss_pct = sum(t.pnl_pct for t in losing) / len(losing) if losing else 0

        # Sharpe ratio (일별 수익률 기반)
        if len(equity_curve) > 1:
            daily_returns = []
            prev = equity_curve[0][1]
            for i in range(12 * 24, len(equity_curve), 12 * 24):  # 일단위
                r = (equity_curve[i][1] - prev) / prev if prev > 0 else 0
                daily_returns.append(r)
                prev = equity_curve[i][1]
            if daily_returns:
                avg_r = sum(daily_returns) / len(daily_returns)
                std_r = (sum((r - avg_r) ** 2 for r in daily_returns) / len(daily_returns)) ** 0.5
                sharpe = (avg_r / std_r * math.sqrt(365)) if std_r > 0 else 0
            else:
                sharpe = 0
        else:
            sharpe = 0

        # B&H 비교
        if first_prices and last_prices:
            bh_returns = []
            for sym in first_prices:
                if sym in last_prices and first_prices[sym] > 0:
                    bh_returns.append((last_prices[sym] - first_prices[sym]) / first_prices[sym] * 100)
            buy_hold_pct = sum(bh_returns) / len(bh_returns) if bh_returns else 0
        else:
            buy_hold_pct = 0

        return V2BacktestResult(
            coins=list(all_data.keys()),
            days=days,
            initial_balance=self._initial_balance,
            final_balance=final_balance,
            total_pnl=total_pnl,
            total_pnl_pct=total_pnl_pct,
            max_drawdown_pct=max_drawdown,
            total_trades=len(trades),
            long_trades=len(long_trades),
            short_trades=len(short_trades),
            winning_trades=len(winning),
            losing_trades=len(losing),
            win_rate=win_rate,
            avg_win_pct=avg_win_pct,
            avg_loss_pct=avg_loss_pct,
            profit_factor=profit_factor,
            sharpe_ratio=sharpe,
            total_fees=total_fees,
            total_funding=total_funding,
            buy_hold_pnl_pct=buy_hold_pct,
            trades=trades,
            equity_curve=equity_curve,
            regime_distribution=regime_counts,
            coin_stats=coin_stats,
        )

    # ── 내부 헬퍼 ─────────────────────────────────────────────

    def _precompute_regimes(self, df_1h: pd.DataFrame) -> list[tuple[datetime, RegimeState]]:
        """1h 캔들마다 레짐을 감지하여 시계열로 반환."""
        detector = RegimeDetector()
        regimes = []
        for i in range(50, len(df_1h)):
            window = df_1h.iloc[:i + 1]
            state = detector.detect(window)
            regimes.append((df_1h.index[i], state))
        return regimes

    def _get_regime_at(
        self,
        regimes: list[tuple[datetime, RegimeState]],
        ts: datetime,
    ) -> RegimeState | None:
        """5m 타임스탬프에 해당하는 가장 최근 1h 레짐 반환."""
        if not regimes:
            return None
        # 이진 검색
        lo, hi = 0, len(regimes) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if regimes[mid][0] <= ts:
                lo = mid
            else:
                hi = mid - 1
        if regimes[lo][0] <= ts:
            return regimes[lo][1]
        return None

    def _calc_margin(
        self,
        decision: StrategyDecision,
        cash: float,
        price: float,
        atr: float,
    ) -> float:
        """ATR 기반 마진 계산."""
        if cash <= 0 or atr <= 0 or price <= 0:
            return 0.0

        risk_per_unit = decision.stop_loss_atr * atr / price
        if risk_per_unit <= 0:
            return 0.0

        margin = (cash * self._base_risk_pct) / risk_per_unit
        margin *= decision.sizing_factor * decision.confidence

        # 최대 비율 제한
        max_margin = cash * self._max_position_pct
        margin = min(margin, max_margin)

        if margin < MIN_MARGIN_USDT:
            return 0.0
        return margin

    def _calc_sl_tp(
        self,
        direction: Direction,
        price: float,
        atr: float,
        sl_mult: float,
        tp_mult: float,
    ) -> tuple[float, float]:
        """ATR 기반 SL/TP 가격 계산."""
        if direction == Direction.LONG:
            sl = price - sl_mult * atr
            tp = price + tp_mult * atr
        else:
            sl = price + sl_mult * atr
            tp = price - tp_mult * atr
        return sl, tp

    def _check_stops(self, pos: V2Position, price: float) -> str | None:
        """SL/TP/트레일링 체크. 히트 시 사유 문자열 반환."""
        if pos.direction == Direction.LONG:
            if price <= pos.sl_price:
                return "stop_loss"
            if price >= pos.tp_price:
                return "take_profit"
            if pos.trailing_active and pos.trail_stop_price and price <= pos.trail_stop_price:
                return "trailing_stop"
        else:  # SHORT
            if price >= pos.sl_price:
                return "stop_loss"
            if price <= pos.tp_price:
                return "take_profit"
            if pos.trailing_active and pos.trail_stop_price and price >= pos.trail_stop_price:
                return "trailing_stop"
        return None

    def _update_trailing(self, pos: V2Position, price: float) -> None:
        """트레일링 스탑 업데이트."""
        if pos.direction == Direction.LONG:
            if price > pos.extreme_price:
                pos.extreme_price = price
            if not pos.trailing_active and price >= pos.trail_activation_price:
                pos.trailing_active = True
            if pos.trailing_active:
                pos.trail_stop_price = pos.extreme_price - pos.trail_stop_atr * pos.atr_at_entry
        else:  # SHORT
            if price < pos.extreme_price:
                pos.extreme_price = price
            if not pos.trailing_active and price <= pos.trail_activation_price:
                pos.trailing_active = True
            if pos.trailing_active:
                pos.trail_stop_price = pos.extreme_price + pos.trail_stop_atr * pos.atr_at_entry

    def _close_position(
        self, pos: V2Position, price: float,
    ) -> tuple[float, float]:
        """포지션 청산. (pnl, fee) 반환."""
        pnl = self._calc_pnl(pos.direction, pos.entry_price, price, pos.quantity)
        fee = pos.quantity * price * (FUTURES_FEE + SLIPPAGE)
        return pnl, fee

    @staticmethod
    def _calc_pnl(
        direction: Direction,
        entry: float,
        exit_price: float,
        quantity: float,
    ) -> float:
        if direction == Direction.LONG:
            return (exit_price - entry) * quantity
        else:
            return (entry - exit_price) * quantity

    def _record_trade(
        self,
        pos: V2Position,
        exit_price: float,
        exit_time: datetime,
        exit_reason: str,
        pnl: float,
        regime: RegimeState | None,
    ) -> V2Trade:
        pnl_pct = pnl / pos.margin * 100 if pos.margin > 0 else 0
        return V2Trade(
            symbol=pos.symbol,
            direction=pos.direction.value,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            quantity=pos.quantity,
            margin=pos.margin,
            pnl=pnl,
            pnl_pct=pnl_pct,
            entry_time=exit_time,  # 근사치 (실제 entry_time 추적하려면 추가 필드 필요)
            exit_time=exit_time,
            exit_reason=exit_reason,
            strategy_name=pos.strategy_name,
            regime=regime.regime.value if regime else "unknown",
        )

    @staticmethod
    def _update_coin_stats(stats: dict, trade: V2Trade) -> None:
        if trade.symbol not in stats:
            return
        s = stats[trade.symbol]
        s["trades"] += 1
        s["pnl"] += trade.pnl
        if trade.pnl > 0:
            s["wins"] += 1
            if trade.direction == "long":
                s["long_wins"] += 1
            else:
                s["short_wins"] += 1
        else:
            s["losses"] += 1
            if trade.direction == "long":
                s["long_losses"] += 1
            else:
                s["short_losses"] += 1

    # ── Walk-Forward ──────────────────────────────────────────

    async def walk_forward(
        self,
        days: int,
        train_days: int = 240,
        val_days: int = 60,
        test_days: int = 60,
    ) -> list[V2BacktestResult]:
        """Walk-Forward 검증. 슬라이딩 윈도우로 일관성 검증."""
        window_total = train_days + val_days + test_days
        stride = test_days

        if days < window_total:
            print(f"데이터 부족: {days}일 < 최소 {window_total}일 필요")
            return []

        num_windows = (days - window_total) // stride + 1
        print(f"\n{'='*60}")
        print(f"  Walk-Forward 검증")
        print(f"  학습: {train_days}일 | 검증: {val_days}일 | 테스트: {test_days}일")
        print(f"  윈도우: {num_windows}개 | 스트라이드: {stride}일")
        print(f"{'='*60}")

        # 전체 데이터 프리페치
        all_data = await self.prefetch(days)
        if not all_data:
            raise ValueError("데이터 없음")

        results = []
        for w in range(num_windows):
            offset_days = w * stride
            test_start_days_ago = days - offset_days - window_total + test_days
            test_end_days_ago = days - offset_days - window_total

            # 테스트 구간 데이터 슬라이싱
            now = datetime.now(timezone.utc)
            test_start = now - timedelta(days=days - offset_days - train_days - val_days)
            test_end = now - timedelta(days=days - offset_days - window_total)

            tz = list(all_data.values())[0][0].index.tz
            if tz:
                test_start = test_start.replace(tzinfo=tz)
                test_end = test_end.replace(tzinfo=tz)

            print(f"\n  ── 윈도우 {w+1}/{num_windows} ──")
            print(f"     테스트 구간: {test_start.date()} ~ {test_end.date()}")

            # 테스트 데이터 슬라이싱
            test_data = {}
            skipped = []
            for sym, (df5m, df1h) in all_data.items():
                df5m_test = df5m[(df5m.index >= test_start) & (df5m.index <= test_end)]
                df1h_test = df1h[(df1h.index >= test_start) & (df1h.index <= test_end)]
                if len(df5m_test) > LOOKBACK_WINDOW and len(df1h_test) > 50:
                    # 워밍업을 위해 앞부분 데이터 포함
                    warmup_start = test_start - timedelta(days=5)
                    df5m_slice = df5m[(df5m.index >= warmup_start) & (df5m.index <= test_end)]
                    df1h_slice = df1h[(df1h.index >= warmup_start) & (df1h.index <= test_end)]
                    test_data[sym] = (df5m_slice, df1h_slice)
                else:
                    skipped.append(sym)

            if skipped:
                print(f"     데이터 부족으로 제외: {', '.join(skipped)}")

            if test_data:
                result = await self._simulate(test_data, test_days + 5)
                results.append(result)
                _print_window_summary(w + 1, result)

        # 종합
        if results:
            _print_wf_summary(results)

        return results


# ── 결과 출력 ─────────────────────────────────────────────────

def print_results(r: V2BacktestResult) -> None:
    print(f"\n{'='*60}")
    print(f"  FuturesEngine V2 백테스트 결과")
    print(f"{'='*60}")
    print(f"  코인: {', '.join(r.coins)}")
    print(f"  기간: {r.days}일")
    print()
    print(f"  초기 잔고:     {r.initial_balance:>12,.2f} USDT")
    print(f"  최종 잔고:     {r.final_balance:>12,.2f} USDT")
    print(f"  총 손익:       {r.total_pnl:>12,.2f} USDT ({r.total_pnl_pct:+.1f}%)")
    print(f"  B&H 대비:      {r.buy_hold_pnl_pct:>11.1f}%")
    print()
    print(f"  총 거래:       {r.total_trades:>8d}")
    print(f"    롱:          {r.long_trades:>8d}")
    print(f"    숏:          {r.short_trades:>8d}")
    print(f"  승:            {r.winning_trades:>8d}")
    print(f"  패:            {r.losing_trades:>8d}")
    print(f"  승률:          {r.win_rate:>8.1f}%")
    print()
    print(f"  평균 수익:     {r.avg_win_pct:>8.2f}%")
    print(f"  평균 손실:     {r.avg_loss_pct:>8.2f}%")
    print(f"  Profit Factor: {r.profit_factor:>8.2f}")
    print(f"  Sharpe Ratio:  {r.sharpe_ratio:>8.2f}")
    print(f"  최대 낙폭:     {r.max_drawdown_pct:>8.2f}%")
    print()
    print(f"  총 수수료:     {r.total_fees:>12,.2f} USDT")
    print(f"  총 펀딩비:     {r.total_funding:>12,.2f} USDT")

    if r.regime_distribution:
        print(f"\n  레짐 분포:")
        total = sum(r.regime_distribution.values())
        for regime, count in sorted(r.regime_distribution.items(), key=lambda x: -x[1]):
            pct = count / total * 100 if total > 0 else 0
            print(f"    {regime:<15s} {count:>8,d} ({pct:.1f}%)")

    if r.coin_stats:
        print(f"\n  코인별 성과:")
        print(f"  {'코인':<12s} {'거래':>6s} {'승':>4s} {'패':>4s} {'승률':>7s} {'PnL':>10s}")
        for sym, s in sorted(r.coin_stats.items(), key=lambda x: -x[1]["pnl"]):
            if s["trades"] == 0:
                continue
            wr = s["wins"] / s["trades"] * 100 if s["trades"] > 0 else 0
            print(f"  {sym:<12s} {s['trades']:>6d} {s['wins']:>4d} {s['losses']:>4d} {wr:>6.1f}% {s['pnl']:>+10.2f}")

    # 타겟 달성 확인
    print(f"\n  ── 타겟 확인 ──")
    pf_ok = r.profit_factor >= 1.5
    mdd_ok = r.max_drawdown_pct <= 15.0
    wr_ok = r.win_rate >= 55.0
    print(f"  PF >= 1.5:    {'✓' if pf_ok else '✗'} ({r.profit_factor:.2f})")
    print(f"  MDD <= 15%:   {'✓' if mdd_ok else '✗'} ({r.max_drawdown_pct:.2f}%)")
    print(f"  Win% >= 55%:  {'✓' if wr_ok else '✗'} ({r.win_rate:.1f}%)")
    all_ok = pf_ok and mdd_ok and wr_ok
    print(f"  종합:         {'✓ PASS' if all_ok else '✗ FAIL'}")
    print(f"{'='*60}")


def _print_window_summary(window_num: int, r: V2BacktestResult) -> None:
    pf_ok = r.profit_factor >= 1.5
    mdd_ok = r.max_drawdown_pct <= 15.0
    wr_ok = r.win_rate >= 55.0
    status = "PASS" if (pf_ok and mdd_ok and wr_ok) else "FAIL"
    print(f"     결과: PnL={r.total_pnl_pct:+.1f}% PF={r.profit_factor:.2f} "
          f"MDD={r.max_drawdown_pct:.1f}% Win={r.win_rate:.0f}% "
          f"거래={r.total_trades} [{status}]")


def _print_wf_summary(results: list[V2BacktestResult]) -> None:
    print(f"\n{'='*60}")
    print(f"  Walk-Forward 종합")
    print(f"{'='*60}")

    pfs = [r.profit_factor for r in results]
    mdds = [r.max_drawdown_pct for r in results]
    wrs = [r.win_rate for r in results]
    pnls = [r.total_pnl_pct for r in results]

    print(f"  윈도우 수:     {len(results)}")
    print(f"  평균 PnL:      {sum(pnls)/len(pnls):+.1f}%")
    print(f"  평균 PF:       {sum(pfs)/len(pfs):.2f}")
    print(f"  평균 MDD:      {sum(mdds)/len(mdds):.1f}%")
    print(f"  평균 승률:     {sum(wrs)/len(wrs):.1f}%")
    print(f"  최악 PF:       {min(pfs):.2f}")
    print(f"  최악 MDD:      {max(mdds):.1f}%")

    pass_count = sum(1 for r in results
                     if r.profit_factor >= 1.5 and r.max_drawdown_pct <= 15.0 and r.win_rate >= 55.0)
    print(f"  통과 윈도우:   {pass_count}/{len(results)}")
    all_pass = pass_count == len(results)
    print(f"  종합:          {'✓ ALL PASS' if all_pass else '✗ SOME FAIL'}")
    print(f"{'='*60}")


# ── CLI ───────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="FuturesEngineV2 백테스터")
    parser.add_argument("--days", type=int, default=540, help="백테스트 기간 (일)")
    parser.add_argument("--coins", nargs="+", default=None,
                        help="코인 목록 (예: BTC ETH SOL)")
    parser.add_argument("--leverage", type=int, default=3, help="레버리지 배수")
    parser.add_argument("--balance", type=float, default=1000.0, help="초기 잔고 (USDT)")
    parser.add_argument("--max-position-pct", type=float, default=0.15, help="최대 포지션 비율")
    parser.add_argument("--cooldown", type=int, default=36, help="쿨다운 캔들 수 (5m 기준, 기본 36=3시간)")
    parser.add_argument("--min-confidence", type=float, default=0.5, help="최소 신뢰도 (0.0-1.0)")
    parser.add_argument("--trending-only", action="store_true", help="추세 레짐에서만 거래")
    parser.add_argument("--eval-interval", type=int, default=12, help="전략 평가 주기 (5m 캔들, 12=1h)")
    parser.add_argument("--v1-strategies", action="store_true", help="v1 7전략 레짐 미니앙상블 모드")
    parser.add_argument("--walk-forward", action="store_true", help="Walk-Forward 검증 모드")
    parser.add_argument("--train-days", type=int, default=240, help="WF 학습 기간")
    parser.add_argument("--val-days", type=int, default=60, help="WF 검증 기간")
    parser.add_argument("--test-days", type=int, default=60, help="WF 테스트 기간")
    return parser.parse_args()


async def main():
    args = parse_args()

    coins = args.coins
    if coins:
        coins = [f"{c}/USDT" if "/" not in c else c for c in coins]
    else:
        coins = COINS_DEFAULT

    # 바이낸스 선물 public API (API 키 불필요)
    from exchange.binance_usdm_adapter import BinanceUSDMAdapter
    print("바이낸스 USDM 선물 연결 중...")
    exchange = BinanceUSDMAdapter(api_key="", api_secret="", testnet=False)
    await exchange.initialize()

    bt = V2Backtester(
        exchange=exchange,
        coins=coins,
        leverage=args.leverage,
        initial_balance=args.balance,
        max_position_pct=args.max_position_pct,
        cooldown_candles=args.cooldown,
        min_confidence=args.min_confidence,
        trending_only=args.trending_only,
        eval_interval=args.eval_interval,
    )

    if args.v1_strategies:
        bt.enable_v1_strategies()
        print("  v1 7전략 레짐 미니앙상블 모드 활성화")

    try:
        if args.walk_forward:
            results = await bt.walk_forward(
                args.days,
                train_days=args.train_days,
                val_days=args.val_days,
                test_days=args.test_days,
            )
        else:
            result = await bt.run(args.days)
            print_results(result)
    finally:
        try:
            await exchange.close_ws()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
