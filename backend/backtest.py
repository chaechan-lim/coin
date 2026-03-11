"""
코인 자동 매매 시스템 — 백테스터
====================================
실행 예시:
  python backtest.py --symbol BTC/KRW --days 90 --balance 500000
  python backtest.py --symbol SOL/KRW --days 30 --strategies rsi bollinger_rsi
  python backtest.py --symbol ETH/KRW --days 60 --timeframe 4h
  python backtest.py --all-coins --days 30
  python backtest.py --symbol BTC/KRW --days 30 --stop-loss 5 --take-profit 8
  python backtest.py --all-coins --days 540 --timeframe 4h
  python backtest.py --all-coins --days 30 --no-trend-filter
  python backtest.py --symbol BTC/KRW --days 90 --trailing-activation 0

로테이션 모드:
  python backtest.py --rotation --days 180 --timeframe 4h
  python backtest.py --rotation --days 180 --surge-threshold 2.0
  python backtest.py --rotation --days 180 --no-strategy-confirm

선물 백테스트 (바이낸스 USDM):
  python backtest.py --futures --symbol BTC/USDT --days 180 --timeframe 4h --leverage 3
  python backtest.py --futures --symbol ETH/USDT --days 180 --leverage 5
  python backtest.py --futures --symbol BTC/USDT --days 180 --leverage 10 --dynamic-sl

선물 포트폴리오 백테스트 (멀티코인 선물):
  python backtest.py --futures --portfolio --days 180
  python backtest.py --futures --portfolio --days 540 --leverage 3 --risk --trade-limits
  python backtest.py --futures --portfolio --portfolio-coins BTC ETH SOL --max-positions 3

포트폴리오 백테스트 (멀티코인):
  python backtest.py --portfolio --days 90
  python backtest.py --portfolio --days 540 --risk --trade-limits --asymmetric
  python backtest.py --portfolio --days 540 --asymmetric --strategy-sell voting
  python backtest.py --portfolio --days 540 --asymmetric --strategy-sell paired
  python backtest.py --portfolio --portfolio-coins BTC ETH SOL --max-positions 3

리스크/매매제한 (기존 모드에도 적용):
  python backtest.py --symbol BTC/KRW --days 90 --risk --trade-limits
  python backtest.py --futures --symbol BTC/USDT --days 180 --dual-timeframe
"""

import asyncio
import argparse
import csv
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import pandas_ta as ta

# structlog 로그 레벨을 WARNING으로 올려서 백테스트 중 불필요한 출력 제거
logging.basicConfig(level=logging.WARNING)
import structlog
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
)

# ── 전략 등록을 위해 import ──────────────────────────────────
from strategies.volatility_breakout import VolatilityBreakoutStrategy
from strategies.ma_crossover import MACrossoverStrategy
from strategies.rsi_strategy import RSIStrategy
from strategies.macd_crossover import MACDCrossoverStrategy
from strategies.bollinger_rsi import BollingerRSIStrategy
from strategies.stochastic_rsi import StochasticRSIStrategy
from strategies.obv_divergence import OBVDivergenceStrategy
from strategies.supertrend import SupertrendStrategy
from strategies.bnf_deviation import BNFDeviationStrategy
from strategies.cis_momentum import CISMomentumStrategy
from strategies.larry_williams import LarryWilliamsStrategy
from strategies.donchian_channel import DonchianChannelStrategy
from strategies.volatility_regime import VolatilityRegimeStrategy
from strategies.bb_squeeze import BBSqueezeStrategy
from strategies.registry import StrategyRegistry
from strategies.combiner import SignalCombiner
from strategies.base import Signal
from core.enums import SignalType
from exchange.bithumb_adapter import BithumbAdapter
from exchange.data_models import Candle, Ticker


# ── 설정 ──────────────────────────────────────────────────────
TAKER_FEE = 0.0025      # 0.25% 빗썸 테이커 수수료
SLIPPAGE  = 0.001       # 0.1% 슬리피지
MIN_TRADE_KRW = 5_000   # 최소 거래대금


def _tf_hours(tf: str) -> float:
    """타임프레임 문자열 → 시간 단위 변환."""
    map_ = {"1m": 1/60, "5m": 5/60, "15m": 15/60, "1h": 1, "4h": 4, "1d": 24}
    return map_.get(tf, 1)

ALL_STRATEGIES_5 = [
    "volatility_breakout", "ma_crossover", "rsi",
    "macd_crossover", "bollinger_rsi",
]

ALL_STRATEGIES_8 = ALL_STRATEGIES_5 + [
    "stochastic_rsi", "obv_divergence", "supertrend",
]

# 6전략 (0% 승률 전략 제거: volatility_breakout, supertrend)
ALL_STRATEGIES_6 = [
    "ma_crossover", "rsi", "macd_crossover",
    "bollinger_rsi", "stochastic_rsi", "obv_divergence",
]

# 7전략 = 6전략 + 변동성 레짐
ALL_STRATEGIES_7 = ALL_STRATEGIES_6 + ["volatility_regime"]

# 10전략 = 기존 6 + 신규 4 (4대 트레이더)
ALL_STRATEGIES_10 = ALL_STRATEGIES_6 + [
    "bnf_deviation", "cis_momentum", "larry_williams", "donchian_channel",
]

# 전체 사용 가능 전략 (CLI 유효성 검사용)
ALL_STRATEGIES = ALL_STRATEGIES_8 + [
    "bnf_deviation", "cis_momentum", "larry_williams", "donchian_channel",
    "volatility_regime", "bb_squeeze",
]

# 5전략 가중치 (기존)
WEIGHTS_5 = {
    "volatility_breakout": 0.10,
    "ma_crossover":        0.10,
    "rsi":                 0.30,
    "macd_crossover":      0.15,
    "bollinger_rsi":       0.35,
}

# 6전략 가중치 (역추세 중심 — vol_breakout/supertrend 제거)
WEIGHTS_6 = {
    "ma_crossover":        0.08,
    "rsi":                 0.25,
    "macd_crossover":      0.08,
    "bollinger_rsi":       0.31,
    "stochastic_rsi":      0.15,
    "obv_divergence":      0.13,
}

# 7전략 가중치 (6전략 + 변동성 레짐)
WEIGHTS_7 = {
    "ma_crossover":        0.07,
    "rsi":                 0.22,
    "macd_crossover":      0.07,
    "bollinger_rsi":       0.27,
    "stochastic_rsi":      0.13,
    "obv_divergence":      0.11,
    "volatility_regime":   0.13,
}

# 8전략 가중치 (역발상 중심 유지 + 신규 3종 배분)
WEIGHTS_8 = {
    "volatility_breakout": 0.07,
    "ma_crossover":        0.07,
    "rsi":                 0.22,
    "macd_crossover":      0.11,
    "bollinger_rsi":       0.25,
    "stochastic_rsi":      0.12,
    "obv_divergence":      0.08,
    "supertrend":          0.08,
}

# 기본값 = 6전략
DEFAULT_WEIGHTS = WEIGHTS_6


# ── 데이터 클래스 ──────────────────────────────────────────────
@dataclass
class BacktestTrade:
    timestamp: datetime
    side: str          # "buy" / "sell" / "sell(sl)" / "sell(tp)" / "sell(close)"
    symbol: str
    price: float
    quantity: float
    cost: float
    fee: float
    strategy: str
    confidence: float
    reason: str
    pnl: float = 0.0
    pnl_pct: float = 0.0

@dataclass
class BacktestResult:
    symbol: str
    days: int
    initial_balance: float
    final_balance: float
    total_pnl: float
    total_pnl_pct: float
    max_drawdown_pct: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    profit_factor: float = 0.0
    buy_hold_pnl_pct: float = 0.0   # 단순 매수 후 보유 대비 비교용
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[tuple] = field(default_factory=list)
    strategy_stats: dict = field(default_factory=dict)


# ── 포트폴리오 백테스트용 데이터 클래스 ──────────────────────────────
@dataclass
class PortfolioPositionState:
    """멀티코인 포트폴리오의 코인별 포지션 상태."""
    symbol: str
    quantity: float
    avg_price: float
    entry_idx: int
    peak_price: float
    trailing_active: bool = False
    dynamic_sl_pct: float = 5.0
    entry_strategy: str = ""


@dataclass
class RiskEvent:
    """리스크 트리거 기록."""
    timestamp: datetime
    event_type: str       # "drawdown_pause", "daily_loss_pause", "concentration_block"
    details: str
    value: float = 0.0


@dataclass
class PortfolioBacktestResult:
    """포트폴리오 백테스트 결과."""
    symbols: list[str]
    days: int
    initial_balance: float
    final_balance: float
    total_pnl: float
    total_pnl_pct: float
    max_drawdown_pct: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    profit_factor: float = 0.0
    buy_hold_pnl_pct: float = 0.0   # 균등배분 B&H
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[tuple] = field(default_factory=list)
    strategy_stats: dict = field(default_factory=dict)
    per_coin_stats: dict = field(default_factory=dict)
    risk_events: list[RiskEvent] = field(default_factory=list)
    risk_stats: dict = field(default_factory=dict)
    trade_limit_stats: dict = field(default_factory=dict)


# ── 백테스트용 리스크 관리자 ─────────────────────────────────────────
class BacktestRiskManager:
    """라이브 RiskManagementAgent의 백테스트 복제 (동기, 경량).

    3개 체크: max drawdown pause, daily loss pause, concentration block.
    """

    def __init__(
        self,
        max_drawdown_pct: float = 0.10,
        daily_loss_limit_pct: float = 0.03,
        max_concentration_pct: float = 0.40,
        enabled: bool = False,
    ):
        self.max_drawdown_pct = max_drawdown_pct
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.max_concentration_pct = max_concentration_pct
        self.enabled = enabled

        # 상태
        self._peak_equity = 0.0
        self._day_start_equity = 0.0
        self._current_day_idx = -1  # 일별 리셋 추적
        self.is_drawdown_paused = False
        self.is_daily_loss_paused = False
        self.events: list[RiskEvent] = []
        self._drawdown_pause_count = 0
        self._daily_loss_pause_count = 0
        self._concentration_block_count = 0

    def update_equity(self, ts: datetime, idx: int, equity: float, candles_per_day: int):
        """매 캔들 호출 — peak/drawdown/daily loss 추적."""
        if not self.enabled:
            return

        # peak equity 추적
        if equity > self._peak_equity:
            self._peak_equity = equity

        # drawdown 체크
        if self._peak_equity > 0:
            dd = (self._peak_equity - equity) / self._peak_equity
            if dd > self.max_drawdown_pct and not self.is_drawdown_paused:
                self.is_drawdown_paused = True
                self._drawdown_pause_count += 1
                self.events.append(RiskEvent(
                    timestamp=ts, event_type="drawdown_pause",
                    details=f"낙폭 {dd*100:.1f}% > {self.max_drawdown_pct*100:.0f}% (peak: {self._peak_equity:,.0f})",
                    value=dd,
                ))
            elif dd <= self.max_drawdown_pct * 0.5 and self.is_drawdown_paused:
                # 회복 시 해제 (낙폭이 한도 절반 이하로 회복)
                self.is_drawdown_paused = False

        # 일별 리셋 (candles_per_day 주기)
        day_idx = idx // candles_per_day
        if day_idx != self._current_day_idx:
            self._current_day_idx = day_idx
            self._day_start_equity = equity
            self.is_daily_loss_paused = False  # 새 날 리셋

        # 일일 손실 체크
        if self._day_start_equity > 0:
            daily_loss = (self._day_start_equity - equity) / self._day_start_equity
            if daily_loss > self.daily_loss_limit_pct and not self.is_daily_loss_paused:
                self.is_daily_loss_paused = True
                self._daily_loss_pause_count += 1
                self.events.append(RiskEvent(
                    timestamp=ts, event_type="daily_loss_pause",
                    details=f"일일 손실 {daily_loss*100:.1f}% > {self.daily_loss_limit_pct*100:.0f}%",
                    value=daily_loss,
                ))

    def can_buy(
        self, ts: datetime, sym: str, buy_value: float,
        position_values: dict[str, float], total_equity: float,
    ) -> tuple[bool, str]:
        """매수 가능 여부 판단."""
        if not self.enabled:
            return True, "OK"

        if self.is_drawdown_paused:
            return False, "drawdown_pause"
        if self.is_daily_loss_paused:
            return False, "daily_loss_pause"

        # 코인 비중 체크
        if total_equity > 0:
            existing = position_values.get(sym, 0.0)
            new_pct = (existing + buy_value) / total_equity
            if new_pct > self.max_concentration_pct:
                self._concentration_block_count += 1
                self.events.append(RiskEvent(
                    timestamp=ts, event_type="concentration_block",
                    details=f"{sym} 비중 {new_pct*100:.1f}% > {self.max_concentration_pct*100:.0f}%",
                    value=new_pct,
                ))
                return False, "concentration_limit"

        return True, "OK"

    @property
    def stats(self) -> dict:
        return {
            "drawdown_pauses": self._drawdown_pause_count,
            "daily_loss_pauses": self._daily_loss_pause_count,
            "concentration_blocks": self._concentration_block_count,
            "total_events": len(self.events),
        }


# ── 백테스트용 매매 제한 ─────────────────────────────────────────────
class BacktestTradeLimiter:
    """라이브 _can_trade()의 백테스트 복제.

    일일 매수 상한, 코인당 일일 매수 상한, 최소 캔들 간격.
    """

    def __init__(
        self,
        daily_buy_limit: int = 20,
        max_coin_buys: int = 3,
        min_interval_candles: int = 1,
        enabled: bool = False,
    ):
        self.daily_buy_limit = daily_buy_limit
        self.max_coin_buys = max_coin_buys
        self.min_interval_candles = min_interval_candles
        self.enabled = enabled

        self._current_day_idx = -1
        self._daily_buy_count = 0
        self._coin_buy_count: dict[str, int] = {}
        self._last_buy_idx: dict[str, int] = {}
        self._total_blocks = 0
        self._block_reasons: dict[str, int] = {}

    @staticmethod
    def calc_min_interval(timeframe: str) -> int:
        """타임프레임에서 1시간 쿨다운에 해당하는 캔들 수 계산."""
        tf_h = _tf_hours(timeframe)
        return max(1, int(1.0 / tf_h))

    def reset_day(self, idx: int, candles_per_day: int):
        """일별 카운터 리셋."""
        if not self.enabled:
            return
        day_idx = idx // candles_per_day
        if day_idx != self._current_day_idx:
            self._current_day_idx = day_idx
            self._daily_buy_count = 0
            self._coin_buy_count.clear()

    def can_buy(self, sym: str, idx: int) -> tuple[bool, str]:
        """매수 가능 여부."""
        if not self.enabled:
            return True, "OK"

        if self._daily_buy_count >= self.daily_buy_limit:
            self._total_blocks += 1
            self._block_reasons["daily_limit"] = self._block_reasons.get("daily_limit", 0) + 1
            return False, "daily_buy_limit"

        coin_buys = self._coin_buy_count.get(sym, 0)
        if coin_buys >= self.max_coin_buys:
            self._total_blocks += 1
            self._block_reasons["coin_limit"] = self._block_reasons.get("coin_limit", 0) + 1
            return False, "coin_daily_limit"

        last_idx = self._last_buy_idx.get(sym, -9999)
        if idx - last_idx < self.min_interval_candles:
            self._total_blocks += 1
            self._block_reasons["cooldown"] = self._block_reasons.get("cooldown", 0) + 1
            return False, "cooldown"

        return True, "OK"

    def record_buy(self, sym: str, idx: int):
        """매수 기록."""
        if not self.enabled:
            return
        self._daily_buy_count += 1
        self._coin_buy_count[sym] = self._coin_buy_count.get(sym, 0) + 1
        self._last_buy_idx[sym] = idx

    @property
    def stats(self) -> dict:
        return {
            "total_blocks": self._total_blocks,
            "block_reasons": dict(self._block_reasons),
        }


# ── 데이터 수집 (모듈 레벨 — Backtester / RotationBacktester 공유) ──
async def fetch_history(
    exchange: BithumbAdapter,
    symbol: str,
    timeframe: str,
    days: int,
) -> pd.DataFrame:
    """과거 OHLCV 데이터를 가져와 DataFrame으로 반환.

    페이지네이션으로 1500캔들 제한 우회, CSV 캐싱으로 재실행 가속.
    """
    candles_needed = int(days * 24 / _tf_hours(timeframe)) + 200
    tf_ms = int(_tf_hours(timeframe) * 3600 * 1000)

    # ── CSV 캐시 ──────────────────────────────────────────────
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
        if cached_df.index.tz is None:
            cached_df.index = cached_df.index.tz_localize("UTC")
        cached_df.sort_index(inplace=True)
        last_cached_ts = int(cached_df.index[-1].timestamp() * 1000)

    # ── 필요한 시작 시점 ──────────────────────────────────────
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - candles_needed * tf_ms

    # 캐시가 충분하면 새 데이터만 추가 요청
    if cached_df is not None and last_cached_ts > start_ms:
        fetch_since = last_cached_ts + tf_ms  # 다음 캔들부터
    else:
        fetch_since = start_ms

    # ── 페이지네이션 루프 ─────────────────────────────────────
    all_new: list[Candle] = []
    page_limit = 1000  # 바이낸스 선물 최대 1000개/요청
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
            break  # 더 이상 새 데이터 없음
        cursor = last_ts + tf_ms
        if len(raw) < page_limit * 0.9:
            break  # 마지막 페이지 (거래소별 반환 개수 오차 허용)

    # ── 새 데이터를 DataFrame으로 ─────────────────────────────
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
        raise ValueError(f"{symbol} 데이터 없음")

    # ── 캐시 저장 ────────────────────────────────────────────
    df.to_csv(cache_path)

    # ── 기술적 지표 계산 (pandas_ta) ─────────────────────────
    df.ta.sma(length=5,  append=True)
    df.ta.sma(length=20, append=True)
    df.ta.sma(length=50, append=True)
    df.ta.sma(length=60, append=True)
    df.ta.ema(length=12, append=True)
    df.ta.ema(length=26, append=True)
    df.ta.rsi(length=14, append=True)
    df.ta.macd(fast=12, slow=26, signal=9, append=True)
    df.ta.bbands(length=20, std=2, append=True)
    df.ta.atr(length=14,  append=True)
    df.ta.adx(length=14,  append=True)

    df["Volume_SMA_20"] = df["volume"].rolling(window=20).mean()

    df.dropna(inplace=True)

    # 날짜 필터: 최근 N일
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    if df.index.tz is not None:
        cutoff = cutoff.replace(tzinfo=df.index.tz)
    df = df[df.index >= cutoff]

    return df


