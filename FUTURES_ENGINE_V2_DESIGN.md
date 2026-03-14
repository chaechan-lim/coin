# 선물 트레이딩 엔진 v2 — 설계 문서

## Regime-Adaptive, Always-In-Position Futures Engine

---

## 1. 현행 시스템 분석 및 문제점

### 1.1 현행 아키텍처 요약

현재 선물 엔진(`BinanceFuturesEngine`)은 `TradingEngine`을 상속(1825줄)하며, 별도 `SurgeEngine`(913줄)과 PM을 공유하는 구조이다. 핵심 흐름은:

```
7개 전략 (4h 캔들) → SignalCombiner (가중 투표) → ML Filter → BUY/SELL/HOLD
→ _process_futures_decision → _open_long/_open_short/_close_position
→ OrderManager → BinanceUSDMAdapter
```

### 1.2 근본적 문제점

1. **느린 반응 속도**: 4시간 캔들 기반 전략 평가 + 5분 폴링 주기. 시장 급변에 수 시간 지연.
2. **컨센서스 투표의 한계**: 7개 전략 중 다수가 HOLD를 내면 아무것도 못 함. 실제 PF 0.98~1.07 수준으로 Break-even.
3. **포지션이 없는 시간이 대부분**: 24시간 쿨다운 + min_confidence 0.55 + ML 필터 = 거래 빈도 극히 낮음.
4. **서지 엔진 분리의 비효율**: 별도 엔진이 PM cash를 직접 조작, 교차 충돌 체크 등 복잡성 증가.
5. **SignalType enum 버그 위험**: 문자열 기반 signal_type 비교가 여러 곳에 산재, 오류 시 고아 포지션 발생.
6. **상속 기반 구조의 경직성**: `TradingEngine`(2224줄) 상속 → 현물 로직이 선물에 불필요하게 포함.

---

## 2. 신규 시스템 아키텍처

### 2.1 3-Layer 아키텍처

```
┌─────────────────────────────────────────────────────┐
│                  FuturesEngineV2                     │
│                 (Orchestrator)                       │
├─────────────────────────────────────────────────────┤
│  Layer 1: RegimeDetector                            │
│  ┌──────────────────────────────────────────────┐   │
│  │ 1h 캔들 → ADX + BB Width + ATR + Volume     │   │
│  │ → TRENDING_UP | TRENDING_DOWN | RANGING |    │   │
│  │   VOLATILE                                   │   │
│  │ 갱신: 매 1시간 (캔들 완성 시)               │   │
│  └──────────────────────────────────────────────┘   │
│                        ↓                            │
│  Layer 2: StrategySelector                          │
│  ┌──────────────────────────────────────────────┐   │
│  │ Regime → 단일 전략 선택 (투표 없음)         │   │
│  │ TRENDING_UP   → TrendFollower               │   │
│  │ TRENDING_DOWN → TrendFollower               │   │
│  │ RANGING       → MeanReversion               │   │
│  │ VOLATILE      → VolBreakout                 │   │
│  └──────────────────────────────────────────────┘   │
│                        ↓                            │
│  Layer 3: ExecutionEngine                           │
│  ┌──────────────────────────────────────────────┐   │
│  │ 5m 캔들 (WebSocket) → 전략 시그널           │   │
│  │ → SafeOrderPipeline → Exchange               │   │
│  │ SAR (Stop-and-Reverse): 방향 즉시 전환      │   │
│  │ ATR-based sizing: 10-100% 연속 사이징       │   │
│  └──────────────────────────────────────────────┘   │
├─────────────────────────────────────────────────────┤
│  Tier 1 Manager: 5-7 코인 상시 포지션 유지         │
│  Tier 2 Scanner: 20-30 코인 기회 포착 (서지 흡수)  │
│  BalanceGuard: 잔고 무결성 + 스파이크 감지          │
│  SafeOrderPipeline: 단일 검증 경로 주문 실행        │
└─────────────────────────────────────────────────────┘
```

### 2.2 컴포넌트 의존성 그래프

```
FuturesEngineV2 (Orchestrator)
├── RegimeDetector
│   └── MarketDataService (기존 재사용)
├── StrategySelector
│   ├── TrendFollowerStrategy
│   ├── MeanReversionStrategy
│   └── VolBreakoutStrategy
├── Tier1Manager
│   ├── SafeOrderPipeline
│   └── PositionStateTracker
├── Tier2Scanner
│   ├── SafeOrderPipeline
│   └── SurgeDetector (기존 SurgeEngine 로직 흡수)
├── SafeOrderPipeline
│   ├── OrderManager (기존 재사용, 확장)
│   └── BalanceGuard
├── PortfolioManager (기존 재사용)
├── BinanceUSDMAdapter (기존 재사용)
└── BalanceGuard
    └── BinanceUSDMAdapter
```

---

## 3. Layer 1: RegimeDetector (레짐 감지)

### 3.1 입력 데이터

- **타임프레임**: 1h 캔들 (200개)
- **갱신 주기**: 매 1시간 (캔들 완성 시각, 예: 00:00, 01:00, ...)
- **기준 심볼**: BTC/USDT (시장 대표)
- **보조 확인**: 개별 코인별 레짐도 계산 (Tier 1 코인)

### 3.2 지표 및 계산

```python
@dataclass(frozen=True)
class RegimeState:
    """불변 레짐 상태 — 생성 후 변경 불가."""
    regime: Regime  # Enum
    confidence: float  # 0.0-1.0
    adx: float
    bb_width: float
    atr_pct: float
    volume_ratio: float
    trend_direction: int  # +1, 0, -1
    timestamp: datetime

class Regime(str, Enum):
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGING = "ranging"
    VOLATILE = "volatile"
```

**지표 계산**:

| 지표 | 계산식 | 역할 |
|------|--------|------|
| ADX(14) | 표준 ADX | 추세 강도 (>25: 추세, <20: 횡보) |
| BB Width | (Upper - Lower) / Middle * 100 | 변동성 대역폭 |
| ATR%(14) | ATR / Close * 100 | 정규화된 변동성 |
| Volume Ratio | Volume / SMA(Volume, 20) | 거래량 활성도 |
| EMA(20) slope | (EMA[-1] - EMA[-5]) / EMA[-5] * 100 | 추세 방향 |
| EMA(20) vs EMA(50) | EMA20 > EMA50 ? +1 : -1 | 중장기 추세 확인 |

### 3.3 레짐 분류 로직

```python
def detect_regime(self, df: pd.DataFrame) -> RegimeState:
    adx = df["adx_14"].iloc[-1]
    bb_width = self._calc_bb_width(df)
    atr_pct = df["atr_14"].iloc[-1] / df["close"].iloc[-1] * 100
    vol_ratio = df["volume"].iloc[-1] / df["volume"].rolling(20).mean().iloc[-1]
    ema20_slope = (df["ema_20"].iloc[-1] - df["ema_20"].iloc[-5]) / df["ema_20"].iloc[-5] * 100
    ema_cross = 1 if df["ema_20"].iloc[-1] > df["ema_50"].iloc[-1] else -1

    # 1차 분류: 추세 vs 비추세
    if adx >= 25:
        # 추세 존재 → 방향 판별
        if ema20_slope > 0.5 and ema_cross == 1:
            regime = Regime.TRENDING_UP
            confidence = min(1.0, (adx - 25) / 25 * 0.5 + 0.5)
        elif ema20_slope < -0.5 and ema_cross == -1:
            regime = Regime.TRENDING_DOWN
            confidence = min(1.0, (adx - 25) / 25 * 0.5 + 0.5)
        else:
            # ADX 높지만 방향 불분명 → VOLATILE
            regime = Regime.VOLATILE
            confidence = 0.6
    else:
        # 비추세
        if bb_width > 6.0 or atr_pct > 4.0:
            # 넓은 밴드 + 높은 변동 = VOLATILE
            regime = Regime.VOLATILE
            confidence = min(1.0, bb_width / 10.0)
        else:
            regime = Regime.RANGING
            confidence = min(1.0, (25 - adx) / 15 * 0.5 + 0.5)

    return RegimeState(
        regime=regime,
        confidence=confidence,
        adx=adx,
        bb_width=bb_width,
        atr_pct=atr_pct,
        volume_ratio=vol_ratio,
        trend_direction=ema_cross,
        timestamp=datetime.now(timezone.utc),
    )
```

