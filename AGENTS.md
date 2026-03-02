# AGENTS.md — AI Assistant Guide

이 문서는 AI 코딩 어시스턴트(Claude, Copilot, Cursor 등)가 이 프로젝트를 이해하고 수정할 때 참조하는 가이드입니다.

---

## Project Overview

빗썸(Bithumb) 현물 + 바이낸스(Binance) USDM 선물 **듀얼 엔진** 암호화폐 자동 매매 시스템. Python(FastAPI) 백엔드 + React(TypeScript) 프론트엔드.

### Core Concept
1. 5분마다 **6개 전략**이 코인을 분석 → 각각 BUY/SELL/HOLD + confidence 리턴
2. **SignalCombiner**가 가중 투표로 최종 결정 (HOLD=기권 방식)
3. **TradingEngine / BinanceFuturesEngine**이 SL/TP/trailing stop/시장 필터 적용 후 실행
4. **AI Agents**가 시장 상태 분석 + 리스크 관리 + 거래 리뷰 + LLM 심층 회고

### Dual Engine Architecture
```
EngineRegistry (싱글턴)
├── "bithumb"
│   ├── TradingEngine (현물, KRW)
│   ├── PortfolioManager (KRW)
│   ├── SignalCombiner + AgentCoordinator
│   └── 서지 로테이션 활성
└── "binance_futures"
    ├── BinanceFuturesEngine (선물, USDT, 3x 레버리지)
    ├── PortfolioManager (USDT)
    ├── SignalCombiner + AgentCoordinator
    └── 롱/숏 양방향, WebSocket 가격 모니터
```

---

## Directory Structure

```
backend/
├── main.py              # FastAPI lifespan, 듀얼 엔진 조립
├── config.py            # Pydantic Settings (AppConfig → ExchangeConfig, BinanceConfig, BinanceTradingConfig, TradingConfig, RiskConfig, LLMConfig)
├── backtest.py          # 백테스터 (Backtester + FuturesBacktester + RotationBacktester)
│
├── core/
│   ├── enums.py         # SignalType, MarketState, OrderSide 등
│   ├── models.py        # SQLAlchemy ORM (Position, Order, PortfolioSnapshot, CapitalTransaction 등)
│   ├── schemas.py       # Pydantic response schemas
│   └── event_bus.py     # 서버 이벤트 DB 기록 + WS 브로드캐스트 + 재시도
│
├── exchange/
│   ├── base.py              # ExchangeAdapter ABC (현물 + 선물 Optional 메서드)
│   ├── bithumb_v2_adapter.py  # 빗썸 실거래 (ccxt public + JWT private)
│   ├── binance_usdm_adapter.py  # 바이낸스 USDM 선물 (ccxt + ccxt.pro WebSocket)
│   ├── paper_adapter.py       # 페이퍼 트레이딩
│   └── data_models.py         # Candle, Ticker, Balance, FuturesPosition DTOs
│
├── strategies/
│   ├── base.py          # BaseStrategy ABC, Signal dataclass
│   ├── registry.py      # StrategyRegistry (auto-register via decorator)
│   ├── combiner.py      # SignalCombiner (가중 투표, HOLD=기권, 적응형 가중치)
│   ├── ma_crossover.py       # 이동평균 크로스오버 (가중치 0.08)
│   ├── rsi_strategy.py       # RSI (가중치 0.25)
│   ├── macd_crossover.py     # MACD (가중치 0.12)
│   ├── bollinger_rsi.py      # 볼린저+RSI (가중치 0.27, 최고)
│   ├── stochastic_rsi.py     # Stochastic RSI (가중치 0.15)
│   ├── obv_divergence.py     # OBV 다이버전스 (가중치 0.13)
│   ├── volatility_breakout.py  # 비활성 (0% 승률)
│   ├── supertrend.py           # 비활성 (0% 승률)
│   ├── grid_trading.py         # 비활성 (독립 관리형)
│   └── dca_momentum.py         # 비활성 (독립 관리형)
│
├── engine/
│   ├── trading_engine.py      # 빗썸 현물: 평가 사이클, SL/TP, 로테이션
│   ├── futures_engine.py      # 바이낸스 선물: 롱/숏, 레버리지, 청산가, WebSocket 모니터
│   ├── order_manager.py       # 주문 생성/체결 관리
│   ├── portfolio_manager.py   # 포지션/잔고/스냅샷/거래소 동기화
│   ├── capital_sync.py        # 입출금 자동 감지 (바이낸스 USDT / 빗썸 KRW)
│   └── scheduler.py           # APScheduler 설정
│
├── agents/
│   ├── market_analysis.py     # 시장 분석 (심볼 분기: BTC/KRW or BTC/USDT)
│   ├── risk_management.py     # 리스크 경고/매수 차단
│   ├── trade_review.py        # 24h 거래 리뷰 (규칙 기반 + LLM 심층 회고, 선물 인식)
│   └── coordinator.py         # 에이전트 조율 + 시스템 로그 발행
│
├── services/
│   ├── market_data.py         # OHLCV + 기술 지표 계산
│   └── notification.py        # 텔레그램 알림
│
├── api/                       # FastAPI routes (모든 엔드포인트 exchange 파라미터)
│   ├── router.py, dependencies.py (EngineRegistry), dashboard.py
│   ├── portfolio.py, trades.py, strategies.py, events.py
│   ├── capital.py             # 입출금 관리 CRUD
│   └── websocket.py
│
├── db/                        # SQLAlchemy async session (PostgreSQL / SQLite)
└── tests/                     # 125 unit tests (pytest + 인메모리 SQLite)
```