# ── 백테스터 엔진 ──────────────────────────────────────────────
class Backtester:

    def __init__(
        self,
        exchange: BithumbAdapter,
        strategy_names: list[str],
        initial_balance: float = 500_000,
        min_confidence: float = 0.50,
        stop_loss_pct: float = 5.0,       # 고정 손절 퍼센트 (0이면 비활성)
        take_profit_pct: float = 10.0,     # 익절 퍼센트 (0이면 비활성)
        trend_filter: bool = True,          # 글로벌 추세 필터
        trailing_activation: float = 3.0,   # 트레일링 활성화 수익% (0이면 비활성)
        trailing_stop: float = 3.0,         # 고점 대비 하락 % (트레일링)
        adaptive_weights: bool = True,      # 적응형 가중치
        dynamic_sl: bool = False,           # ATR+시장상태 동적 손절
        agent_market: bool = True,          # Agent 스코어링 시장 감지
        trade_cooldown: int = 12,           # 매매 간 최소 캔들 수
        asymmetric: bool = False,           # 비대칭 전략 (하락장 방어 / 상승장 공격)
        risk_enabled: bool = False,
        trade_limit_enabled: bool = False,
        risk_max_drawdown: float = 0.10,
        risk_daily_loss: float = 0.03,
        risk_max_concentration: float = 0.40,
        trade_daily_buy_limit: int = 20,
        trade_max_coin_buys: int = 3,
    ):
        self._exchange = exchange
        self._initial_balance = initial_balance
        self._min_confidence = min_confidence
        self._stop_loss_pct = stop_loss_pct
        self._take_profit_pct = take_profit_pct
        self._trend_filter = trend_filter
        self._trailing_activation = trailing_activation
        self._trailing_stop = trailing_stop
        self._adaptive_weights = adaptive_weights
        self._dynamic_sl = dynamic_sl
        self._agent_market = agent_market
        self._trade_cooldown = trade_cooldown
        self._asymmetric = asymmetric

        self._risk_manager = BacktestRiskManager(
            max_drawdown_pct=risk_max_drawdown,
            daily_loss_limit_pct=risk_daily_loss,
            max_concentration_pct=risk_max_concentration,
            enabled=risk_enabled,
        ) if risk_enabled else None

        self._trade_limiter = BacktestTradeLimiter(
            daily_buy_limit=trade_daily_buy_limit,
            max_coin_buys=trade_max_coin_buys,
            enabled=trade_limit_enabled,
        ) if trade_limit_enabled else None

        # 전략 로드 (인스턴스 생성)
        all_strats = StrategyRegistry.create_all()
        self._strategies = {
            name: strat for name, strat in all_strats.items()
            if name in strategy_names
        }
        # 전략 수에 맞는 가중치 선택
        if set(strategy_names) <= set(WEIGHTS_5.keys()):
            base_weights = WEIGHTS_5
        elif set(strategy_names) <= set(WEIGHTS_6.keys()):
            base_weights = WEIGHTS_6
        elif set(strategy_names) <= set(WEIGHTS_7.keys()):
            base_weights = WEIGHTS_7
        elif set(strategy_names) <= set(WEIGHTS_8.keys()):
            base_weights = WEIGHTS_8
        else:
            base_weights = {name: 1.0 / len(strategy_names) for name in strategy_names}
        weights = {k: v for k, v in base_weights.items() if k in strategy_names}
        # 가중치 정규화
        total_w = sum(weights.values())
        if total_w > 0:
            weights = {k: v / total_w for k, v in weights.items()}
        self._combiner = SignalCombiner(
            strategy_weights=weights,
            min_confidence=min_confidence,
        )

    async def fetch_history(
        self, symbol: str, timeframe: str, days: int
    ) -> pd.DataFrame:
        """Backtester 인스턴스 메서드 — 모듈 레벨 함수에 위임."""
        return await fetch_history(self._exchange, symbol, timeframe, days)

    def _execute_sell(
        self, ts, current_price, holdings, avg_buy_price,
        side_label, strategy_name, confidence, reason,
    ) -> tuple[float, float, BacktestTrade]:
        """매도 실행 → (proceeds, pnl, trade)"""
        exec_price = current_price * (1 - SLIPPAGE)
        cost = holdings * exec_price
        fee = cost * TAKER_FEE
        proceeds = cost - fee

        buy_cost = avg_buy_price * holdings
        pnl = proceeds - buy_cost
        pnl_pct = pnl / buy_cost * 100 if buy_cost > 0 else 0

        t = BacktestTrade(
            timestamp=ts, side=side_label, symbol="",
            price=exec_price, quantity=holdings,
            cost=cost, fee=fee,
            strategy=strategy_name,
            confidence=confidence,
            reason=reason,
            pnl=pnl, pnl_pct=round(pnl_pct, 2),
        )
        return proceeds, pnl, t

    async def run(self, symbol: str, timeframe: str = "1h", days: int = 30) -> BacktestResult:
        """심볼 하나에 대해 백테스트를 실행한다."""
        print(f"\n{'='*60}")
        print(f"  백테스트: {symbol} | {timeframe} | {days}일")
        print(f"  전략: {', '.join(self._strategies.keys())}")
        sl_str = "동적(ATR+시장)" if self._dynamic_sl else (
            f"고정 {self._stop_loss_pct}%" if self._stop_loss_pct > 0 else "OFF")
        tp_str = f"{self._take_profit_pct}%" if self._take_profit_pct > 0 else "OFF"
        tf_str = "ON" if self._trend_filter else "OFF"
        trail_str = (f"활성 +{self._trailing_activation}% / 스탑 -{self._trailing_stop}%"
                     if self._trailing_activation > 0 else "OFF")
        aw_str = "ON" if self._adaptive_weights else "OFF"
        mm_str = "Agent(5-factor)" if self._agent_market else "Legacy(SMA+ADX)"
        asym_str = "ON" if self._asymmetric else "OFF"
        print(f"  손절: {sl_str} | 익절: {tp_str} | 최소 신뢰도: {self._min_confidence}")
        print(f"  추세 필터: {tf_str} | 트레일링: {trail_str} | 적응형 가중치: {aw_str}")
        print(f"  시장 감지: {mm_str} | 쿨다운: {self._trade_cooldown}캔들 | 비대칭: {asym_str}")
        print(f"{'='*60}")

        df = await self.fetch_history(symbol, timeframe, days)
        print(f"  데이터: {len(df)}개 캔들 ({df.index[0].date()} ~ {df.index[-1].date()})")

        # Buy & Hold 기준 (첫 캔들 종가 → 마지막 캔들 종가)
        first_close = float(df.iloc[0]["close"])
        last_close = float(df.iloc[-1]["close"])
        buy_hold_pnl_pct = (last_close - first_close) / first_close * 100

        # ── 시뮬레이션 상태 ─────────────────────────────────────
        cash = self._initial_balance
        holdings = 0.0
        avg_buy_price = 0.0
        dynamic_sl_pct = self._stop_loss_pct  # 동적 손절 (매수/시장전환 시 갱신)
        current_market_state = "sideways"
        peak_price_since_entry = 0.0      # 트레일링 스탑용
        trailing_active = False            # 트레일링 활성 여부
        trades: list[BacktestTrade] = []
        equity_curve: list[tuple] = []
        peak_equity = self._initial_balance
        max_drawdown = 0.0
        last_trade_idx = -9999

        strategy_wins   = {name: 0 for name in self._strategies}
        strategy_losses = {name: 0 for name in self._strategies}
        strategy_trades = {name: 0 for name in self._strategies}

        win_count  = 0
        loss_count = 0
        total_win_pct = 0.0
        total_loss_pct = 0.0

        market_confidence = 0.5  # 시장 상태 신뢰도

        # 적응형 가중치: 마지막 재평가 인덱스
        last_weight_eval_idx = -9999

        tf_hours = _tf_hours(timeframe)
        candles_per_day = max(1, int(24 / tf_hours))
        if self._trade_limiter:
            self._trade_limiter.min_interval_candles = BacktestTradeLimiter.calc_min_interval(timeframe)

        # ── 캔들 루프 ───────────────────────────────────────────
        rows = list(df.iterrows())
        for i, (ts, row) in enumerate(rows):
            current_price = float(row["close"])

            current_equity = cash + holdings * current_price

            # 에쿼티 곡선 / 낙폭 계산
            equity_curve.append((ts, current_equity))
            if current_equity > peak_equity:
                peak_equity = current_equity
            drawdown = (peak_equity - current_equity) / peak_equity * 100
            if drawdown > max_drawdown:
                max_drawdown = drawdown

            if i < 60:  # 지표 안정화 구간 스킵
                continue

            if self._risk_manager:
                self._risk_manager.update_equity(ts, i, current_equity, candles_per_day)
            if self._trade_limiter:
                self._trade_limiter.reset_day(i, candles_per_day)

            # ── 24캔들(≈1일)마다 시장 상태 재평가 ─────────────────
            if i - last_weight_eval_idx >= 24:
                prev_state = current_market_state
                current_market_state, market_confidence = _detect_market_state(
                    row, df, i, use_agent_scoring=self._agent_market,
                )
                if current_market_state != prev_state:
                    print(f"  [{ts.strftime('%m/%d %H:%M')}] 시장: {current_market_state} (신뢰도 {market_confidence:.0%})")
                if self._adaptive_weights:
                    new_weights = _get_adaptive_weights(current_market_state, list(self._strategies.keys()))
                    self._combiner.update_weights(new_weights, source="backtest")
                # 보유 중이면 동적 손절도 시장 상태에 맞게 갱신
                if self._dynamic_sl and holdings > 0:
                    dynamic_sl_pct = _calc_dynamic_sl(row, current_price, current_market_state)
                last_weight_eval_idx = i

            # ── 손절/익절/트레일링 체크 (보유 중일 때) ────────────
            if holdings > 0 and avg_buy_price > 0:
                unrealized_pct = (current_price - avg_buy_price) / avg_buy_price * 100

                # 트레일링 스탑: 고점 추적
                if current_price > peak_price_since_entry:
                    peak_price_since_entry = current_price

                # 비대칭 트레일링: 상승장이면 넓게, 하락장이면 타이트하게
                if self._asymmetric:
                    _asym_trail = {
                        "strong_uptrend": (5.0, 4.0),  # 활성 5%, 스탑 4%
                        "uptrend":        (4.0, 3.5),  # 활성 4%, 스탑 3.5%
                        "sideways":       (2.5, 2.0),  # 활성 2.5%, 스탑 2%
                    }
                    eff_trail_act, eff_trail_stop = _asym_trail.get(
                        current_market_state, (self._trailing_activation, self._trailing_stop))
                else:
                    eff_trail_act = self._trailing_activation
                    eff_trail_stop = self._trailing_stop

                # 트레일링 활성화 체크
                if (eff_trail_act > 0
                        and not trailing_active
                        and unrealized_pct >= eff_trail_act):
                    trailing_active = True

                # 트레일링 스탑 발동
                if trailing_active and eff_trail_stop > 0:
                    drop_from_peak = (peak_price_since_entry - current_price) / peak_price_since_entry * 100
                    if drop_from_peak >= eff_trail_stop:
                        actual_pnl_pct = unrealized_pct
                        proceeds, pnl, t = self._execute_sell(
                            ts, current_price, holdings, avg_buy_price,
                            "sell(trail)", "trailing_stop", 0,
                            f"트레일링 스탑: 고점 {peak_price_since_entry:,.0f} 대비 "
                            f"-{drop_from_peak:.1f}% (수익 {actual_pnl_pct:+.1f}%)",
                        )
                        t.symbol = symbol
                        cash += proceeds
                        trades.append(t)
                        if pnl > 0:
                            win_count += 1
                            total_win_pct += t.pnl_pct
                        else:
                            loss_count += 1
                            total_loss_pct += abs(t.pnl_pct)
                        holdings = 0
                        avg_buy_price = 0
                        peak_price_since_entry = 0
                        trailing_active = False
                        last_trade_idx = i
                        continue

                # 손절 (동적 ATR 또는 고정 %)
                if dynamic_sl_pct > 0 and unrealized_pct <= -dynamic_sl_pct:
                    proceeds, pnl, t = self._execute_sell(
                        ts, current_price, holdings, avg_buy_price,
                        "sell(sl)", "stop_loss", 0,
                        f"손절: {unrealized_pct:.1f}% (한도: -{dynamic_sl_pct:.1f}%)",
                    )
                    t.symbol = symbol
                    cash += proceeds
                    trades.append(t)
                    loss_count += 1
                    total_loss_pct += abs(t.pnl_pct)
                    holdings = 0
                    avg_buy_price = 0
                    peak_price_since_entry = 0
                    trailing_active = False
                    last_trade_idx = i
                    continue

                # 익절 (트레일링 미활성 시에만)
                if (not trailing_active
                        and self._take_profit_pct > 0
                        and unrealized_pct >= self._take_profit_pct):
                    proceeds, pnl, t = self._execute_sell(
                        ts, current_price, holdings, avg_buy_price,
                        "sell(tp)", "take_profit", 0,
                        f"익절: +{unrealized_pct:.1f}% (목표: +{self._take_profit_pct}%)",
                    )
                    t.symbol = symbol
                    cash += proceeds
                    trades.append(t)
                    win_count += 1
                    total_win_pct += t.pnl_pct
                    holdings = 0
                    avg_buy_price = 0
                    peak_price_since_entry = 0
                    trailing_active = False
                    last_trade_idx = i
                    continue

            # 쿨다운: 마지막 매매로부터 최소 N캔들 후
            if i - last_trade_idx < self._trade_cooldown:
                continue

            # ── 전략 신호 수집 ─────────────────────────────────
            slice_df = df.iloc[max(0, i-200):i+1]  # 최근 200캔들만 전달 (성능)
            ticker = Ticker(
                symbol=symbol,
                last=current_price,
                bid=current_price * 0.9995,
                ask=current_price * 1.0005,
                high=float(row["high"]),
                low=float(row["low"]),
                volume=float(row.get("volume", 0)),
                timestamp=ts,
            )

            signals: list[Signal] = []
            for name, strategy in self._strategies.items():
                try:
                    sig = await strategy.analyze(slice_df.copy(), ticker)
                    signals.append(sig)
                except Exception:
                    pass

            if not signals:
                continue

            decision = self._combiner.combine(signals, market_state=current_market_state)

            # ── 글로벌 추세 필터: 하락장에서 매수 차단 ────────────
            if (self._trend_filter
                    and decision.action == SignalType.BUY
                    and _is_downtrend(row)):
                continue  # 매수 차단, 매도는 허용

            # ── 매수 ──────────────────────────────────────────
            if decision.action == SignalType.BUY and holdings == 0:
                # ── 비대칭 전략: 시장 상태별 차등 매수 기준 ──
                if self._asymmetric:
                    # 하락장 방어: crash/downtrend에서 매수 완전 차단
                    if current_market_state in ("crash", "downtrend"):
                        continue
                    # 시장 상태별 신뢰도 임계값 조정
                    _asym_conf = {
                        "strong_uptrend": max(self._min_confidence - 0.15, 0.35),
                        "uptrend":        max(self._min_confidence - 0.10, 0.40),
                        "sideways":       self._min_confidence + 0.05,
                    }
                    buy_threshold = _asym_conf.get(current_market_state, self._min_confidence)
                else:
                    # 기존 로직: 시장 신뢰도 낮으면 진입 기준 상향
                    buy_threshold = self._min_confidence
                    if market_confidence < 0.35:
                        buy_threshold = self._min_confidence + 0.10
                if decision.combined_confidence < buy_threshold:
                    continue

                # 리스크 관리자 체크
                if self._risk_manager:
                    ok, _ = self._risk_manager.can_buy(ts, symbol, cash * 0.95, {}, current_equity)
                    if not ok:
                        continue
                # 매매 제한 체크
                if self._trade_limiter:
                    ok, _ = self._trade_limiter.can_buy(symbol, i)
                    if not ok:
                        continue

                # ── 비대칭 포지션 사이징 ──
                if self._asymmetric:
                    _asym_size = {
                        "strong_uptrend": 0.95,   # 풀 사이즈
                        "uptrend":        0.80,   # 80%
                        "sideways":       0.50,   # 50% (보수적)
                    }
                    size_mult = _asym_size.get(current_market_state, 0.50)
                    trade_size = cash * size_mult
                else:
                    trade_size = cash * 0.95
                if trade_size < MIN_TRADE_KRW:
                    continue

                exec_price = current_price * (1 + SLIPPAGE)
                fee = trade_size * TAKER_FEE
                qty = (trade_size - fee) / exec_price
                cost = qty * exec_price

                cash -= (cost + fee)
                holdings = qty
                avg_buy_price = exec_price
                peak_price_since_entry = current_price
                trailing_active = False

                # 동적 손절 계산
                if self._dynamic_sl:
                    dynamic_sl_pct = _calc_dynamic_sl(row, current_price, current_market_state)
                else:
                    dynamic_sl_pct = self._stop_loss_pct

                # BUY 신호를 낸 전략 중 최고 신뢰도
                buy_signals = [s for s in signals if s.signal_type == SignalType.BUY]
                best_signal = max(buy_signals, key=lambda s: s.confidence) if buy_signals else signals[0]

                t = BacktestTrade(
                    timestamp=ts, side="buy", symbol=symbol,
                    price=exec_price, quantity=qty, cost=cost, fee=fee,
                    strategy=best_signal.strategy_name,
                    confidence=float(decision.combined_confidence),
                    reason=best_signal.reason,
                )
                trades.append(t)
                strategy_trades[best_signal.strategy_name] = strategy_trades.get(best_signal.strategy_name, 0) + 1
                last_trade_idx = i
                if self._trade_limiter:
                    self._trade_limiter.record_buy(symbol, i)

            # ── 매도 (전략 신호) ──────────────────────────────
            elif decision.action == SignalType.SELL and holdings > 0:
                proceeds, pnl, t = self._execute_sell(
                    ts, current_price, holdings, avg_buy_price,
                    "sell", "", float(decision.combined_confidence), "",
                )
                t.symbol = symbol

                sell_signals = [s for s in signals if s.signal_type == SignalType.SELL]
                best_signal = max(sell_signals, key=lambda s: s.confidence) if sell_signals else signals[0]
                t.strategy = best_signal.strategy_name
                t.reason = best_signal.reason

                cash += proceeds
                trades.append(t)
                strategy_trades[best_signal.strategy_name] = strategy_trades.get(best_signal.strategy_name, 0) + 1
                last_trade_idx = i

                if pnl > 0:
                    win_count += 1
                    total_win_pct += t.pnl_pct
                    strategy_wins[best_signal.strategy_name] = strategy_wins.get(best_signal.strategy_name, 0) + 1
                else:
                    loss_count += 1
                    total_loss_pct += abs(t.pnl_pct)
                    strategy_losses[best_signal.strategy_name] = strategy_losses.get(best_signal.strategy_name, 0) + 1

                holdings = 0
                avg_buy_price = 0
                peak_price_since_entry = 0
                trailing_active = False

        # ── 미청산 포지션 강제 청산 ────────────────────────────
        if holdings > 0:
            proceeds, pnl, t = self._execute_sell(
                df.index[-1], last_close, holdings, avg_buy_price,
                "sell(close)", "forced_close", 0, "백테스트 종료 강제 청산",
            )
            t.symbol = symbol
            cash += proceeds
            trades.append(t)
            if pnl > 0:
                win_count += 1
                total_win_pct += t.pnl_pct
            else:
                loss_count += 1
                total_loss_pct += abs(t.pnl_pct)

        final_balance = cash
        total_pnl = final_balance - self._initial_balance
        total_pnl_pct = total_pnl / self._initial_balance * 100
        total_sell_trades = len([t for t in trades if "sell" in t.side])
        win_rate = win_count / (win_count + loss_count) * 100 if (win_count + loss_count) > 0 else 0

        avg_win = total_win_pct / win_count if win_count > 0 else 0
        avg_loss = total_loss_pct / loss_count if loss_count > 0 else 0
        profit_factor = (total_win_pct / total_loss_pct) if total_loss_pct > 0 else float("inf") if total_win_pct > 0 else 0

        # 전략별 통계
        strategy_stats = {}
        for name in self._strategies:
            n = strategy_trades.get(name, 0)
            w = strategy_wins.get(name, 0)
            l = strategy_losses.get(name, 0)
            strategy_stats[name] = {
                "trades": n,
                "wins": w,
                "losses": l,
                "win_rate": round(w / (w + l) * 100, 1) if (w + l) > 0 else 0,
            }

        return BacktestResult(
            symbol=symbol,
            days=days,
            initial_balance=self._initial_balance,
            final_balance=round(final_balance, 0),
            total_pnl=round(total_pnl, 0),
            total_pnl_pct=round(total_pnl_pct, 2),
            max_drawdown_pct=round(max_drawdown, 2),
            total_trades=total_sell_trades,
            winning_trades=win_count,
            losing_trades=loss_count,
            win_rate=round(win_rate, 1),
            avg_win_pct=round(avg_win, 2),
            avg_loss_pct=round(avg_loss, 2),
            profit_factor=round(profit_factor, 2),
            buy_hold_pnl_pct=round(buy_hold_pnl_pct, 2),
            trades=trades,
            equity_curve=equity_curve,
            strategy_stats=strategy_stats,
        )


def _is_downtrend(row) -> bool:
    """SMA20 < SMA60이면 하락 추세로 판단."""
    sma20 = row.get("SMA_20")
    sma60 = row.get("SMA_60")
    if sma20 is None or sma60 is None or pd.isna(sma20) or pd.isna(sma60):
        return False
    return float(sma20) < float(sma60)


def _detect_market_state_legacy(row) -> str:
    """SMA20/SMA60 + ADX + RSI로 시장 상태 감지 (레거시 3요소).

    Returns: 'strong_uptrend', 'uptrend', 'sideways', 'downtrend', 'crash'
    """
    from core.enums import MarketState

    sma20 = row.get("SMA_20")
    sma60 = row.get("SMA_60")
    adx = row.get("ADX_14")
    rsi = row.get("RSI_14")

    # 기본값
    if any(v is None or (isinstance(v, float) and pd.isna(v))
           for v in [sma20, sma60, adx, rsi]):
        return MarketState.SIDEWAYS.value

    sma20, sma60, adx, rsi = float(sma20), float(sma60), float(adx), float(rsi)

    uptrend = sma20 > sma60
    strong_trend = adx > 25

    if uptrend and strong_trend and rsi > 55:
        return MarketState.STRONG_UPTREND.value
    elif uptrend:
        return MarketState.UPTREND.value
    elif not uptrend and strong_trend and rsi < 35:
        return MarketState.CRASH.value
    elif not uptrend and (strong_trend or rsi < 45):
        return MarketState.DOWNTREND.value
    else:
        return MarketState.SIDEWAYS.value


def _detect_market_state_v2(row, df: pd.DataFrame, i: int) -> tuple[str, float]:
    """에이전트 스타일 5요소 스코어링 시장 상태 감지.

    Factors: Price vs SMA20, SMA20/SMA50 정렬, RSI, 7일 가격변동, 거래량/SMA20.
    Returns: (state_str, confidence)
    """
    from core.enums import MarketState

    scores = {
        MarketState.STRONG_UPTREND: 0.0,
        MarketState.UPTREND: 0.0,
        MarketState.SIDEWAYS: 0.0,
        MarketState.DOWNTREND: 0.0,
    }

    current_price = float(row["close"])

    # 1. Price vs SMA20 거리
    sma20 = row.get("SMA_20")
    if sma20 is not None and not (isinstance(sma20, float) and pd.isna(sma20)):
        sma20 = float(sma20)
        if sma20 > 0:
            if current_price > sma20 * 1.05:
                scores[MarketState.STRONG_UPTREND] += 2
            elif current_price > sma20:
                scores[MarketState.UPTREND] += 1.5
            elif current_price < sma20 * 0.95:
                scores[MarketState.DOWNTREND] += 1.5
            elif current_price < sma20:
                scores[MarketState.DOWNTREND] += 1.5

    # 2. SMA20 vs SMA50 정렬
    sma50 = row.get("SMA_50")
    if (sma20 is not None and sma50 is not None
            and not (isinstance(sma20, float) and pd.isna(sma20))
            and not (isinstance(sma50, float) and pd.isna(sma50))):
        sma50_f = float(sma50)
        sma20_f = float(sma20) if not isinstance(sma20, float) else sma20
        if sma20_f > sma50_f:
            scores[MarketState.UPTREND] += 1
            scores[MarketState.STRONG_UPTREND] += 0.5
        else:
            scores[MarketState.DOWNTREND] += 1

    # 3. RSI
    rsi = row.get("RSI_14")
    if rsi is not None and not (isinstance(rsi, float) and pd.isna(rsi)):
        rsi = float(rsi)
        if rsi > 70:
            scores[MarketState.STRONG_UPTREND] += 1
        elif rsi > 55:
            scores[MarketState.UPTREND] += 1
        elif rsi < 30:
            scores[MarketState.DOWNTREND] += 1.5
        elif rsi < 45:
            scores[MarketState.DOWNTREND] += 1
        else:
            scores[MarketState.SIDEWAYS] += 1.5

    # 4. 7일 가격변동 (캔들 간격에서 자동 계산)
    if len(df) > 1:
        # 타임프레임 자동 감지: 첫 두 캔들 간격으로 추정
        td = (df.index[1] - df.index[0]).total_seconds() / 3600  # hours
        candles_per_7d = int(7 * 24 / td) if td > 0 else 42
        lookback_idx = max(0, i - candles_per_7d)
        if lookback_idx < len(df):
            week_ago_price = float(df.iloc[lookback_idx]["close"])
            if week_ago_price > 0:
                week_change_pct = (current_price - week_ago_price) / week_ago_price * 100
                if week_change_pct > 10:
                    scores[MarketState.STRONG_UPTREND] += 2
                elif week_change_pct > 3:
                    scores[MarketState.UPTREND] += 1.5
                elif week_change_pct < -10:
                    scores[MarketState.DOWNTREND] += 2
                elif week_change_pct < -3:
                    scores[MarketState.DOWNTREND] += 1.5
                else:
                    scores[MarketState.SIDEWAYS] += 2

    # 5. 거래량 / Volume_SMA_20
    vol_sma = row.get("Volume_SMA_20")
    cur_vol = row.get("volume")
    if (vol_sma is not None and cur_vol is not None
            and not (isinstance(vol_sma, float) and pd.isna(vol_sma))
            and not (isinstance(cur_vol, float) and pd.isna(cur_vol))):
        vol_sma_f = float(vol_sma)
        if vol_sma_f > 0:
            vol_ratio = float(cur_vol) / vol_sma_f
            if vol_ratio > 2.0:
                scores[MarketState.STRONG_UPTREND] += 0.5
                scores[MarketState.DOWNTREND] += 0.5

    # 최고 스코어 상태 결정
    best_state = max(scores, key=scores.get)
    total = sum(scores.values())
    confidence = scores[best_state] / total if total > 0 else 0.3

    # CRASH 매핑: downtrend + 높은 신뢰도 + 높은 raw score
    if best_state == MarketState.DOWNTREND and confidence >= 0.55 and scores[MarketState.DOWNTREND] >= 5.0:
        return MarketState.CRASH.value, round(confidence, 2)

    return best_state.value, round(confidence, 2)


def _detect_market_state(row, df=None, i: int = 0, use_agent_scoring: bool = True) -> tuple[str, float]:
    """시장 상태 감지 디스패처.

    use_agent_scoring=True (기본): 5요소 스코어링 (에이전트 방식)
    use_agent_scoring=False:       레거시 3요소 (SMA+ADX+RSI)
    Returns: (state_str, confidence)
    """
    if use_agent_scoring and df is not None:
        return _detect_market_state_v2(row, df, i)
    return _detect_market_state_legacy(row), 0.5


# 시장 상태별 적응형 가중치 프로필 (6전략 — vol_breakout/supertrend 제거)
_ADAPTIVE_WEIGHT_PROFILES = {
    "strong_uptrend": {
        "ma_crossover": 0.12, "rsi": 0.18, "macd_crossover": 0.12,
        "bollinger_rsi": 0.28, "stochastic_rsi": 0.15, "obv_divergence": 0.15,
    },
    "uptrend": {
        "ma_crossover": 0.10, "rsi": 0.22, "macd_crossover": 0.10,
        "bollinger_rsi": 0.28, "stochastic_rsi": 0.15, "obv_divergence": 0.15,
    },
    "sideways": {
        "ma_crossover": 0.05, "rsi": 0.27, "macd_crossover": 0.08,
        "bollinger_rsi": 0.32, "stochastic_rsi": 0.15, "obv_divergence": 0.13,
    },
    "downtrend": {
        "ma_crossover": 0.12, "rsi": 0.22, "macd_crossover": 0.15,
        "bollinger_rsi": 0.26, "stochastic_rsi": 0.13, "obv_divergence": 0.12,
    },
    "crash": {
        "ma_crossover": 0.10, "rsi": 0.22, "macd_crossover": 0.12,
        "bollinger_rsi": 0.28, "stochastic_rsi": 0.15, "obv_divergence": 0.13,
    },
}


def _get_adaptive_weights(market_state: str, strategy_names: list[str] | None = None) -> dict[str, float]:
    """시장 상태에 맞는 가중치 반환. strategy_names 제공 시 해당 전략만 필터+정규화."""
    profile = _ADAPTIVE_WEIGHT_PROFILES.get(market_state, DEFAULT_WEIGHTS).copy()
    if strategy_names:
        profile = {k: v for k, v in profile.items() if k in strategy_names}
        total = sum(profile.values())
        if total > 0:
            profile = {k: v / total for k, v in profile.items()}
    return profile


# ── 시장 상태별 동적 손절 프로필 (하이브리드) ─────────────────────
# (atr_multiplier, floor_pct, cap_pct)
# floor: 최소 손절폭. BTC처럼 ATR이 작은 코인이 floor에 걸림
#   → 너무 낮으면 상승장 조정(-3~5%)에서 조기 탈출
#   → crash만 타이트(3%), 나머지는 4% 이상으로 여유
_DYNAMIC_SL_PROFILES = {
    "strong_uptrend": (2.5, 4.0, 12.0),  # 넓게 — 수익 구간 오래 버팀
    "uptrend":        (2.0, 4.0, 10.0),
    "sideways":       (2.0, 4.0,  7.0),
    "downtrend":      (2.0, 4.0,  7.0),  # 조정일 수 있으므로 moderate
    "crash":          (1.5, 3.0,  5.0),  # 진짜 폭락만 타이트
}
_DEFAULT_SL_PROFILE = (2.0, 4.0, 7.0)


def _calc_dynamic_sl(row, price: float, market_state: str) -> float:
    """ATR + 시장 상태 기반 동적 손절 % 계산.

    Returns: 손절 퍼센트 (예: 5.0 → -5% 시 손절)
    """
    atr_mult, floor_pct, cap_pct = _DYNAMIC_SL_PROFILES.get(
        market_state, _DEFAULT_SL_PROFILE,
    )
    atr_val = row.get("ATRr_14")
    if atr_val is None or (isinstance(atr_val, float) and pd.isna(atr_val)) or price <= 0:
        return cap_pct  # ATR 없으면 캡으로 폴백

    atr_pct = float(atr_val) / price * 100
    raw_sl = atr_pct * atr_mult
    return max(floor_pct, min(raw_sl, cap_pct))


# ── 포트폴리오 백테스터 (PortfolioBacktester) ──────────────────────────
DEFAULT_PORTFOLIO_COINS = ["BTC/KRW", "ETH/KRW", "XRP/KRW", "SOL/KRW", "ADA/KRW"]