### 3.4 레짐 전환 안정화

레짐 빈번한 전환(whipsaw)을 방지하기 위한 규칙:

1. **연속 확인**: 레짐 변경은 2회 연속 같은 레짐 감지 시에만 전환 (2시간 확인 기간)
2. **히스테리시스**: ADX 25 기준에 +/-2 버퍼 (상승 시 27 이상, 하강 시 23 이하에서 전환)
3. **최소 유지 시간**: 레짐 전환 후 최소 3시간 유지 (3 캔들)
4. **전환 시 포지션 영향**: 레짐 전환 = 전략 교체이므로, 현재 포지션의 exit condition만 재설정 (즉시 청산하지 않음)

```python
class RegimeDetector:
    _HYSTERESIS_ADX_UP = 27      # 추세 진입: ADX 27 이상
    _HYSTERESIS_ADX_DOWN = 23    # 추세 이탈: ADX 23 이하
    _MIN_REGIME_DURATION_H = 3   # 최소 레짐 유지 시간
    _CONFIRM_COUNT = 2           # 레짐 전환 확인 횟수

    def __init__(self, market_data: MarketDataService):
        self._market_data = market_data
        self._current_regime: RegimeState | None = None
        self._pending_regime: Regime | None = None
        self._pending_count: int = 0
        self._last_transition: datetime | None = None
        self._per_coin_regimes: dict[str, RegimeState] = {}
```

---

## 4. Layer 2: StrategySelector (전략 선택)

### 4.1 레짐-전략 매핑

투표 없음. 레짐이 단일 전략을 결정한다.

| Regime | 전략 | 핵심 원리 | 5m 시그널 |
|--------|------|-----------|-----------|
| TRENDING_UP | TrendFollower | 추세 순응, 풀백 매수 | EMA(9)/EMA(21) + RSI pullback |
| TRENDING_DOWN | TrendFollower | 추세 순응, 랠리 매도 | EMA(9)/EMA(21) + RSI rally |
| RANGING | MeanReversion | BB 밴드 반전 | BB(20,2) touch + RSI extreme |
| VOLATILE | VolBreakout | 돌파 추종 | Keltner Channel 돌파 + ATR 확장 |

### 4.2 전략 인터페이스 (새 베이스 클래스)

기존 `BaseStrategy`는 4h 기반이며 `analyze(df, ticker) -> Signal` 반환. 새 전략은 5분 캔들 기반이며 더 풍부한 출력이 필요하다.

```python
@dataclass(frozen=True)
class StrategyDecision:
    """전략 결정 — 불변 객체."""
    direction: Direction  # LONG, SHORT, FLAT
    confidence: float     # 0.0-1.0
    sizing_factor: float  # 0.1-1.0 (포지션 비율)
    stop_loss_atr: float  # ATR 배수 (예: 1.5)
    take_profit_atr: float  # ATR 배수 (예: 3.0)
    reason: str
    strategy_name: str
    indicators: dict

class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"  # 포지션 청산 (SAR 전환 중간 상태 없음)

class RegimeStrategy(ABC):
    """레짐별 전략 베이스 — 5분 캔들 기반."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def target_regimes(self) -> list[Regime]: ...

    @abstractmethod
    async def evaluate(
        self,
        df_5m: pd.DataFrame,    # 5분 캔들 (200개+)
        df_1h: pd.DataFrame,    # 1시간 캔들 (컨텍스트)
        regime: RegimeState,
        current_position: Direction | None,
    ) -> StrategyDecision: ...
```

### 4.3 TrendFollower 전략 상세

```python
class TrendFollowerStrategy(RegimeStrategy):
    """추세 순응 전략 — EMA 크로스 + RSI 풀백."""

    @property
    def name(self) -> str:
        return "trend_follower"

    @property
    def target_regimes(self) -> list[Regime]:
        return [Regime.TRENDING_UP, Regime.TRENDING_DOWN]

    async def evaluate(self, df_5m, df_1h, regime, current_position):
        ema_fast = df_5m["ema_9"].iloc[-1]
        ema_slow = df_5m["ema_21"].iloc[-1]
        rsi = df_5m["rsi_14"].iloc[-1]
        atr = df_5m["atr_14"].iloc[-1]
        close = df_5m["close"].iloc[-1]

        if regime.regime == Regime.TRENDING_UP:
            # 롱 진입: EMA9 > EMA21 + RSI 풀백 (30-50 영역)
            if ema_fast > ema_slow and 30 <= rsi <= 50:
                conf = min(1.0, (ema_fast - ema_slow) / ema_slow * 100 / 0.5)
                return StrategyDecision(
                    direction=Direction.LONG,
                    confidence=conf,
                    sizing_factor=self._calc_sizing(conf, atr, close),
                    stop_loss_atr=1.5,
                    take_profit_atr=3.0,
                    reason=f"Trend pullback buy: EMA9>{ema_slow:.1f}, RSI={rsi:.0f}",
                    strategy_name=self.name,
                    indicators={"ema_fast": ema_fast, "ema_slow": ema_slow, "rsi": rsi},
                )
            # SAR: 추세 이탈 시 포지션 종료
            if ema_fast < ema_slow and current_position == Direction.LONG:
                return StrategyDecision(
                    direction=Direction.SHORT,  # SAR → 숏 전환
                    confidence=0.7,
                    sizing_factor=0.5,  # 초기 전환은 작게
                    stop_loss_atr=2.0,
                    take_profit_atr=2.5,
                    reason="SAR: Trend reversal EMA cross down",
                    strategy_name=self.name,
                    indicators={},
                )

        elif regime.regime == Regime.TRENDING_DOWN:
            # 숏 진입: EMA9 < EMA21 + RSI 랠리 (50-70 영역)
            if ema_fast < ema_slow and 50 <= rsi <= 70:
                conf = min(1.0, (ema_slow - ema_fast) / ema_slow * 100 / 0.5)
                return StrategyDecision(
                    direction=Direction.SHORT,
                    confidence=conf,
                    sizing_factor=self._calc_sizing(conf, atr, close),
                    stop_loss_atr=1.5,
                    take_profit_atr=3.0,
                    reason=f"Trend rally sell: EMA9<{ema_slow:.1f}, RSI={rsi:.0f}",
                    strategy_name=self.name,
                    indicators={},
                )

        # 유지 (변경 없음)
        return StrategyDecision(
            direction=current_position or Direction.FLAT,
            confidence=0.5,
            sizing_factor=0.0,  # 변경 없음 표시
            stop_loss_atr=0, take_profit_atr=0,
            reason="Hold current position",
            strategy_name=self.name,
            indicators={},
        )

    def _calc_sizing(self, confidence: float, atr: float, close: float) -> float:
        """ATR 기반 사이징: 변동성 낮으면 크게, 높으면 작게."""
        atr_pct = atr / close * 100
        base = 0.5 + confidence * 0.3  # 0.5-0.8
        if atr_pct < 1.0:
            return min(1.0, base * 1.3)  # 저변동: 130%
        elif atr_pct > 3.0:
            return max(0.1, base * 0.5)  # 고변동: 50%
        return base
```

### 4.4 MeanReversion 전략 상세