---

## Key Patterns & Conventions

### Config System
- `config.py`의 `TradingConfig`에 `env_prefix = "TRADING_"`
- `BinanceTradingConfig`에 `env_prefix = "BINANCE_TRADING_"`
- `.env`에 `TRADING_ROTATION_ENABLED=true` → `config.trading.rotation_enabled`로 매핑
- `AppConfig` → `ExchangeConfig`, `BinanceConfig`, `BinanceTradingConfig`, `TradingConfig`, `RiskConfig`, `LLMConfig`

### Strategy Pattern
```python
@StrategyRegistry.register
class MyStrategy(BaseStrategy):
    name = "my_strategy"
    applicable_market_types = ["trending", "sideways"]
    async def analyze(self, df: pd.DataFrame, ticker: Ticker) -> Signal:
        return Signal(strategy_name="...", signal_type=SignalType.BUY, confidence=0.7, reason="...")
```
- 전략 추가 시: `strategies/` 디렉토리에 파일 생성 + `@register` + `main.py`에 import 추가
- `combiner.py`의 `DEFAULT_WEIGHTS` + `ADAPTIVE_PROFILES`에 가중치 추가

### Signal Combiner
```
6전략 → Signal(type, confidence)
    ↓
SignalCombiner (가중 투표, HOLD=기권)
  - BUY/SELL만 경쟁 — HOLD는 투표 미참여
  - active_weight로 정규화
  - active_weight < 0.12 → 의견 부족 HOLD (crash 시 0.06)
  - min_confidence(0.50/0.55) 이상만 실행
    ↓
시장 상태별 적응형 가중치 (5개 프로필) 자동 적용
```

### 6 Active Strategies (가중치)
| 전략 | 가중치 | 역할 |
|------|--------|------|
| bollinger_rsi | 0.27 | 볼린저밴드+RSI 이중 확인, 최고 가중치 |
| rsi | 0.25 | RSI 과매수/과매도 |
| stochastic_rsi | 0.15 | K/D 크로스, 과매수/매도 구간 |
| obv_divergence | 0.13 | 가격-OBV 다이버전스 |
| macd_crossover | 0.12 | MACD 시그널 크로스 |
| ma_crossover | 0.08 | SMA20/50 교차, 최저 가중치 |