class PortfolioBacktester:
    """멀티코인 포트폴리오 백테스터.

    5개 코인 동시 운용 + 리스크 에이전트 + 매매 제한 시뮬레이션.
    RotationBacktester의 멀티포지션 패턴 재활용.
    """

    def __init__(
        self,
        exchange: BithumbAdapter,
        strategy_names: list[str],
        symbols: list[str] | None = None,
        initial_balance: float = 500_000,
        min_confidence: float = 0.50,
        stop_loss_pct: float = 5.0,
        take_profit_pct: float = 10.0,
        trend_filter: bool = True,
        trailing_activation: float = 3.0,
        trailing_stop: float = 3.0,
        adaptive_weights: bool = True,
        dynamic_sl: bool = False,
        agent_market: bool = True,
        trade_cooldown: int = 12,
        asymmetric: bool = False,
        max_positions: int = 5,
        max_trade_size_pct: float = 0.20,
        # 리스크 관리
        risk_enabled: bool = False,
        max_drawdown_pct: float = 0.10,
        daily_loss_limit_pct: float = 0.03,
        max_concentration_pct: float = 0.40,
        # 매매 제한
        trade_limit_enabled: bool = False,
        daily_buy_limit: int = 20,
        max_coin_buys: int = 3,
        # 전략 매도 모드
        strategy_sell_mode: str = "none",  # "none" | "voting" | "paired"
    ):
        self._exchange = exchange
        self._initial_balance = initial_balance
        self._min_confidence = min_confidence
        self._stop_loss_pct = stop_loss_pct
        self._take_profit_pct = take_profit_pct
        self._trend_filter = trend_filter
        self._trailing_activation = trailing_activation
        self._trailing_stop = trailing_stop
        self._adaptive_weights = adaptive_weights
        self._dynamic_sl = dynamic_sl
        self._agent_market = agent_market
        self._trade_cooldown = trade_cooldown
        self._asymmetric = asymmetric
        self._max_positions = max_positions
        self._max_trade_size_pct = max_trade_size_pct
        self._strategy_sell_mode = strategy_sell_mode
        self._symbols = symbols or DEFAULT_PORTFOLIO_COINS

        # 리스크 관리자
        self._risk_manager = BacktestRiskManager(
            max_drawdown_pct=max_drawdown_pct,
            daily_loss_limit_pct=daily_loss_limit_pct,
            max_concentration_pct=max_concentration_pct,
            enabled=risk_enabled,
        ) if risk_enabled else None

        # 매매 제한
        self._trade_limiter: BacktestTradeLimiter | None = None
        if trade_limit_enabled:
            self._trade_limiter = BacktestTradeLimiter(
                daily_buy_limit=daily_buy_limit,
                max_coin_buys=max_coin_buys,
                enabled=True,
            )

        # 전략 로드
        all_strats = StrategyRegistry.create_all()
        self._strategies = {
            name: strat for name, strat in all_strats.items()
            if name in strategy_names
        }
        if set(strategy_names) <= set(WEIGHTS_5.keys()):
            base_weights = WEIGHTS_5
        elif set(strategy_names) <= set(WEIGHTS_6.keys()):
            base_weights = WEIGHTS_6
        elif set(strategy_names) <= set(WEIGHTS_7.keys()):
            base_weights = WEIGHTS_7
        elif set(strategy_names) <= set(WEIGHTS_8.keys()):
            base_weights = WEIGHTS_8
        else:
            base_weights = {name: 1.0 / len(strategy_names) for name in strategy_names}
        weights = {k: v for k, v in base_weights.items() if k in strategy_names}
        total_w = sum(weights.values())
        if total_w > 0:
            weights = {k: v / total_w for k, v in weights.items()}
        self._combiner = SignalCombiner(
            strategy_weights=weights,
            min_confidence=min_confidence,
        )

    async def prefetch_all(
        self, timeframe: str, days: int,
    ) -> dict[str, pd.DataFrame]:
        """전 코인 + BTC 레퍼런스 데이터 프리페치."""
        all_data: dict[str, pd.DataFrame] = {}
        # BTC 먼저 (시장 상태용) — USDT/KRW 자동 판별
        btc_ref = "BTC/USDT" if any(s.endswith("/USDT") for s in self._symbols) else "BTC/KRW"
        all_syms = list(dict.fromkeys([btc_ref] + list(self._symbols)))
        total = len(all_syms)
        for idx, sym in enumerate(all_syms, 1):
            try:
                print(f"  [{idx}/{total}] {sym} 데이터 로딩...", end="", flush=True)
                df = await fetch_history(self._exchange, sym, timeframe, days)
                all_data[sym] = df
                print(f" {len(df)}캔들")
            except Exception as e:
                print(f" 실패({e})")
        return all_data

    def _execute_sell(
        self, ts, current_price, pos: PortfolioPositionState,
        side_label: str, strategy_name: str, confidence: float, reason: str,
    ) -> tuple[float, float, float, BacktestTrade]:
        """매도 실행 → (proceeds, pnl, pnl_pct, trade)"""
        exec_price = current_price * (1 - SLIPPAGE)
        cost = pos.quantity * exec_price
        fee = cost * TAKER_FEE
        proceeds = cost - fee

        buy_cost = pos.avg_price * pos.quantity
        pnl = proceeds - buy_cost
        pnl_pct = pnl / buy_cost * 100 if buy_cost > 0 else 0

        t = BacktestTrade(
            timestamp=ts, side=side_label, symbol=pos.symbol,
            price=exec_price, quantity=pos.quantity,
            cost=cost, fee=fee,
            strategy=strategy_name,
            confidence=confidence,
            reason=reason,
            pnl=pnl, pnl_pct=round(pnl_pct, 2),
        )
        return proceeds, pnl, pnl_pct, t

    async def run(self, timeframe: str = "1h", days: int = 30) -> PortfolioBacktestResult:
        """멀티코인 포트폴리오 백테스트 실행."""
        tf_hours = _tf_hours(timeframe)
        candles_per_day = max(1, int(24 / tf_hours))

        print(f"\n{'='*60}")
        print(f"  포트폴리오 백테스트 | {timeframe} | {days}일")
        print(f"  코인: {', '.join(self._symbols)}")
        print(f"  전략: {', '.join(self._strategies.keys())}")
        sl_str = "동적(ATR+시장)" if self._dynamic_sl else (
            f"고정 {self._stop_loss_pct}%" if self._stop_loss_pct > 0 else "OFF")
        tp_str = f"{self._take_profit_pct}%" if self._take_profit_pct > 0 else "OFF"
        trail_str = (f"활성 +{self._trailing_activation}% / 스탑 -{self._trailing_stop}%"
                     if self._trailing_activation > 0 else "OFF")
        asym_str = "ON" if self._asymmetric else "OFF"
        risk_str = "ON" if self._risk_manager else "OFF"
        limit_str = "ON" if self._trade_limiter else "OFF"
        print(f"  최대 동시 포지션: {self._max_positions} | 코인당 자금: {self._max_trade_size_pct*100:.0f}%")
        print(f"  손절: {sl_str} | 익절: {tp_str} | 트레일링: {trail_str}")
        sell_mode_str = {"none": "OFF", "voting": "투표", "paired": "페어링"}.get(self._strategy_sell_mode, "OFF")
        print(f"  비대칭: {asym_str} | 쿨다운: {self._trade_cooldown}캔들 | 전략매도: {sell_mode_str}")
        print(f"  리스크 관리: {risk_str} | 매매 제한: {limit_str}")
        print(f"{'='*60}")

        # 1. 데이터 프리페치
        all_data = await self.prefetch_all(timeframe, days)
        if not all_data:
            raise ValueError("사용 가능한 코인 데이터 없음")
        print(f"\n  {len(all_data)}개 코인 로딩 완료")

        # trade limiter 쿨다운 자동 계산
        if self._trade_limiter:
            self._trade_limiter.min_interval_candles = BacktestTradeLimiter.calc_min_interval(timeframe)

        # BTC B&H + 균등배분 B&H
        btc_ref = "BTC/USDT" if any(s.endswith("/USDT") for s in self._symbols) else "BTC/KRW"
        btc_df = all_data.get(btc_ref)
        portfolio_syms = [s for s in self._symbols if s in all_data]

        # 2. 유니온 타임스탬프
        all_timestamps = sorted(set().union(*(df.index for df in all_data.values())))
        print(f"  타임라인: {len(all_timestamps)}개 캔들 ({all_timestamps[0].date()} ~ {all_timestamps[-1].date()})")

        # 3. 초기화
        cash = self._initial_balance
        positions: dict[str, PortfolioPositionState] = {}
        current_market_state = "sideways"
        market_confidence = 0.5

        trades: list[BacktestTrade] = []
        equity_curve: list[tuple] = []
        peak_equity = self._initial_balance
        max_drawdown = 0.0
        last_weight_eval_idx = -9999
        last_trade_idx_per_coin: dict[str, int] = {}

        strategy_wins: dict[str, int] = {name: 0 for name in self._strategies}
        strategy_losses: dict[str, int] = {name: 0 for name in self._strategies}
        strategy_trades: dict[str, int] = {name: 0 for name in self._strategies}

        # 코인별 통계
        coin_stats: dict[str, dict] = {sym: {"wins": 0, "losses": 0, "trades": 0, "pnl": 0.0} for sym in portfolio_syms}

        win_count = 0
        loss_count = 0
        total_win_pct = 0.0
        total_loss_pct = 0.0

        # 4. 캔들 루프
        for candle_idx, ts in enumerate(all_timestamps):
            # 4a. 에쿼티 계산
            equity = cash
            position_values: dict[str, float] = {}
            for sym, pos in positions.items():
                if sym in all_data and ts in all_data[sym].index:
                    val = pos.quantity * float(all_data[sym].loc[ts, "close"])
                else:
                    val = pos.quantity * pos.avg_price
                position_values[sym] = val
                equity += val

            # 4b. 에쿼티 곡선/낙폭
            equity_curve.append((ts, equity))
            if equity > peak_equity:
                peak_equity = equity
            drawdown = (peak_equity - equity) / peak_equity * 100
            if drawdown > max_drawdown:
                max_drawdown = drawdown

            if candle_idx < 60:
                continue

            # 4c. 리스크 관리자 업데이트
            if self._risk_manager:
                self._risk_manager.update_equity(ts, candle_idx, equity, candles_per_day)

            # 4d. 매매 제한 일별 리셋
            if self._trade_limiter:
                self._trade_limiter.reset_day(candle_idx, candles_per_day)

            # 4e. 24캔들마다 시장 상태 재평가 (BTC 기준)
            if candle_idx - last_weight_eval_idx >= 24:
                if btc_df is not None and ts in btc_df.index:
                    prev_state = current_market_state
                    btc_iloc = btc_df.index.get_loc(ts)
                    if isinstance(btc_iloc, slice):
                        btc_iloc = btc_iloc.start
                    current_market_state, market_confidence = _detect_market_state(
                        btc_df.loc[ts], btc_df, btc_iloc,
                        use_agent_scoring=self._agent_market,
                    )
                    if current_market_state != prev_state:
                        print(f"  [{ts.strftime('%m/%d %H:%M')}] 시장: {current_market_state} (신뢰도 {market_confidence:.0%})")
                    if self._adaptive_weights:
                        new_weights = _get_adaptive_weights(current_market_state, list(self._strategies.keys()))
                        self._combiner.update_weights(new_weights, source="backtest")
                last_weight_eval_idx = candle_idx

            # 4f. 보유 포지션 SL/TP/트레일링 체크
            to_close: list[str] = []
            for sym, pos in positions.items():
                if sym not in all_data or ts not in all_data[sym].index:
                    continue
                cur_price = float(all_data[sym].loc[ts, "close"])
                unrealized_pct = (cur_price - pos.avg_price) / pos.avg_price * 100

                if cur_price > pos.peak_price:
                    pos.peak_price = cur_price

                sell_tag = None
                sell_text = None

                # 비대칭 트레일링
                if self._asymmetric:
                    _asym_trail = {
                        "strong_uptrend": (5.0, 4.0), "uptrend": (4.0, 3.5), "sideways": (2.5, 2.0),
                    }
                    eff_trail_act, eff_trail_stop = _asym_trail.get(
                        current_market_state, (self._trailing_activation, self._trailing_stop))
                else:
                    eff_trail_act = self._trailing_activation
                    eff_trail_stop = self._trailing_stop

                # 트레일링 활성화
                if (eff_trail_act > 0 and not pos.trailing_active and unrealized_pct >= eff_trail_act):
                    pos.trailing_active = True

                # 트레일링 스탑
                if pos.trailing_active and eff_trail_stop > 0:
                    drop = (pos.peak_price - cur_price) / pos.peak_price * 100
                    if drop >= eff_trail_stop:
                        sell_tag = "sell(trail)"
                        sell_text = f"트레일링 ({sym}) 고점 대비 -{drop:.1f}% (수익 {unrealized_pct:+.1f}%)"

                # 손절
                if not sell_tag and pos.dynamic_sl_pct > 0 and unrealized_pct <= -pos.dynamic_sl_pct:
                    sell_tag = "sell(sl)"
                    sell_text = f"손절 ({sym}) {unrealized_pct:.1f}% (한도 -{pos.dynamic_sl_pct:.1f}%)"

                # 익절 (트레일링 미활성 시)
                if (not sell_tag and not pos.trailing_active
                        and self._take_profit_pct > 0 and unrealized_pct >= self._take_profit_pct):
                    sell_tag = "sell(tp)"
                    sell_text = f"익절 ({sym}) +{unrealized_pct:.1f}%"

                if sell_tag:
                    proceeds, pnl, pnl_pct, t = self._execute_sell(
                        ts, cur_price, pos, sell_tag, pos.entry_strategy, 0, sell_text,
                    )
                    cash += proceeds
                    trades.append(t)
                    coin_stats[sym]["trades"] += 1
                    coin_stats[sym]["pnl"] += pnl
                    if pnl > 0:
                        win_count += 1
                        total_win_pct += abs(pnl_pct)
                        coin_stats[sym]["wins"] += 1
                    else:
                        loss_count += 1
                        total_loss_pct += abs(pnl_pct)
                        coin_stats[sym]["losses"] += 1
                    to_close.append(sym)

            for sym in to_close:
                del positions[sym]

            # 4f-2. 전략 신호 기반 매도 (voting/paired 모드)
            if self._strategy_sell_mode != "none":
                strat_sell_list: list[str] = []
                for sym, pos in positions.items():
                    if sym not in all_data or ts not in all_data[sym].index:
                        continue
                    sym_df = all_data[sym]
                    sym_iloc = sym_df.index.get_loc(ts)
                    if isinstance(sym_iloc, slice):
                        sym_iloc = sym_iloc.start
                    cur_price = float(sym_df.loc[ts, "close"])
                    slice_df = sym_df.iloc[max(0, sym_iloc - 200):sym_iloc + 1]
                    row = sym_df.iloc[sym_iloc]
                    ticker = Ticker(
                        symbol=sym, last=cur_price,
                        bid=cur_price * 0.9995, ask=cur_price * 1.0005,
                        high=float(row["high"]), low=float(row["low"]),
                        volume=float(row.get("volume", 0)), timestamp=ts,
                    )

                    should_sell = False
                    sell_strategy = ""
                    sell_confidence = 0.0
                    sell_reason = ""

                    if self._strategy_sell_mode == "paired":
                        entry_strat = self._strategies.get(pos.entry_strategy)
                        if entry_strat is None:
                            continue
                        try:
                            sig = await entry_strat.analyze(slice_df.copy(), ticker)
                            if sig.signal_type == SignalType.SELL:
                                should_sell = True
                                sell_strategy = pos.entry_strategy
                                sell_confidence = sig.confidence
                                sell_reason = f"페어링 매도 ({sym}) {sig.reason}"
                        except Exception:
                            pass

                    elif self._strategy_sell_mode == "voting":
                        signals: list[Signal] = []
                        for name, strategy in self._strategies.items():
                            try:
                                sig = await strategy.analyze(slice_df.copy(), ticker)
                                signals.append(sig)
                            except Exception:
                                pass
                        if signals:
                            decision = self._combiner.combine(signals, market_state=current_market_state)
                            if decision.action == SignalType.SELL:
                                sell_sigs = [s for s in signals if s.signal_type == SignalType.SELL]
                                best_sig = max(sell_sigs, key=lambda s: s.confidence) if sell_sigs else signals[0]
                                should_sell = True
                                sell_strategy = best_sig.strategy_name
                                sell_confidence = float(decision.combined_confidence)
                                sell_reason = f"투표 매도 ({sym}) {best_sig.reason}"

                    if should_sell:
                        proceeds, pnl, pnl_pct, t = self._execute_sell(
                            ts, cur_price, pos, "sell(strat)", sell_strategy,
                            sell_confidence, sell_reason,
                        )
                        cash += proceeds
                        trades.append(t)
                        coin_stats[sym]["trades"] += 1
                        coin_stats[sym]["pnl"] += pnl
                        if pnl > 0:
                            win_count += 1
                            total_win_pct += abs(pnl_pct)
                            coin_stats[sym]["wins"] += 1
                        else:
                            loss_count += 1
                            total_loss_pct += abs(pnl_pct)
                            coin_stats[sym]["losses"] += 1
                        strat_sell_list.append(sym)

                for sym in strat_sell_list:
                    del positions[sym]

            # 4g. 미보유 코인 순회: 전략 시그널 → 매수 후보 수집
            buy_candidates: list[tuple[str, float, Signal, object]] = []  # (sym, confidence, best_signal, decision)

            for sym in portfolio_syms:
                if sym in positions:
                    continue  # 이미 보유 중
                if sym not in all_data or ts not in all_data[sym].index:
                    continue
                if len(positions) >= self._max_positions:
                    break  # 최대 포지션 도달

                # 코인별 쿨다운
                last_idx = last_trade_idx_per_coin.get(sym, -9999)
                if candle_idx - last_idx < self._trade_cooldown:
                    continue

                sym_df = all_data[sym]
                sym_iloc = sym_df.index.get_loc(ts)
                if isinstance(sym_iloc, slice):
                    sym_iloc = sym_iloc.start

                row = sym_df.iloc[sym_iloc]
                cur_price = float(row["close"])

                # 추세 필터
                if self._trend_filter and _is_downtrend(row):
                    continue

                # 전략 신호 수집
                slice_df = sym_df.iloc[max(0, sym_iloc - 200):sym_iloc + 1]
                ticker = Ticker(
                    symbol=sym, last=cur_price,
                    bid=cur_price * 0.9995, ask=cur_price * 1.0005,
                    high=float(row["high"]), low=float(row["low"]),
                    volume=float(row.get("volume", 0)), timestamp=ts,
                )

                signals: list[Signal] = []
                for name, strategy in self._strategies.items():
                    try:
                        sig = await strategy.analyze(slice_df.copy(), ticker)
                        signals.append(sig)
                    except Exception:
                        pass

                if not signals:
                    continue

                decision = self._combiner.combine(signals, market_state=current_market_state)
                if decision.action != SignalType.BUY:
                    continue

                # 비대칭 전략
                if self._asymmetric:
                    if current_market_state in ("crash", "downtrend"):
                        continue
                    _asym_conf = {
                        "strong_uptrend": max(self._min_confidence - 0.15, 0.35),
                        "uptrend": max(self._min_confidence - 0.10, 0.40),
                        "sideways": self._min_confidence + 0.05,
                    }
                    buy_threshold = _asym_conf.get(current_market_state, self._min_confidence)
                else:
                    buy_threshold = self._min_confidence
                    if market_confidence < 0.35:
                        buy_threshold = self._min_confidence + 0.10

                if decision.combined_confidence < buy_threshold:
                    continue

                buy_signals = [s for s in signals if s.signal_type == SignalType.BUY]
                best_signal = max(buy_signals, key=lambda s: s.confidence) if buy_signals else signals[0]

                buy_candidates.append((sym, float(decision.combined_confidence), best_signal, decision))

            # 신뢰도 내림차순 정렬 (현금 경쟁 시 강한 시그널 우선)
            buy_candidates.sort(key=lambda x: x[1], reverse=True)

            for sym, conf, best_signal, decision in buy_candidates:
                if len(positions) >= self._max_positions:
                    break

                # 리스크 관리자 체크
                if self._risk_manager:
                    # 포지션 가치 재계산 (매수 완료된 것 포함)
                    cur_pos_values = {}
                    for s, p in positions.items():
                        if s in all_data and ts in all_data[s].index:
                            cur_pos_values[s] = p.quantity * float(all_data[s].loc[ts, "close"])
                        else:
                            cur_pos_values[s] = p.quantity * p.avg_price
                    cur_equity = cash + sum(cur_pos_values.values())
                    buy_value = cash * self._max_trade_size_pct
                    ok, reason = self._risk_manager.can_buy(ts, sym, buy_value, cur_pos_values, cur_equity)
                    if not ok:
                        continue

                # 매매 제한 체크
                if self._trade_limiter:
                    ok, reason = self._trade_limiter.can_buy(sym, candle_idx)
                    if not ok:
                        continue

                # 포지션 사이징
                if self._asymmetric:
                    _asym_size = {
                        "strong_uptrend": 0.95, "uptrend": 0.80, "sideways": 0.50,
                    }
                    size_mult = _asym_size.get(current_market_state, 0.50)
                    trade_size = min(cash * self._max_trade_size_pct, cash * size_mult)
                else:
                    trade_size = cash * self._max_trade_size_pct

                if trade_size < MIN_TRADE_KRW:
                    continue

                cur_price = float(all_data[sym].loc[ts, "close"])
                exec_price = cur_price * (1 + SLIPPAGE)
                fee = trade_size * TAKER_FEE
                qty = (trade_size - fee) / exec_price
                cost = qty * exec_price

                cash -= (cost + fee)

                # 동적 손절
                row = all_data[sym].loc[ts]
                if self._dynamic_sl:
                    dyn_sl = _calc_dynamic_sl(row, cur_price, current_market_state)
                else:
                    dyn_sl = self._stop_loss_pct

                positions[sym] = PortfolioPositionState(
                    symbol=sym, quantity=qty, avg_price=exec_price,
                    entry_idx=candle_idx, peak_price=cur_price,
                    dynamic_sl_pct=dyn_sl, entry_strategy=best_signal.strategy_name,
                )

                t = BacktestTrade(
                    timestamp=ts, side="buy", symbol=sym,
                    price=exec_price, quantity=qty, cost=cost, fee=fee,
                    strategy=best_signal.strategy_name,
                    confidence=conf,
                    reason=best_signal.reason,
                )
                trades.append(t)
                strategy_trades[best_signal.strategy_name] = strategy_trades.get(best_signal.strategy_name, 0) + 1
                last_trade_idx_per_coin[sym] = candle_idx

                if self._trade_limiter:
                    self._trade_limiter.record_buy(sym, candle_idx)

        # 5. 잔여 포지션 강제 청산
        for sym, pos in list(positions.items()):
            if sym in all_data:
                last_price = float(all_data[sym].iloc[-1]["close"])
            else:
                last_price = pos.avg_price
            proceeds, pnl, pnl_pct, t = self._execute_sell(
                all_timestamps[-1], last_price, pos,
                "sell(close)", "forced_close", 0,
                f"백테스트 종료 강제 청산 ({sym})",
            )
            cash += proceeds
            trades.append(t)
            coin_stats[sym]["trades"] += 1
            coin_stats[sym]["pnl"] += pnl
            if pnl > 0:
                win_count += 1
                total_win_pct += abs(pnl_pct)
                coin_stats[sym]["wins"] += 1
            else:
                loss_count += 1
                total_loss_pct += abs(pnl_pct)
                coin_stats[sym]["losses"] += 1

        # 6. 통계 집계
        final_balance = cash
        total_pnl = final_balance - self._initial_balance
        total_pnl_pct = total_pnl / self._initial_balance * 100
        total_sell_trades = len([t for t in trades if "sell" in t.side])
        win_rate = win_count / (win_count + loss_count) * 100 if (win_count + loss_count) > 0 else 0
        avg_win = total_win_pct / win_count if win_count > 0 else 0
        avg_loss = total_loss_pct / loss_count if loss_count > 0 else 0
        profit_factor = (total_win_pct / total_loss_pct) if total_loss_pct > 0 else float("inf") if total_win_pct > 0 else 0

        # B&H 균등배분
        bh_pnl_pct = 0.0
        bh_count = 0
        for sym in portfolio_syms:
            if sym in all_data and len(all_data[sym]) > 1:
                df = all_data[sym]
                first_c = float(df.iloc[0]["close"])
                last_c = float(df.iloc[-1]["close"])
                bh_pnl_pct += (last_c - first_c) / first_c * 100
                bh_count += 1
        if bh_count > 0:
            bh_pnl_pct /= bh_count

        # 전략별 통계
        strategy_stats = {}
        for name in self._strategies:
            n = strategy_trades.get(name, 0)
            w = strategy_wins.get(name, 0)
            l = strategy_losses.get(name, 0)
            strategy_stats[name] = {
                "trades": n, "wins": w, "losses": l,
                "win_rate": round(w / (w + l) * 100, 1) if (w + l) > 0 else 0,
            }

        # 코인별 승률 계산
        for sym in coin_stats:
            cs = coin_stats[sym]
            wl = cs["wins"] + cs["losses"]
            cs["win_rate"] = round(cs["wins"] / wl * 100, 1) if wl > 0 else 0

        return PortfolioBacktestResult(
            symbols=portfolio_syms,
            days=days,
            initial_balance=self._initial_balance,
            final_balance=round(final_balance, 0),
            total_pnl=round(total_pnl, 0),
            total_pnl_pct=round(total_pnl_pct, 2),
            max_drawdown_pct=round(max_drawdown, 2),
            total_trades=total_sell_trades,
            winning_trades=win_count,
            losing_trades=loss_count,
            win_rate=round(win_rate, 1),
            avg_win_pct=round(avg_win, 2),
            avg_loss_pct=round(avg_loss, 2),
            profit_factor=round(profit_factor, 2),
            buy_hold_pnl_pct=round(bh_pnl_pct, 2),
            trades=trades,
            equity_curve=equity_curve,
            strategy_stats=strategy_stats,
            per_coin_stats=coin_stats,
            risk_events=self._risk_manager.events if self._risk_manager else [],
            risk_stats=self._risk_manager.stats if self._risk_manager else {},
            trade_limit_stats=self._trade_limiter.stats if self._trade_limiter else {},
        )


# ── 선물 백테스트 (FuturesBacktester) ──────────────────────────────

FUTURES_FEE = 0.0004   # 0.04% 바이낸스 선물 수수료
FUNDING_RATE = 0.0001  # 0.01% 8시간 펀딩비 (기본값)


@dataclass
class FuturesPositionState:
    side: str            # "long" / "short"
    entry_price: float
    quantity: float      # 레버리지 반영 수량
    leverage: int
    margin: float        # 격리 마진 (실제 투입 현금)
    peak_price: float    # 트레일링용 (롱=최고, 숏=최저)
    trailing_active: bool = False


@dataclass
class FuturesBacktestResult:
    symbol: str
    days: int
    leverage: int
    initial_balance: float
    final_balance: float
    total_pnl: float
    total_pnl_pct: float
    max_drawdown_pct: float
    total_trades: int
    long_trades: int
    short_trades: int
    long_wins: int
    long_losses: int
    short_wins: int
    short_losses: int
    win_rate: float
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    profit_factor: float = 0.0
    buy_hold_pnl_pct: float = 0.0
    liquidations: int = 0
    total_funding: float = 0.0
    total_fees: float = 0.0
    long_pnl: float = 0.0
    short_pnl: float = 0.0
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[tuple] = field(default_factory=list)
    strategy_stats: dict = field(default_factory=dict)