```python
class MeanReversionStrategy(RegimeStrategy):
    """평균 회귀 전략 — BB 밴드 터치 + RSI 극단값."""

    @property
    def name(self) -> str:
        return "mean_reversion"

    @property
    def target_regimes(self) -> list[Regime]:
        return [Regime.RANGING]

    async def evaluate(self, df_5m, df_1h, regime, current_position):
        close = df_5m["close"].iloc[-1]
        bb_upper = df_5m["bb_upper_20"].iloc[-1]
        bb_lower = df_5m["bb_lower_20"].iloc[-1]
        bb_mid = df_5m["bb_mid_20"].iloc[-1]
        rsi = df_5m["rsi_14"].iloc[-1]
        atr = df_5m["atr_14"].iloc[-1]

        bb_pos = (close - bb_lower) / (bb_upper - bb_lower) if (bb_upper - bb_lower) > 0 else 0.5

        # 하단 터치 + RSI 과매도 → 롱
        if bb_pos < 0.1 and rsi < 35:
            conf = min(1.0, (35 - rsi) / 20)
            return StrategyDecision(
                direction=Direction.LONG,
                confidence=conf,
                sizing_factor=self._calc_sizing(conf, atr, close),
                stop_loss_atr=1.0,   # 횡보장은 타이트한 SL
                take_profit_atr=1.5, # 밴드 중앙까지
                reason=f"BB lower touch: pos={bb_pos:.2f}, RSI={rsi:.0f}",
                strategy_name=self.name,
                indicators={"bb_pos": bb_pos, "rsi": rsi},
            )

        # 상단 터치 + RSI 과매수 → 숏
        if bb_pos > 0.9 and rsi > 65:
            conf = min(1.0, (rsi - 65) / 20)
            return StrategyDecision(
                direction=Direction.SHORT,
                confidence=conf,
                sizing_factor=self._calc_sizing(conf, atr, close),
                stop_loss_atr=1.0,
                take_profit_atr=1.5,
                reason=f"BB upper touch: pos={bb_pos:.2f}, RSI={rsi:.0f}",
                strategy_name=self.name,
                indicators={},
            )

        # 포지션 있고 중앙 도달 → 청산
        if current_position == Direction.LONG and bb_pos > 0.5:
            return StrategyDecision(
                direction=Direction.FLAT,
                confidence=0.6,
                sizing_factor=0.0,
                stop_loss_atr=0, take_profit_atr=0,
                reason="Mean reversion target reached (BB mid)",
                strategy_name=self.name,
                indicators={},
            )

        return StrategyDecision(
            direction=current_position or Direction.FLAT,
            confidence=0.5, sizing_factor=0.0,
            stop_loss_atr=0, take_profit_atr=0,
            reason="Hold", strategy_name=self.name, indicators={},
        )
```

### 4.5 VolBreakout 전략 상세

```python
class VolBreakoutStrategy(RegimeStrategy):
    """변동성 돌파 전략 — Keltner Channel 돌파 추종."""

    @property
    def name(self) -> str:
        return "vol_breakout"

    @property
    def target_regimes(self) -> list[Regime]:
        return [Regime.VOLATILE]

    async def evaluate(self, df_5m, df_1h, regime, current_position):
        close = df_5m["close"].iloc[-1]
        ema_20 = df_5m["ema_20"].iloc[-1]
        atr = df_5m["atr_14"].iloc[-1]
        volume = df_5m["volume"].iloc[-1]
        vol_avg = df_5m["volume"].rolling(20).mean().iloc[-1]

        kc_upper = ema_20 + 2.0 * atr
        kc_lower = ema_20 - 2.0 * atr
        vol_ratio = volume / vol_avg if vol_avg > 0 else 1.0

        # 상단 돌파 + 거래량 확인 → 롱
        if close > kc_upper and vol_ratio > 1.5:
            conf = min(1.0, vol_ratio / 3.0 * 0.5 + (close - kc_upper) / atr * 0.2)
            return StrategyDecision(
                direction=Direction.LONG,
                confidence=conf,
                sizing_factor=min(0.7, conf * 0.7),  # 변동성 큰 환경 → 작게
                stop_loss_atr=2.0,
                take_profit_atr=4.0,
                reason=f"KC upper breakout: vol_ratio={vol_ratio:.1f}",
                strategy_name=self.name,
                indicators={"kc_upper": kc_upper, "vol_ratio": vol_ratio},
            )

        # 하단 돌파 + 거래량 → 숏
        if close < kc_lower and vol_ratio > 1.5:
            conf = min(1.0, vol_ratio / 3.0 * 0.5 + (kc_lower - close) / atr * 0.2)
            return StrategyDecision(
                direction=Direction.SHORT,
                confidence=conf,
                sizing_factor=min(0.7, conf * 0.7),
                stop_loss_atr=2.0,
                take_profit_atr=4.0,
                reason=f"KC lower breakout: vol_ratio={vol_ratio:.1f}",
                strategy_name=self.name,
                indicators={},
            )

        # 돌파 실패 복귀 → 포지션 청산
        if current_position == Direction.LONG and close < ema_20:
            return StrategyDecision(
                direction=Direction.FLAT, confidence=0.7,
                sizing_factor=0.0,
                stop_loss_atr=0, take_profit_atr=0,
                reason="Breakout failure: price below EMA20",
                strategy_name=self.name, indicators={},
            )

        return StrategyDecision(
            direction=current_position or Direction.FLAT,
            confidence=0.5, sizing_factor=0.0,
            stop_loss_atr=0, take_profit_atr=0,
            reason="Hold", strategy_name=self.name, indicators={},
        )
```

---

## 5. Tier 1 Manager: 상시 포지션 관리

### 5.1 대상 코인

```python
TIER1_COINS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT"]
# 최대 7개까지 확장 가능 (config에서 설정)
```

### 5.2 SAR (Stop-and-Reverse) 로직

Tier 1 코인은 항상 포지션을 유지한다. 방향 전환은 즉시 이루어진다 (쿨다운 없음).

```python
class Tier1Manager:
    """Tier 1 코인의 상시 포지션 관리."""

    def __init__(
        self,
        coins: list[str],
        safe_order: "SafeOrderPipeline",
        position_tracker: "PositionStateTracker",
        regime_detector: RegimeDetector,
        strategy_selector: StrategySelector,
        portfolio_manager: PortfolioManager,
        max_position_pct: float = 0.15,  # 코인당 최대 15% 배분
    ):
        self._coins = coins
        self._safe_order = safe_order
        self._positions = position_tracker
        self._regime = regime_detector
        self._strategies = strategy_selector
        self._pm = portfolio_manager
        self._max_position_pct = max_position_pct
        self._eval_interval_sec = 60  # 5m 캔들 확인 주기 (60초)

    async def evaluation_cycle(self, session: AsyncSession) -> None:
        """모든 Tier 1 코인 평가 (60초마다 호출)."""
        regime = self._regime.current_regime
        if regime is None:
            return

        for coin in self._coins:
            try:
                await self._evaluate_coin(session, coin, regime)
            except Exception as e:
                logger.error("tier1_eval_error", coin=coin, error=str(e))

    async def _evaluate_coin(
        self, session: AsyncSession, symbol: str, regime: RegimeState
    ) -> None:
        current_pos = self._positions.get_direction(symbol)
        strategy = self._strategies.select(regime)

        df_5m = await self._market_data.get_candles(symbol, "5m", 200)
        df_1h = await self._market_data.get_candles(symbol, "1h", 200)
        decision = await strategy.evaluate(df_5m, df_1h, regime, current_pos)

        if decision.sizing_factor == 0.0:
            return  # 변경 없음

        # SAR: 방향 전환
        if decision.direction != current_pos and decision.direction != Direction.FLAT:
            if current_pos is not None and current_pos != Direction.FLAT:
                # 기존 포지션 청산
                await self._safe_order.close_position(
                    session, symbol, current_pos,
                    reason=f"SAR: {current_pos.value} → {decision.direction.value}"
                )
            # 새 방향 진입
            await self._safe_order.open_position(
                session, symbol, decision,
                max_pct=self._max_position_pct,
            )
        elif decision.direction == Direction.FLAT and current_pos:
            # 포지션 청산 (레짐 불확실 등)
            await self._safe_order.close_position(
                session, symbol, current_pos,
                reason=decision.reason,
            )
        elif decision.direction == current_pos and decision.sizing_factor > 0:
            # 같은 방향 사이징 조정
            await self._safe_order.adjust_position_size(
                session, symbol, decision,
                max_pct=self._max_position_pct,
            )
```

### 5.3 포지션 사이징 공식

