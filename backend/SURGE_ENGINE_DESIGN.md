# Surge/Momentum Trading Engine — Design Document

> Research-only. No code written. This document describes the architecture, algorithms,
> and implementation plan for a dedicated short-timeframe surge trading engine.

---

## 1. Motivation & Problem Statement

The current system operates on 4h candles with 8-12 day cooldowns. This is optimal for
trend-following (PF 2.85, +274% in 540d backtest) but **structurally blind** to:

- **Intraday volume explosions** (e.g., 10x volume in 5 minutes after listing/news)
- **Momentum continuation** after breakout (price moves 5-15% in 1-4 hours)
- **Mean-reversion after spike exhaustion** (overextended moves retrace 30-60%)

These events happen on minute-scale timeframes and require fundamentally different
risk parameters than the main engine. The existing `_scan_volume_surges` in
`trading_engine.py` operates on 1h candles with 20-period SMA — far too slow to
capture real surges, which develop and exhaust within minutes to hours.

### Non-Goals

- Not replacing the main 4h engine (which continues independently)
- Not adding strategies to the existing `SignalCombiner` pipeline
- Not requiring ML model (should work without, ML can enhance later)

---

## 2. Architecture Overview

### 2.1 Engine Placement

```
EngineRegistry
  ├── "bithumb"          → TradingEngine        (4h, spot KRW)
  ├── "binance_spot"     → TradingEngine        (4h, spot USDT)
  ├── "binance_futures"  → BinanceFuturesEngine  (4h, futures USDT 3x)
  └── "binance_surge"    → SurgeEngine [NEW]     (5m/15m, futures USDT 2x)
```

The SurgeEngine is a **peer** of the existing engines, registered independently in
`EngineRegistry`. It uses the **same Binance USDM adapter** as the futures engine
(shared ccxt exchange instance, shared WebSocket connection) but with its own:

- Dedicated `PortfolioManager` (separate capital allocation)
- Dedicated `OrderManager` (separate order tracking)
- No `SignalCombiner` (uses its own internal scoring)
- No 4h strategy pipeline
- Own DB positions with `exchange="binance_surge"`

### 2.2 Capital Isolation

The surge engine operates with a **separate capital pool**:

```
Total Binance USDT balance
  ├── 85% → binance_futures (main 4h engine)
  └── 15% → binance_surge (surge engine)
```

Capital is configured independently via `SurgeTradingConfig` (env prefix `SURGE_TRADING_`).
The surge engine never touches the main engine's capital, and vice versa.

### 2.3 Component Diagram

```
┌─────────────────────────────────────────────────────────┐
│                    SurgeEngine                          │
│                                                         │
│  ┌──────────────┐    ┌──────────────┐                   │
│  │  SurgeScanner │    │ SurgeScorer  │                   │
│  │  (WebSocket)  │───>│ (Multi-signal│                   │
│  │  5s tick data │    │  composite)  │                   │
│  └──────────────┘    └──────┬───────┘                   │
│                             │ score > threshold          │
│                     ┌───────▼───────┐                   │
│                     │ SurgeExecutor │                    │
│                     │ (Entry/Exit   │                    │
│                     │  management)  │                    │
│                     └───────┬───────┘                    │
│                             │                            │
│  ┌──────────────┐    ┌──────▼───────┐                   │
│  │ SurgeTracker │    │ SurgeExitMgr │                    │
│  │ (position    │<───│ (trailing,   │                    │
│  │  state)      │    │  time-based) │                    │
│  └──────────────┘    └──────────────┘                    │
│                                                          │
│  ┌──────────────────────────────────┐                    │
│  │ SurgeRiskManager                │                    │
│  │ - Max 3 concurrent positions    │                    │
│  │ - Daily loss limit              │                    │
│  │ - Cross-engine conflict check   │                    │
│  └──────────────────────────────────┘                    │
└──────────────────────────────────────────────────────────┘
         │                        │
    uses same               registers in
    BinanceUSDM              EngineRegistry
    Adapter                  as "binance_surge"
```

---

## 3. Surge Detection Algorithm

### 3.1 Data Source

**WebSocket ticker stream** for top-50 USDT perpetual contracts by 24h volume.
The existing `BinanceUSDMAdapter.watch_tickers()` already supports multi-symbol
subscriptions. The surge scanner subscribes to all 50 symbols and maintains
rolling windows in memory.

### 3.2 Rolling Window State (Per Symbol)