class FuturesBacktester:
    """선물 백테스터 — 롱/숏 양방향 + 레버리지 + 청산 시뮬레이션.

    선물 전용 튜닝:
    - 숏 진입: downtrend/crash 시장에서만 허용 (--short-all로 해제)
    - 레버리지 적응형 SL: base_sl / sqrt(leverage)
    - 레버리지별 포지션 축소: position_pct / sqrt(leverage)
    - 쿨다운 기본 6캔들 (현물 12 대비 축소)
    """

    # 숏 진입 허용 시장 상태
    SHORT_ALLOWED_STATES = {"downtrend", "crash"}

    def __init__(
        self,
        exchange,
        strategy_names: list[str],
        initial_balance: float = 10_000,     # USDT
        min_confidence: float = 0.50,
        stop_loss_pct: float = 5.0,
        take_profit_pct: float = 10.0,
        trend_filter: bool = True,
        trailing_activation: float = 3.0,
        trailing_stop: float = 3.0,
        adaptive_weights: bool = True,
        dynamic_sl: bool = False,
        agent_market: bool = True,
        trade_cooldown: int = 6,
        leverage: int = 3,
        futures_fee: float = FUTURES_FEE,
        funding_rate: float = FUNDING_RATE,
        position_pct: float = 0.30,
        short_all: bool = False,             # 모든 시장에서 숏 허용
        short_sideways: bool = False,        # sideways+downtrend+crash에서 숏 허용
        dynamic_position: bool = False,      # 시장 상태별 동적 포지션 사이징
        dual_timeframe: bool = False,        # 듀얼 타임프레임 (4h+1h)
        directional_weights: bool = False,   # 방향별 가중치 (롱=추세, 숏=평균회귀)
        risk_enabled: bool = False,
        trade_limit_enabled: bool = False,
        risk_max_drawdown: float = 0.10,
        risk_daily_loss: float = 0.03,
        risk_max_concentration: float = 0.40,
        trade_daily_buy_limit: int = 20,
        trade_max_coin_buys: int = 3,
    ):
        self._exchange = exchange
        self._initial_balance = initial_balance
        self._directional_weights = directional_weights
        self._min_confidence = min_confidence
        self._stop_loss_pct = stop_loss_pct
        self._take_profit_pct = take_profit_pct
        self._trend_filter = trend_filter
        self._trailing_activation = trailing_activation
        self._trailing_stop = trailing_stop
        self._adaptive_weights = adaptive_weights
        self._dynamic_sl = dynamic_sl
        self._agent_market = agent_market
        self._trade_cooldown = trade_cooldown
        self._leverage = leverage
        self._futures_fee = futures_fee
        self._funding_rate = funding_rate
        self._short_all = short_all
        self._short_sideways = short_sideways
        self._dynamic_position = dynamic_position
        self._dual_timeframe = dual_timeframe
        self._base_position_pct = position_pct  # 동적 사이징 기준값

        self._risk_manager = BacktestRiskManager(
            max_drawdown_pct=risk_max_drawdown,
            daily_loss_limit_pct=risk_daily_loss,
            max_concentration_pct=risk_max_concentration,
            enabled=risk_enabled,
        ) if risk_enabled else None

        self._trade_limiter = BacktestTradeLimiter(
            daily_buy_limit=trade_daily_buy_limit,
            max_coin_buys=trade_max_coin_buys,
            enabled=trade_limit_enabled,
        ) if trade_limit_enabled else None

        # ── 레버리지 적응형 파라미터 ──────────────────────────
        import math
        lev_sqrt = math.sqrt(leverage)
        # 포지션 사이즈: 고배율 → 자동 축소
        self._position_pct = position_pct / lev_sqrt
        # SL/TP: 마진 대비 %이므로 레버리지 반영 불필요 — 가격 변동폭만 축소
        self._effective_sl = stop_loss_pct / lev_sqrt
        self._effective_tp = take_profit_pct / lev_sqrt
        # 트레일링도 레버리지 반영
        self._effective_trail_act = trailing_activation / lev_sqrt if trailing_activation > 0 else 0
        self._effective_trail_stop = trailing_stop / lev_sqrt if trailing_stop > 0 else 0

        all_strats = StrategyRegistry.create_all()
        self._strategies = {
            name: strat for name, strat in all_strats.items()
            if name in strategy_names
        }
        if set(strategy_names) <= set(WEIGHTS_5.keys()):
            base_weights = WEIGHTS_5
        elif set(strategy_names) <= set(WEIGHTS_6.keys()):
            base_weights = WEIGHTS_6
        elif set(strategy_names) <= set(WEIGHTS_7.keys()):
            base_weights = WEIGHTS_7
        elif set(strategy_names) <= set(WEIGHTS_8.keys()):
            base_weights = WEIGHTS_8
        else:
            base_weights = {name: 1.0 / len(strategy_names) for name in strategy_names}
        weights = {k: v for k, v in base_weights.items() if k in strategy_names}
        total_w = sum(weights.values())
        if total_w > 0:
            weights = {k: v / total_w for k, v in weights.items()}
        self._combiner = SignalCombiner(
            strategy_weights=weights,
            min_confidence=min_confidence,
            directional_weights=self._directional_weights,
        )

    async def fetch_history(
        self, symbol: str, timeframe: str, days: int
    ) -> pd.DataFrame:
        return await fetch_history(self._exchange, symbol, timeframe, days)

    def _calc_liquidation_price(self, side: str, entry: float) -> float:
        """격리 마진 청산가격 계산."""
        lev = self._leverage
        fee = self._futures_fee
        if side == "long":
            return entry * (1 - 1 / lev + fee)
        else:  # short
            return entry * (1 + 1 / lev - fee)

    def _calc_unrealized_pnl(self, side: str, entry: float, current: float, qty: float) -> float:
        """미실현 PnL 계산."""
        if side == "long":
            return (current - entry) * qty
        else:
            return (entry - current) * qty

    def _execute_futures_close(
        self, ts, pos: FuturesPositionState, current_price: float,
        side_label: str, strategy_name: str, confidence: float, reason: str,
    ) -> tuple[float, float, float, BacktestTrade]:
        """포지션 청산 → (pnl, fee, pnl_pct, trade)"""
        exec_price = current_price  # 선물은 슬리피지 미적용 (유동성 풍부)
        pnl = self._calc_unrealized_pnl(pos.side, pos.entry_price, exec_price, pos.quantity)
        fee = abs(pos.quantity * exec_price) * self._futures_fee
        net_pnl = pnl - fee

        pnl_pct = net_pnl / pos.margin * 100 if pos.margin > 0 else 0

        t = BacktestTrade(
            timestamp=ts, side=side_label, symbol="",
            price=exec_price, quantity=pos.quantity,
            cost=pos.quantity * exec_price, fee=fee,
            strategy=strategy_name,
            confidence=confidence,
            reason=reason,
            pnl=net_pnl, pnl_pct=round(pnl_pct, 2),
        )
        return net_pnl, fee, pnl_pct, t

    async def run(self, symbol: str, timeframe: str = "4h", days: int = 180) -> FuturesBacktestResult:
        """선물 백테스트 실행."""
        tf_hours = _tf_hours(timeframe)
        candles_per_8h = max(1, int(8 / tf_hours))

        print(f"\n{'='*60}")
        print(f"  선물 백테스트: {symbol} | {timeframe} | {days}일")
        print(f"  전략: {', '.join(self._strategies.keys())}")
        print(f"  레버리지: {self._leverage}x | 수수료: {self._futures_fee*100:.2f}%")
        dpos_str = f"동적(기본 {self._base_position_pct*100:.0f}%)" if self._dynamic_position else f"고정 {self._position_pct*100:.1f}%"
        print(f"  펀딩비: {self._funding_rate*100:.3f}%/8h | 포지션: {dpos_str}")
        sl_str = "동적(ATR+시장)" if self._dynamic_sl else (
            f"고정 {self._effective_sl:.1f}%" if self._effective_sl > 0 else "OFF")
        tp_str = f"{self._effective_tp:.1f}%" if self._effective_tp > 0 else "OFF"
        trail_str = (f"활성 +{self._effective_trail_act:.1f}% / 스탑 -{self._effective_trail_stop:.1f}%"
                     if self._effective_trail_act > 0 else "OFF")
        print(f"  손절: {sl_str} | 익절: {tp_str} | 트레일링: {trail_str}")
        short_str = "전체" if self._short_all else ("sideways+downtrend/crash" if self._short_sideways else "downtrend/crash만")
        print(f"  숏 허용: {short_str} | 쿨다운: {self._trade_cooldown}캔들 | 최소 신뢰도: {self._min_confidence}")
        print(f"{'='*60}")

        df = await self.fetch_history(symbol, timeframe, days)
        print(f"  데이터: {len(df)}개 캔들 ({df.index[0].date()} ~ {df.index[-1].date()})")

        # 듀얼 타임프레임: 빠른 TF 데이터 추가 fetch
        df_fast: pd.DataFrame | None = None
        if self._dual_timeframe:
            _FAST_TF_MAP = {"4h": "1h", "1d": "4h", "1h": "15m"}
            fast_tf = _FAST_TF_MAP.get(timeframe)
            if fast_tf:
                print(f"  듀얼 TF: {timeframe}(장기) + {fast_tf}(단기) 로딩...", end="", flush=True)
                df_fast = await self.fetch_history(symbol, fast_tf, days)
                print(f" {len(df_fast)}캔들")

        first_close = float(df.iloc[0]["close"])
        last_close = float(df.iloc[-1]["close"])
        buy_hold_pnl_pct = (last_close - first_close) / first_close * 100

        # ── 시뮬레이션 상태 ─────────────────────────────────────
        cash = self._initial_balance
        position: FuturesPositionState | None = None
        dynamic_sl_pct = self._effective_sl
        current_market_state = "sideways"
        market_confidence = 0.5

        trades: list[BacktestTrade] = []
        equity_curve: list[tuple] = []
        peak_equity = self._initial_balance
        max_drawdown = 0.0
        last_trade_idx = -9999
        last_weight_eval_idx = -9999

        candles_per_day = max(1, int(24 / tf_hours))
        if self._trade_limiter:
            self._trade_limiter.min_interval_candles = BacktestTradeLimiter.calc_min_interval(timeframe)

        strategy_wins = {name: 0 for name in self._strategies}
        strategy_losses = {name: 0 for name in self._strategies}
        strategy_trades = {name: 0 for name in self._strategies}

        long_wins = 0
        long_losses = 0
        short_wins = 0
        short_losses = 0
        total_win_pct = 0.0
        total_loss_pct = 0.0
        liquidations = 0
        total_funding = 0.0
        total_fees = 0.0

        # ── 캔들 루프 ───────────────────────────────────────────
        rows = list(df.iterrows())
        for i, (ts, row) in enumerate(rows):
            current_price = float(row["close"])
            high_price = float(row["high"])
            low_price = float(row["low"])

            # 에쿼티 계산
            if position:
                unrealized = self._calc_unrealized_pnl(
                    position.side, position.entry_price, current_price, position.quantity
                )
                current_equity = cash + position.margin + unrealized
            else:
                current_equity = cash

            equity_curve.append((ts, current_equity))
            if current_equity > peak_equity:
                peak_equity = current_equity
            drawdown = (peak_equity - current_equity) / peak_equity * 100
            if drawdown > max_drawdown:
                max_drawdown = drawdown

            if i < 60:
                continue

            if self._risk_manager:
                self._risk_manager.update_equity(ts, i, current_equity, candles_per_day)
            if self._trade_limiter:
                self._trade_limiter.reset_day(i, candles_per_day)

            # ── 펀딩비 (8시간마다) ──────────────────────────────
            if position and i % candles_per_8h == 0:
                notional = position.quantity * current_price
                if position.side == "long":
                    funding_cost = notional * self._funding_rate
                else:
                    funding_cost = -notional * self._funding_rate  # 숏은 펀딩비 수취
                cash -= funding_cost
                total_funding += funding_cost

            # ── 24캔들마다 시장 상태 재평가 ─────────────────────
            if i - last_weight_eval_idx >= 24:
                prev_state = current_market_state
                state_4h, conf_4h = _detect_market_state(
                    row, df, i, use_agent_scoring=self._agent_market,
                )

                # 듀얼 타임프레임 결합
                if self._dual_timeframe and df_fast is not None:
                    # 빠른 TF에서 가장 가까운 타임스탬프 매칭
                    fast_idx = df_fast.index.searchsorted(ts, side="right") - 1
                    if 0 <= fast_idx < len(df_fast):
                        fast_row = df_fast.iloc[fast_idx]
                        state_1h, conf_1h = _detect_market_state(
                            fast_row, df_fast, fast_idx, use_agent_scoring=self._agent_market,
                        )
                        _STATE_RANK = {
                            "crash": 0, "downtrend": 1, "sideways": 2,
                            "uptrend": 3, "strong_uptrend": 4,
                        }
                        _RANK_STATE = {v: k for k, v in _STATE_RANK.items()}
                        rank_4h = _STATE_RANK.get(state_4h, 2)
                        rank_1h = _STATE_RANK.get(state_1h, 2)
                        if rank_1h < rank_4h:
                            final_rank = max(rank_4h - 1, rank_1h)
                            current_market_state = _RANK_STATE.get(final_rank, state_4h)
                            market_confidence = (conf_4h + conf_1h) / 2
                        else:
                            current_market_state = state_4h
                            market_confidence = conf_4h
                    else:
                        current_market_state = state_4h
                        market_confidence = conf_4h
                else:
                    current_market_state = state_4h
                    market_confidence = conf_4h

                if current_market_state != prev_state:
                    print(f"  [{ts.strftime('%m/%d %H:%M')}] 시장: {current_market_state} (신뢰도 {market_confidence:.0%})")
                if self._adaptive_weights:
                    new_weights = _get_adaptive_weights(current_market_state, list(self._strategies.keys()))
                    self._combiner.update_weights(new_weights, source="backtest")
                if self._dynamic_sl and position:
                    import math
                    raw_sl = _calc_dynamic_sl(row, current_price, current_market_state)
                    dynamic_sl_pct = raw_sl / math.sqrt(self._leverage)
                last_weight_eval_idx = i

            # ── 포지션 보유 중: 청산/SL/TP/트레일링 체크 ────────
            if position:
                liq_price = self._calc_liquidation_price(position.side, position.entry_price)

                # 강제 청산 체크 (캔들 내 고/저가 기준)
                liquidated = False
                if position.side == "long" and low_price <= liq_price:
                    liquidated = True
                    close_price = liq_price
                elif position.side == "short" and high_price >= liq_price:
                    liquidated = True
                    close_price = liq_price

                if liquidated:
                    # 마진 전액 손실
                    lost_margin = position.margin
                    fee = 0  # 청산 수수료는 마진에서 이미 차감
                    t = BacktestTrade(
                        timestamp=ts, side=f"sell(liq-{position.side})", symbol=symbol,
                        price=close_price, quantity=position.quantity,
                        cost=position.quantity * close_price, fee=fee,
                        strategy="liquidation", confidence=0,
                        reason=f"강제청산 ({position.side}) liq={liq_price:,.2f}",
                        pnl=-lost_margin, pnl_pct=-100.0,
                    )
                    trades.append(t)
                    total_fees += fee
                    liquidations += 1
                    if position.side == "long":
                        long_losses += 1
                    else:
                        short_losses += 1
                    total_loss_pct += 100.0
                    # 마진은 이미 cash에서 빠졌으므로 반환 없음
                    position = None
                    last_trade_idx = i
                    continue

                # 미실현 손익 (마진 대비 %)
                unrealized_pnl = self._calc_unrealized_pnl(
                    position.side, position.entry_price, current_price, position.quantity
                )
                unrealized_pct = unrealized_pnl / position.margin * 100 if position.margin > 0 else 0

                # 트레일링: 고점/저점 추적
                if position.side == "long":
                    if current_price > position.peak_price:
                        position.peak_price = current_price
                else:  # short
                    if current_price < position.peak_price:
                        position.peak_price = current_price

                # 트레일링 활성화
                if (self._effective_trail_act > 0
                        and not position.trailing_active
                        and unrealized_pct >= self._effective_trail_act):
                    position.trailing_active = True

                # 트레일링 스탑 발동
                if position.trailing_active and self._effective_trail_stop > 0:
                    if position.side == "long":
                        drop = (position.peak_price - current_price) / position.peak_price * 100
                    else:
                        drop = (current_price - position.peak_price) / position.peak_price * 100
                    if drop >= self._effective_trail_stop:
                        net_pnl, fee, pnl_pct, t = self._execute_futures_close(
                            ts, position, current_price,
                            f"sell(trail-{position.side})", "trailing_stop", 0,
                            f"트레일링 ({position.side}) 피크 대비 -{drop:.1f}% (수익 {unrealized_pct:+.1f}%)",
                        )
                        t.symbol = symbol
                        cash += position.margin + net_pnl
                        total_fees += fee
                        trades.append(t)
                        if net_pnl > 0:
                            if position.side == "long": long_wins += 1
                            else: short_wins += 1
                            total_win_pct += abs(pnl_pct)
                        else:
                            if position.side == "long": long_losses += 1
                            else: short_losses += 1
                            total_loss_pct += abs(pnl_pct)
                        position = None
                        last_trade_idx = i
                        continue

                # 손절 — high/low 기반 intra-candle SL 체크
                sl_triggered = False
                sl_close_price = current_price
                if dynamic_sl_pct > 0:
                    if position.side == "long":
                        sl_price_level = position.entry_price * (1 - dynamic_sl_pct / 100)
                        if low_price <= sl_price_level:
                            sl_triggered = True
                            sl_close_price = sl_price_level  # SL 가격에 체결 가정
                    else:  # short
                        sl_price_level = position.entry_price * (1 + dynamic_sl_pct / 100)
                        if high_price >= sl_price_level:
                            sl_triggered = True
                            sl_close_price = sl_price_level
                    # close 기준 체크 (margin-relative fallback)
                    if not sl_triggered and unrealized_pct <= -dynamic_sl_pct:
                        sl_triggered = True
                        sl_close_price = current_price

                if sl_triggered:
                    sl_unrealized = self._calc_unrealized_pnl(
                        position.side, position.entry_price, sl_close_price, position.quantity
                    )
                    sl_pct_actual = sl_unrealized / position.margin * 100 if position.margin > 0 else 0
                    net_pnl, fee, pnl_pct, t = self._execute_futures_close(
                        ts, position, sl_close_price,
                        f"sell(sl-{position.side})", "stop_loss", 0,
                        f"손절 ({position.side}) {sl_pct_actual:.1f}% (한도 -{dynamic_sl_pct:.1f}%)",
                    )
                    t.symbol = symbol
                    cash += position.margin + net_pnl
                    total_fees += fee
                    trades.append(t)
                    if position.side == "long": long_losses += 1
                    else: short_losses += 1
                    total_loss_pct += abs(pnl_pct)
                    position = None
                    last_trade_idx = i
                    continue

                # 익절 (트레일링 미활성 시)
                if (not position.trailing_active
                        and self._effective_tp > 0
                        and unrealized_pct >= self._effective_tp):
                    net_pnl, fee, pnl_pct, t = self._execute_futures_close(
                        ts, position, current_price,
                        f"sell(tp-{position.side})", "take_profit", 0,
                        f"익절 ({position.side}) +{unrealized_pct:.1f}% (목표 +{self._effective_tp:.1f}%)",
                    )
                    t.symbol = symbol
                    cash += position.margin + net_pnl
                    total_fees += fee
                    trades.append(t)
                    if position.side == "long": long_wins += 1
                    else: short_wins += 1
                    total_win_pct += abs(pnl_pct)
                    position = None
                    last_trade_idx = i
                    continue

            # 쿨다운
            if i - last_trade_idx < self._trade_cooldown:
                continue

            # ── 전략 신호 수집 ─────────────────────────────────
            slice_df = df.iloc[max(0, i-200):i+1]
            ticker = Ticker(
                symbol=symbol,
                last=current_price,
                bid=current_price * 0.9999,
                ask=current_price * 1.0001,
                high=float(row["high"]),
                low=float(row["low"]),
                volume=float(row.get("volume", 0)),
                timestamp=ts,
            )

            signals: list[Signal] = []
            for name, strategy in self._strategies.items():
                try:
                    sig = await strategy.analyze(slice_df.copy(), ticker)
                    signals.append(sig)
                except Exception:
                    pass

            if not signals:
                continue

            decision = self._combiner.combine(signals, market_state=current_market_state)

            # ── 포지션 없음: 롱/숏 진입 ────────────────────────
            if position is None:
                # 동적 포지션 사이징: 시장 상태별 포지션 크기 조절
                if self._dynamic_position:
                    import math as _m
                    _dyn_pos_mult = {
                        "strong_uptrend": 1.6,   # 강세: 공격적 롱
                        "uptrend":        1.2,   # 상승: 보통 롱
                        "sideways":       0.7,   # 횡보: 보수적
                        "downtrend":      1.2,   # 하락: 공격적 숏
                        "crash":          0.8,   # 폭락: 보수적 숏 (반등 리스크)
                    }
                    dyn_mult = _dyn_pos_mult.get(current_market_state, 1.0)
                    eff_position_pct = self._base_position_pct * dyn_mult / _m.sqrt(self._leverage)
                else:
                    eff_position_pct = self._position_pct

                # ATR 적응형 리스크: 마진/레버리지 축소 (차단 대신)
                _atr_val = row.get("ATRr_14")
                _atr_pct = (float(_atr_val) / current_price * 100) if (_atr_val and not pd.isna(_atr_val) and current_price > 0) else None
                # ATR 티어: (threshold, margin_mult, lev_override)
                _atr_margin_mult = 1.0
                _atr_lev = self._leverage
                if _atr_pct is not None:
                    for _thr, _mm, _lo in ((2.0, 1.2, None), (3.0, 1.1, None),
                                           (5.0, 1.0, None), (10.0, 0.7, None),
                                           (20.0, 0.5, 2), (999, 0.3, 1)):
                        if _atr_pct <= _thr:
                            _atr_margin_mult = _mm
                            if _lo is not None:
                                _atr_lev = _lo
                            break

                if decision.action == SignalType.BUY:
                    buy_threshold = self._min_confidence
                    if market_confidence < 0.35:
                        buy_threshold = self._min_confidence + 0.10
                    if decision.combined_confidence < buy_threshold:
                        continue

                    if self._risk_manager:
                        ok, _ = self._risk_manager.can_buy(ts, symbol, cash * eff_position_pct, {}, current_equity)
                        if not ok:
                            continue
                    if self._trade_limiter:
                        ok, _ = self._trade_limiter.can_buy(symbol, i)
                        if not ok:
                            continue

                    margin = cash * eff_position_pct * _atr_margin_mult
                    if margin < 1.0:  # 최소 1 USDT
                        continue

                    entry_fee = margin * _atr_lev * self._futures_fee
                    effective_margin = margin - entry_fee
                    qty = effective_margin * _atr_lev / current_price

                    cash -= margin
                    total_fees += entry_fee
                    position = FuturesPositionState(
                        side="long",
                        entry_price=current_price,
                        quantity=qty,
                        leverage=_atr_lev,
                        margin=margin,
                        peak_price=current_price,
                    )

                    if self._dynamic_sl:
                        import math
                        dynamic_sl_pct = _calc_dynamic_sl(row, current_price, current_market_state) / math.sqrt(_atr_lev)
                    else:
                        dynamic_sl_pct = self._effective_sl

                    buy_signals = [s for s in signals if s.signal_type == SignalType.BUY]
                    best_signal = max(buy_signals, key=lambda s: s.confidence) if buy_signals else signals[0]

                    t = BacktestTrade(
                        timestamp=ts, side="buy(long)", symbol=symbol,
                        price=current_price, quantity=qty, cost=margin, fee=entry_fee,
                        strategy=best_signal.strategy_name,
                        confidence=float(decision.combined_confidence),
                        reason=f"롱 진입 {self._leverage}x | {best_signal.reason}",
                    )
                    trades.append(t)
                    strategy_trades[best_signal.strategy_name] = strategy_trades.get(best_signal.strategy_name, 0) + 1
                    last_trade_idx = i
                    if self._trade_limiter:
                        self._trade_limiter.record_buy(symbol, i)

                elif decision.action == SignalType.SELL:
                    # 숏 진입 — 시장 상태 게이팅
                    if self._short_sideways:
                        _allowed = {"sideways", "downtrend", "crash"}
                    else:
                        _allowed = self.SHORT_ALLOWED_STATES
                    if not self._short_all and current_market_state not in _allowed:
                        continue  # uptrend/sideways에서 숏 차단

                    # 숏 신뢰도 상향: 최소 0.55
                    short_threshold = max(self._min_confidence, 0.55)
                    if decision.combined_confidence < short_threshold:
                        continue

                    if self._risk_manager:
                        ok, _ = self._risk_manager.can_buy(ts, symbol, cash * eff_position_pct, {}, current_equity)
                        if not ok:
                            continue
                    if self._trade_limiter:
                        ok, _ = self._trade_limiter.can_buy(symbol, i)
                        if not ok:
                            continue

                    margin = cash * eff_position_pct * _atr_margin_mult
                    if margin < 1.0:
                        continue

                    entry_fee = margin * _atr_lev * self._futures_fee
                    effective_margin = margin - entry_fee
                    qty = effective_margin * _atr_lev / current_price

                    cash -= margin
                    total_fees += entry_fee
                    position = FuturesPositionState(
                        side="short",
                        entry_price=current_price,
                        quantity=qty,
                        leverage=_atr_lev,
                        margin=margin,
                        peak_price=current_price,  # 숏은 최저가 추적
                    )

                    if self._dynamic_sl:
                        import math
                        dynamic_sl_pct = _calc_dynamic_sl(row, current_price, current_market_state) / math.sqrt(_atr_lev)
                    else:
                        dynamic_sl_pct = self._effective_sl

                    sell_signals = [s for s in signals if s.signal_type == SignalType.SELL]
                    best_signal = max(sell_signals, key=lambda s: s.confidence) if sell_signals else signals[0]

                    t = BacktestTrade(
                        timestamp=ts, side="buy(short)", symbol=symbol,
                        price=current_price, quantity=qty, cost=margin, fee=entry_fee,
                        strategy=best_signal.strategy_name,
                        confidence=float(decision.combined_confidence),
                        reason=f"숏 진입 {self._leverage}x | {best_signal.reason}",
                    )
                    trades.append(t)
                    strategy_trades[best_signal.strategy_name] = strategy_trades.get(best_signal.strategy_name, 0) + 1
                    last_trade_idx = i
                    if self._trade_limiter:
                        self._trade_limiter.record_buy(symbol, i)

            # ── 포지션 보유 중: 반대 신호로 청산 ───────────────
            elif position.side == "long" and decision.action == SignalType.SELL:
                net_pnl, fee, pnl_pct, t = self._execute_futures_close(
                    ts, position, current_price,
                    "sell(close-long)", "", float(decision.combined_confidence), "",
                )
                t.symbol = symbol

                sell_signals = [s for s in signals if s.signal_type == SignalType.SELL]
                best_signal = max(sell_signals, key=lambda s: s.confidence) if sell_signals else signals[0]
                t.strategy = best_signal.strategy_name
                t.reason = f"롱 청산 | {best_signal.reason}"

                cash += position.margin + net_pnl
                total_fees += fee
                trades.append(t)
                strategy_trades[best_signal.strategy_name] = strategy_trades.get(best_signal.strategy_name, 0) + 1
                last_trade_idx = i

                if net_pnl > 0:
                    long_wins += 1
                    total_win_pct += abs(pnl_pct)
                    strategy_wins[best_signal.strategy_name] = strategy_wins.get(best_signal.strategy_name, 0) + 1
                else:
                    long_losses += 1
                    total_loss_pct += abs(pnl_pct)
                    strategy_losses[best_signal.strategy_name] = strategy_losses.get(best_signal.strategy_name, 0) + 1
                position = None

            elif position.side == "short" and decision.action == SignalType.BUY:
                net_pnl, fee, pnl_pct, t = self._execute_futures_close(
                    ts, position, current_price,
                    "sell(close-short)", "", float(decision.combined_confidence), "",
                )
                t.symbol = symbol

                buy_signals = [s for s in signals if s.signal_type == SignalType.BUY]
                best_signal = max(buy_signals, key=lambda s: s.confidence) if buy_signals else signals[0]
                t.strategy = best_signal.strategy_name
                t.reason = f"숏 청산 | {best_signal.reason}"

                cash += position.margin + net_pnl
                total_fees += fee
                trades.append(t)
                strategy_trades[best_signal.strategy_name] = strategy_trades.get(best_signal.strategy_name, 0) + 1
                last_trade_idx = i

                if net_pnl > 0:
                    short_wins += 1
                    total_win_pct += abs(pnl_pct)
                    strategy_wins[best_signal.strategy_name] = strategy_wins.get(best_signal.strategy_name, 0) + 1
                else:
                    short_losses += 1
                    total_loss_pct += abs(pnl_pct)
                    strategy_losses[best_signal.strategy_name] = strategy_losses.get(best_signal.strategy_name, 0) + 1
                position = None

        # ── 미청산 포지션 강제 청산 ─────────────────────────────
        if position:
            net_pnl, fee, pnl_pct, t = self._execute_futures_close(
                df.index[-1], position, last_close,
                f"sell(close-{position.side})", "forced_close", 0,
                f"백테스트 종료 강제 청산 ({position.side})",
            )
            t.symbol = symbol
            cash += position.margin + net_pnl
            total_fees += fee
            trades.append(t)
            if net_pnl > 0:
                if position.side == "long": long_wins += 1
                else: short_wins += 1
                total_win_pct += abs(pnl_pct)
            else:
                if position.side == "long": long_losses += 1
                else: short_losses += 1
                total_loss_pct += abs(pnl_pct)

        # ── 결과 집계 ──────────────────────────────────────────
        final_balance = cash
        total_pnl = final_balance - self._initial_balance
        total_pnl_pct = total_pnl / self._initial_balance * 100

        win_count = long_wins + short_wins
        loss_count = long_losses + short_losses
        total_closes = win_count + loss_count
        win_rate = win_count / total_closes * 100 if total_closes > 0 else 0

        avg_win = total_win_pct / win_count if win_count > 0 else 0
        avg_loss = total_loss_pct / loss_count if loss_count > 0 else 0
        profit_factor = (total_win_pct / total_loss_pct) if total_loss_pct > 0 else float("inf") if total_win_pct > 0 else 0

        long_total = long_wins + long_losses
        short_total = short_wins + short_losses

        # 방향별 PnL 집계
        long_pnl_total = sum(t.pnl for t in trades if "long" in t.side and "sell" in t.side)
        short_pnl_total = sum(t.pnl for t in trades if "short" in t.side and "sell" in t.side)

        strategy_stats = {}
        for name in self._strategies:
            n = strategy_trades.get(name, 0)
            w = strategy_wins.get(name, 0)
            l = strategy_losses.get(name, 0)
            strategy_stats[name] = {
                "trades": n,
                "wins": w,
                "losses": l,
                "win_rate": round(w / (w + l) * 100, 1) if (w + l) > 0 else 0,
            }

        return FuturesBacktestResult(
            symbol=symbol,
            days=days,
            leverage=self._leverage,
            initial_balance=self._initial_balance,
            final_balance=round(final_balance, 2),
            total_pnl=round(total_pnl, 2),
            total_pnl_pct=round(total_pnl_pct, 2),
            max_drawdown_pct=round(max_drawdown, 2),
            total_trades=total_closes,
            long_trades=long_total,
            short_trades=short_total,
            long_wins=long_wins,
            long_losses=long_losses,
            short_wins=short_wins,
            short_losses=short_losses,
            win_rate=round(win_rate, 1),
            avg_win_pct=round(avg_win, 2),
            avg_loss_pct=round(avg_loss, 2),
            profit_factor=round(profit_factor, 2),
            buy_hold_pnl_pct=round(buy_hold_pnl_pct, 2),
            liquidations=liquidations,
            total_funding=round(total_funding, 2),
            total_fees=round(total_fees, 2),
            long_pnl=round(long_pnl_total, 2),
            short_pnl=round(short_pnl_total, 2),
            trades=trades,
            equity_curve=equity_curve,
            strategy_stats=strategy_stats,
        )