```python
def calculate_position_size(
    self,
    cash: float,
    decision: StrategyDecision,
    atr: float,
    close: float,
    leverage: int,
    max_pct: float,
) -> float:
    """ATR 기반 연속 사이징.

    공식:
      base_risk = 0.02 (계좌의 2% 리스크)
      atr_pct = atr / close
      risk_per_unit = atr_pct * decision.stop_loss_atr
      raw_size = (cash * base_risk) / risk_per_unit
      position_size = raw_size * decision.sizing_factor * leverage

    제한:
      max_margin = cash * max_pct
      final_margin = min(position_size / leverage, max_margin)
    """
    if close <= 0 or atr <= 0:
        return 0.0

    atr_pct = atr / close
    risk_per_unit = atr_pct * max(decision.stop_loss_atr, 0.5)

    # base_risk: 계좌 잔고의 2%를 1회 거래 리스크로 제한
    base_risk = 0.02
    raw_margin = (cash * base_risk) / risk_per_unit

    # Confidence + sizing_factor 반영
    adjusted_margin = raw_margin * decision.sizing_factor * decision.confidence

    # Max position cap
    max_margin = cash * max_pct
    final_margin = min(adjusted_margin, max_margin)

    # 최소 주문 금액 (5 USDT)
    if final_margin < 5.0:
        return 0.0

    notional = final_margin * leverage
    amount = notional / close

    return amount
```

---

## 6. Tier 2 Scanner: 기회 포착 (서지 흡수)

### 6.1 기존 SurgeEngine 로직 통합

현재 `SurgeEngine`의 핵심 로직을 `Tier2Scanner`로 흡수한다:
- 5m 캔들 OHLCV 기반 거래량 감지
- 서지 스코어 계산 (volume_signal + price_signal + accel_signal)
- RSI/소진/스프레드 필터

### 6.2 확장된 진입 조건

서지 외에 추가 조건:
1. **강한 모멘텀**: 1h RSI가 극단 후 반전 (RSI가 30 이하에서 35 돌파, 또는 70 이상에서 65 돌파)
2. **거래량 급등**: 5m 거래량이 60개 평균의 5배 이상 + 가격 변동 > 1.5%
3. **레짐 확인**: 해당 코인의 개별 레짐이 TRENDING 또는 VOLATILE

```python
class Tier2Scanner:
    """Tier 2 코인 스캐너 — 기존 SurgeEngine 흡수."""

    SCAN_COINS: list[str] = [
        "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT",
        "DOGE/USDT", "ADA/USDT", "AVAX/USDT", "LINK/USDT", "DOT/USDT",
        "NEAR/USDT", "SUI/USDT", "1000PEPE/USDT", "WIF/USDT", "ATOM/USDT",
        "FIL/USDT", "ARB/USDT", "OP/USDT", "TRX/USDT", "AAVE/USDT",
        "ETC/USDT", "APT/USDT", "IMX/USDT", "INJ/USDT", "SEI/USDT",
        "FET/USDT", "RENDER/USDT", "TIA/USDT", "JUP/USDT", "PENDLE/USDT",
    ]

    def __init__(
        self,
        safe_order: "SafeOrderPipeline",
        exchange: ExchangeAdapter,
        portfolio_manager: PortfolioManager,
        *,
        max_concurrent: int = 5,
        max_position_pct: float = 0.05,  # Tier 2는 작은 포지션
        max_hold_minutes: int = 120,
        scan_interval_sec: int = 60,
    ):
        self._safe_order = safe_order
        self._exchange = exchange
        self._pm = portfolio_manager
        self._max_concurrent = max_concurrent
        self._max_position_pct = max_position_pct
        self._max_hold_minutes = max_hold_minutes
        self._scan_interval = scan_interval_sec
        self._tier2_positions: dict[str, Tier2PositionState] = {}
        self._candle_vol_ratios: dict[str, float] = {}
        self._candle_price_chgs: dict[str, float] = {}
        self._cooldowns: dict[str, datetime] = {}
        self._daily_trades: int = 0

    async def scan_cycle(self, session: AsyncSession) -> None:
        """Tier 2 스캔 사이클 (60초마다)."""
        # 1. 기존 포지션 exit 체크 (시간 초과, SL/TP)
        await self._check_exits(session)

        # 2. 캔들 거래량 데이터 갱신
        await self._update_candle_volume()

        # 3. 신규 진입 스캔
        if len(self._tier2_positions) < self._max_concurrent:
            await self._scan_entries(session)
```

### 6.3 Tier 2 포지션 관리

- **SL/TP**: SL 2%, TP 4%, trailing 1%/0.8% (기존 서지 설정 유지)
- **최대 보유 시간**: 120분 (2시간)
- **포지션 크기**: 계좌의 5% (Tier 1의 1/3)
- **쿨다운**: 코인당 30분

---

## 7. SafeOrderPipeline: 주문 안전 파이프라인

**이것이 이 설계의 가장 핵심적인 부분이다.** 현재 시스템의 SignalType enum 버그, 고아 포지션, 잔고 스파이크 등 모든 문제를 방지하는 단일 검증 경로.

### 7.1 설계 원칙

1. **모든 주문은 반드시 이 파이프라인을 통과** — 직접 OrderManager 호출 금지
2. **DB 기록 선행**: 거래소 주문 전에 pending 상태로 DB에 기록
3. **원자적 잔고 변경**: DB 트랜잭션 내에서 order + position + cash를 한 번에 처리
4. **타입 안전**: `Direction` enum만 사용, 문자열 방향 비교 완전 제거
5. **사전/사후 검증**: 주문 전 잔고 확인, 주문 후 포지션 검증

### 7.2 주문 생명 주기

```
1. validate_preconditions()
   ├── cash 잔고 >= required margin?
   ├── 코인 포지션 한도 초과?
   ├── 총 포지션 한도 초과?
   ├── 하루 거래 횟수 한도?
   └── 심볼 거래 가능? (delisted 체크)

2. create_pending_order(session)
   └── DB에 Order(status=PENDING) 삽입

3. execute_on_exchange()
   └── OrderManager.create_order() → exchange API

4. reconcile_result(session)
   ├── 체결 성공:
   │   ├── Order status → FILLED
   │   ├── Position 갱신 (PM.update_position_on_buy/sell)
   │   ├── PositionStateTracker 갱신
   │   └── session.commit()
   ├── 체결 실패:
   │   ├── Order status → FAILED
   │   ├── cash 원복 (예약된 margin 반환)
   │   └── session.commit()
   └── 부분 체결:
       ├── Order status → PARTIALLY_FILLED
       ├── 체결된 부분만 Position 갱신
       └── 미체결 margin 반환

5. post_validate(session)
   ├── 내부 잔고 vs 기대값 비교
   ├── 포지션 수량 양수 검증
   └── 이상 감지 시 emit_event + pause
```

### 7.3 구현 구조

