# AGENTS.md — AI Assistant Guide

이 문서는 AI 코딩 어시스턴트(Claude, Copilot, Cursor 등)가 이 프로젝트를 이해하고 수정할 때 참조하는 가이드입니다.

---

## Project Overview

Bithumb 거래소 기반 암호화폐 자동 매매 시스템. Python(FastAPI) 백엔드 + React(TypeScript) 프론트엔드.

### Core Concept
1. 5분마다 **5개 전략**이 코인을 분석 → 각각 BUY/SELL/HOLD + confidence 리턴
2. **SignalCombiner**가 가중 투표로 최종 결정
3. **TradingEngine**이 SL/TP/trailing stop/추세 필터/거래량 로테이션 적용 후 실행
4. **AI Agents**가 시장 상태 분석 + 리스크 관리 + 가중치 자동 조정

---

## Directory Structure

```
backend/
├── main.py              # FastAPI lifespan, 의존성 조립
├── config.py            # Pydantic Settings (env_prefix로 .env 자동 매핑)
├── backtest.py          # 백테스터 (Backtester + RotationBacktester)
│
├── core/
│   ├── enums.py         # SignalType, MarketState, OrderSide 등
│   ├── models.py        # SQLAlchemy ORM (Position, Order, PortfolioSnapshot 등)
│   ├── schemas.py       # Pydantic response schemas
│   └── exceptions.py    # Custom exceptions
│
├── exchange/
│   ├── base.py          # ExchangeAdapter ABC
│   ├── bithumb_v2_adapter.py  # 실거래 (ccxt public + JWT private)
│   ├── paper_adapter.py       # 페이퍼 트레이딩
│   └── data_models.py         # Candle, Ticker, Balance DTOs
│
├── strategies/
│   ├── base.py          # BaseStrategy ABC, Signal dataclass
│   ├── registry.py      # StrategyRegistry (auto-register via decorator)
│   ├── combiner.py      # SignalCombiner (weighted voting)
│   ├── rsi_strategy.py
│   ├── bollinger_rsi.py
│   ├── macd_crossover.py
│   ├── volatility_breakout.py
│   ├── ma_crossover.py
│   ├── grid_trading.py       # 비활성 (combiner에서 제외)
│   └── dca_momentum.py       # 비활성 (combiner에서 제외)
│
├── engine/
│   ├── trading_engine.py      # 핵심: 평가 사이클, SL/TP, 로테이션
│   ├── order_manager.py       # 주문 생성/체결 관리
│   ├── portfolio_manager.py   # 포지션/잔고/스냅샷 관리
│   └── scheduler.py           # APScheduler 설정
│
├── agents/
│   ├── market_analysis.py     # 시장 분석 (BTC 기준)
│   ├── risk_management.py     # 리스크 경고/매수 차단
│   ├── trade_review.py        # 24h 거래 리뷰
│   └── coordinator.py         # 에이전트 조율
│
├── services/
│   ├── market_data.py         # OHLCV + 기술 지표 계산
│   └── notification.py        # 텔레그램 알림
│
├── api/                       # FastAPI routes
├── db/                        # SQLAlchemy async session
└── tests/
```

---

## Key Patterns & Conventions

### Config System
- `config.py`의 `TradingConfig`에 `env_prefix = "TRADING_"`
- `.env`에 `TRADING_ROTATION_ENABLED=true` → `config.trading.rotation_enabled`로 매핑
- `AppConfig` → `ExchangeConfig`, `TradingConfig`, `RiskConfig`, `DatabaseConfig`

### Strategy Pattern
```python
@StrategyRegistry.register("strategy_name")
class MyStrategy(BaseStrategy):
    async def analyze(self, df: pd.DataFrame, ticker: Ticker) -> Signal:
        return Signal(strategy_name="...", signal_type=SignalType.BUY, confidence=0.7, reason="...")
```
- 전략 추가 시: `strategies/` 디렉토리에 파일 생성 + `@register` + `main.py`에 import 추가
- `combiner.py`의 `DEFAULT_WEIGHTS`에 가중치 추가

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

### Market State Detection
`TradingEngine._detect_market_state()` — 4단계 (crash는 downtrend에 통합):
- `strong_uptrend`: SMA20 > SMA60 + ADX > 25 + RSI > 55
- `uptrend`: SMA20 > SMA60
- `downtrend`: SMA20 < SMA60 + (ADX > 25 or RSI < 45) → 매수 50% 축소
- `sideways`: 나머지

### Trading Engine Cycle (5분)
1. `_maybe_update_market_state()` — BTC 4h 기준 시장 상태 감지 (30분 간격)
2. `_evaluate_coin()` per tracked coin (5종: BTC/ETH/XRP/SOL/ADA):
   - SL/TP/trailing stop 체크 → 매도
   - 5개 전략 시그널 수집 → combiner → 매수/매도 결정
3. `_scan_volume_surges()` — 20코인 거래량 서지 스캔 (전체 점수 캐시 → API 노출)
4. `_try_rotation()` — 서지(≥2.0x) 확인 + 전략 확인 → 로테이션

### Exchange Adapter (Bithumb V2)
- Public API (OHLCV, ticker, orderbook): ccxt 라이브러리
- Private API (balance, orders): aiohttp + JWT (SHA512)
- Symbol 변환: `BTC/KRW` ↔ `KRW-BTC`
- Market buy: `ord_type=price` (KRW 금액 지정)
- Market sell: `ord_type=market` (코인 수량 지정)

---

## Common Tasks

### 새 전략 추가
1. `backend/strategies/new_strategy.py` 생성
2. `@StrategyRegistry.register("new_strategy")` 데코레이터 적용
3. `BaseStrategy` 상속, `analyze()` 메서드 구현
4. `combiner.py`의 `DEFAULT_WEIGHTS`에 가중치 추가
5. `main.py`의 `initialize()`에 `import strategies.new_strategy` 추가

### 설정 추가
1. `config.py`의 해당 Config 클래스에 필드 추가
2. `.env`에 `PREFIX_FIELD_NAME=value` 추가
3. `.env.example`에 동일하게 추가 (값은 기본값으로)

### 백테스트 실행
```bash
cd backend && source .venv/bin/activate
python backtest.py                          # 기본
python backtest.py --dynamic-sl             # 동적 손절
python backtest.py --rotation               # 거래량 로테이션
python backtest.py --trend-filter --dynamic-sl  # 추세 필터 + 동적 손절
```

### 라이브 서버 시작
```bash
cd backend && source .venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 8000
# 엔진 시작
curl -X POST http://localhost:8000/api/v1/engine/start
```

---

### 로테이션 모니터 API
```
GET /api/v1/engine/rotation-status
→ RotationStatusResponse: 서지 점수 전체, 임계값, 시장 상태, 추적/로테이션 코인 목록
```
프론트엔드 `RotationMonitor.tsx`가 30초마다 폴링.

---

## Important Notes

- **빗썸은 현물 전용**: futures/margin/short 불가
- **최소 주문금액**: 500 KRW
- **수수료**: 0.25% (0.3% 마진으로 계산)
- **Grid/DCA 전략**: 파일은 보존하되 combiner에서 제외 (독립 관리형이라 가중 투표에 부적합)
- **backtest vs live 컬럼명 차이**: backtest(`SMA_20`, `ATRr_14`) vs live(`sma_20`, `atr_14`)
- **DB**: 개발환경 SQLite, 프로덕션 PostgreSQL
- **캐시**: `backend/.cache/`에 CSV 캐싱 (빗썸 API 제한 우회)