def print_futures_result(r: FuturesBacktestResult):
    """선물 백테스트 결과 출력."""
    pnl_sign = "+" if r.total_pnl >= 0 else ""
    dd_warn = " !!!" if r.max_drawdown_pct > 15 else ""
    bh_sign = "+" if r.buy_hold_pnl_pct >= 0 else ""
    alpha = r.total_pnl_pct - r.buy_hold_pnl_pct
    alpha_sign = "+" if alpha >= 0 else ""

    print(f"\n{'='*60}")
    print(f"  {r.symbol} 선물 백테스트 결과 ({r.days}일, {r.leverage}x)")
    print(f"{'='*60}")
    print(f"  초기 자산    : {r.initial_balance:>12,.2f} USDT")
    print(f"  최종 자산    : {r.final_balance:>12,.2f} USDT")
    print(f"  총 수익      : {pnl_sign}{r.total_pnl:>10,.2f} USDT  ({pnl_sign}{r.total_pnl_pct:.2f}%)")
    print(f"  최대 낙폭    : {r.max_drawdown_pct:.2f}%{dd_warn}")
    print(f"{'─'*60}")
    print(f"  현물 B&H     : {bh_sign}{r.buy_hold_pnl_pct:.2f}%")
    print(f"  초과 수익(α) : {alpha_sign}{alpha:.2f}%")
    print(f"{'─'*60}")
    print(f"  총 청산 횟수 : {r.total_trades}회")

    long_wr = r.long_wins / r.long_trades * 100 if r.long_trades > 0 else 0
    short_wr = r.short_wins / r.short_trades * 100 if r.short_trades > 0 else 0
    long_pnl_sign = "+" if r.long_pnl >= 0 else ""
    short_pnl_sign = "+" if r.short_pnl >= 0 else ""
    print(f"  롱  거래     : {r.long_trades}회  ({r.long_wins}승/{r.long_losses}패, 승률 {long_wr:.1f}%)  PnL {long_pnl_sign}{r.long_pnl:,.2f}")
    print(f"  숏  거래     : {r.short_trades}회  ({r.short_wins}승/{r.short_losses}패, 승률 {short_wr:.1f}%)  PnL {short_pnl_sign}{r.short_pnl:,.2f}")
    print(f"  전체 승률    : {r.win_rate:.1f}%")
    print(f"  평균 수익    : +{r.avg_win_pct:.2f}% | 평균 손실: -{r.avg_loss_pct:.2f}%")
    print(f"  Profit Factor: {r.profit_factor:.2f}")
    print(f"{'─'*60}")
    print(f"  강제청산     : {r.liquidations}회")
    print(f"  총 펀딩비    : {r.total_funding:>+10,.2f} USDT")
    print(f"  총 수수료    : {r.total_fees:>10,.2f} USDT")
    print(f"{'─'*60}")
    print(f"  전략별 기여:")
    for name, stat in r.strategy_stats.items():
        if stat["trades"] > 0:
            print(f"    {name:<22}: {stat['trades']:>3}회  승률 {stat['win_rate']:>5.1f}%")
    print(f"{'─'*60}")

    sell_trades = [t for t in r.trades if "sell" in t.side]
    if sell_trades:
        print(f"  매매 내역 (최근 10건):")
        for t in sell_trades[-10:]:
            arrow = "+" if t.pnl >= 0 else ""
            side_info = t.side.replace("sell(", "").replace(")", "")
            tag_map = {
                "sl-long": "롱손절", "sl-short": "숏손절",
                "tp-long": "롱익절", "tp-short": "숏익절",
                "trail-long": "롱트레일", "trail-short": "숏트레일",
                "close-long": "롱청산", "close-short": "숏청산",
                "liq-long": "롱강청", "liq-short": "숏강청",
            }
            tag = tag_map.get(side_info, side_info)
            print(f"    {t.timestamp.strftime('%m/%d %H:%M')}  {arrow}{t.pnl_pct:>+6.1f}%  "
                  f"[{tag}] {t.reason[:50]}")
    print(f"{'='*60}\n")


# ── 선물 포트폴리오 백테스터 ──────────────────────────────────────────

DEFAULT_FUTURES_PORTFOLIO_COINS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT"]

# 동적 포트폴리오 후보군 — 바이낸스 선물 주요 코인 (라이브와 유사한 유니버스)
DEFAULT_FUTURES_CANDIDATE_COINS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT",
    "ADA/USDT", "DOGE/USDT", "AVAX/USDT", "LINK/USDT", "DOT/USDT",
    "UNI/USDT", "NEAR/USDT", "FIL/USDT", "ATOM/USDT",
    "LTC/USDT", "ARB/USDT", "OP/USDT", "APT/USDT", "SUI/USDT",
    "TRX/USDT", "ETC/USDT", "AAVE/USDT", "PEPE/USDT",
    "WIF/USDT", "HYPE/USDT", "DYDX/USDT", "SEI/USDT", "INJ/USDT",
]


@dataclass
class FuturesPortfolioPositionState:
    """선물 포트폴리오의 코인별 포지션 상태."""
    symbol: str
    side: str              # "long" / "short"
    entry_price: float
    quantity: float        # 레버리지 반영 수량
    leverage: int
    margin: float          # 격리 마진 (실제 투입 현금)
    peak_price: float      # 트레일링용
    trailing_active: bool = False
    entry_strategy: str = ""
    entry_idx: int = 0


@dataclass
class FuturesPortfolioBacktestResult:
    """선물 포트폴리오 백테스트 결과."""
    symbols: list[str]
    days: int
    leverage: int
    initial_balance: float
    final_balance: float
    total_pnl: float
    total_pnl_pct: float
    max_drawdown_pct: float
    total_trades: int
    long_trades: int
    short_trades: int
    long_wins: int
    long_losses: int
    short_wins: int
    short_losses: int
    win_rate: float
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    profit_factor: float = 0.0
    buy_hold_pnl_pct: float = 0.0
    liquidations: int = 0
    total_funding: float = 0.0
    total_fees: float = 0.0
    long_pnl: float = 0.0
    short_pnl: float = 0.0
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[tuple] = field(default_factory=list)
    strategy_stats: dict = field(default_factory=dict)
    per_coin_stats: dict = field(default_factory=dict)
    risk_events: list[RiskEvent] = field(default_factory=list)
    risk_stats: dict = field(default_factory=dict)
    trade_limit_stats: dict = field(default_factory=dict)