```python
class SafeOrderPipeline:
    """모든 주문의 단일 검증 경로.

    불변 규칙:
    1. 모든 주문은 이 클래스를 통해서만 실행
    2. Direction enum만 사용 (문자열 "long"/"short" 비교 절대 금지)
    3. DB 기록 → 거래소 실행 → DB 갱신 순서 엄격 준수
    4. 실패 시 반드시 원상복구 (margin 반환, 상태 rollback)
    """

    def __init__(
        self,
        order_manager: OrderManager,
        portfolio_manager: PortfolioManager,
        balance_guard: "BalanceGuard",
        exchange: ExchangeAdapter,
        leverage: int = 3,
    ):
        self._om = order_manager
        self._pm = portfolio_manager
        self._guard = balance_guard
        self._exchange = exchange
        self._leverage = leverage
        self._order_lock = asyncio.Lock()  # 동시 주문 방지

    async def open_position(
        self,
        session: AsyncSession,
        symbol: str,
        decision: StrategyDecision,
        *,
        max_pct: float = 0.15,
        tier: str = "tier1",
    ) -> bool:
        """포지션 진입 — 전체 검증 파이프라인."""
        async with self._order_lock:
            # 1. 사전 검증
            margin, amount = self._calc_order_params(symbol, decision, max_pct)
            if margin <= 0 or amount <= 0:
                return False

            pre_cash = self._pm.cash_balance

            if not self._guard.validate_pre_order(
                cash=pre_cash,
                required_margin=margin,
                symbol=symbol,
            ):
                return False

            # 2. margin 예약 (commit 전 — 실패 시 rollback으로 자동 원복)
            self._pm.cash_balance -= margin

            # 3. 거래소 실행
            side = "buy" if decision.direction == Direction.LONG else "sell"
            signal = Signal(
                strategy_name=decision.strategy_name,
                signal_type=SignalType.BUY if decision.direction == Direction.LONG else SignalType.SELL,
                confidence=decision.confidence,
                reason=decision.reason,
                indicators=decision.indicators,
            )

            try:
                order = await self._om.create_order(
                    session=session,
                    symbol=symbol,
                    side=side,
                    amount=amount,
                    price=0,  # 시장가
                    signal=signal,
                    order_type="market",
                    direction=decision.direction.value,
                    leverage=self._leverage,
                    margin_used=margin,
                )
            except Exception as e:
                # 실패 시 margin 원복
                self._pm.cash_balance = pre_cash
                logger.error("order_execution_failed", symbol=symbol, error=str(e))
                return False

            # 4. 체결 확인 및 포지션 갱신
            if order.status != "filled":
                self._pm.cash_balance = pre_cash
                logger.warning("order_not_filled", symbol=symbol, status=order.status)
                return False

            exec_price = order.executed_price or 0
            exec_qty = order.executed_quantity or 0
            fee = order.fee or (exec_price * exec_qty * 0.0004)

            # PM을 통한 포지션 갱신 (cash는 이미 차감됨, 다시 차감 방지)
            # 주의: PM.update_position_on_buy가 내부적으로 cash를 차감하므로,
            # 여기서는 원복 후 PM에 위임
            self._pm.cash_balance = pre_cash  # 원복
            await self._pm.update_position_on_buy(
                session, symbol, exec_qty, exec_price,
                margin + fee, fee,
                strategy_name=decision.strategy_name,
            )

            # 5. 사후 검증
            await self._guard.validate_post_order(
                session=session,
                symbol=symbol,
                expected_cash_delta=-margin - fee,
                pre_cash=pre_cash,
            )

            await session.commit()
            return True

    async def close_position(
        self,
        session: AsyncSession,
        symbol: str,
        direction: Direction,
        *,
        reason: str = "",
        quantity: float | None = None,  # None이면 전량 청산
    ) -> bool:
        """포지션 청산 — 전체 검증 파이프라인."""
        async with self._order_lock:
            # 포지션 조회
            result = await session.execute(
                select(Position).where(
                    Position.symbol == symbol,
                    Position.quantity > 0,
                    Position.exchange == self._om._exchange_name,
                )
            )
            position = result.scalar_one_or_none()
            if not position:
                logger.warning("close_no_position", symbol=symbol)
                return False

            qty = quantity or position.quantity
            side = "sell" if direction == Direction.LONG else "buy"
            close_signal_type = SignalType.SELL if direction == Direction.LONG else SignalType.BUY

            signal = Signal(
                strategy_name="engine_v2",
                signal_type=close_signal_type,
                confidence=1.0,
                reason=reason,
            )

            pre_cash = self._pm.cash_balance

            try:
                order = await self._om.create_order(
                    session=session,
                    symbol=symbol,
                    side=side,
                    amount=qty,
                    price=0,
                    signal=signal,
                    order_type="market",
                    direction=direction.value,
                    leverage=position.leverage or self._leverage,
                    entry_price=position.average_buy_price,
                )
            except Exception as e:
                logger.error("close_order_failed", symbol=symbol, error=str(e))
                return False

            if order.status != "filled":
                logger.warning("close_not_filled", symbol=symbol)
                return False

            exec_price = order.executed_price or 0
            fee = order.fee or 0

            await self._pm.update_position_on_sell(
                session, symbol, qty, exec_price,
                qty * exec_price, fee,
            )

            await self._guard.validate_post_order(
                session=session,
                symbol=symbol,
                expected_cash_delta=None,  # 청산은 PnL 포함이라 정확한 예측 어려움
                pre_cash=pre_cash,
            )

            await session.commit()
            return True
```

---

## 8. BalanceGuard: 잔고 무결성

### 8.1 설계

```python
class BalanceGuard:
    """잔고 무결성 감시 — 스파이크 감지 + 교차 검증."""

    # 내부 vs 거래소 차이 임계값
    DIVERGENCE_WARN_PCT = 3.0   # 3% 이상 → 경고 로그
    DIVERGENCE_PAUSE_PCT = 5.0  # 5% 이상 → 엔진 일시중지 + 알림

    # 스냅샷 간 총자산 변동 임계값
    SPIKE_WARN_PCT = 5.0        # 5% 이상 변동 → 경고
    SPIKE_REJECT_PCT = 10.0     # 10% 이상 변동 → 스냅샷 거부 + 알림

    def __init__(
        self,
        exchange: ExchangeAdapter,
        exchange_name: str = "binance_futures",
    ):
        self._exchange = exchange
        self._exchange_name = exchange_name
        self._last_total_value: float | None = None
        self._paused = False
        self._pause_reason: str = ""

    def validate_pre_order(
        self,
        cash: float,
        required_margin: float,
        symbol: str,
    ) -> bool:
        """주문 전 검증."""
        if self._paused:
            logger.warning("guard_paused", reason=self._pause_reason)
            return False

        if cash < required_margin:
            logger.warning("insufficient_cash",
                           cash=round(cash, 2),
                           required=round(required_margin, 2))
            return False

        if required_margin < 5.0:
            return False

        # NaN/Inf 체크
        if not (0 < required_margin < 1_000_000):
            logger.error("invalid_margin", margin=required_margin)
            return False

        return True

    async def validate_post_order(
        self,
        session: AsyncSession,
        symbol: str,
        expected_cash_delta: float | None,
        pre_cash: float,
    ) -> None:
        """주문 후 검증 — 잔고 일관성 체크."""
        current_cash = self._pm.cash_balance

        # 예상 변동과 비교 (가능한 경우)
        if expected_cash_delta is not None:
            expected = pre_cash + expected_cash_delta
            if abs(current_cash - expected) > 1.0:  # 1 USDT 이상 차이
                logger.warning("post_order_cash_mismatch",
                               expected=round(expected, 2),
                               actual=round(current_cash, 2),
                               symbol=symbol)

    async def periodic_reconcile(self) -> None:
        """주기적 교차 검증 (5분마다) — 내부 장부 vs 거래소."""
        try:
            balance = await self._exchange.fetch_balance()
            usdt = balance.get("USDT")
            if not usdt:
                return

            exchange_wallet = usdt.total
            # unrealized PnL 제외
            positions = await self._exchange.fetch_positions()
            unrealized = sum(
                float(p.get("unrealizedPnl", 0) or 0)
                for p in (positions or [])
            )
            exchange_cash = exchange_wallet - unrealized - sum(
                float(p.get("initialMargin", 0) or 0)
                for p in (positions or [])
            )

            internal_cash = self._pm.cash_balance
            if exchange_cash <= 0:
                return

            diff_pct = abs(exchange_cash - internal_cash) / exchange_cash * 100

            if diff_pct >= self.DIVERGENCE_PAUSE_PCT:
                self._paused = True
                self._pause_reason = (
                    f"Balance divergence {diff_pct:.1f}%: "
                    f"internal={internal_cash:.2f}, exchange={exchange_cash:.2f}"
                )
                logger.critical("BALANCE_DIVERGENCE_PAUSE", **{
                    "internal": round(internal_cash, 2),
                    "exchange": round(exchange_cash, 2),
                    "diff_pct": round(diff_pct, 1),
                })
                await emit_event(
                    "critical", "balance",
                    f"잔고 괴리 {diff_pct:.1f}% — 엔진 일시중지",
                    detail=self._pause_reason,
                )
            elif diff_pct >= self.DIVERGENCE_WARN_PCT:
                logger.warning("balance_divergence_warn",
                               diff_pct=round(diff_pct, 1))
        except Exception as e:
            logger.warning("reconcile_error", error=str(e))
```

---

## 9. PositionStateTracker: 인메모리 포지션 상태