### Indicator Columns
`MarketDataService._compute_indicators()`에서 생성. 전략에서 사용하는 컬럼명:
- SMA: `sma_9`, `sma_20`, `sma_50`, `sma_60`, `sma_200`
- EMA: `ema_12`, `ema_26`
- RSI: `rsi_14`
- MACD: `MACD_12_26_9`, `MACDs_12_26_9`, `MACDh_12_26_9`
- Bollinger: `BBL_20_2.0`, `BBM_20_2.0`, `BBU_20_2.0`
- ATR: `atr_14`
- ADX: `ADX_14`
- Volume: `volume_sma_20`
- StochRSI: `STOCHRSIk_14_14_3_3`, `STOCHRSId_14_14_3_3`
- OBV: `OBV`

> **backtest vs live 컬럼명 차이**: backtest(`SMA_20`, `ATRr_14`) vs live(`sma_20`, `atr_14`)

### Market State Detection (5-Factor)
| 요소 | 판단 기준 |
|---|---|
| Price vs SMA20 거리 | >5% 상회→strong_up, 상회→up, <5% 하회→down |
| SMA20 vs SMA50 정렬 | 위→up, 아래→down |
| RSI | >70→strong, >55→up, <30→down, <45→down |
| 7일 가격변동 | >10%→strong, >3%→up, <-10%→down, <-3%→down |
| 거래량/SMA20 | >2x→변동성 |

선물: 듀얼 타임프레임 (4h 장기 + 1h 단기 결합, 10분 갱신)

### Trading Engine Cycle (5분)
1. `_maybe_update_market_state()` — BTC 기준 시장 상태 감지
2. `_evaluate_coin()` per tracked coin:
   - SL/TP/trailing stop 체크 → 매도
   - 6개 전략 시그널 수집 → combiner → 매수/매도 결정
3. 빗썸: `_scan_volume_surges()` → `_try_rotation()` (서지 코인 매수)
4. 선물: WebSocket 실시간 가격 모니터 (~1초, 별도 루프)

### BinanceFuturesEngine (선물 전용)
- 롱/숏 양방향 (**전체 시장 숏 허용**)
- **4h 타임프레임**, **동적 SL** (ATR 기반 + 시장 상태별 프로필)
- SL/TP/트레일링: `/ sqrt(leverage)` 자동 축소
- **포지션 사이징**: 25% × 시장 상태 조정 (crash=25%, downtrend=50%)
- **min_confidence**: 0.55
- 청산가 2% 이내 긴급 청산
- WebSocket 실시간 가격 모니터 (ccxt.pro, ~1초)
- 펀딩비 30분 주기 조회
- 로테이션 비활성

### Exchange Adapters
**빗썸 V2**:
- Public API (OHLCV, ticker, orderbook): ccxt
- Private API (balance, orders): aiohttp + JWT (SHA512)
- Symbol: `BTC/KRW` ↔ `KRW-BTC`
- Market buy: `order_type=price` (KRW), Market sell: `order_type=market` (coin)
- 수수료: 0.25%

**바이낸스 USDM 선물**:
- ccxt binanceusdm + ccxt.pro WebSocket
- `set_leverage`, `fetch_futures_position`, `fetch_funding_rate`, `watch_tickers`
- 수수료: 0.04%
- 시장가 주문 전용

### Portfolio Manager
- `sync_exchange_positions()`: 시작 시 거래소 실제 잔고 → DB 동기화
  - 선물: `fetch_positions()` 기반 레버리지/방향/마진 메타데이터 보정
  - ccxt `leverage=None` 시 `notional/margin` 계산으로 fallback
- `restore_state_from_db()`: 최신 스냅샷에서 peak_value, realized_pnl 복원
- `load_initial_balance_from_db()`: CapitalTransaction에서 입출금 합산 → initial_balance 재계산

### Trade Review (선물 인식)
- `_is_futures`: 거래소명 기반 자동 판별
- 숏 P&L: sell=entry(진입), buy=exit(청산) → P&L = (entry - exit) * qty - fee
- 롱/현물: buy=entry, sell=exit → P&L = (exit - entry) * qty - fee
- LLM 회고: Claude Haiku API, 방향/레버리지/마진 정보 포함