class FuturesPortfolioBacktester:
    """선물 멀티코인 포트폴리오 백테스터.

    PortfolioBacktester의 멀티코인 관리 + FuturesBacktester의 선물 메카닉 통합.
    - 멀티코인 동시 운용 (union timestamps)
    - 롱/숏 양방향 + 레버리지
    - 격리 마진 + 강제 청산
    - 펀딩비 (8시간마다)
    - 리스크 관리 + 매매 제한
    """

    SHORT_ALLOWED_STATES = {"downtrend", "crash"}

    def __init__(
        self,
        exchange,
        strategy_names: list[str],
        symbols: list[str] | None = None,
        initial_balance: float = 10_000,     # USDT
        min_confidence: float = 0.50,
        stop_loss_pct: float = 5.0,
        take_profit_pct: float = 10.0,
        trend_filter: bool = True,
        trailing_activation: float = 3.0,
        trailing_stop: float = 3.0,
        adaptive_weights: bool = True,
        dynamic_sl: bool = False,
        agent_market: bool = True,
        trade_cooldown: int = 6,
        leverage: int = 3,
        futures_fee: float = FUTURES_FEE,
        funding_rate: float = FUNDING_RATE,
        position_pct: float = 0.30,
        short_all: bool = False,
        short_sideways: bool = False,
        dynamic_position: bool = False,
        dual_timeframe: bool = False,
        directional_weights: bool = False,
        max_positions: int = 5,
        long_block_states: set | None = None,
        long_sizing_states: dict | None = None,
        dynamic_portfolio: bool = False,
        dynamic_max_coins: int = 10,
        dynamic_refresh_candles: int = 6,  # 4h에서 6캔들=24시간
        # 선택적 거래
        confidence_sizing: bool = False,
        volatility_filter: bool = False,
        ml_filter_path: str | None = None,
        ml_min_win_prob: float = 0.55,
        # 리스크 관리
        risk_enabled: bool = False,
        risk_max_drawdown: float = 0.10,
        risk_daily_loss: float = 0.03,
        risk_max_concentration: float = 0.40,
        # 매매 제한
        trade_limit_enabled: bool = False,
        trade_daily_buy_limit: int = 20,
        trade_max_coin_buys: int = 3,
    ):
        self._exchange = exchange
        self._initial_balance = initial_balance
        self._min_confidence = min_confidence
        self._stop_loss_pct = stop_loss_pct
        self._take_profit_pct = take_profit_pct
        self._trend_filter = trend_filter
        self._trailing_activation = trailing_activation
        self._trailing_stop = trailing_stop
        self._adaptive_weights = adaptive_weights
        self._dynamic_sl = dynamic_sl
        self._agent_market = agent_market
        self._trade_cooldown = trade_cooldown
        self._leverage = leverage
        self._futures_fee = futures_fee
        self._funding_rate = funding_rate
        self._short_all = short_all
        self._short_sideways = short_sideways
        self._dynamic_position = dynamic_position
        self._dual_timeframe = dual_timeframe
        self._directional_weights = directional_weights
        self._max_positions = max_positions
        self._long_block_states = long_block_states or set()
        self._long_sizing_states = long_sizing_states or {}
        self._base_position_pct = position_pct
        self._dynamic_portfolio = dynamic_portfolio
        self._dynamic_max_coins = dynamic_max_coins
        self._dynamic_refresh_candles = dynamic_refresh_candles
        self._confidence_sizing = confidence_sizing
        self._volatility_filter = volatility_filter
        self._ml_filter = None
        if ml_filter_path:
            from strategies.ml_filter import MLSignalFilter
            self._ml_filter = MLSignalFilter(
                min_win_prob=ml_min_win_prob,
                model_path=ml_filter_path,
            )
        self._ml_min_win_prob = ml_min_win_prob
        if dynamic_portfolio:
            # 후보 전체 프리페치 — 기본 코인 + 후보군 합집합
            base = list(symbols or DEFAULT_FUTURES_PORTFOLIO_COINS)
            self._base_coins = base
            self._symbols = list(dict.fromkeys(base + DEFAULT_FUTURES_CANDIDATE_COINS))
        else:
            self._base_coins = list(symbols or DEFAULT_FUTURES_PORTFOLIO_COINS)
            self._symbols = self._base_coins

        # 레버리지 적응형 파라미터
        import math
        lev_sqrt = math.sqrt(leverage)
        self._position_pct = position_pct / lev_sqrt
        self._effective_sl = stop_loss_pct / lev_sqrt
        self._effective_tp = take_profit_pct / lev_sqrt
        self._effective_trail_act = trailing_activation / lev_sqrt if trailing_activation > 0 else 0
        self._effective_trail_stop = trailing_stop / lev_sqrt if trailing_stop > 0 else 0

        self._risk_manager = BacktestRiskManager(
            max_drawdown_pct=risk_max_drawdown,
            daily_loss_limit_pct=risk_daily_loss,
            max_concentration_pct=risk_max_concentration,
            enabled=risk_enabled,
        ) if risk_enabled else None

        self._trade_limiter = BacktestTradeLimiter(
            daily_buy_limit=trade_daily_buy_limit,
            max_coin_buys=trade_max_coin_buys,
            enabled=trade_limit_enabled,
        ) if trade_limit_enabled else None

        # 전략 로드
        all_strats = StrategyRegistry.create_all()
        self._strategies = {
            name: strat for name, strat in all_strats.items()
            if name in strategy_names
        }
        if set(strategy_names) <= set(WEIGHTS_5.keys()):
            base_weights = WEIGHTS_5
        elif set(strategy_names) <= set(WEIGHTS_6.keys()):
            base_weights = WEIGHTS_6
        elif set(strategy_names) <= set(WEIGHTS_7.keys()):
            base_weights = WEIGHTS_7
        elif set(strategy_names) <= set(WEIGHTS_8.keys()):
            base_weights = WEIGHTS_8
        else:
            base_weights = {name: 1.0 / len(strategy_names) for name in strategy_names}
        weights = {k: v for k, v in base_weights.items() if k in strategy_names}
        total_w = sum(weights.values())
        if total_w > 0:
            weights = {k: v / total_w for k, v in weights.items()}
        self._combiner = SignalCombiner(
            strategy_weights=weights,
            min_confidence=min_confidence,
            directional_weights=self._directional_weights,
        )

    async def prefetch_all(
        self, timeframe: str, days: int,
    ) -> dict[str, pd.DataFrame]:
        """전 코인 + BTC 레퍼런스 데이터 프리페치."""
        all_data: dict[str, pd.DataFrame] = {}
        all_syms = list(dict.fromkeys(["BTC/USDT"] + list(self._symbols)))
        total = len(all_syms)
        for idx, sym in enumerate(all_syms, 1):
            try:
                print(f"  [{idx}/{total}] {sym} 데이터 로딩...", end="", flush=True)
                df = await fetch_history(self._exchange, sym, timeframe, days)
                all_data[sym] = df
                print(f" {len(df)}캔들")
            except Exception as e:
                print(f" 실패({e})")
        return all_data

    def _select_dynamic_coins(
        self, all_data: dict[str, "pd.DataFrame"], ts, candle_idx: int,
    ) -> list[str]:
        """거래량 기반 동적 코인 선택 — 라이브 _refresh_dynamic_coins() 시뮬레이션.

        최근 24h(6캔들@4h) 거래대금으로 상위 코인 선택 후 base_coins와 합집합.
        """
        lookback = min(6, candle_idx)  # 24h @ 4h
        volumes: list[tuple[str, float]] = []
        for sym, df in all_data.items():
            if ts not in df.index:
                continue
            iloc = df.index.get_loc(ts)
            if isinstance(iloc, slice):
                iloc = iloc.start
            start = max(0, iloc - lookback)
            recent = df.iloc[start:iloc + 1]
            quote_vol = float((recent["close"] * recent["volume"]).sum())
            volumes.append((sym, quote_vol))

        # 거래대금 기준 내림차순 정렬
        volumes.sort(key=lambda x: x[1], reverse=True)

        # base_coins는 항상 포함, 나머지에서 상위 N개 선택
        base_set = set(self._base_coins)
        dynamic = []
        for sym, _ in volumes:
            if sym in base_set:
                continue
            dynamic.append(sym)
            if len(dynamic) >= self._dynamic_max_coins:
                break

        active = list(dict.fromkeys(self._base_coins + dynamic))
        return active

    def _calc_liquidation_price(self, side: str, entry: float) -> float:
        lev = self._leverage
        fee = self._futures_fee
        if side == "long":
            return entry * (1 - 1 / lev + fee)
        else:
            return entry * (1 + 1 / lev - fee)

    def _calc_unrealized_pnl(self, side: str, entry: float, current: float, qty: float) -> float:
        if side == "long":
            return (current - entry) * qty
        else:
            return (entry - current) * qty

    def _execute_futures_close(
        self, ts, pos: FuturesPortfolioPositionState, current_price: float,
        side_label: str, strategy_name: str, confidence: float, reason: str,
    ) -> tuple[float, float, float, BacktestTrade]:
        """포지션 청산 → (net_pnl, fee, pnl_pct, trade)"""
        pnl = self._calc_unrealized_pnl(pos.side, pos.entry_price, current_price, pos.quantity)
        fee = abs(pos.quantity * current_price) * self._futures_fee
        net_pnl = pnl - fee
        pnl_pct = net_pnl / pos.margin * 100 if pos.margin > 0 else 0

        t = BacktestTrade(
            timestamp=ts, side=side_label, symbol=pos.symbol,
            price=current_price, quantity=pos.quantity,
            cost=pos.quantity * current_price, fee=fee,
            strategy=strategy_name,
            confidence=confidence,
            reason=reason,
            pnl=net_pnl, pnl_pct=round(pnl_pct, 2),
        )
        return net_pnl, fee, pnl_pct, t

    async def run(self, timeframe: str = "4h", days: int = 180) -> FuturesPortfolioBacktestResult:
        """선물 멀티코인 포트폴리오 백테스트 실행."""
        import math

        tf_hours = _tf_hours(timeframe)
        candles_per_8h = max(1, int(8 / tf_hours))
        candles_per_day = max(1, int(24 / tf_hours))
        lev_sqrt = math.sqrt(self._leverage)

        print(f"\n{'='*60}")
        print(f"  선물 포트폴리오 백테스트 | {timeframe} | {days}일")
        print(f"  코인: {', '.join(self._symbols)}")
        print(f"  전략: {', '.join(self._strategies.keys())}")
        print(f"  레버리지: {self._leverage}x | 수수료: {self._futures_fee*100:.2f}%")
        dpos_str = f"동적(기본 {self._base_position_pct*100:.0f}%)" if self._dynamic_position else f"고정 {self._position_pct*100:.1f}%"
        print(f"  펀딩비: {self._funding_rate*100:.3f}%/8h | 포지션: {dpos_str}")
        sl_str = "동적(ATR+시장)" if self._dynamic_sl else (
            f"고정 {self._effective_sl:.1f}%" if self._effective_sl > 0 else "OFF")
        tp_str = f"{self._effective_tp:.1f}%" if self._effective_tp > 0 else "OFF"
        trail_str = (f"활성 +{self._effective_trail_act:.1f}% / 스탑 -{self._effective_trail_stop:.1f}%"
                     if self._effective_trail_act > 0 else "OFF")
        short_str = "전체" if self._short_all else ("sideways+downtrend/crash" if self._short_sideways else "downtrend/crash만")
        print(f"  최대 동시 포지션: {self._max_positions}")
        print(f"  손절: {sl_str} | 익절: {tp_str} | 트레일링: {trail_str}")
        print(f"  숏 허용: {short_str} | 쿨다운: {self._trade_cooldown}캔들")
        dyn_str = f"ON (base {len(self._base_coins)} + 상위 {self._dynamic_max_coins}, {self._dynamic_refresh_candles}캔들 갱신)" if self._dynamic_portfolio else "OFF"
        print(f"  동적 포트폴리오: {dyn_str}")
        risk_str = "ON" if self._risk_manager else "OFF"
        limit_str = "ON" if self._trade_limiter else "OFF"
        print(f"  리스크 관리: {risk_str} | 매매 제한: {limit_str}")
        print(f"{'='*60}")

        # 1. 데이터 프리페치
        all_data = await self.prefetch_all(timeframe, days)
        if not all_data:
            raise ValueError("사용 가능한 코인 데이터 없음")
        print(f"\n  {len(all_data)}개 코인 로딩 완료")

        if self._trade_limiter:
            self._trade_limiter.min_interval_candles = BacktestTradeLimiter.calc_min_interval(timeframe)

        # 듀얼 타임프레임: 빠른 TF 데이터
        all_data_fast: dict[str, pd.DataFrame] = {}
        if self._dual_timeframe:
            _FAST_TF_MAP = {"4h": "1h", "1d": "4h", "1h": "15m"}
            fast_tf = _FAST_TF_MAP.get(timeframe)
            if fast_tf:
                print(f"  듀얼 TF: {timeframe}(장기) + {fast_tf}(단기) 로딩...")
                for sym in ["BTC/USDT"]:
                    if sym in all_data:
                        try:
                            df_fast = await fetch_history(self._exchange, sym, fast_tf, days)
                            all_data_fast[sym] = df_fast
                            print(f"    {sym} 단기: {len(df_fast)}캔들")
                        except Exception:
                            pass

        portfolio_syms = [s for s in self._symbols if s in all_data]
        btc_df = all_data.get("BTC/USDT")

        # 2. 유니온 타임스탬프
        all_timestamps = sorted(set().union(*(df.index for df in all_data.values())))
        print(f"  타임라인: {len(all_timestamps)}개 캔들 ({all_timestamps[0].date()} ~ {all_timestamps[-1].date()})")

        # 동적 포트폴리오: 현재 활성 코인 (진입 후보 스캔 대상)
        if self._dynamic_portfolio:
            active_coins = list(self._base_coins)  # 초기: base만
            _last_dyn_refresh_idx = -9999
            print(f"  동적 포트폴리오: 후보 {len(portfolio_syms)}코인 중 base {len(self._base_coins)} + 거래량 상위 {self._dynamic_max_coins}")
        else:
            active_coins = portfolio_syms

        # 3. 초기화
        cash = self._initial_balance
        positions: dict[str, FuturesPortfolioPositionState] = {}
        current_market_state = "sideways"
        market_confidence = 0.5
        dynamic_sl_pct: dict[str, float] = {}  # 코인별 동적 SL

        trades: list[BacktestTrade] = []
        equity_curve: list[tuple] = []
        peak_equity = self._initial_balance
        max_drawdown = 0.0
        last_weight_eval_idx = -9999
        last_trade_idx_per_coin: dict[str, int] = {}

        strategy_wins: dict[str, int] = {name: 0 for name in self._strategies}
        strategy_losses: dict[str, int] = {name: 0 for name in self._strategies}
        strategy_trades: dict[str, int] = {name: 0 for name in self._strategies}

        coin_stats: dict[str, dict] = {
            sym: {"wins": 0, "losses": 0, "trades": 0, "pnl": 0.0,
                  "long_wins": 0, "long_losses": 0, "short_wins": 0, "short_losses": 0,
                  "long_pnl": 0.0, "short_pnl": 0.0}
            for sym in portfolio_syms
        }

        long_wins = 0
        long_losses = 0
        short_wins = 0
        short_losses = 0
        total_win_pct = 0.0
        total_loss_pct = 0.0
        liquidations = 0
        total_funding = 0.0
        total_fees = 0.0

        # 4. 캔들 루프
        for candle_idx, ts in enumerate(all_timestamps):
            # 4a. 에쿼티 계산
            equity = cash
            position_values: dict[str, float] = {}
            for sym, pos in positions.items():
                if sym in all_data and ts in all_data[sym].index:
                    cur_price = float(all_data[sym].loc[ts, "close"])
                    unrealized = self._calc_unrealized_pnl(pos.side, pos.entry_price, cur_price, pos.quantity)
                    val = pos.margin + unrealized
                else:
                    val = pos.margin  # 가격 없으면 마진만
                position_values[sym] = val
                equity += val

            # 4b. 에쿼티 곡선/낙폭
            equity_curve.append((ts, equity))
            if equity > peak_equity:
                peak_equity = equity
            drawdown = (peak_equity - equity) / peak_equity * 100
            if drawdown > max_drawdown:
                max_drawdown = drawdown

            if candle_idx < 60:
                continue

            if self._risk_manager:
                self._risk_manager.update_equity(ts, candle_idx, equity, candles_per_day)
            if self._trade_limiter:
                self._trade_limiter.reset_day(candle_idx, candles_per_day)

            # 4c. 펀딩비 (8시간마다)
            if candle_idx % candles_per_8h == 0:
                for sym, pos in positions.items():
                    if sym in all_data and ts in all_data[sym].index:
                        cur_price = float(all_data[sym].loc[ts, "close"])
                        notional = pos.quantity * cur_price
                        if pos.side == "long":
                            funding_cost = notional * self._funding_rate
                        else:
                            funding_cost = -notional * self._funding_rate
                        cash -= funding_cost
                        total_funding += funding_cost

            # 4d. 24캔들마다 시장 상태 재평가 (BTC 기준)
            if candle_idx - last_weight_eval_idx >= 24:
                if btc_df is not None and ts in btc_df.index:
                    prev_state = current_market_state
                    btc_iloc = btc_df.index.get_loc(ts)
                    if isinstance(btc_iloc, slice):
                        btc_iloc = btc_iloc.start

                    state_4h, conf_4h = _detect_market_state(
                        btc_df.loc[ts], btc_df, btc_iloc,
                        use_agent_scoring=self._agent_market,
                    )

                    # 듀얼 타임프레임 결합
                    if self._dual_timeframe and "BTC/USDT" in all_data_fast:
                        df_fast = all_data_fast["BTC/USDT"]
                        fast_idx = df_fast.index.searchsorted(ts, side="right") - 1
                        if 0 <= fast_idx < len(df_fast):
                            fast_row = df_fast.iloc[fast_idx]
                            state_1h, conf_1h = _detect_market_state(
                                fast_row, df_fast, fast_idx, use_agent_scoring=self._agent_market,
                            )
                            _STATE_RANK = {
                                "crash": 0, "downtrend": 1, "sideways": 2,
                                "uptrend": 3, "strong_uptrend": 4,
                            }
                            _RANK_STATE = {v: k for k, v in _STATE_RANK.items()}
                            rank_4h = _STATE_RANK.get(state_4h, 2)
                            rank_1h = _STATE_RANK.get(state_1h, 2)
                            if rank_1h < rank_4h:
                                final_rank = max(rank_4h - 1, rank_1h)
                                current_market_state = _RANK_STATE.get(final_rank, state_4h)
                                market_confidence = (conf_4h + conf_1h) / 2
                            else:
                                current_market_state = state_4h
                                market_confidence = conf_4h
                        else:
                            current_market_state = state_4h
                            market_confidence = conf_4h
                    else:
                        current_market_state = state_4h
                        market_confidence = conf_4h

                    if current_market_state != prev_state:
                        print(f"  [{ts.strftime('%m/%d %H:%M')}] 시장: {current_market_state} (신뢰도 {market_confidence:.0%})")
                    if self._adaptive_weights:
                        new_weights = _get_adaptive_weights(current_market_state, list(self._strategies.keys()))
                        self._combiner.update_weights(new_weights, source="backtest")
                last_weight_eval_idx = candle_idx

            # 4e. 보유 포지션: 청산/SL/TP/트레일링/강제청산 체크
            to_close: list[str] = []
            for sym, pos in positions.items():
                if sym not in all_data or ts not in all_data[sym].index:
                    continue
                cur_price = float(all_data[sym].loc[ts, "close"])
                high_price = float(all_data[sym].loc[ts, "high"])
                low_price = float(all_data[sym].loc[ts, "low"])

                # 강제 청산 체크
                liq_price = self._calc_liquidation_price(pos.side, pos.entry_price)
                liquidated = False
                if pos.side == "long" and low_price <= liq_price:
                    liquidated = True
                elif pos.side == "short" and high_price >= liq_price:
                    liquidated = True

                if liquidated:
                    lost_margin = pos.margin
                    t = BacktestTrade(
                        timestamp=ts, side=f"sell(liq-{pos.side})", symbol=sym,
                        price=liq_price, quantity=pos.quantity,
                        cost=pos.quantity * liq_price, fee=0,
                        strategy="liquidation", confidence=0,
                        reason=f"강제청산 ({pos.side}) liq={liq_price:,.2f}",
                        pnl=-lost_margin, pnl_pct=-100.0,
                    )
                    trades.append(t)
                    liquidations += 1
                    if pos.side == "long":
                        long_losses += 1
                        coin_stats[sym]["long_losses"] += 1
                    else:
                        short_losses += 1
                        coin_stats[sym]["short_losses"] += 1
                    total_loss_pct += 100.0
                    coin_stats[sym]["trades"] += 1
                    coin_stats[sym]["losses"] += 1
                    coin_stats[sym]["pnl"] -= lost_margin
                    to_close.append(sym)
                    last_trade_idx_per_coin[sym] = candle_idx
                    continue

                # 미실현 손익 (margin-relative)
                unrealized_pnl = self._calc_unrealized_pnl(pos.side, pos.entry_price, cur_price, pos.quantity)
                unrealized_pct = unrealized_pnl / pos.margin * 100 if pos.margin > 0 else 0

                # 피크 추적
                if pos.side == "long":
                    if cur_price > pos.peak_price:
                        pos.peak_price = cur_price
                else:
                    if cur_price < pos.peak_price:
                        pos.peak_price = cur_price

                # 트레일링 활성화
                if (self._effective_trail_act > 0
                        and not pos.trailing_active
                        and unrealized_pct >= self._effective_trail_act):
                    pos.trailing_active = True

                sell_tag = None
                sell_text = None

                # 트레일링 스탑
                if pos.trailing_active and self._effective_trail_stop > 0:
                    if pos.side == "long":
                        drop = (pos.peak_price - cur_price) / pos.peak_price * 100
                    else:
                        drop = (cur_price - pos.peak_price) / pos.peak_price * 100
                    if drop >= self._effective_trail_stop:
                        sell_tag = f"sell(trail-{pos.side})"
                        sell_text = f"트레일링 ({pos.side}) 피크 대비 -{drop:.1f}% (수익 {unrealized_pct:+.1f}%)"

                # 손절
                eff_sl = dynamic_sl_pct.get(sym, self._effective_sl)
                if not sell_tag and eff_sl > 0 and unrealized_pct <= -eff_sl:
                    sell_tag = f"sell(sl-{pos.side})"
                    sell_text = f"손절 ({pos.side}) {unrealized_pct:.1f}% (한도 -{eff_sl:.1f}%)"

                # 익절 (트레일링 미활성 시)
                if (not sell_tag and not pos.trailing_active
                        and self._effective_tp > 0 and unrealized_pct >= self._effective_tp):
                    sell_tag = f"sell(tp-{pos.side})"
                    sell_text = f"익절 ({pos.side}) +{unrealized_pct:.1f}%"

                if sell_tag:
                    net_pnl, fee, pnl_pct, t = self._execute_futures_close(
                        ts, pos, cur_price, sell_tag, pos.entry_strategy, 0, sell_text,
                    )
                    cash += pos.margin + net_pnl
                    total_fees += fee
                    trades.append(t)
                    coin_stats[sym]["trades"] += 1
                    coin_stats[sym]["pnl"] += net_pnl
                    if net_pnl > 0:
                        total_win_pct += abs(pnl_pct)
                        coin_stats[sym]["wins"] += 1
                        if pos.side == "long":
                            long_wins += 1
                            coin_stats[sym]["long_wins"] += 1
                        else:
                            short_wins += 1
                            coin_stats[sym]["short_wins"] += 1
                    else:
                        total_loss_pct += abs(pnl_pct)
                        coin_stats[sym]["losses"] += 1
                        if pos.side == "long":
                            long_losses += 1
                            coin_stats[sym]["long_losses"] += 1
                        else:
                            short_losses += 1
                            coin_stats[sym]["short_losses"] += 1
                    to_close.append(sym)
                    last_trade_idx_per_coin[sym] = candle_idx

            for sym in to_close:
                del positions[sym]
                dynamic_sl_pct.pop(sym, None)

            # 4f-pre. 동적 포트폴리오 갱신
            if self._dynamic_portfolio and candle_idx - _last_dyn_refresh_idx >= self._dynamic_refresh_candles:
                active_coins = self._select_dynamic_coins(all_data, ts, candle_idx)
                # 신규 코인 coin_stats 초기화
                for sym in active_coins:
                    if sym not in coin_stats:
                        coin_stats[sym] = {
                            "wins": 0, "losses": 0, "trades": 0, "pnl": 0.0,
                            "long_wins": 0, "long_losses": 0, "short_wins": 0, "short_losses": 0,
                            "long_pnl": 0.0, "short_pnl": 0.0,
                        }
                _last_dyn_refresh_idx = candle_idx

            # 4f. 미보유 코인: 전략 시그널 → 롱/숏 후보 수집
            entry_candidates: list[tuple[str, str, float, Signal, object]] = []
            # (sym, side, confidence, best_signal, decision)

            for sym in active_coins:
                if sym in positions:
                    continue
                if sym not in all_data or ts not in all_data[sym].index:
                    continue
                if len(positions) >= self._max_positions:
                    break

                # 코인별 쿨다운
                last_idx = last_trade_idx_per_coin.get(sym, -9999)
                if candle_idx - last_idx < self._trade_cooldown:
                    continue

                sym_df = all_data[sym]
                sym_iloc = sym_df.index.get_loc(ts)
                if isinstance(sym_iloc, slice):
                    sym_iloc = sym_iloc.start

                row = sym_df.iloc[sym_iloc]
                cur_price = float(row["close"])

                # 전략 신호 수집
                slice_df = sym_df.iloc[max(0, sym_iloc - 200):sym_iloc + 1]
                ticker = Ticker(
                    symbol=sym, last=cur_price,
                    bid=cur_price * 0.9999, ask=cur_price * 1.0001,
                    high=float(row["high"]), low=float(row["low"]),
                    volume=float(row.get("volume", 0)), timestamp=ts,
                )

                signals: list[Signal] = []
                for name, strategy in self._strategies.items():
                    try:
                        sig = await strategy.analyze(slice_df.copy(), ticker)
                        signals.append(sig)
                    except Exception:
                        pass

                if not signals:
                    continue

                decision = self._combiner.combine(signals, market_state=current_market_state)

                # ML 필터: 수익 확률이 낮은 시그널 차단
                if self._ml_filter and decision.action != SignalType.HOLD:
                    from strategies.ml_filter import MLSignalFilter
                    _ml_features = MLSignalFilter.extract_features(
                        signals=signals, row=row, price=cur_price,
                        market_state=current_market_state,
                        combined_confidence=decision.combined_confidence,
                    )
                    _ml_pred = self._ml_filter.predict(_ml_features)
                    if not _ml_pred.should_trade:
                        continue

                if decision.action == SignalType.BUY:
                    # 롱 시장 게이팅 — 지정 상태에서 차단
                    if current_market_state in self._long_block_states:
                        continue
                    buy_threshold = self._min_confidence
                    if market_confidence < 0.35:
                        buy_threshold = self._min_confidence + 0.10
                    if decision.combined_confidence >= buy_threshold:
                        buy_signals = [s for s in signals if s.signal_type == SignalType.BUY]
                        best = max(buy_signals, key=lambda s: s.confidence) if buy_signals else signals[0]
                        entry_candidates.append((sym, "long", float(decision.combined_confidence), best, decision))

                elif decision.action == SignalType.SELL:
                    # 숏 시장 게이팅
                    if self._short_sideways:
                        _allowed = {"sideways", "downtrend", "crash"}
                    else:
                        _allowed = self.SHORT_ALLOWED_STATES
                    if not self._short_all and current_market_state not in _allowed:
                        continue
                    short_threshold = max(self._min_confidence, 0.55)
                    if decision.combined_confidence >= short_threshold:
                        sell_signals = [s for s in signals if s.signal_type == SignalType.SELL]
                        best = max(sell_signals, key=lambda s: s.confidence) if sell_signals else signals[0]
                        entry_candidates.append((sym, "short", float(decision.combined_confidence), best, decision))

            # 신뢰도 내림차순 정렬 (현금 경쟁 시 강한 시그널 우선)
            entry_candidates.sort(key=lambda x: x[2], reverse=True)

            for sym, side, conf, best_signal, decision in entry_candidates:
                if len(positions) >= self._max_positions:
                    break

                # 변동성 필터: ATR 높은데 신뢰도 낮으면 스킵
                if self._volatility_filter:
                    _vf_row = all_data[sym].loc[ts]
                    _vf_atr = _vf_row.get("ATRr_14")
                    if _vf_atr and not pd.isna(_vf_atr):
                        _vf_atr_pct = float(_vf_atr) / float(_vf_row["close"]) * 100
                        # 고변동(ATR>5%) + 낮은 신뢰도 → 스킵
                        if _vf_atr_pct > 5.0 and conf < 0.70:
                            continue
                        # 극고변동(ATR>10%) → 0.80 이상만 진입
                        if _vf_atr_pct > 10.0 and conf < 0.80:
                            continue

                # 동적 포지션 사이징
                if self._dynamic_position:
                    _dyn_pos_mult = {
                        "strong_uptrend": 1.6, "uptrend": 1.2, "sideways": 0.7,
                        "downtrend": 1.2, "crash": 0.8,
                    }
                    dyn_mult = _dyn_pos_mult.get(current_market_state, 1.0)
                    eff_position_pct = self._base_position_pct * dyn_mult / lev_sqrt
                else:
                    eff_position_pct = self._position_pct

                # 롱 사이징 조절 (시장 상태별)
                if side == "long" and current_market_state in self._long_sizing_states:
                    eff_position_pct *= self._long_sizing_states[current_market_state]

                # 신뢰도 비례 포지션 사이징: 높은 확신 → 큰 포지션
                if self._confidence_sizing:
                    # conf 0.55→0.7x, 0.70→1.0x, 0.85→1.5x, 1.0→2.0x
                    _cs_mult = min(2.0, max(0.5, 0.5 + (conf - 0.55) * (1.5 / 0.45)))
                    eff_position_pct *= _cs_mult

                # 리스크 관리자 체크
                if self._risk_manager:
                    cur_pos_values = {}
                    for s, p in positions.items():
                        if s in all_data and ts in all_data[s].index:
                            cp = float(all_data[s].loc[ts, "close"])
                            ur = self._calc_unrealized_pnl(p.side, p.entry_price, cp, p.quantity)
                            cur_pos_values[s] = p.margin + ur
                        else:
                            cur_pos_values[s] = p.margin
                    ok, _ = self._risk_manager.can_buy(ts, sym, cash * eff_position_pct, cur_pos_values, equity)
                    if not ok:
                        continue

                if self._trade_limiter:
                    ok, _ = self._trade_limiter.can_buy(sym, candle_idx)
                    if not ok:
                        continue

                margin = cash * eff_position_pct
                if margin < 1.0:
                    continue

                entry_fee = margin * self._leverage * self._futures_fee
                effective_margin = margin - entry_fee
                cur_price = float(all_data[sym].loc[ts, "close"])
                qty = effective_margin * self._leverage / cur_price

                cash -= margin
                total_fees += entry_fee

                positions[sym] = FuturesPortfolioPositionState(
                    symbol=sym,
                    side=side,
                    entry_price=cur_price,
                    quantity=qty,
                    leverage=self._leverage,
                    margin=margin,
                    peak_price=cur_price,
                    entry_strategy=best_signal.strategy_name,
                    entry_idx=candle_idx,
                )

                # 동적 SL
                row = all_data[sym].loc[ts]
                if self._dynamic_sl:
                    dynamic_sl_pct[sym] = _calc_dynamic_sl(row, cur_price, current_market_state) / lev_sqrt
                else:
                    dynamic_sl_pct[sym] = self._effective_sl

                side_label = f"buy({side})"
                t = BacktestTrade(
                    timestamp=ts, side=side_label, symbol=sym,
                    price=cur_price, quantity=qty, cost=margin, fee=entry_fee,
                    strategy=best_signal.strategy_name,
                    confidence=conf,
                    reason=f"{side} 진입 {self._leverage}x | {best_signal.reason}",
                )
                trades.append(t)
                strategy_trades[best_signal.strategy_name] = strategy_trades.get(best_signal.strategy_name, 0) + 1
                last_trade_idx_per_coin[sym] = candle_idx

                if self._trade_limiter:
                    self._trade_limiter.record_buy(sym, candle_idx)

            # 4g. 포지션 보유 중: 반대 신호로 청산
            for sym, pos in list(positions.items()):
                if sym not in all_data or ts not in all_data[sym].index:
                    continue
                if sym in to_close:
                    continue  # 이미 SL/TP 등으로 청산됨

                sym_df = all_data[sym]
                sym_iloc = sym_df.index.get_loc(ts)
                if isinstance(sym_iloc, slice):
                    sym_iloc = sym_iloc.start

                row = sym_df.iloc[sym_iloc]
                cur_price = float(row["close"])

                # 쿨다운
                last_idx = last_trade_idx_per_coin.get(sym, -9999)
                if candle_idx - last_idx < self._trade_cooldown:
                    continue

                slice_df = sym_df.iloc[max(0, sym_iloc - 200):sym_iloc + 1]
                ticker = Ticker(
                    symbol=sym, last=cur_price,
                    bid=cur_price * 0.9999, ask=cur_price * 1.0001,
                    high=float(row["high"]), low=float(row["low"]),
                    volume=float(row.get("volume", 0)), timestamp=ts,
                )

                signals: list[Signal] = []
                for name, strategy in self._strategies.items():
                    try:
                        sig = await strategy.analyze(slice_df.copy(), ticker)
                        signals.append(sig)
                    except Exception:
                        pass

                if not signals:
                    continue

                decision = self._combiner.combine(signals, market_state=current_market_state)

                should_close = False
                if pos.side == "long" and decision.action == SignalType.SELL:
                    should_close = True
                elif pos.side == "short" and decision.action == SignalType.BUY:
                    should_close = True

                if should_close:
                    rel_signals = [s for s in signals if s.signal_type == decision.action]
                    best_signal = max(rel_signals, key=lambda s: s.confidence) if rel_signals else signals[0]

                    net_pnl, fee, pnl_pct, t = self._execute_futures_close(
                        ts, pos, cur_price,
                        f"sell(close-{pos.side})", best_signal.strategy_name,
                        float(decision.combined_confidence),
                        f"{pos.side} 청산 | {best_signal.reason}",
                    )
                    cash += pos.margin + net_pnl
                    total_fees += fee
                    trades.append(t)
                    strategy_trades[best_signal.strategy_name] = strategy_trades.get(best_signal.strategy_name, 0) + 1
                    coin_stats[sym]["trades"] += 1
                    coin_stats[sym]["pnl"] += net_pnl

                    if net_pnl > 0:
                        total_win_pct += abs(pnl_pct)
                        coin_stats[sym]["wins"] += 1
                        strategy_wins[best_signal.strategy_name] = strategy_wins.get(best_signal.strategy_name, 0) + 1
                        if pos.side == "long":
                            long_wins += 1
                            coin_stats[sym]["long_wins"] += 1
                        else:
                            short_wins += 1
                            coin_stats[sym]["short_wins"] += 1
                    else:
                        total_loss_pct += abs(pnl_pct)
                        coin_stats[sym]["losses"] += 1
                        strategy_losses[best_signal.strategy_name] = strategy_losses.get(best_signal.strategy_name, 0) + 1
                        if pos.side == "long":
                            long_losses += 1
                            coin_stats[sym]["long_losses"] += 1
                        else:
                            short_losses += 1
                            coin_stats[sym]["short_losses"] += 1

                    del positions[sym]
                    dynamic_sl_pct.pop(sym, None)
                    last_trade_idx_per_coin[sym] = candle_idx

        # 5. 미청산 포지션 강제 청산
        for sym, pos in list(positions.items()):
            if sym in all_data:
                last_price = float(all_data[sym].iloc[-1]["close"])
            else:
                last_price = pos.entry_price

            net_pnl, fee, pnl_pct, t = self._execute_futures_close(
                all_timestamps[-1], pos, last_price,
                f"sell(close-{pos.side})", "forced_close", 0,
                f"백테스트 종료 강제 청산 ({pos.side})",
            )
            cash += pos.margin + net_pnl
            total_fees += fee
            trades.append(t)
            coin_stats[sym]["trades"] += 1
            coin_stats[sym]["pnl"] += net_pnl
            if net_pnl > 0:
                total_win_pct += abs(pnl_pct)
                coin_stats[sym]["wins"] += 1
                if pos.side == "long":
                    long_wins += 1
                    coin_stats[sym]["long_wins"] += 1
                else:
                    short_wins += 1
                    coin_stats[sym]["short_wins"] += 1
            else:
                total_loss_pct += abs(pnl_pct)
                coin_stats[sym]["losses"] += 1
                if pos.side == "long":
                    long_losses += 1
                    coin_stats[sym]["long_losses"] += 1
                else:
                    short_losses += 1
                    coin_stats[sym]["short_losses"] += 1

        # 6. 통계 집계
        final_balance = cash
        total_pnl = final_balance - self._initial_balance
        total_pnl_pct = total_pnl / self._initial_balance * 100

        win_count = long_wins + short_wins
        loss_count = long_losses + short_losses
        total_closes = win_count + loss_count
        win_rate = win_count / total_closes * 100 if total_closes > 0 else 0

        avg_win = total_win_pct / win_count if win_count > 0 else 0
        avg_loss = total_loss_pct / loss_count if loss_count > 0 else 0
        profit_factor = (total_win_pct / total_loss_pct) if total_loss_pct > 0 else float("inf") if total_win_pct > 0 else 0

        # B&H 균등배분
        bh_pnl_pct = 0.0
        bh_count = 0
        for sym in portfolio_syms:
            if sym in all_data and len(all_data[sym]) > 1:
                df = all_data[sym]
                first_c = float(df.iloc[0]["close"])
                last_c = float(df.iloc[-1]["close"])
                bh_pnl_pct += (last_c - first_c) / first_c * 100
                bh_count += 1
        if bh_count > 0:
            bh_pnl_pct /= bh_count

        # 전략별 통계
        strategy_stats = {}
        for name in self._strategies:
            n = strategy_trades.get(name, 0)
            w = strategy_wins.get(name, 0)
            l = strategy_losses.get(name, 0)
            strategy_stats[name] = {
                "trades": n, "wins": w, "losses": l,
                "win_rate": round(w / (w + l) * 100, 1) if (w + l) > 0 else 0,
            }

        # 코인별 승률 계산
        for sym in coin_stats:
            cs = coin_stats[sym]
            wl = cs["wins"] + cs["losses"]
            cs["win_rate"] = round(cs["wins"] / wl * 100, 1) if wl > 0 else 0

        long_total = long_wins + long_losses
        short_total = short_wins + short_losses

        # 방향별 PnL 집계 (trades 기반)
        long_pnl_total = sum(t.pnl for t in trades if "long" in t.side and "sell" in t.side)
        short_pnl_total = sum(t.pnl for t in trades if "short" in t.side and "sell" in t.side)

        # 코인별 방향 PnL 집계
        for t in trades:
            if "sell" not in t.side:
                continue
            sym = t.symbol
            if sym not in coin_stats:
                continue
            if "long" in t.side:
                coin_stats[sym]["long_pnl"] += t.pnl
            elif "short" in t.side:
                coin_stats[sym]["short_pnl"] += t.pnl

        return FuturesPortfolioBacktestResult(
            symbols=portfolio_syms,
            days=days,
            leverage=self._leverage,
            initial_balance=self._initial_balance,
            final_balance=round(final_balance, 2),
            total_pnl=round(total_pnl, 2),
            total_pnl_pct=round(total_pnl_pct, 2),
            max_drawdown_pct=round(max_drawdown, 2),
            total_trades=total_closes,
            long_trades=long_total,
            short_trades=short_total,
            long_wins=long_wins,
            long_losses=long_losses,
            short_wins=short_wins,
            short_losses=short_losses,
            win_rate=round(win_rate, 1),
            avg_win_pct=round(avg_win, 2),
            avg_loss_pct=round(avg_loss, 2),
            profit_factor=round(profit_factor, 2),
            buy_hold_pnl_pct=round(bh_pnl_pct, 2),
            liquidations=liquidations,
            total_funding=round(total_funding, 2),
            total_fees=round(total_fees, 2),
            long_pnl=round(long_pnl_total, 2),
            short_pnl=round(short_pnl_total, 2),
            trades=trades,
            equity_curve=equity_curve,
            strategy_stats=strategy_stats,
            per_coin_stats=coin_stats,
            risk_events=self._risk_manager.events if self._risk_manager else [],
            risk_stats=self._risk_manager.stats if self._risk_manager else {},
            trade_limit_stats=self._trade_limiter.stats if self._trade_limiter else {},
        )


def print_futures_portfolio_result(r: FuturesPortfolioBacktestResult):
    """선물 포트폴리오 백테스트 결과 출력."""
    pnl_sign = "+" if r.total_pnl >= 0 else ""
    dd_warn = " !!!" if r.max_drawdown_pct > 15 else ""
    bh_sign = "+" if r.buy_hold_pnl_pct >= 0 else ""
    alpha = r.total_pnl_pct - r.buy_hold_pnl_pct
    alpha_sign = "+" if alpha >= 0 else ""

    print(f"\n{'='*60}")
    print(f"  선물 포트폴리오 결과 ({r.days}일, {len(r.symbols)}코인, {r.leverage}x)")
    print(f"{'='*60}")
    print(f"  초기 자산    : {r.initial_balance:>12,.2f} USDT")
    print(f"  최종 자산    : {r.final_balance:>12,.2f} USDT")
    print(f"  총 수익      : {pnl_sign}{r.total_pnl:>10,.2f} USDT  ({pnl_sign}{r.total_pnl_pct:.2f}%)")
    print(f"  최대 낙폭    : {r.max_drawdown_pct:.2f}%{dd_warn}")
    print(f"{'─'*60}")
    print(f"  균등배분 B&H : {bh_sign}{r.buy_hold_pnl_pct:.2f}%")
    print(f"  초과 수익(α) : {alpha_sign}{alpha:.2f}%")
    print(f"{'─'*60}")
    print(f"  총 청산 횟수 : {r.total_trades}회")

    long_wr = r.long_wins / r.long_trades * 100 if r.long_trades > 0 else 0
    short_wr = r.short_wins / r.short_trades * 100 if r.short_trades > 0 else 0
    long_pnl_sign = "+" if r.long_pnl >= 0 else ""
    short_pnl_sign = "+" if r.short_pnl >= 0 else ""
    print(f"  롱  거래     : {r.long_trades}회  ({r.long_wins}승/{r.long_losses}패, 승률 {long_wr:.1f}%)  PnL {long_pnl_sign}{r.long_pnl:,.2f}")
    print(f"  숏  거래     : {r.short_trades}회  ({r.short_wins}승/{r.short_losses}패, 승률 {short_wr:.1f}%)  PnL {short_pnl_sign}{r.short_pnl:,.2f}")
    print(f"  전체 승률    : {r.win_rate:.1f}%")
    print(f"  평균 수익    : +{r.avg_win_pct:.2f}% | 평균 손실: -{r.avg_loss_pct:.2f}%")
    print(f"  Profit Factor: {r.profit_factor:.2f}")
    print(f"{'─'*60}")
    print(f"  강제청산     : {r.liquidations}회")
    print(f"  총 펀딩비    : {r.total_funding:>+10,.2f} USDT")
    print(f"  총 수수료    : {r.total_fees:>10,.2f} USDT")
    print(f"{'─'*60}")

    # 코인별 분석
    if r.per_coin_stats:
        # 거래가 있는 코인만 표시, PnL 기준 정렬
        traded = {s: cs for s, cs in r.per_coin_stats.items() if cs["trades"] > 0}
        traded_count = len(traded)
        print(f"  코인별 분석 ({traded_count}코인 거래):")
        for sym, cs in sorted(traded.items(), key=lambda x: x[1]["pnl"], reverse=True):
            sym_short = sym.replace("/USDT", "").replace("/KRW", "")
            pnl_sign = "+" if cs["pnl"] >= 0 else ""
            lt = cs.get("long_wins", 0) + cs.get("long_losses", 0)
            st = cs.get("short_wins", 0) + cs.get("short_losses", 0)
            lp = cs.get("long_pnl", 0.0)
            sp = cs.get("short_pnl", 0.0)
            print(f"    {sym_short:<6}  {cs['trades']:>3}회  "
                  f"(L:{cs.get('long_wins',0)}/{lt} S:{cs.get('short_wins',0)}/{st})  "
                  f"PnL {pnl_sign}{cs['pnl']:>+10,.2f}  (L:{lp:+.1f} S:{sp:+.1f})")
        print(f"{'─'*60}")

    # 전략별 기여
    if r.strategy_stats:
        print(f"  전략별 기여:")
        for name, stat in r.strategy_stats.items():
            if stat["trades"] > 0:
                print(f"    {name:<22}: {stat['trades']:>3}회  승률 {stat['win_rate']:>5.1f}%")
        print(f"{'─'*60}")

    # 리스크 관리 통계
    if r.risk_stats:
        rs = r.risk_stats
        print(f"  리스크 관리:")
        print(f"    낙폭 일시중지 : {rs.get('drawdown_pauses', 0)}회")
        print(f"    일일손실 중지 : {rs.get('daily_loss_pauses', 0)}회")
        print(f"    비중 초과 차단: {rs.get('concentration_blocks', 0)}회")
        print(f"{'─'*60}")

    # 매매 제한 통계
    if r.trade_limit_stats:
        tls = r.trade_limit_stats
        print(f"  매매 제한:")
        print(f"    총 차단 횟수: {tls.get('total_blocks', 0)}회")
        reasons = tls.get("block_reasons", {})
        for reason, count in reasons.items():
            label = {"daily_limit": "일일 상한", "coin_limit": "코인별 상한", "cooldown": "쿨다운"}.get(reason, reason)
            print(f"      {label}: {count}회")
        print(f"{'─'*60}")

    # 매매 내역
    sell_trades = [t for t in r.trades if "sell" in t.side]
    if sell_trades:
        print(f"  매매 내역 (최근 15건):")
        for t in sell_trades[-15:]:
            sym_short = t.symbol.replace("/USDT", "").replace("/KRW", "")
            side_info = t.side.replace("sell(", "").replace(")", "")
            tag_map = {
                "sl-long": "롱손절", "sl-short": "숏손절",
                "tp-long": "롱익절", "tp-short": "숏익절",
                "trail-long": "롱트레일", "trail-short": "숏트레일",
                "close-long": "롱청산", "close-short": "숏청산",
                "liq-long": "롱강청", "liq-short": "숏강청",
            }
            tag = tag_map.get(side_info, side_info)
            print(f"    {t.timestamp.strftime('%m/%d %H:%M')}  {sym_short:>6}  "
                  f"{t.pnl_pct:>+6.1f}%  [{tag}] {t.reason[:45]}")
    print(f"{'='*60}\n")