기존 `PositionTracker` dataclass를 확장하여 더 안전한 상태 관리를 제공한다.

```python
@dataclass
class PositionState:
    """코인별 포지션 상태 (인메모리, DB와 동기화)."""
    symbol: str
    direction: Direction
    entry_price: float
    quantity: float
    margin: float
    leverage: int
    extreme_price: float  # 롱=peak, 숏=trough
    stop_loss_atr: float  # ATR 배수
    take_profit_atr: float
    trailing_activation_atr: float
    trailing_stop_atr: float
    trailing_active: bool = False
    entered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    tier: str = "tier1"  # "tier1" or "tier2"
    strategy_name: str = ""


class PositionStateTracker:
    """모든 포지션의 인메모리 상태 관리 + DB 동기화."""

    def __init__(self):
        self._states: dict[str, PositionState] = {}
        self._lock = asyncio.Lock()

    def get_direction(self, symbol: str) -> Direction | None:
        state = self._states.get(symbol)
        return state.direction if state else None

    def get_state(self, symbol: str) -> PositionState | None:
        return self._states.get(symbol)

    async def update_on_open(
        self, symbol: str, state: PositionState
    ) -> None:
        async with self._lock:
            self._states[symbol] = state

    async def update_on_close(self, symbol: str) -> None:
        async with self._lock:
            self._states.pop(symbol, None)

    async def restore_from_db(self, session: AsyncSession, exchange_name: str) -> None:
        """서버 재시작 시 DB에서 포지션 상태 복원."""
        result = await session.execute(
            select(Position).where(
                Position.quantity > 0,
                Position.exchange == exchange_name,
            )
        )
        for pos in result.scalars().all():
            direction = Direction(pos.direction) if pos.direction else Direction.LONG
            self._states[pos.symbol] = PositionState(
                symbol=pos.symbol,
                direction=direction,
                entry_price=pos.average_buy_price,
                quantity=pos.quantity,
                margin=pos.total_invested,
                leverage=pos.leverage or 3,
                extreme_price=pos.highest_price or pos.average_buy_price,
                stop_loss_atr=pos.stop_loss_pct or 1.5,
                take_profit_atr=pos.take_profit_pct or 3.0,
                trailing_activation_atr=pos.trailing_activation_pct or 2.0,
                trailing_stop_atr=pos.trailing_stop_pct or 1.0,
                trailing_active=pos.trailing_active or False,
                entered_at=pos.entered_at or datetime.now(timezone.utc),
                tier="tier1" if not pos.is_surge else "tier2",
                strategy_name=pos.strategy_name or "",
            )
```

---

## 10. FuturesEngineV2: 최상위 오케스트레이터

```python
class FuturesEngineV2:
    """선물 엔진 v2 — 레짐 적응형, 상시 포지션.

    TradingEngine을 상속하지 않음 (완전 독립 구현).
    SurgeEngine을 대체 (Tier 2로 통합).
    """

    EXCHANGE_NAME = "binance_futures"

    def __init__(
        self,
        config: AppConfig,
        exchange: ExchangeAdapter,
        market_data: MarketDataService,
        order_manager: OrderManager,
        portfolio_manager: PortfolioManager,
    ):
        self._config = config
        self._exchange = exchange
        self._market_data = market_data

        # 핵심 컴포넌트
        self._regime = RegimeDetector(market_data)
        self._strategies = StrategySelector()
        self._positions = PositionStateTracker()
        self._guard = BalanceGuard(exchange, self.EXCHANGE_NAME)

        self._safe_order = SafeOrderPipeline(
            order_manager=order_manager,
            portfolio_manager=portfolio_manager,
            balance_guard=self._guard,
            exchange=exchange,
            leverage=config.binance.default_leverage,
        )

        tier1_coins = list(config.binance.tracked_coins)
        self._tier1 = Tier1Manager(
            coins=tier1_coins,
            safe_order=self._safe_order,
            position_tracker=self._positions,
            regime_detector=self._regime,
            strategy_selector=self._strategies,
            portfolio_manager=portfolio_manager,
            max_position_pct=0.15,
        )

        self._tier2 = Tier2Scanner(
            safe_order=self._safe_order,
            exchange=exchange,
            portfolio_manager=portfolio_manager,
            max_concurrent=5,
            max_position_pct=0.05,
        )

        self._pm = portfolio_manager
        self._om = order_manager
        self._is_running = False
        self._tasks: list[asyncio.Task] = []

    # ── EngineRegistry 호환 인터페이스 ──────────

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def tracked_coins(self) -> list[str]:
        return list(self._config.binance.tracked_coins)

    @property
    def exchange_name(self) -> str:
        return self.EXCHANGE_NAME

    def set_engine_registry(self, registry) -> None:
        self._engine_registry = registry

    def set_recovery_manager(self, recovery) -> None:
        self._recovery_manager = recovery

    def set_broadcast_callback(self, callback) -> None:
        self._broadcast_callback = callback

    # ── 시작/중지 ──────────────────────────────

    async def initialize(self) -> None:
        """초기화: 포지션 복원 + 레버리지 설정."""
        sf = get_session_factory()
        async with sf() as session:
            await self._positions.restore_from_db(session, self.EXCHANGE_NAME)

        for symbol in self.tracked_coins:
            try:
                await self._exchange.set_leverage(
                    symbol, self._config.binance.default_leverage
                )
            except Exception:
                pass

    async def start(self) -> None:
        if self._is_running:
            return
        self._is_running = True
        await emit_event("info", "engine", "선물 엔진 v2 시작")

        self._tasks = [
            asyncio.create_task(self._regime_loop(), name="regime_detector"),
            asyncio.create_task(self._tier1_loop(), name="tier1_manager"),
            asyncio.create_task(self._tier2_loop(), name="tier2_scanner"),
            asyncio.create_task(self._ws_monitor_loop(), name="ws_monitor"),
            asyncio.create_task(self._balance_guard_loop(), name="balance_guard"),
            asyncio.create_task(self._income_loop(), name="income_poll"),
        ]
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def stop(self) -> None:
        self._is_running = False
        for task in self._tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._tasks = []
        try:
            await self._exchange.close_ws()
        except Exception:
            pass
        await emit_event("info", "engine", "선물 엔진 v2 중지")

    # ── 루프들 ──────────────────────────────────

    async def _regime_loop(self) -> None:
        """1시간마다 레짐 업데이트."""
        while self._is_running:
            try:
                await self._regime.update()
            except Exception as e:
                logger.error("regime_update_error", error=str(e))
            await asyncio.sleep(3600)  # 1시간

    async def _tier1_loop(self) -> None:
        """60초마다 Tier 1 코인 평가."""
        while self._is_running:
            try:
                sf = get_session_factory()
                async with sf() as session:
                    await self._tier1.evaluation_cycle(session)
            except Exception as e:
                logger.error("tier1_loop_error", error=str(e))
            await asyncio.sleep(60)

    async def _tier2_loop(self) -> None:
        """60초마다 Tier 2 스캔."""
        while self._is_running:
            try:
                sf = get_session_factory()
                async with sf() as session:
                    await self._tier2.scan_cycle(session)
            except Exception as e:
                logger.error("tier2_loop_error", error=str(e))
            await asyncio.sleep(60)

    async def _ws_monitor_loop(self) -> None:
        """WebSocket 가격 모니터 — 실시간 SL/TP 체크."""
        # 기존 futures_engine._price_monitor_loop 로직 재사용
        # + 자동 재연결 (지수 백오프)
        ...

    async def _balance_guard_loop(self) -> None:
        """5분마다 잔고 교차 검증."""
        while self._is_running:
            await asyncio.sleep(300)
            try:
                await self._guard.periodic_reconcile()
            except Exception as e:
                logger.warning("balance_guard_error", error=str(e))

    async def _income_loop(self) -> None:
        """8시간마다 펀딩비 반영."""
        await asyncio.sleep(30)
        while self._is_running:
            try:
                await self._pm.apply_income(self._exchange)
            except Exception:
                pass
            await asyncio.sleep(8 * 3600)
```

---

## 11. Configuration: 새 Config 클래스