---

## Common Tasks

### 새 전략 추가
1. `backend/strategies/new_strategy.py` 생성
2. `@StrategyRegistry.register` 데코레이터 적용
3. `BaseStrategy` 상속, `name`, `applicable_market_types`, `analyze()` 구현
4. `combiner.py`의 `DEFAULT_WEIGHTS` + `ADAPTIVE_PROFILES`에 가중치 추가
5. `main.py`의 전략 import 추가
6. `backtest.py`의 `ALL_STRATEGIES_N`, `WEIGHTS_N` 추가

### 설정 추가
1. `config.py`의 해당 Config 클래스에 필드 추가
2. `.env`에 `PREFIX_FIELD_NAME=value` 추가
3. `.env.example`에 동일하게 추가

### 백테스트 실행
```bash
cd backend && .venv/bin/python backtest.py                                    # 현물 기본
.venv/bin/python backtest.py --futures --symbol BTC/USDT --days 365 --leverage 3  # 선물
.venv/bin/python backtest.py --rotation --days 180                            # 로테이션
.venv/bin/python backtest.py --futures --dynamic-sl --short-all --timeframe 4h # 선물 풀옵션
```

### 라이브 서버
```bash
cd backend && .venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
curl -X POST http://localhost:8000/api/v1/engine/start                        # 빗썸
curl -X POST "http://localhost:8000/api/v1/engine/start?exchange=binance_futures"  # 선물
```

### 테스트
```bash
cd backend && .venv/bin/python -m pytest tests/ -v    # 209 tests
```

---

## DB Schema (주요 테이블)

- **Position**: symbol, exchange, quantity, average_buy_price, direction, leverage, liquidation_price, margin_used, stop_loss_pct, take_profit_pct, trailing_activation_pct, trailing_stop_pct, trailing_active, highest_price, max_hold_hours
- **Order**: symbol, exchange, direction, leverage, margin_used, strategy_name, signal_confidence, contributing_strategies
- **Trade**: symbol, exchange, side, price, quantity
- **PortfolioSnapshot**: exchange, total_value_krw, peak_value, realized_pnl
- **CapitalTransaction**: exchange, tx_type(deposit/withdrawal), amount, currency, source, confirmed
- **StrategyLog**: exchange, strategy_name, signal_type, confidence
- **AgentAnalysisLog**: exchange, agent_type, analysis_result

> 모든 테이블에 `exchange` 컬럼 (기본값 "bithumb"). Position은 (symbol, exchange) 복합 유니크.

---

## Important Notes

- **빗썸은 현물 전용**: futures/margin/short 불가, 수수료 0.25%
- **바이낸스는 선물 전용**: USDM perpetual, 수수료 0.04%, 3x 레버리지
- **거래소 독립 운영**: 빗썸 paper + 바이낸스 live (또는 반대) 가능
- **Grid/DCA 전략**: combiner에서 제외 (독립 관리형)
- **volatility_breakout/supertrend**: 0% 승률로 비활성
- **DB**: PostgreSQL 16 (docker compose), SQLite (테스트 폴백)
- **캐시**: `backend/.cache/`에 CSV 캐싱 (API 제한 우회)
- **DateTime**: 모든 컬럼 `DateTime(timezone=True)`, `_utcnow()` 헬퍼
- **PositionTracker DB 영속화**: SL/TP/trailing 상태를 Position 테이블에 저장, 재시작 시 복원
- **교차 거래소 안전장치**: 현물 롱 보유 시 선물 숏 차단, 선물 숏 보유 시 현물 매수 차단
- **매도 후 재매수 대기**: `cooldown_after_sell_sec=14400` (4시간, 당일 왕복 방지)
- **스냅샷 정합성**: sync/eval 인터리빙 방지 — 스냅샷 직전 `reconcile_cash_from_db()` 호출