def print_result(r: BacktestResult):
    """백테스트 결과를 보기 좋게 출력."""
    pnl_sign = "+" if r.total_pnl >= 0 else ""
    dd_warn  = " !!!" if r.max_drawdown_pct > 15 else ""
    bh_sign  = "+" if r.buy_hold_pnl_pct >= 0 else ""
    alpha    = r.total_pnl_pct - r.buy_hold_pnl_pct
    alpha_sign = "+" if alpha >= 0 else ""

    print(f"\n{'='*60}")
    print(f"  {r.symbol} 백테스트 결과 ({r.days}일)")
    print(f"{'='*60}")
    print(f"  초기 자산    : {r.initial_balance:>12,.0f} 원")
    print(f"  최종 자산    : {r.final_balance:>12,.0f} 원")
    print(f"  총 수익      : {pnl_sign}{r.total_pnl:>10,.0f} 원  ({pnl_sign}{r.total_pnl_pct:.2f}%)")
    print(f"  최대 낙폭    : {r.max_drawdown_pct:.2f}%{dd_warn}")
    print(f"{'─'*60}")
    print(f"  단순 보유(B&H): {bh_sign}{r.buy_hold_pnl_pct:.2f}%")
    print(f"  초과 수익(a) : {alpha_sign}{alpha:.2f}%")
    print(f"{'─'*60}")
    print(f"  총 매매 횟수 : {r.total_trades}회")
    print(f"  승리 / 패배  : {r.winning_trades}승 / {r.losing_trades}패")
    print(f"  승률         : {r.win_rate:.1f}%")
    print(f"  평균 수익    : +{r.avg_win_pct:.2f}% | 평균 손실: -{r.avg_loss_pct:.2f}%")
    print(f"  Profit Factor: {r.profit_factor:.2f}")
    print(f"{'─'*60}")
    print(f"  전략별 기여:")
    for name, stat in r.strategy_stats.items():
        if stat["trades"] > 0:
            print(f"    {name:<22}: {stat['trades']:>3}회  승률 {stat['win_rate']:>5.1f}%")
    print(f"{'─'*60}")

    # 모든 매도 거래 출력
    sell_trades = [t for t in r.trades if "sell" in t.side]
    if sell_trades:
        print(f"  매매 내역 (매도 기준, 최근 10건):")
        for t in sell_trades[-10:]:
            arrow = "+" if t.pnl >= 0 else ""
            tag = {"sell(sl)": "손절", "sell(tp)": "익절", "sell(close)": "강제청산"}.get(t.side, "신호")
            print(f"    {t.timestamp.strftime('%m/%d %H:%M')}  {arrow}{t.pnl_pct:>+6.1f}%  "
                  f"[{tag}] {t.reason[:50]}")
    print(f"{'='*60}\n")


# ── 거래량 서지 스코어 ────────────────────────────────────────────
def _calc_surge_score(df: pd.DataFrame, index: int, lookback: int = 20) -> float:
    """현재 캔들의 거래량 / 최근 lookback 캔들 평균 거래량."""
    if index < lookback:
        return 0.0
    vols = df.iloc[index - lookback:index]["volume"].values
    avg_vol = vols.mean()
    if avg_vol <= 0:
        return 0.0
    return float(df.iloc[index]["volume"]) / avg_vol


# ── 서지 포지션 (로테이션 백테스트 내부) ──────────────────────────
@dataclass
class _SurgePosition:
    symbol: str
    quantity: float
    avg_buy_price: float
    entry_candle_idx: int
    peak_price: float
    trailing_active: bool = False
    stop_loss_pct: float = 2.5
    take_profit_pct: float = 5.0
    trailing_activation_pct: float = 1.5
    trailing_stop_pct: float = 2.0


# ── 로테이션 백테스트 결과 ────────────────────────────────────────
@dataclass
class RotationResult:
    days: int
    initial_balance: float
    final_balance: float
    total_pnl: float
    total_pnl_pct: float
    max_drawdown_pct: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    profit_factor: float = 0.0
    buy_hold_btc_pnl_pct: float = 0.0
    total_rotations: int = 0
    symbols_traded: list[str] = field(default_factory=list)
    avg_hold_candles: float = 0.0
    rotation_log: list[dict] = field(default_factory=list)
    trades: list[BacktestTrade] = field(default_factory=list)


# ── 기본 로테이션 대상 코인 (빗썸 거래대금 상위) ──────────────────
DEFAULT_ROTATION_COINS = [
    "BTC/KRW", "ETH/KRW", "XRP/KRW", "SOL/KRW", "DOGE/KRW",
    "WLD/KRW", "AVAX/KRW", "DOT/KRW", "LINK/KRW", "ATOM/KRW",
    "BCH/KRW", "ETC/KRW", "ADA/KRW", "AAVE/KRW", "UNI/KRW",
    "SAND/KRW", "AXS/KRW", "STEEM/KRW", "MANA/KRW", "VIRTUAL/KRW",
]


# ── 로테이션 백테스터 ─────────────────────────────────────────────
class RotationBacktester:

    def __init__(
        self,
        exchange: BithumbAdapter,
        strategy_names: list[str],
        symbols: list[str] | None = None,
        initial_balance: float = 500_000,
        min_confidence: float = 0.35,
        stop_loss_pct: float = 4.0,
        take_profit_pct: float = 8.0,
        trend_filter: bool = True,
        trailing_activation: float = 1.5,
        trailing_stop: float = 2.0,
        adaptive_weights: bool = True,
        dynamic_sl: bool = False,
        agent_market: bool = True,
        surge_threshold: float = 3.0,
        rotation_cooldown: int = 6,
        require_strategy_confirm: bool = True,
        surge_buy_pct: float = 0.15,
        surge_max_hold_hours: float = 48,
        strict_confirm: bool = True,
    ):
        self._exchange = exchange
        self._initial_balance = initial_balance
        self._min_confidence = min_confidence
        self._stop_loss_pct = stop_loss_pct
        self._take_profit_pct = take_profit_pct
        self._trend_filter = trend_filter
        self._trailing_activation = trailing_activation
        self._trailing_stop = trailing_stop
        self._adaptive_weights = adaptive_weights
        self._dynamic_sl = dynamic_sl
        self._agent_market = agent_market
        self._surge_threshold = surge_threshold
        self._rotation_cooldown = rotation_cooldown
        self._require_strategy_confirm = require_strategy_confirm
        self._surge_buy_pct = surge_buy_pct
        self._surge_max_hold_hours = surge_max_hold_hours
        self._strict_confirm = strict_confirm
        self._symbols = symbols or DEFAULT_ROTATION_COINS

        all_strats = StrategyRegistry.create_all()
        self._strategies = {
            name: strat for name, strat in all_strats.items()
            if name in strategy_names
        }
        weights = {k: v for k, v in DEFAULT_WEIGHTS.items() if k in strategy_names}
        self._combiner = SignalCombiner(
            strategy_weights=weights,
            min_confidence=min_confidence,
        )

    async def prefetch_all(
        self, timeframe: str, days: int,
    ) -> dict[str, pd.DataFrame]:
        """전 코인 데이터 프리페치. 실패 코인 건너뜀."""
        all_data: dict[str, pd.DataFrame] = {}
        total = len(self._symbols)
        for idx, sym in enumerate(self._symbols, 1):
            try:
                print(f"  [{idx}/{total}] {sym} 데이터 로딩...", end="", flush=True)
                df = await fetch_history(self._exchange, sym, timeframe, days)
                all_data[sym] = df
                print(f" {len(df)}캔들")
            except Exception as e:
                print(f" 실패({e})")
        return all_data

    def _scan_surges(
        self, all_data: dict[str, pd.DataFrame], ts,
    ) -> list[tuple[str, float]]:
        """전 코인 서지 스코어 계산. threshold 이상만 (symbol, score) 반환."""
        surges = []
        for sym, df in all_data.items():
            if ts not in df.index:
                continue
            idx = df.index.get_loc(ts)
            if isinstance(idx, slice):
                idx = idx.start
            score = _calc_surge_score(df, idx)
            if score >= self._surge_threshold:
                surges.append((sym, score))
        surges.sort(key=lambda x: x[1], reverse=True)
        return surges

    async def _get_strategy_confirmation(
        self, df: pd.DataFrame, ticker: Ticker, i: int,
    ) -> tuple[bool, float]:
        """서지 매수 전략 확인 (완화 모드).

        서지 코인은 거래량으로 이미 검증됨:
        - BUY 시그널 → 허용 (전략 신뢰도)
        - HOLD (추상) → 허용 (기본 0.30)
        - SELL 시그널 → 거부
        """
        row = df.iloc[i]

        # 추세 필터
        if self._trend_filter and _is_downtrend(row):
            return False, 0.0

        slice_df = df.iloc[max(0, i - 200):i + 1]
        signals: list[Signal] = []
        for name, strategy in self._strategies.items():
            try:
                sig = await strategy.analyze(slice_df.copy(), ticker)
                signals.append(sig)
            except Exception:
                pass

        if not signals:
            return (False, 0.0) if self._strict_confirm else (True, 0.30)

        decision = self._combiner.combine(signals, market_state=current_market_state)
        if decision.action == SignalType.SELL:
            return False, 0.0
        if decision.action == SignalType.BUY and decision.combined_confidence >= self._min_confidence:
            return True, float(decision.combined_confidence)
        if self._strict_confirm:
            return False, 0.0  # BUY만 허용
        # HOLD 또는 낮은 신뢰도 → 서지가 이미 확인, 기본 허용
        return True, 0.30

    async def run(self, timeframe: str = "4h", days: int = 180) -> RotationResult:
        """로테이션 백테스트 실행 (다중 포지션, 서지 전용 프로필)."""
        tf_hours = _tf_hours(timeframe)
        max_hold_candles = int(self._surge_max_hold_hours / tf_hours) if self._surge_max_hold_hours > 0 else 0

        print(f"\n{'='*60}")
        print(f"  로테이션 백테스트 | {timeframe} | {days}일")
        print(f"  대상: {len(self._symbols)}개 코인")
        print(f"  서지 임계: {self._surge_threshold}x | 쿨다운: {self._rotation_cooldown}캔들")
        confirm_str = "OFF" if not self._require_strategy_confirm else ("BUY만" if self._strict_confirm else "완화(HOLD허용)")
        print(f"  전략 확인: {confirm_str}")
        print(f"  서지 프로필: SL {self._stop_loss_pct}% | TP {self._take_profit_pct}% | "
              f"트레일 +{self._trailing_activation}%/-{self._trailing_stop}%")
        hold_str = f"{self._surge_max_hold_hours:.0f}h ({max_hold_candles}캔들)" if max_hold_candles > 0 else "무제한"
        print(f"  최대 보유: {hold_str} | 매수 비율: 현금의 {self._surge_buy_pct*100:.0f}%")
        mm_str = "Agent(5-factor)" if self._agent_market else "Legacy(SMA+ADX)"
        print(f"  시장 감지: {mm_str}")
        print(f"{'='*60}")

        # 1. 데이터 프리페치
        all_data = await self.prefetch_all(timeframe, days)
        if not all_data:
            raise ValueError("사용 가능한 코인 데이터 없음")
        print(f"\n  {len(all_data)}개 코인 로딩 완료")

        # BTC B&H 기준
        btc_df = all_data.get("BTC/KRW")
        btc_bh_pct = 0.0
        if btc_df is not None and len(btc_df) > 1:
            btc_bh_pct = (float(btc_df.iloc[-1]["close"]) - float(btc_df.iloc[0]["close"])) / float(btc_df.iloc[0]["close"]) * 100

        # 2. 유니온 타임스탬프 인덱스
        all_timestamps = sorted(set().union(*(df.index for df in all_data.values())))
        print(f"  타임라인: {len(all_timestamps)}개 캔들 ({all_timestamps[0].date()} ~ {all_timestamps[-1].date()})")

        # 3. 시뮬레이션 상태 — 다중 포지션
        cash = self._initial_balance
        positions: dict[str, _SurgePosition] = {}
        current_market_state = "sideways"
        market_confidence = 0.5

        trades: list[BacktestTrade] = []
        rotation_log: list[dict] = []
        symbols_traded: set[str] = set()
        total_rotations = 0
        total_hold_candles = 0
        num_completed_trades = 0

        peak_equity = self._initial_balance
        max_drawdown = 0.0

        win_count = 0
        loss_count = 0
        total_win_pct = 0.0
        total_loss_pct = 0.0

        last_rotation_idx = -9999
        last_weight_eval_idx = -9999

        # 4. 캔들 루프
        for candle_idx, ts in enumerate(all_timestamps):
            # 에쿼티 계산 — 전 포지션 합산
            equity = cash
            for sym, pos in positions.items():
                if sym in all_data and ts in all_data[sym].index:
                    equity += pos.quantity * float(all_data[sym].loc[ts, "close"])
                else:
                    equity += pos.quantity * pos.avg_buy_price

            if equity > peak_equity:
                peak_equity = equity
            drawdown = (peak_equity - equity) / peak_equity * 100
            if drawdown > max_drawdown:
                max_drawdown = drawdown

            if candle_idx < 60:
                continue

            # ── 24캔들마다 시장 상태 재평가 ─────────────────────
            if candle_idx - last_weight_eval_idx >= 24:
                if btc_df is not None and ts in btc_df.index:
                    prev_state = current_market_state
                    btc_iloc = btc_df.index.get_loc(ts)
                    if isinstance(btc_iloc, slice):
                        btc_iloc = btc_iloc.start
                    current_market_state, market_confidence = _detect_market_state(
                        btc_df.loc[ts], btc_df, btc_iloc,
                        use_agent_scoring=self._agent_market,
                    )
                    if current_market_state != prev_state:
                        print(f"  [{ts.strftime('%m/%d %H:%M')}] 시장: {current_market_state} (신뢰도 {market_confidence:.0%})")
                    if self._adaptive_weights:
                        new_weights = _get_adaptive_weights(current_market_state, list(self._strategies.keys()))
                        self._combiner.update_weights(new_weights, source="backtest")
                last_weight_eval_idx = candle_idx

            # ── 보유 포지션 SL/TP/트레일링/시간 체크 ──────────
            to_close: list[str] = []
            for sym, pos in positions.items():
                if sym not in all_data or ts not in all_data[sym].index:
                    continue

                cur_price = float(all_data[sym].loc[ts, "close"])
                unrealized_pct = (cur_price - pos.avg_buy_price) / pos.avg_buy_price * 100

                if cur_price > pos.peak_price:
                    pos.peak_price = cur_price

                sell_tag = None
                sell_text = None

                # 1) 트레일링 활성화
                if (pos.trailing_activation_pct > 0
                        and not pos.trailing_active
                        and unrealized_pct >= pos.trailing_activation_pct):
                    pos.trailing_active = True

                # 2) 트레일링 스탑
                if pos.trailing_active and pos.trailing_stop_pct > 0:
                    drop = (pos.peak_price - cur_price) / pos.peak_price * 100
                    if drop >= pos.trailing_stop_pct:
                        sell_tag = "sell(trail)"
                        sell_text = f"트레일링 ({sym}) 고점 대비 -{drop:.1f}%"

                # 3) 손절
                if not sell_tag and pos.stop_loss_pct > 0 and unrealized_pct <= -pos.stop_loss_pct:
                    sell_tag = "sell(sl)"
                    sell_text = f"손절 ({sym}) {unrealized_pct:.1f}% (한도 -{pos.stop_loss_pct:.1f}%)"

                # 4) 익절 (트레일링 미활성 시)
                if (not sell_tag and not pos.trailing_active
                        and pos.take_profit_pct > 0
                        and unrealized_pct >= pos.take_profit_pct):
                    sell_tag = "sell(tp)"
                    sell_text = f"익절 ({sym}) +{unrealized_pct:.1f}%"

                # 5) 시간 기반 강제 청산
                if not sell_tag and max_hold_candles > 0:
                    held = candle_idx - pos.entry_candle_idx
                    if held >= max_hold_candles:
                        sell_tag = "sell(time)"
                        sell_text = f"시간 초과 ({sym}) {held}캔들/{max_hold_candles} (수익 {unrealized_pct:+.1f}%)"

                if sell_tag:
                    exec_price = cur_price * (1 - SLIPPAGE)
                    cost = pos.quantity * exec_price
                    fee = cost * TAKER_FEE
                    proceeds = cost - fee
                    pnl = proceeds - (pos.avg_buy_price * pos.quantity)
                    pnl_pct = pnl / (pos.avg_buy_price * pos.quantity) * 100

                    trades.append(BacktestTrade(
                        timestamp=ts, side=sell_tag, symbol=sym,
                        price=exec_price, quantity=pos.quantity, cost=cost, fee=fee,
                        strategy="surge_rotation", confidence=0,
                        reason=sell_text, pnl=pnl, pnl_pct=round(pnl_pct, 2),
                    ))
                    cash += proceeds
                    total_hold_candles += candle_idx - pos.entry_candle_idx
                    num_completed_trades += 1
                    if pnl > 0:
                        win_count += 1; total_win_pct += abs(pnl_pct)
                    else:
                        loss_count += 1; total_loss_pct += abs(pnl_pct)
                    to_close.append(sym)

            for sym in to_close:
                del positions[sym]

            # ── 서지 스캔 ─────────────────────────────────────
            surges = self._scan_surges(all_data, ts)
            if not surges:
                continue

            # 쿨다운 체크
            if candle_idx - last_rotation_idx < self._rotation_cooldown:
                continue

            for surge_sym, surge_score in surges:
                if surge_sym in positions:
                    continue  # 이미 보유 중

                sym_df = all_data[surge_sym]
                if ts not in sym_df.index:
                    continue
                sym_idx = sym_df.index.get_loc(ts)
                if isinstance(sym_idx, slice):
                    sym_idx = sym_idx.start

                sym_row = sym_df.iloc[sym_idx]
                surge_price = float(sym_row["close"])

                # 전략 확인 (서지 완화)
                if self._require_strategy_confirm:
                    ticker = Ticker(
                        symbol=surge_sym,
                        last=surge_price,
                        bid=surge_price * 0.9995,
                        ask=surge_price * 1.0005,
                        high=float(sym_row["high"]),
                        low=float(sym_row["low"]),
                        volume=float(sym_row.get("volume", 0)),
                        timestamp=ts,
                    )
                    confirmed, confidence = await self._get_strategy_confirmation(
                        sym_df, ticker, sym_idx,
                    )
                    if not confirmed:
                        continue
                else:
                    confidence = surge_score / 10.0

                # ── 서지 코인 매수 (현금의 일부) ──────────────
                trade_size = cash * self._surge_buy_pct
                if trade_size < MIN_TRADE_KRW:
                    continue

                exec_price = surge_price * (1 + SLIPPAGE)
                fee = trade_size * TAKER_FEE
                qty = (trade_size - fee) / exec_price
                cost = qty * exec_price

                cash -= (cost + fee)
                positions[surge_sym] = _SurgePosition(
                    symbol=surge_sym,
                    quantity=qty,
                    avg_buy_price=exec_price,
                    entry_candle_idx=candle_idx,
                    peak_price=surge_price,
                    stop_loss_pct=self._stop_loss_pct,
                    take_profit_pct=self._take_profit_pct,
                    trailing_activation_pct=self._trailing_activation,
                    trailing_stop_pct=self._trailing_stop,
                )
                symbols_traded.add(surge_sym)
                total_rotations += 1

                trades.append(BacktestTrade(
                    timestamp=ts, side="buy", symbol=surge_sym,
                    price=exec_price, quantity=qty, cost=cost, fee=fee,
                    strategy="surge_rotation", confidence=confidence,
                    reason=f"서지 {surge_score:.1f}x (현금 {self._surge_buy_pct*100:.0f}%)",
                ))

                rotation_log.append({
                    "timestamp": ts,
                    "from": None,
                    "to": surge_sym,
                    "surge_score": round(surge_score, 1),
                    "confidence": round(confidence, 2),
                })
                last_rotation_idx = candle_idx
                # 같은 캔들에 여러 서지 매수 가능 (현금 잔여 시)

        # 5. 종료 시 전체 청산
        for sym, pos in list(positions.items()):
            sym_df = all_data[sym]
            last_price = float(sym_df.iloc[-1]["close"])
            exec_price = last_price * (1 - SLIPPAGE)
            cost = pos.quantity * exec_price
            fee = cost * TAKER_FEE
            proceeds = cost - fee
            pnl = proceeds - (pos.avg_buy_price * pos.quantity)
            pnl_pct = pnl / (pos.avg_buy_price * pos.quantity) * 100 if pos.avg_buy_price * pos.quantity > 0 else 0

            trades.append(BacktestTrade(
                timestamp=all_timestamps[-1], side="sell(close)", symbol=sym,
                price=exec_price, quantity=pos.quantity, cost=cost, fee=fee,
                strategy="forced_close", confidence=0,
                reason=f"백테스트 종료 강제 청산 ({sym})",
                pnl=pnl, pnl_pct=round(pnl_pct, 2),
            ))
            cash += proceeds
            total_hold_candles += len(all_timestamps) - 1 - pos.entry_candle_idx
            num_completed_trades += 1
            if pnl > 0:
                win_count += 1; total_win_pct += abs(pnl_pct)
            else:
                loss_count += 1; total_loss_pct += abs(pnl_pct)

        # 6. 결과 통계
        final_balance = cash
        total_pnl = final_balance - self._initial_balance
        total_pnl_pct = total_pnl / self._initial_balance * 100
        total_sell_trades = len([t for t in trades if "sell" in t.side])
        win_rate = win_count / (win_count + loss_count) * 100 if (win_count + loss_count) > 0 else 0
        avg_win = total_win_pct / win_count if win_count > 0 else 0
        avg_loss = total_loss_pct / loss_count if loss_count > 0 else 0
        profit_factor = (total_win_pct / total_loss_pct) if total_loss_pct > 0 else float("inf") if total_win_pct > 0 else 0
        avg_hold = total_hold_candles / num_completed_trades if num_completed_trades > 0 else 0

        return RotationResult(
            days=days,
            initial_balance=self._initial_balance,
            final_balance=round(final_balance, 0),
            total_pnl=round(total_pnl, 0),
            total_pnl_pct=round(total_pnl_pct, 2),
            max_drawdown_pct=round(max_drawdown, 2),
            total_trades=total_sell_trades,
            winning_trades=win_count,
            losing_trades=loss_count,
            win_rate=round(win_rate, 1),
            avg_win_pct=round(avg_win, 2),
            avg_loss_pct=round(avg_loss, 2),
            profit_factor=round(profit_factor, 2),
            buy_hold_btc_pnl_pct=round(btc_bh_pct, 2),
            total_rotations=total_rotations,
            symbols_traded=sorted(symbols_traded),
            avg_hold_candles=round(avg_hold, 1),
            rotation_log=rotation_log,
            trades=trades,
        )


def print_rotation_result(r: RotationResult):
    """로테이션 백테스트 결과 출력."""
    pnl_sign = "+" if r.total_pnl >= 0 else ""
    dd_warn  = " !!!" if r.max_drawdown_pct > 15 else ""
    bh_sign  = "+" if r.buy_hold_btc_pnl_pct >= 0 else ""
    alpha    = r.total_pnl_pct - r.buy_hold_btc_pnl_pct
    alpha_sign = "+" if alpha >= 0 else ""

    print(f"\n{'='*60}")
    print(f"  로테이션 백테스트 결과 ({r.days}일)")
    print(f"{'='*60}")
    print(f"  초기 자산    : {r.initial_balance:>12,.0f} 원")
    print(f"  최종 자산    : {r.final_balance:>12,.0f} 원")
    print(f"  총 수익      : {pnl_sign}{r.total_pnl:>10,.0f} 원  ({pnl_sign}{r.total_pnl_pct:.2f}%)")
    print(f"  최대 낙폭    : {r.max_drawdown_pct:.2f}%{dd_warn}")
    print(f"{'─'*60}")
    print(f"  BTC B&H      : {bh_sign}{r.buy_hold_btc_pnl_pct:.2f}%")
    print(f"  초과 수익(a) : {alpha_sign}{alpha:.2f}%")
    print(f"{'─'*60}")
    print(f"  총 매매      : {r.total_trades}회  (서지 매수 {r.total_rotations}회)")
    print(f"  승리 / 패배  : {r.winning_trades}승 / {r.losing_trades}패")
    print(f"  승률         : {r.win_rate:.1f}%")
    print(f"  평균 수익    : +{r.avg_win_pct:.2f}% | 평균 손실: -{r.avg_loss_pct:.2f}%")
    print(f"  Profit Factor: {r.profit_factor:.2f}")
    print(f"  평균 보유    : {r.avg_hold_candles:.1f}캔들")
    print(f"  거래 코인    : {len(r.symbols_traded)}개 — {', '.join(r.symbols_traded)}")
    print(f"{'─'*60}")

    # 로테이션 로그
    if r.rotation_log:
        print(f"  로테이션 로그 (최근 15건):")
        for entry in r.rotation_log[-15:]:
            ts = entry["timestamp"]
            ts_str = ts.strftime("%m/%d %H:%M") if hasattr(ts, "strftime") else str(ts)[:16]
            from_sym = (entry["from"] or "현금").replace("/KRW", "")
            to_sym = entry["to"].replace("/KRW", "")
            print(f"    {ts_str}  {from_sym:>6} → {to_sym:<6}  "
                  f"서지 {entry['surge_score']:.1f}x  신뢰도 {entry['confidence']:.2f}")

    # 매도 내역
    sell_trades = [t for t in r.trades if "sell" in t.side]
    if sell_trades:
        print(f"{'─'*60}")
        print(f"  매매 내역 (최근 15건):")
        for t in sell_trades[-15:]:
            sym_short = t.symbol.replace("/KRW", "")
            tag = {"sell(sl)": "손절", "sell(tp)": "익절",
                   "sell(trail)": "트레일", "sell(rot)": "로테",
                   "sell(time)": "시간", "sell(close)": "청산"}.get(t.side, "신호")
            print(f"    {t.timestamp.strftime('%m/%d %H:%M')}  {sym_short:>6}  "
                  f"{t.pnl_pct:>+6.1f}%  [{tag}] {t.reason[:45]}")
    print(f"{'='*60}\n")