```python
class FuturesV2Config(BaseSettings):
    """선물 엔진 v2 전용 설정."""
    enabled: bool = False
    mode: str = "paper"
    leverage: int = 3
    max_leverage: int = 5

    # Tier 1
    tier1_coins: list[str] = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT"]
    tier1_max_position_pct: float = 0.15    # 코인당 최대 15%
    tier1_eval_interval_sec: int = 60       # 60초
    tier1_base_risk_pct: float = 0.02       # 1회 리스크 2%

    # Tier 2
    tier2_enabled: bool = True
    tier2_max_concurrent: int = 5
    tier2_max_position_pct: float = 0.05    # 코인당 최대 5%
    tier2_max_hold_minutes: int = 120
    tier2_scan_interval_sec: int = 60
    tier2_vol_threshold: float = 5.0
    tier2_price_threshold: float = 1.5
    tier2_sl_pct: float = 2.0
    tier2_tp_pct: float = 4.0
    tier2_daily_trade_limit: int = 20

    # Regime detector
    regime_timeframe: str = "1h"
    regime_adx_threshold: float = 25.0
    regime_hysteresis: float = 2.0
    regime_min_duration_h: int = 3
    regime_confirm_count: int = 2

    # Balance guard
    balance_divergence_warn_pct: float = 3.0
    balance_divergence_pause_pct: float = 5.0
    balance_check_interval_sec: int = 300

    # WebSocket
    ws_enabled: bool = True
    ws_reconnect_min_sec: int = 5
    ws_reconnect_max_sec: int = 300

    model_config = {"env_prefix": "FUTURES_V2_"}
```

---

## 12. Database 변경

### 12.1 기존 테이블 변경 없음

새 엔진은 기존 `positions`, `orders`, `trades` 테이블을 그대로 사용한다. `exchange="binance_futures"`로 격리.

### 12.2 새 테이블

```python
class RegimeLog(Base):
    """레짐 변경 이력."""
    __tablename__ = "regime_logs"
    __table_args__ = (
        Index("ix_regime_logs_at", "detected_at"),
    )

    id = Column(Integer, primary_key=True)
    exchange = Column(String(20), default="binance_futures")
    symbol = Column(String(20), default="BTC/USDT")  # 기준 심볼
    regime = Column(String(20), nullable=False)       # trending_up, etc.
    prev_regime = Column(String(20), nullable=True)
    confidence = Column(Float)
    adx = Column(Float)
    bb_width = Column(Float)
    atr_pct = Column(Float)
    volume_ratio = Column(Float)
    detected_at = Column(DateTime, default=_utcnow)
```

### 12.3 Position 테이블 활용

기존 `Position.strategy_name` 컬럼에 v2 전략 이름을 기록. `Position.is_surge` 컬럼은 Tier 2 포지션 식별에 재활용. 신규 컬럼 추가 불필요.

---

## 13. API 변경

### 13.1 기존 API 호환 유지

`EngineRegistry` 인터페이스를 준수하므로 기존 프론트엔드 API는 변경 없이 작동:
- `GET /api/v1/portfolio?exchange=binance_futures`
- `GET /api/v1/positions?exchange=binance_futures`
- `POST /api/v1/engine/start?exchange=binance_futures`

### 13.2 새 API 엔드포인트

```
GET /api/v1/futures-v2/regime
Response: {
    "regime": "trending_up",
    "confidence": 0.78,
    "adx": 32.5,
    "bb_width": 4.2,
    "atr_pct": 2.1,
    "volume_ratio": 1.3,
    "since": "2026-03-14T10:00:00Z",
    "pending_transition": null
}

GET /api/v1/futures-v2/tier1/status
Response: {
    "coins": [
        {
            "symbol": "BTC/USDT",
            "direction": "long",
            "confidence": 0.72,
            "sizing": 0.12,
            "entry_price": 85000.0,
            "current_price": 86200.0,
            "pnl_pct": 4.2,
            "strategy": "trend_follower",
            "regime": "trending_up"
        },
        ...
    ]
}

GET /api/v1/futures-v2/tier2/status
Response: {
    "open_positions": 2,
    "max_concurrent": 5,
    "daily_trades": 8,
    "scan_scores": [
        {"symbol": "NEAR/USDT", "score": 0.72, "vol_ratio": 6.3},
        ...
    ]
}
```

---

## 14. 마이그레이션 계획

v1은 구조적 결함이 검증되었으므로 병행 비교 없이, 백테스트 우선 검증 후 전환한다.

### 14.1 백테스트 검증 (필수 게이트)

1. 5m 540일 데이터 수집 (5코인)
2. Walk-Forward 검증 6회 실행
3. **통과 기준**: PF > 1.5, MDD < 15%, 승률 > 55%
4. 미달 시 파라미터 조정 후 재검증 — 통과 전까지 다음 단계 진행 불가

### 14.2 Live 전환 (1일)

1. v1 + 서지 엔진 중지, 기존 포지션 청산
2. `FuturesEngineV2`를 `exchange_name="binance_futures"`, `mode=live`로 즉시 실행
3. 최초 3일간 Tier1 사이징 50%로 시작 (안전 마진)
4. 정상 확인 후 100% 사이징

### 14.4 롤백 계획

- `FUTURES_V2_ENABLED=false` → 즉시 v1 복원 (config 한 줄)
- v2 전용 DB 테이블(`regime_logs`)은 v1에 영향 없음
- `EngineRegistry`에서 교체만으로 전환 완료

---

## 15. 백테스트 전략

### 15.1 5분 데이터 수집

```bash
# 540일 5m 캔들 수집 (필요 캔들 수: 540 * 24 * 12 = 155,520)
# Binance API: 1회 1000개 → 156회 호출 (rate limit 고려 ~30분)
python scripts/fetch_5m_data.py --coins BTC,ETH,SOL,XRP,BNB --days 540
```

### 15.2 Walk-Forward 검증

```python
# 학습 240일 + 검증 60일 + 테스트 60일, 60일 슬라이딩
# 총 6회 walk-forward 구간

WALK_FORWARD_PARAMS = {
    "train_days": 240,
    "val_days": 60,
    "test_days": 60,
    "step_days": 60,
}
```

### 15.3 슬리피지 모델

```python
# 5m 캔들 기반 시장가 슬리피지 추정
def estimate_slippage(volume: float, order_size: float, atr: float) -> float:
    """시장 충격 모델: order_size / volume 비율 + ATR 기반."""
    impact = (order_size / volume) * 0.1  # 0.1% per 1% of volume
    spread = atr * 0.05  # ATR의 5%를 스프레드로
    return impact + spread
```

### 15.4 성과 지표 목표

| 지표 | 현재 v1 | 목표 v2 | 비고 |
|------|---------|---------|------|
| Profit Factor | 0.98-1.07 | > 1.5 | 주요 KPI |
| 연간 수익률 | ~0% | > 30% | 3x 레버리지 기준 |
| MDD | ~15% | < 15% | 리스크 우선 |
| 승률 | ~45% | > 55% | 소규모 빈번 거래 |
| 일 평균 거래 수 | 0.5회 | 5-15회 | 빈도 10배 증가 |
| Sharpe Ratio | < 0.5 | > 1.5 | 위험 대비 수익 |

---

## 16. 재사용 컴포넌트

### 16.1 그대로 재사용

| 컴포넌트 | 파일 | 이유 |
|----------|------|------|
| `BinanceUSDMAdapter` | `exchange/binance_usdm_adapter.py` | WebSocket, REST, 서킷브레이커 완성도 높음 |
| `OrderManager` | `engine/order_manager.py` | DB 기록 + 거래소 실행 잘 분리됨 |
| `PortfolioManager` | `engine/portfolio_manager.py` | 선물 PnL, cash 관리, 스냅샷 기능 완성 |
| `MarketDataService` | `services/market_data.py` | 캐싱, 지표 계산, 재시도 로직 |
| `ExchangeAdapter` | `exchange/base.py` | 인터페이스 변경 불필요 |
| `EngineRegistry` | `api/dependencies.py` | API 레이어 호환 |
| `RecoveryManager` | `engine/recovery.py` | 자기 치유 로직 |
| `HealthMonitor` | `engine/health_monitor.py` | 상태 감시 |
| DB Models | `core/models.py` | Position, Order, Trade 등 |
| Event Bus | `core/event_bus.py` | 알림 시스템 |