```python
@dataclass
class SymbolSurgeState:
    # 1-minute volume bars (rolling 60 entries = 1 hour)
    volume_1m: deque[float]           # maxlen=60

    # 5-minute OHLCV bars (rolling 48 entries = 4 hours)
    ohlcv_5m: deque[OHLCVBar]        # maxlen=48

    # Tick-level (last 30 seconds)
    tick_prices: deque[float]          # maxlen=100
    tick_volumes: deque[float]         # maxlen=100

    # Computed (updated every 5s)
    volume_ratio_1m: float = 0.0      # current 1m vol / avg(prev 20 1m bars)
    volume_ratio_5m: float = 0.0      # current 5m vol / avg(prev 12 5m bars)
    price_change_5m: float = 0.0      # % price change over last 5 min
    price_change_15m: float = 0.0     # % price change over last 15 min
    bid_ask_imbalance: float = 0.0    # orderbook imbalance [-1, +1]
    rsi_5m: float = 50.0             # 14-period RSI on 5m bars
    atr_5m_pct: float = 0.0          # ATR% on 5m bars
```

### 3.3 Surge Score Computation

The surge score is a weighted composite of multiple real-time signals:

```
SurgeScore = w1 * VolumeSignal
           + w2 * PriceSignal
           + w3 * AccelerationSignal
           + w4 * OrderbookSignal
```

**VolumeSignal** (w1 = 0.35):
- `volume_ratio_1m`: current 1-minute volume / 20-period average
- Threshold: > 5x = strong, > 3x = moderate
- Normalized to [0, 1] via `min(ratio / 10, 1.0)`

**PriceSignal** (w2 = 0.30):
- `price_change_5m`: absolute price change over 5 minutes
- Threshold: > 2% = strong, > 1% = moderate
- Direction matters: positive = bullish surge, negative = bearish surge
- Normalized: `min(abs(pct_change) / 5, 1.0)`

**AccelerationSignal** (w3 = 0.20):
- Rate of change of volume_ratio (is the surge intensifying or fading?)
- `accel = (vol_ratio_now - vol_ratio_2min_ago) / 2`
- Positive acceleration = surge building, negative = fading
- Normalized: `max(0, min(accel / 3, 1.0))`

**OrderbookSignal** (w4 = 0.15):
- Bid/ask volume imbalance from orderbook top 20 levels
- `imbalance = (bid_volume - ask_volume) / (bid_volume + ask_volume)`
- For long: positive imbalance = demand; for short: negative = supply
- Periodic fetch (every 30s to avoid rate limits)
- Normalized: `abs(imbalance)` with direction matching price movement

**Final threshold**: SurgeScore >= 0.60 triggers entry evaluation.

### 3.4 Surge Classification

```
Score 0.60-0.75: MODERATE_SURGE  → smaller position (50% of max)
Score 0.75-0.90: STRONG_SURGE   → standard position (75% of max)
Score 0.90+:     EXTREME_SURGE  → full position (100% of max)
```

### 3.5 Anti-Noise Filters

1. **Minimum volume threshold**: 24h quote volume > 20M USDT (illiquid coins produce false surges)
2. **Stale data filter**: Last tick > 10s ago = skip (low liquidity)
3. **Spike exhaustion filter**: If price already moved > 8% in 15m, skip (late entry)
4. **Recent surge cooldown**: Same symbol cannot re-trigger within 30 minutes
5. **Market-wide panic filter**: If > 5 symbols surge simultaneously with negative price = market crash, reduce all positions
6. **Spread filter**: If bid-ask spread > 0.15%, skip (too expensive to enter)

---

## 4. Entry Strategy

### 4.1 Entry Decision Flow

```
SurgeScore >= 0.60
  → Anti-noise filters pass
  → Risk manager approval (max positions, daily loss, conflicts)
  → Direction determination (price_change sign + orderbook imbalance)
  → Quick confirmation (RSI not extremely overbought/oversold in direction)
  → Market order execution
```

### 4.2 Direction Determination

- **Long**: price_change_5m > +1% AND orderbook imbalance > +0.10
- **Short**: price_change_5m < -1% AND orderbook imbalance < -0.10
- **Skip**: Mixed signals (price up but orderbook bearish, or vice versa)

### 4.3 Quick Confirmation (Lightweight)

Instead of running 6 strategies, the surge engine uses a **minimal confirmation**
based on fast indicators computed from the 5m rolling bars:

1. **RSI(14) on 5m**: Don't buy if RSI > 85 (extremely overbought), don't short if RSI < 15
2. **Price vs VWAP**: Entry should be within 3% of VWAP (avoid chasing extended moves)
3. **Spread check**: Bid-ask spread < 0.10% for large-cap, < 0.15% for mid-cap

No SignalCombiner. No strategy voting. Speed is paramount — the entire entry decision
should complete within 100ms of surge detection.

### 4.4 Entry Execution

- **Order type**: Market order (speed > price improvement)
- **Slippage budget**: 0.05% expected, max 0.15% acceptable
- **Leverage**: 2x (lower than main engine to compensate for higher frequency/volatility)

---

## 5. Exit Strategy

### 5.1 Exit Mechanisms (Priority Order)

| # | Mechanism | Trigger | Notes |
|---|-----------|---------|-------|
| 1 | Emergency SL | PnL < -1.5% (price-based) | Hard stop, market order |
| 2 | Trailing stop | Peak PnL > +1.0%, then drawdown > 0.8% from peak | Locks in profit |
| 3 | Time-based exit | Position held > 2 hours | Surges exhaust; don't become bag-holder |
| 4 | Volume fade exit | Volume ratio drops below 1.5x (back to normal) | Surge momentum gone |
| 5 | Reversal exit | Price reverses direction > 0.5% from entry with volume | Active reversal detection |
| 6 | Take profit | PnL > +3.0% | Capture the bulk of the move |

### 5.2 Exit Monitoring