def print_portfolio_result(r: PortfolioBacktestResult):
    """포트폴리오 백테스트 결과 출력."""
    pnl_sign = "+" if r.total_pnl >= 0 else ""
    dd_warn = " !!!" if r.max_drawdown_pct > 15 else ""
    bh_sign = "+" if r.buy_hold_pnl_pct >= 0 else ""
    alpha = r.total_pnl_pct - r.buy_hold_pnl_pct
    alpha_sign = "+" if alpha >= 0 else ""

    print(f"\n{'='*60}")
    print(f"  포트폴리오 백테스트 결과 ({r.days}일, {len(r.symbols)}코인)")
    print(f"{'='*60}")
    print(f"  초기 자산    : {r.initial_balance:>12,.0f} 원")
    print(f"  최종 자산    : {r.final_balance:>12,.0f} 원")
    print(f"  총 수익      : {pnl_sign}{r.total_pnl:>10,.0f} 원  ({pnl_sign}{r.total_pnl_pct:.2f}%)")
    print(f"  최대 낙폭    : {r.max_drawdown_pct:.2f}%{dd_warn}")
    print(f"{'─'*60}")
    print(f"  균등배분 B&H : {bh_sign}{r.buy_hold_pnl_pct:.2f}%")
    print(f"  초과 수익(α) : {alpha_sign}{alpha:.2f}%")
    print(f"{'─'*60}")
    print(f"  총 매매 횟수 : {r.total_trades}회")
    print(f"  승리 / 패배  : {r.winning_trades}승 / {r.losing_trades}패")
    print(f"  승률         : {r.win_rate:.1f}%")
    print(f"  평균 수익    : +{r.avg_win_pct:.2f}% | 평균 손실: -{r.avg_loss_pct:.2f}%")
    print(f"  Profit Factor: {r.profit_factor:.2f}")
    print(f"{'─'*60}")

    # 코인별 분석
    if r.per_coin_stats:
        print(f"  코인별 분석:")
        for sym, cs in r.per_coin_stats.items():
            sym_short = sym.replace("/KRW", "").replace("/USDT", "")
            pnl_sign = "+" if cs["pnl"] >= 0 else ""
            print(f"    {sym_short:<6}  {cs['trades']:>3}회  "
                  f"({cs['wins']}승/{cs['losses']}패, 승률 {cs['win_rate']:>5.1f}%)  "
                  f"PnL {pnl_sign}{cs['pnl']:>+10,.0f}")
        print(f"{'─'*60}")

    # 전략별 기여
    if r.strategy_stats:
        print(f"  전략별 기여:")
        for name, stat in r.strategy_stats.items():
            if stat["trades"] > 0:
                print(f"    {name:<22}: {stat['trades']:>3}회  승률 {stat['win_rate']:>5.1f}%")
        print(f"{'─'*60}")

    # 리스크 관리 통계
    if r.risk_stats:
        rs = r.risk_stats
        print(f"  리스크 관리:")
        print(f"    낙폭 일시중지 : {rs.get('drawdown_pauses', 0)}회")
        print(f"    일일손실 중지 : {rs.get('daily_loss_pauses', 0)}회")
        print(f"    비중 초과 차단: {rs.get('concentration_blocks', 0)}회")
        print(f"    총 이벤트     : {rs.get('total_events', 0)}건")
        print(f"{'─'*60}")

    # 매매 제한 통계
    if r.trade_limit_stats:
        ts = r.trade_limit_stats
        print(f"  매매 제한:")
        print(f"    총 차단 횟수: {ts.get('total_blocks', 0)}회")
        reasons = ts.get("block_reasons", {})
        for reason, count in reasons.items():
            label = {"daily_limit": "일일 상한", "coin_limit": "코인별 상한", "cooldown": "쿨다운"}.get(reason, reason)
            print(f"      {label}: {count}회")
        print(f"{'─'*60}")

    # 매매 내역
    sell_trades = [t for t in r.trades if "sell" in t.side]
    if sell_trades:
        print(f"  매매 내역 (최근 15건):")
        for t in sell_trades[-15:]:
            sym_short = t.symbol.replace("/KRW", "").replace("/USDT", "")
            tag = {"sell(sl)": "손절", "sell(tp)": "익절", "sell(trail)": "트레일",
                   "sell(close)": "청산"}.get(t.side, "신호")
            print(f"    {t.timestamp.strftime('%m/%d %H:%M')}  {sym_short:>6}  "
                  f"{t.pnl_pct:>+6.1f}%  [{tag}] {t.reason[:45]}")
    print(f"{'='*60}\n")


# ── CLI 진입점 ─────────────────────────────────────────────────
async def main():
    parser = argparse.ArgumentParser(
        description="코인 자동 매매 시스템 백테스터",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  python backtest.py --symbol BTC/KRW --days 90
  python backtest.py --symbol SOL/KRW --days 30 --strategies rsi bollinger_rsi
  python backtest.py --all-coins --days 30 --balance 1000000
  python backtest.py --symbol ETH/KRW --days 60 --timeframe 4h
  python backtest.py --symbol BTC/KRW --days 30 --stop-loss 3 --take-profit 5
  python backtest.py --rotation --days 180 --timeframe 4h
  python backtest.py --rotation --days 180 --surge-threshold 2.0
  python backtest.py --portfolio --days 90
  python backtest.py --portfolio --days 540 --risk --trade-limits --asymmetric
  python backtest.py --futures --symbol BTC/USDT --days 180 --dual-timeframe
  python backtest.py --futures --portfolio --days 180
  python backtest.py --futures --portfolio --days 540 --leverage 3 --risk
        """,
    )
    parser.add_argument("--symbol",         default="BTC/KRW",  help="코인 심볼 (예: BTC/KRW)")
    parser.add_argument("--all-coins",      action="store_true", help="추적 중인 모든 코인 백테스트")
    parser.add_argument("--days",           type=int, default=30, help="백테스트 기간 (일, 기본 30)")
    parser.add_argument("--timeframe",      default="1h",        help="캔들 단위 (1h/4h/1d, 기본 1h)")
    parser.add_argument("--balance",        type=float, default=500_000, help="초기 잔액 (원, 기본 50만)")
    parser.add_argument("--strategies",     nargs="+", default=None,
                        help=f"사용할 전략 목록 (기본: 6전략)\n선택: {', '.join(ALL_STRATEGIES)}")
    parser.add_argument("--use-5",          action="store_true",
                        help="기존 5전략만 사용 (비교용)")
    parser.add_argument("--use-7",          action="store_true",
                        help="7전략 사용 (6전략 + 변동성 레짐)")
    parser.add_argument("--use-8",          action="store_true",
                        help="8전략 전체 사용 (비교용)")
    parser.add_argument("--min-confidence", type=float, default=0.50, help="최소 신뢰도 임계값 (기본 0.50)")
    parser.add_argument("--min-sell-weight", type=float, default=0.0,
                        help="SELL 전용 최소 참여 가중치 (0=비활성, 0.20=2전략 이상 필요)")
    parser.add_argument("--trade-cooldown", type=int, default=12,
                        help="매매 간 최소 캔들 수 (기본 12)")
    parser.add_argument("--stop-loss",      type=float, default=5.0,  help="손절 %% (0=비활성, 기본 5)")
    parser.add_argument("--take-profit",    type=float, default=10.0, help="익절 %% (0=비활성, 기본 10)")
    # 추세 필터
    parser.add_argument("--trend-filter",    dest="trend_filter", action="store_true", default=True,
                        help="글로벌 추세 필터 ON (기본)")
    parser.add_argument("--no-trend-filter", dest="trend_filter", action="store_false",
                        help="글로벌 추세 필터 OFF")
    # 트레일링 스탑
    parser.add_argument("--trailing-activation", type=float, default=3.0,
                        help="트레일링 활성화 수익%% (0=비활성, 기본 3)")
    parser.add_argument("--trailing-stop",  type=float, default=3.0,
                        help="트레일링 스탑 고점 대비 하락%% (기본 3)")
    # 적응형 가중치
    parser.add_argument("--adaptive-weights",    dest="adaptive_weights", action="store_true", default=True,
                        help="적응형 가중치 ON (기본)")
    parser.add_argument("--no-adaptive-weights", dest="adaptive_weights", action="store_false",
                        help="적응형 가중치 OFF")
    # 방향별 가중치
    parser.add_argument("--directional-weights", dest="directional_weights", action="store_true", default=False,
                        help="방향별 가중치 ON (롱=추세추종, 숏=평균회귀)")
    # 동적 손절 (ATR + 시장 상태)
    parser.add_argument("--dynamic-sl",         dest="dynamic_sl", action="store_true", default=False,
                        help="ATR+시장상태 기반 동적 손절 ON")
    parser.add_argument("--no-dynamic-sl",      dest="dynamic_sl", action="store_false",
                        help="동적 손절 OFF (고정 %%사용, 기본)")
    # Agent 스코어링 시장 감지
    parser.add_argument("--agent-market",       dest="agent_market", action="store_true", default=True,
                        help="Agent 스코어링 시장 감지 (기본 ON)")
    parser.add_argument("--no-agent-market",    dest="agent_market", action="store_false",
                        help="레거시(SMA+ADX) 시장 감지")
    # 비대칭 전략 (하락장 방어 / 상승장 공격)
    parser.add_argument("--asymmetric",        action="store_true", default=False,
                        help="비대칭 전략: 하락장 매수 차단 + 상승장 공격적 진입")
    # 로테이션 모드
    parser.add_argument("--rotation",       action="store_true",
                        help="거래량 서지 코인 로테이션 모드")
    parser.add_argument("--rotation-coins", nargs="+", default=None,
                        help=f"로테이션 대상 코인 (기본 {len(DEFAULT_ROTATION_COINS)}개)")
    parser.add_argument("--surge-threshold", type=float, default=2.0,
                        help="서지 임계 배수 (기본 2.0)")
    parser.add_argument("--rotation-cooldown", type=int, default=6,
                        help="로테이션 최소 간격 캔들 수 (기본 6)")
    parser.add_argument("--no-strategy-confirm", dest="strategy_confirm",
                        action="store_false", default=True,
                        help="전략 확인 없이 서지만으로 매매")
    parser.add_argument("--strict-confirm", dest="strict_confirm",
                        action="store_true", default=False,
                        help="서지 확인 강화: BUY 시그널만 허용 (HOLD 불가)")
    parser.add_argument("--surge-buy-pct",  type=float, default=0.15,
                        help="서지 매수 시 현금 비율 (기본 0.15=15%%)")
    parser.add_argument("--surge-max-hold", type=float, default=24,
                        help="서지 최대 보유 시간 (기본 24h, 0=무제한)")
    parser.add_argument("--dynamic-rotation", action="store_true", default=False,
                        help="빗썸 거래대금 상위 코인 자동 선정 (기본 OFF=하드코딩 20개)")
    parser.add_argument("--min-volume-krw", type=float, default=1e9,
                        help="동적 로테이션 최소 24h 거래대금 (기본 10억원)")
    # 선물 모드
    parser.add_argument("--futures",       action="store_true",
                        help="선물 백테스트 모드 (롱+숏+레버리지)")
    parser.add_argument("--leverage",      type=int, default=3,
                        help="선물 레버리지 (기본 3)")
    parser.add_argument("--futures-fee",   type=float, default=0.0004,
                        help="선물 수수료 (기본 0.0004=0.04%%)")
    parser.add_argument("--funding-rate",  type=float, default=0.0001,
                        help="8시간 펀딩비 (기본 0.0001=0.01%%)")
    parser.add_argument("--position-pct",  type=float, default=0.30,
                        help="포지션 크기 (현금 대비 %%, 기본 0.30=30%%)")
    parser.add_argument("--short-all",     action="store_true", default=False,
                        help="모든 시장에서 숏 허용 (기본: downtrend/crash만)")
    parser.add_argument("--short-sideways", action="store_true", default=False,
                        help="sideways+downtrend+crash에서 숏 허용")
    parser.add_argument("--dynamic-position", action="store_true", default=False,
                        help="선물: 시장 상태별 동적 포지션 사이징")
    parser.add_argument("--dynamic-portfolio", action="store_true", default=False,
                        help="선물: 거래량 기반 동적 코인 선택 (라이브와 동일)")
    parser.add_argument("--confidence-sizing", action="store_true", default=False,
                        help="신뢰도 비례 포지션 사이징 (높은 확신 → 큰 포지션)")
    parser.add_argument("--volatility-filter", action="store_true", default=False,
                        help="변동성 필터 (ATR 높은데 신뢰도 낮으면 스킵)")
    parser.add_argument("--ml-filter", type=str, default=None,
                        help="ML 시그널 필터 모델 경로 (예: data/ml_models/signal_filter.pkl)")
    parser.add_argument("--ml-min-win-prob", type=float, default=0.55,
                        help="ML 필터 최소 수익 확률 (기본 0.55)")
    parser.add_argument("--dynamic-max-coins", type=int, default=10,
                        help="동적 포트폴리오 상위 코인 수 (기본 10)")
    # 포트폴리오 모드
    parser.add_argument("--portfolio",       action="store_true",
                        help="멀티코인 포트폴리오 모드")
    parser.add_argument("--portfolio-coins", nargs="+", default=None,
                        help=f"포트폴리오 코인 목록 (기본: {' '.join(DEFAULT_PORTFOLIO_COINS)})")
    parser.add_argument("--max-positions",   type=int, default=5,
                        help="최대 동시 포지션 (기본 5)")
    parser.add_argument("--max-trade-size",  type=float, default=0.20,
                        help="코인당 자금 배분 비율 (기본 0.20=20%%)")
    # 리스크 관리
    parser.add_argument("--risk",            action="store_true", default=False,
                        help="리스크 관리 ON")
    parser.add_argument("--max-drawdown",    type=float, default=10,
                        help="드로다운 한도 %% (기본 10)")
    parser.add_argument("--daily-loss-limit", type=float, default=3,
                        help="일일 손실 한도 %% (기본 3)")
    parser.add_argument("--max-concentration", type=float, default=40,
                        help="코인 비중 한도 %% (기본 40)")
    # 매매 제한
    parser.add_argument("--trade-limits",    action="store_true", default=False,
                        help="매매 제한 ON")
    parser.add_argument("--daily-buy-limit", type=int, default=20,
                        help="일일 매수 상한 (기본 20)")
    parser.add_argument("--max-coin-buys",   type=int, default=3,
                        help="코인당 일일 매수 상한 (기본 3)")
    # 듀얼 타임프레임
    parser.add_argument("--dual-timeframe",  action="store_true", default=False,
                        help="선물 듀얼 타임프레임 ON (4h+1h)")
    # 전략 매도 모드 (현물 포트폴리오)
    parser.add_argument("--strategy-sell",   choices=["none", "voting", "paired"], default="none",
                        help="전략 매도 모드: none=SL/TP만, voting=투표, paired=진입전략만 (기본 none)")
    # 바이낸스 현물 데이터 사용 (빗썸 불가 시)
    parser.add_argument("--use-binance",     action="store_true", default=False,
                        help="현물 백테스트에 바이낸스 현물 데이터 사용 (빗썸 대신)")

    args = parser.parse_args()

    # 전략 세트 선택
    if args.use_5:
        args.strategies = ALL_STRATEGIES_5
    elif args.use_7:
        args.strategies = ALL_STRATEGIES_7
    elif args.use_8:
        args.strategies = ALL_STRATEGIES_8
    elif args.strategies is None:
        args.strategies = ALL_STRATEGIES_6  # 기본: 6전략 (0% 승률 전략 제거)

    # 전략 유효성 검사
    invalid = set(args.strategies) - set(ALL_STRATEGIES)
    if invalid:
        print(f"알 수 없는 전략: {invalid}")
        print(f"사용 가능: {ALL_STRATEGIES}")
        sys.exit(1)

    # ── 선물 모드 ──────────────────────────────────────────────
    if args.futures:
        # 선물 데이터는 바이낸스 public API에서 가져옴
        from exchange.binance_usdm_adapter import BinanceUSDMAdapter
        print("바이낸스 USDM 선물 연결 중...")
        exchange = BinanceUSDMAdapter(api_key="", api_secret="", testnet=False)
        await exchange.initialize()

        # 선물은 USDT 기본 잔액 (--balance를 USDT로 해석)
        fut_balance = args.balance
        if fut_balance >= 100_000:
            # KRW 기본값(500,000)이면 USDT 환산 (대략적 편의)
            fut_balance = 10_000
            print(f"  (--balance 미지정 — 기본 {fut_balance:,.0f} USDT 사용)")

        # 선물 기본 쿨다운: 6캔들 (사용자가 명시 안 했으면)
        fut_cooldown = args.trade_cooldown
        if fut_cooldown == 12:  # CLI 기본값 = 현물용
            fut_cooldown = 6

        # 선물 기본값 보정: CLI 기본값(현물용)이면 라이브 선물 설정으로 교체
        if args.timeframe == "1h":
            args.timeframe = "4h"
        if args.stop_loss == 5.0:
            args.stop_loss = 8.0
        if args.take_profit == 10.0:
            args.take_profit = 16.0
        if args.trailing_activation == 3.0:
            args.trailing_activation = 5.0
        if args.trailing_stop == 3.0:
            args.trailing_stop = 3.5
        if args.position_pct == 0.30:
            args.position_pct = 0.35
        if args.min_confidence == 0.50:
            args.min_confidence = 0.55

        # ── 선물 + 포트폴리오 결합 모드 ──────────────────────
        if args.portfolio:
            portfolio_coins = args.portfolio_coins
            if portfolio_coins:
                portfolio_coins = [
                    c if "/" in c else f"{c}/USDT"
                    for c in portfolio_coins
                ]
                # KRW → USDT 자동 변환
                portfolio_coins = [
                    c.replace("/KRW", "/USDT") for c in portfolio_coins
                ]

            bt = FuturesPortfolioBacktester(
                exchange=exchange,
                strategy_names=args.strategies,
                symbols=portfolio_coins,
                initial_balance=fut_balance,
                min_confidence=args.min_confidence,
                stop_loss_pct=args.stop_loss,
                take_profit_pct=args.take_profit,
                trend_filter=args.trend_filter,
                trailing_activation=args.trailing_activation,
                trailing_stop=args.trailing_stop,
                adaptive_weights=args.adaptive_weights,
                dynamic_sl=args.dynamic_sl,
                agent_market=args.agent_market,
                trade_cooldown=fut_cooldown,
                leverage=args.leverage,
                futures_fee=args.futures_fee,
                funding_rate=args.funding_rate,
                position_pct=args.position_pct,
                short_all=args.short_all,
                short_sideways=args.short_sideways,
                dynamic_position=args.dynamic_position,
                dual_timeframe=args.dual_timeframe,
                directional_weights=args.directional_weights,
                max_positions=args.max_positions,
                risk_enabled=args.risk,
                risk_max_drawdown=args.max_drawdown / 100,
                risk_daily_loss=args.daily_loss_limit / 100,
                risk_max_concentration=args.max_concentration / 100,
                trade_limit_enabled=args.trade_limits,
                trade_daily_buy_limit=args.daily_buy_limit,
                trade_max_coin_buys=args.max_coin_buys,
                dynamic_portfolio=args.dynamic_portfolio,
                dynamic_max_coins=args.dynamic_max_coins,
                confidence_sizing=args.confidence_sizing,
                volatility_filter=args.volatility_filter,
                ml_filter_path=args.ml_filter,
                ml_min_win_prob=args.ml_min_win_prob,
            )
            if args.min_sell_weight > 0:
                bt._combiner.MIN_SELL_ACTIVE_WEIGHT = args.min_sell_weight
            result = await bt.run(args.timeframe, args.days)
            print_futures_portfolio_result(result)
            await exchange.close()
            return

        # ── 선물 단일 코인 모드 ──────────────────────────────
        bt = FuturesBacktester(
            exchange=exchange,
            strategy_names=args.strategies,
            initial_balance=fut_balance,
            min_confidence=args.min_confidence,
            stop_loss_pct=args.stop_loss,
            take_profit_pct=args.take_profit,
            trend_filter=args.trend_filter,
            trailing_activation=args.trailing_activation,
            trailing_stop=args.trailing_stop,
            adaptive_weights=args.adaptive_weights,
            dynamic_sl=args.dynamic_sl,
            agent_market=args.agent_market,
            trade_cooldown=fut_cooldown,
            leverage=args.leverage,
            futures_fee=args.futures_fee,
            funding_rate=args.funding_rate,
            position_pct=args.position_pct,
            short_all=args.short_all,
            short_sideways=args.short_sideways,
            dynamic_position=args.dynamic_position,
            dual_timeframe=args.dual_timeframe,
            directional_weights=args.directional_weights,
            risk_enabled=args.risk,
            trade_limit_enabled=args.trade_limits,
            risk_max_drawdown=args.max_drawdown / 100,
            risk_daily_loss=args.daily_loss_limit / 100,
            risk_max_concentration=args.max_concentration / 100,
            trade_daily_buy_limit=args.daily_buy_limit,
            trade_max_coin_buys=args.max_coin_buys,
        )

        if args.min_sell_weight > 0:
            bt._combiner.MIN_SELL_ACTIVE_WEIGHT = args.min_sell_weight

        # 선물 심볼 자동 변환: BTC/KRW → BTC/USDT
        symbol = args.symbol
        if symbol.endswith("/KRW"):
            symbol = symbol.replace("/KRW", "/USDT")
            print(f"  심볼 자동 변환: {args.symbol} → {symbol}")

        result = await bt.run(symbol, args.timeframe, args.days)
        print_futures_result(result)
        await exchange.close()
        return

    if args.use_binance:
        from exchange.binance_spot_adapter import BinanceSpotAdapter
        print("바이낸스 현물 연결 중...")
        exchange = BinanceSpotAdapter(api_key="", api_secret="")
        await exchange.initialize()
    else:
        print("빗썸 연결 중...")
        exchange = BithumbAdapter(api_key="", api_secret="")
        await exchange.initialize()

    # ── 로테이션 모드 ─────────────────────────────────────────
    if args.rotation:
        rotation_coins = args.rotation_coins
        if rotation_coins:
            rotation_coins = [
                c if "/" in c else f"{c}/KRW" for c in rotation_coins
            ]
        elif args.dynamic_rotation:
            # 빗썸 거래대금 상위 코인 자동 선정
            print("거래대금 기준 로테이션 코인 선정 중...")
            tickers = await exchange._exchange.fetch_tickers()
            tracked = {"BTC/KRW", "ETH/KRW", "XRP/KRW", "SOL/KRW", "ADA/KRW"}
            stables = {"USDT/KRW", "USDC/KRW", "DAI/KRW", "TUSD/KRW"}
            ranked = []
            for sym, t in tickers.items():
                if not sym.endswith("/KRW"):
                    continue
                if sym in tracked or sym in stables:
                    continue
                vol = t.get("quoteVolume") or 0
                if vol >= args.min_volume_krw:
                    ranked.append((sym, vol))
            ranked.sort(key=lambda x: x[1], reverse=True)
            rotation_coins = [s for s, _ in ranked]
            print(f"  {len(rotation_coins)}개 코인 선정 (거래대금 > {args.min_volume_krw/1e8:.0f}억원)")
            if ranked[:5]:
                for s, v in ranked[:5]:
                    print(f"    {s:12s} {v/1e8:>8.1f}억원")
                print(f"    ...")

        # 서지 전용 프로필 — CLI 기본값(5/10/3/3)과 다르면 사용자 지정으로 판단
        rot_kwargs: dict = {}
        if args.stop_loss != 5.0:
            rot_kwargs["stop_loss_pct"] = args.stop_loss
        if args.take_profit != 10.0:
            rot_kwargs["take_profit_pct"] = args.take_profit
        if args.trailing_activation != 3.0:
            rot_kwargs["trailing_activation"] = args.trailing_activation
        if args.trailing_stop != 3.0:
            rot_kwargs["trailing_stop"] = args.trailing_stop

        rot = RotationBacktester(
            exchange=exchange,
            strategy_names=args.strategies,
            symbols=rotation_coins,
            initial_balance=args.balance,
            min_confidence=args.min_confidence,
            trend_filter=args.trend_filter,
            adaptive_weights=args.adaptive_weights,
            dynamic_sl=args.dynamic_sl,
            agent_market=args.agent_market,
            surge_threshold=args.surge_threshold,
            rotation_cooldown=args.rotation_cooldown,
            require_strategy_confirm=args.strategy_confirm,
            surge_buy_pct=args.surge_buy_pct,
            surge_max_hold_hours=args.surge_max_hold,
            strict_confirm=args.strict_confirm,
            **rot_kwargs,
        )
        result = await rot.run(args.timeframe, args.days)
        print_rotation_result(result)
        await exchange.close()
        return

    # ── 포트폴리오 모드 ──────────────────────────────────────
    if args.portfolio:
        portfolio_coins = args.portfolio_coins
        quote = "USDT" if args.use_binance else "KRW"
        if portfolio_coins:
            portfolio_coins = [c if "/" in c else f"{c}/{quote}" for c in portfolio_coins]
        elif args.use_binance:
            portfolio_coins = [c.replace("/KRW", "/USDT") for c in DEFAULT_PORTFOLIO_COINS]

        bt = PortfolioBacktester(
            exchange=exchange,
            strategy_names=args.strategies,
            symbols=portfolio_coins,
            initial_balance=args.balance,
            min_confidence=args.min_confidence,
            stop_loss_pct=args.stop_loss,
            take_profit_pct=args.take_profit,
            trend_filter=args.trend_filter,
            trailing_activation=args.trailing_activation,
            trailing_stop=args.trailing_stop,
            adaptive_weights=args.adaptive_weights,
            dynamic_sl=args.dynamic_sl,
            agent_market=args.agent_market,
            trade_cooldown=args.trade_cooldown,
            asymmetric=args.asymmetric,
            max_positions=args.max_positions,
            max_trade_size_pct=args.max_trade_size,
            risk_enabled=args.risk,
            max_drawdown_pct=args.max_drawdown / 100,
            daily_loss_limit_pct=args.daily_loss_limit / 100,
            max_concentration_pct=args.max_concentration / 100,
            trade_limit_enabled=args.trade_limits,
            daily_buy_limit=args.daily_buy_limit,
            max_coin_buys=args.max_coin_buys,
            strategy_sell_mode=args.strategy_sell,
        )
        result = await bt.run(args.timeframe, args.days)
        print_portfolio_result(result)
        await exchange.close()
        return

    # ── 기존 단일/전체 코인 모드 ──────────────────────────────
    backtester = Backtester(
        exchange=exchange,
        strategy_names=args.strategies,
        initial_balance=args.balance,
        min_confidence=args.min_confidence,
        stop_loss_pct=args.stop_loss,
        take_profit_pct=args.take_profit,
        trend_filter=args.trend_filter,
        trailing_activation=args.trailing_activation,
        trailing_stop=args.trailing_stop,
        adaptive_weights=args.adaptive_weights,
        dynamic_sl=args.dynamic_sl,
        agent_market=args.agent_market,
        trade_cooldown=args.trade_cooldown,
        asymmetric=args.asymmetric,
        risk_enabled=args.risk,
        trade_limit_enabled=args.trade_limits,
        risk_max_drawdown=args.max_drawdown / 100,
        risk_daily_loss=args.daily_loss_limit / 100,
        risk_max_concentration=args.max_concentration / 100,
        trade_daily_buy_limit=args.daily_buy_limit,
        trade_max_coin_buys=args.max_coin_buys,
    )

    symbols = (
        ["BTC/KRW", "ETH/KRW", "XRP/KRW", "SOL/KRW", "ADA/KRW"]
        if args.all_coins
        else [args.symbol]
    )

    results = []
    for symbol in symbols:
        try:
            r = await backtester.run(symbol, args.timeframe, args.days)
            print_result(r)
            results.append(r)
        except Exception as e:
            print(f"  {symbol} 실패: {e}")

    if len(results) > 1:
        print(f"\n{'='*60}")
        print(f"  전체 요약 ({args.days}일)")
        print(f"{'='*60}")
        total_pnl = sum(r.total_pnl for r in results)
        for r in results:
            sign = "+" if r.total_pnl_pct >= 0 else ""
            bh = "+" if r.buy_hold_pnl_pct >= 0 else ""
            print(f"  {r.symbol:<12}: {sign}{r.total_pnl_pct:>7.2f}%  "
                  f"B&H {bh}{r.buy_hold_pnl_pct:>6.2f}%  "
                  f"승률 {r.win_rate:>5.1f}%  낙폭 {r.max_drawdown_pct:.1f}%  "
                  f"({r.total_trades}회)")
        print(f"{'─'*60}")
        avg_pnl = sum(r.total_pnl_pct for r in results) / len(results)
        avg_bh  = sum(r.buy_hold_pnl_pct for r in results) / len(results)
        sign = "+" if avg_pnl >= 0 else ""
        bh_sign = "+" if avg_bh >= 0 else ""
        print(f"  평균 수익률  : {sign}{avg_pnl:.2f}%  (B&H: {bh_sign}{avg_bh:.2f}%)")
        print(f"  합산 손익    : {total_pnl:>+,.0f} 원")
        print(f"{'='*60}")

    await exchange.close()


if __name__ == "__main__":
    asyncio.run(main())