### 16.2 수정 필요

| 컴포넌트 | 수정 내용 |
|----------|-----------|
| `config.py` | `FuturesV2Config` 클래스 추가 |
| `main.py` | v2 엔진 조립 코드 추가 (v1과 config로 전환) |
| `core/enums.py` | `Regime` enum, `Direction` enum 추가 |
| `MarketDataService` | 5m 캔들용 지표 추가 (BB bands, Keltner Channel) |

### 16.3 신규 작성

| 파일 | 설명 |
|------|------|
| `engine/futures_engine_v2.py` | 최상위 오케스트레이터 |
| `engine/regime_detector.py` | 레짐 감지 (Layer 1) |
| `engine/strategy_selector.py` | 전략 선택 (Layer 2) |
| `engine/tier1_manager.py` | Tier 1 상시 포지션 관리 |
| `engine/tier2_scanner.py` | Tier 2 기회 포착 (서지 흡수) |
| `engine/safe_order_pipeline.py` | 주문 안전 파이프라인 |
| `engine/balance_guard.py` | 잔고 무결성 감시 |
| `engine/position_state_tracker.py` | 인메모리 포지션 상태 |
| `strategies/regime_base.py` | 레짐 전략 베이스 클래스 |
| `strategies/trend_follower.py` | 추세 추종 전략 |
| `strategies/mean_reversion.py` | 평균 회귀 전략 |
| `strategies/vol_breakout.py` | 변동성 돌파 전략 |
| `core/models.py` (수정) | `RegimeLog` 테이블 추가 |

---

## 17. 파일 생성/수정 전체 목록

### 신규 파일 (12개)

```
backend/engine/futures_engine_v2.py         # ~400줄, 오케스트레이터
backend/engine/regime_detector.py           # ~250줄, 레짐 감지
backend/engine/strategy_selector.py         # ~80줄, 전략 선택
backend/engine/tier1_manager.py             # ~300줄, Tier 1 관리
backend/engine/tier2_scanner.py             # ~350줄, Tier 2 스캔 (서지 흡수)
backend/engine/safe_order_pipeline.py       # ~300줄, 주문 안전 파이프라인
backend/engine/balance_guard.py             # ~200줄, 잔고 무결성
backend/engine/position_state_tracker.py    # ~150줄, 포지션 상태 추적
backend/strategies/regime_base.py           # ~60줄, 레짐 전략 인터페이스
backend/strategies/trend_follower.py        # ~200줄, 추세 추종
backend/strategies/mean_reversion.py        # ~180줄, 평균 회귀
backend/strategies/vol_breakout.py          # ~180줄, 변동성 돌파
```

### 수정 파일 (5개)

```
backend/config.py                           # FuturesV2Config 추가, AppConfig에 연결
backend/core/enums.py                       # Regime, Direction enum 추가
backend/core/models.py                      # RegimeLog 테이블 추가
backend/main.py                             # v2 엔진 조립 블록 추가
backend/services/market_data.py             # 5m 지표 확장 (BB, KC)
```

### 테스트 파일 (12개 이상)

```
backend/tests/test_regime_detector.py
backend/tests/test_strategy_selector.py
backend/tests/test_tier1_manager.py
backend/tests/test_tier2_scanner.py
backend/tests/test_safe_order_pipeline.py
backend/tests/test_balance_guard.py
backend/tests/test_position_state_tracker.py
backend/tests/test_trend_follower.py
backend/tests/test_mean_reversion.py
backend/tests/test_vol_breakout.py
backend/tests/test_futures_engine_v2.py
backend/tests/test_futures_v2_integration.py
```

---

## 18. 구현 순서 (권장)

### Sprint 1: 안전 인프라 (3-4일)
1. `core/enums.py` — `Regime`, `Direction` enum 추가
2. `engine/balance_guard.py` — 잔고 무결성
3. `engine/safe_order_pipeline.py` — 주문 안전 파이프라인
4. `engine/position_state_tracker.py` — 포지션 상태 추적
5. 테스트 작성 (위 4개)

### Sprint 2: 레짐 + 전략 (3-4일)
1. `engine/regime_detector.py` — 레짐 감지
2. `strategies/regime_base.py` — 인터페이스
3. `strategies/trend_follower.py`
4. `strategies/mean_reversion.py`
5. `strategies/vol_breakout.py`
6. `engine/strategy_selector.py` — 전략 선택
7. 테스트 작성

### Sprint 3: Tier 관리 + 통합 (3-4일)
1. `engine/tier1_manager.py`
2. `engine/tier2_scanner.py`
3. `engine/futures_engine_v2.py` — 오케스트레이터
4. `config.py` — FuturesV2Config
5. `core/models.py` — RegimeLog
6. `main.py` — v2 조립
7. 통합 테스트

### Sprint 4: 백테스트 + 검증 — 필수 게이트 (3-5일)
1. 5m 데이터 수집 스크립트
2. v2 전용 백테스터 작성
3. Walk-forward 검증 (PF > 1.5, MDD < 15%, 승률 > 55%)
4. 미달 시 파라미터 조정 + 재검증 (통과 전까지 다음 단계 불가)

### Sprint 5: Live 전환 (1-2일)
1. v1 + 서지 중지, 기존 포지션 청산
2. v2 Live 즉시 실행 (최초 3일 50% 사이징)
3. 정상 확인 후 100% 사이징 + 모니터링

---

## 19. 잠재적 리스크 및 완화책

| 리스크 | 영향 | 완화책 |
|--------|------|--------|
| 레짐 오감지 | 잘못된 전략 실행 | 2회 확인 + 히스테리시스 + 최소 유지 시간 |
| SAR 빈번 전환 | 수수료 손실 | 5m 캔들 기반이므로 최소 5분 간격, 컨펌 로직 추가 |
| Tier 1 손실 누적 | 계좌 감소 | ATR 기반 사이징으로 변동성 큰 시 자동 축소 + 일일 손실 한도 |
| 5m 데이터 지연 | 시그널 지연 | WebSocket 우선, REST 폴백 |
| 거래소 장애 | 주문 실패 | SafeOrderPipeline의 rollback + 서킷브레이커 |
| 잔고 괴리 | 고아 포지션 | BalanceGuard 5분 교차 검증, 5% 초과 시 자동 정지 |
| 메모리 증가 | 라즈베리파이 한계 | 5m 캔들 200개 * 30코인 * ~1KB = ~6MB, 현행 범위 내 |

---

### Critical Files for Implementation
List of the 5 most critical files for implementing this plan:

- `/home/chans/coin/backend/engine/safe_order_pipeline.py` (신규) - 모든 주문의 단일 검증 경로, 잔고 스파이크/고아 포지션 방지의 핵심. 가장 먼저 구현하여 나머지 모든 컴포넌트가 이를 통해 주문 실행.
- `/home/chans/coin/backend/engine/regime_detector.py` (신규) - 3-Layer 아키텍처의 핵심 Layer 1. ADX + BB Width + ATR 기반 레짐 분류 + 히스테리시스 안정화. 전체 시스템의 전략 선택을 결정.
- `/home/chans/coin/backend/engine/futures_engine_v2.py` (신규) - 최상위 오케스트레이터. 6개 비동기 루프(regime/tier1/tier2/ws/balance/income) 조율. EngineRegistry 호환 인터페이스 제공.
- `/home/chans/coin/backend/engine/tier1_manager.py` (신규) - Tier 1 상시 포지션 SAR 로직, 5m 캔들 기반 60초 평가 사이클. 시스템 수익의 핵심.
- `/home/chans/coin/backend/config.py` (수정) - `FuturesV2Config` 클래스 추가. 모든 새 컴포넌트의 파라미터 중앙 관리. `AppConfig`에 `futures_v2` 필드 연결. 기존 `BinanceTradingConfig`/`SurgeTradingConfig`와의 전환 로직.