The exit manager runs on the **same WebSocket stream** as the scanner.
Every price tick for held symbols is checked against all exit conditions.
This provides sub-second exit latency (compared to the main engine's 30s polling).

### 5.3 Trailing Stop Details

```
Phase 1: Position open, PnL < +1.0%
  → Only hard SL active (-1.5%)

Phase 2: PnL crosses +1.0% (trailing activates)
  → Track peak price
  → Exit if drawdown from peak > 0.8%
  → Effective: locks in at least +0.2% profit

Phase 3: PnL crosses +2.0%
  → Tighten trailing to 0.5% from peak
  → Effective: locks in at least +1.5% profit
```

### 5.4 Comparison with Main Engine

| Parameter | Main Engine (4h) | Surge Engine (5m) |
|-----------|-----------------|-------------------|
| SL | 8% (margin) | 1.5% (price) |
| TP | 16% (margin) | 3.0% (price) |
| Trailing activation | 5% | 1.0% |
| Trailing stop | 3.5% | 0.8% |
| Max hold time | Unlimited | 2 hours |
| Leverage | 3x | 2x |

---

## 6. Risk Management

### 6.1 Position Sizing

```python
base_size = cash_balance * 0.08  # 8% of surge capital per trade
# Scale by surge strength:
#   MODERATE (0.60-0.75): base * 0.50 = 4%
#   STRONG (0.75-0.90):   base * 0.75 = 6%
#   EXTREME (0.90+):      base * 1.00 = 8%
```

With 15% of total USDT allocated to surge, and 8% max per trade,
the maximum single-trade exposure is ~1.2% of total portfolio.

### 6.2 Concurrent Position Limits

- **Max 3 simultaneous surge positions** (hard limit)
- **Max 1 position per symbol** (no pyramiding)
- **No same-direction overlap with main engine**: If the main futures engine has
  a long on BTC, the surge engine can only short BTC (or skip it). This uses the
  existing `engine_registry` cross-engine conflict check.

### 6.3 Daily Loss Limits

- **Max daily surge loss**: 5% of surge capital (e.g., 150 USDT of 3,000 USDT allocation)
- **Max daily trades**: 15 (prevents overtrading during choppy markets)
- **Consecutive loss pause**: 3 consecutive losses → 30-minute pause
- **Daily counter reset**: 00:00 UTC

### 6.4 Market-Wide Circuit Breaker

If the surge scanner detects > 5 simultaneous negative surges across different symbols,
this indicates a market-wide crash event. Response:

1. Close all surge positions immediately (market orders)
2. Pause surge scanning for 15 minutes
3. Log event for review
4. Resume with reduced position sizes (50%) for 1 hour

### 6.5 Cross-Engine Coordination

```
SurgeEngine._check_cross_engine_conflict(symbol, direction):
    # Query main futures engine positions via engine_registry
    main_engine = engine_registry.get_engine("binance_futures")
    if main_engine has position on symbol:
        main_direction = position.direction
        if main_direction == surge_direction:
            return ALLOW  # Same direction = reinforcing
        else:
            return BLOCK  # Opposite direction = conflict
    return ALLOW
```

---

## 7. Backtesting Approach

### 7.1 Challenge

The existing backtester uses 4h/1h candles. Surge trading requires 1m or 5m candle
data, which means:

- **Data volume**: 540 days of 1m data = 777,600 candles per symbol (vs 3,240 for 4h)
- **Data availability**: Binance provides max 1000 candles per API call; paginating
  540 days of 1m data requires ~778 API calls per symbol
- **Simulation fidelity**: Tick-level surge detection can't be perfectly simulated
  from 1m candles

### 7.2 Design: SurgeBacktester

```python
class SurgeBacktester:
    """Surge trading backtester using 5m candles with 1m volume overlay."""

    def __init__(
        self,
        exchange: BinanceUSDMAdapter,
        symbols: list[str],      # Top-20 coins by average volume
        initial_balance: float = 3000.0,    # USDT
        leverage: int = 2,
        sl_pct: float = 1.5,
        tp_pct: float = 3.0,
        trail_activation: float = 1.0,
        trail_stop: float = 0.8,
        max_hold_minutes: int = 120,
        surge_threshold: float = 5.0,      # Volume ratio threshold
        price_threshold: float = 1.0,      # % price change threshold
        max_concurrent: int = 3,
        position_pct: float = 0.08,
    ):
        ...
```

### 7.3 Data Strategy

1. **Primary timeframe**: 5m candles (manageable volume: 540d = 155,520 candles/symbol)
2. **Volume reference**: 1h candles for volume SMA baseline (avoids noise in 5m vol)
3. **Surge detection**: `5m_volume / avg_1h_volume_per_5m_bar` (normalize 1h → 5m)
4. **Price change**: Computed from 5m close prices

### 7.4 Backtest Simulation Flow

```
For each 5m candle:
  1. Update rolling volume/price state for all symbols
  2. Compute surge scores
  3. Check exit conditions for held positions (using 5m OHLC for intra-bar SL)
  4. If surge detected → check filters → enter position
  5. Track equity curve
```

### 7.5 Intra-Candle SL Simulation

Unlike the main backtester which uses close prices for SL checks, the surge
backtester uses the **low** (for longs) and **high** (for shorts) of each 5m candle
to check if SL was hit within the candle:

```python
# For long position:
if candle_low <= entry_price * (1 - sl_pct / 100):
    # SL hit during this candle — use SL price, not close
    exit_price = entry_price * (1 - sl_pct / 100)
```

This is more realistic than close-only checking and avoids the problem documented
in backtest-analysis.md where SL overshoot produced unrealistic results.

### 7.6 CLI Integration

```bash
# New CLI flags:
python backtest.py --surge --days 90
python backtest.py --surge --days 90 --surge-sl 1.5 --surge-tp 3.0
python backtest.py --surge --days 180 --surge-coins 20 --surge-max-hold 120
```

Shorter default period (90 days) because 5m data takes longer to fetch and
initial development should iterate faster.

---

## 8. Implementation Plan

### Phase 1: Core Infrastructure (Files to Create)

| File | Description |
|------|-------------|
| `engine/surge_engine.py` | Main SurgeEngine class (NOT a TradingEngine subclass — too different) |
| `engine/surge_scanner.py` | WebSocket-based real-time surge detection |
| `engine/surge_scorer.py` | Multi-signal surge scoring algorithm |
| `engine/surge_risk.py` | Surge-specific risk management |
| `config.py` (modify) | Add `SurgeTradingConfig` class (env prefix `SURGE_TRADING_`) |

### Phase 2: Backtesting

| File | Description |
|------|-------------|
| `backtest.py` (modify) | Add `SurgeBacktester` class, `--surge` CLI flag |
| `backtest.py` (modify) | Add `SurgeBacktestResult` dataclass |

### Phase 3: Integration

| File | Description |
|------|-------------|
| `main.py` (modify) | Assembly: create SurgeEngine, register as "binance_surge" |
| `api/dependencies.py` (modify) | Add "binance_surge" to VALID_EXCHANGES |
| `api/dashboard.py` (modify) | Expose surge engine status and positions |
| `core/models.py` (modify) | Possibly add surge-specific columns to Position or a new SurgePosition table |

### Phase 4: Testing

| File | Description |
|------|-------------|
| `tests/test_surge_engine.py` | Unit tests for SurgeEngine lifecycle |
| `tests/test_surge_scanner.py` | Unit tests for surge detection logic |
| `tests/test_surge_scorer.py` | Unit tests for scoring algorithm |
| `tests/test_surge_risk.py` | Unit tests for risk management |
| `tests/test_surge_backtest.py` | Unit tests for SurgeBacktester |

### 8.1 Why Not Subclass TradingEngine/BinanceFuturesEngine

The existing engine hierarchy (`TradingEngine` -> `BinanceFuturesEngine`) is deeply
tied to the 4h evaluation cycle pattern:

- `_evaluation_cycle()` runs every 5 minutes, iterates over tracked coins,
  runs all strategies via `SignalCombiner`
- Position management uses `PositionTracker` with DB persistence designed
  for hours/days hold times
- Cooldown system (cd48/cd72) is fundamentally incompatible with minute-scale trading

The SurgeEngine should be a standalone class that:
- Shares the same `ExchangeAdapter` (Binance USDM)
- Shares the same DB models (`Position`, `Order`)
- But has completely independent evaluation logic (event-driven, not poll-based)

### 8.2 Shared vs Dedicated Components

| Component | Shared | Dedicated | Notes |
|-----------|--------|-----------|-------|
| BinanceUSDMAdapter | Yes | | Same exchange connection |
| WebSocket connection | Yes | | Same `ccxt.pro` instance |
| DB models (Position, Order) | Yes | | exchange="binance_surge" isolation |
| PortfolioManager | | Yes | Separate capital pool |
| OrderManager | | Yes | Separate order tracking |
| MarketDataService | | Yes | Separate cache (5m vs 4h) |
| SignalCombiner | | No | Not used — internal scoring |
| Strategies (10 classes) | | No | Not used — too slow |
| EngineRegistry | Yes | | Registered as "binance_surge" |

### 8.3 Implementation Order

```
Step 1: SurgeTradingConfig in config.py
Step 2: SurgeScorer (pure function, no IO — easiest to test)
Step 3: SurgeScanner (WebSocket integration)
Step 4: SurgeEngine (ties it all together)
Step 5: SurgeBacktester (validate parameters)
Step 6: main.py integration + API exposure
Step 7: Tests for all components
```

---

## 9. Configuration

### 9.1 SurgeTradingConfig

```python
class SurgeTradingConfig(BaseSettings):
    """서지/모멘텀 트레이딩 전용 설정."""
    enabled: bool = False                    # 기본 비활성
    mode: str = "paper"                      # "paper" or "live"
    initial_balance_usdt: float = 3000.0     # 서지 전용 자본
    leverage: int = 2
    max_concurrent_positions: int = 3
    position_pct: float = 0.08               # 1회 포지션 크기 (자본 대비)
    stop_loss_pct: float = 1.5               # 가격 기준 SL
    take_profit_pct: float = 3.0             # 가격 기준 TP
    trailing_activation_pct: float = 1.0
    trailing_stop_pct: float = 0.8
    max_hold_minutes: int = 120              # 최대 보유 시간
    surge_threshold: float = 0.60            # 서지 점수 진입 임계값
    volume_ratio_threshold: float = 5.0      # 거래량 배수 임계값
    price_change_threshold: float = 1.0      # 가격 변동 % 임계값
    daily_trade_limit: int = 15
    daily_loss_limit_pct: float = 5.0        # 일일 최대 손실 %
    consecutive_loss_pause: int = 3          # 연속 손실 후 일시 정지
    pause_duration_minutes: int = 30
    scan_symbols_count: int = 50             # 스캔 대상 심볼 수
    cooldown_per_symbol_sec: int = 1800      # 심볼별 재진입 대기 (30분)

    model_config = {"env_prefix": "SURGE_TRADING_"}
```

---

## 10. Key Risks and Mitigations

### 10.1 Execution Risk

| Risk | Impact | Mitigation |
|------|--------|-----------|
| Slippage on market orders | Reduced PnL, especially on illiquid coins | Min 20M USDT 24h volume filter; spread check before entry |
| WebSocket disconnection | Missed exits, positions stuck | Fast SL fallback (30s polling, same as futures engine); hard SL via exchange stop-limit |
| API rate limits | Delayed entries/exits | Share WS connection; limit orderbook fetches to 30s intervals |
| Fill latency | Entry at worse price | Accept 0.05% expected slippage in sizing; budget for 0.15% worst case |

### 10.2 Strategy Risk

| Risk | Impact | Mitigation |
|------|--------|-----------|
| False surges (wash trading, bot activity) | Losing trades on fake volume | Orderbook depth check; spread filter; multi-signal composite (not volume alone) |
| Late entry (surge already peaked) | Buy at top | 8% max price move filter; VWAP proximity check |
| Surge reversal | Rapid loss | Tight SL (1.5%); 2-hour max hold; volume fade exit |
| Correlated positions | Multiple simultaneous losses | Max 3 positions; market-wide circuit breaker |
| Overtrading in choppy markets | Death by a thousand cuts | Daily trade limit (15); consecutive loss pause; daily loss limit |

### 10.3 Infrastructure Risk

| Risk | Impact | Mitigation |
|------|--------|-----------|
| Memory usage (rolling windows for 50 symbols) | OOM on Raspberry Pi | Limit to 50 symbols; use deque with maxlen; ~5MB estimated |
| CPU usage (continuous computation) | Contention with main engine | 5-second computation cycle (not tick-by-tick); async non-blocking |
| Capital conflict with main engine | Accounting errors | Completely separate PortfolioManager with separate DB exchange tag |
| Raspberry Pi performance | Latency too high | Profile first; if >200ms latency, reduce to 20 symbols |

### 10.4 Backtesting Risk

| Risk | Impact | Mitigation |
|------|--------|-----------|
| 5m candles can't capture tick-level dynamics | Overestimated entry quality | Use conservative slippage (0.10%); intra-candle SL with high/low |
| Data volume (155K candles/symbol * 20 symbols) | Slow backtest, API limits | Use cached CSV files (like existing `fetch_long_history.py`); paginate |
| Backtest-live gap wider than 4h engine | False confidence in results | Paper trade for 2+ weeks before live; track fill quality metrics |

---

## 11. Monitoring & Observability

### 11.1 Metrics to Track

- **Surge detection rate**: How many surges detected per day
- **Entry hit rate**: % of detected surges that pass all filters
- **Win rate**: % of trades with positive PnL
- **Average hold time**: Should be 15-45 minutes (not 2 hours regularly)
- **Fill quality**: Actual fill price vs expected price at detection time
- **Volume ratio at entry vs exit**: Confirms surge was real

### 11.2 Discord Notifications

Use existing `emit_event()` system with new category `"surge_trade"`:

```
[Surge] LONG BTC/USDT @ $68,450 | Score 0.82 (STRONG)
  Volume: 7.3x avg | Price: +2.1% (5m) | Size: 240 USDT (2x)
  SL: $67,423 (-1.5%) | TP: $70,504 (+3.0%)

[Surge] CLOSED BTC/USDT @ $69,100 | +0.95% | Trailing stop
  Hold: 34 min | Peak: +1.8% | Volume at exit: 2.1x
```

### 11.3 Dashboard Integration

Add "Surge" tab to frontend dashboard showing:
- Active surge positions with real-time PnL
- Recent surge detections (hit/miss)
- Daily P&L chart
- Current volume heat map across scanned symbols

---

## 12. Success Criteria

Before going live, the surge engine must demonstrate:

1. **Backtest PF > 1.3** on 90-day 5m data across 20+ coins
2. **Win rate > 45%** (higher than main engine's 35% because holding is short)
3. **Average win > 1.5x average loss** (R:R ratio)
4. **Max drawdown < 15%** of surge capital
5. **Paper trading 2 weeks**: Confirm fill quality within 0.15% of backtest assumptions
6. **No interference**: Main engine performance unchanged during surge engine operation

---

## 13. Future Enhancements (Out of Scope for V1)

1. **ML-enhanced surge scoring**: Train a model on historical surges to predict
   which surges continue vs. exhaust (similar to existing `MLSignalFilter`)
2. **Cross-exchange arbitrage**: Detect surges on one exchange before they
   propagate to others
3. **News integration**: Correlate surges with news events for context
4. **Adaptive parameters**: Adjust SL/TP/position size based on market regime
   (from main engine's market state detection)
5. **Partial exits**: Scale out of position at different profit levels
   (e.g., 50% at +1.5%, remaining 50% with trailing)
6. **Funding rate awareness**: Prefer long surges when funding is negative
   (get paid to hold), short surges when funding is positive
