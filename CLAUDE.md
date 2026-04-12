# CLAUDE.md — AI Assistant Guide

이 프로젝트에서 작업할 때 반드시 따르는 규칙과 컨텍스트.

---

## Project Summary

R&D 기반 멀티 전략 암호화폐 자동 매매 시스템.
Python 3.12 (FastAPI) + React 18 (TypeScript) + PostgreSQL 16.

**현재 상태 (2026-04-13)**: 메인 엔진(4전략/V2/서지) 비활성 → 6개 R&D 전략 라이브 운영 중.

### R&D 엔진 (활성)
| 엔진 | 거래소 | 시장 | 전략 | 자금 |
|------|--------|------|------|------|
| DonchianDailyEngine | 현물 | long-only | 일봉 Donchian 앙상블 (10/20/40/55/90) | 200 USDT |
| DonchianFuturesBiEngine | 선물 2x | long+short | 양방향 Donchian | 200 USDT |
| PairsTradingLiveEngine | 선물 2x | delta-neutral | BTC-ETH z-score | 150 USDT |
| MomentumRotationLiveEngine | 선물 2x | long+short | 주간 상대강약 top2/bottom2 | 200 USDT |
| HMMRegimeLiveEngine | 선물 2x | long/short/flat | 1h HMM 3-state 체제전환 | 200 USDT |
| FearGreedDCAEngine | 현물 | long-only DCA | RSI+30일변동 기반 분할매수 | 200 USDT |

### 비활성 (메인 엔진)
| 엔진 | 사유 |
|------|------|
| TradingEngine (4전략) | 백테스트 look-ahead bias 발견, alpha 없음 확인 |
| FuturesEngineV2 (레짐) | 동일 — 모든 V2 결과 무효화 |
| SurgeEngine | 선물 V2와 함께 비활성 |

---

## Mandatory Rules

### 1. Testing
- **모든 코드 변경에 테스트 추가/수정 필수**. 테스트 없는 PR은 허용하지 않음.
- 변경 후 반드시 `cd backend && .venv/bin/python -m pytest tests/ -x -q` 로 전체 테스트 통과 확인.
- 테스트는 인메모리 SQLite 기반 (aiosqlite). 외부 의존성 Mock 필수.
- 테스트 수가 줄어들면 안 됨.

### 2. Backtest Validation
- **전략 파라미터 변경 시 반드시 540일 백테스트 검증** 후 적용.
- 백테스트 결과는 `backtest-analysis.md`(메모리)에 기록 — 성공/실패 모두.
- 기존 실패 이력 확인 후 작업 시작 (`backtest-analysis.md`의 "실패 패턴 요약" 참고).
- 실행: `cd backend && .venv/bin/python backtest.py --futures --portfolio --leverage 3 --trade-cooldown 6 --min-sell-weight 0.20 --dynamic-sl --short-all --days 540`

### 3. Documentation
- 코드 변경 시 **PROGRESS.md + MEMORY.md 동시 업데이트**.
- MEMORY.md는 200줄 이내 유지 — 상세 내용은 토픽 파일로 분리.
- 버전 업 시 CHANGELOG.md에 기록.

### 4. Commit
- 코드 변경을 그때그때 **즉시 커밋**. 큰 변경은 논리 단위로 분리.
- 커밋 메시지: `feat:`, `fix:`, `refactor:`, `test:`, `docs:` prefix 사용.

### 5. Deployment
- 배포: `git pull` → `sudo systemctl restart coin-backend` → 엔진 start API.
- **nohup 절대 사용 금지** — 반드시 systemd 서비스로 실행.
- 서버 재시작 후 반드시 엔진 start 호출 (자동 시작 아님).
- 상세: `DEPLOYMENT.md` 참고.

---

## Key Architecture

### Strategy Config
- **현물** (4h, 4전략): cis_momentum(0.42), bnf_deviation(0.25), donchian_channel(0.24), larry_williams(0.10) — Optuna 바이낸스 USDT 재최적화 (v0.39)
- **선물 V2** (5m+1h, 2 레짐 전략): MeanReversion(RANGING+VOLATILE), TrendFollower(TRENDING_UP+DOWN)
- min_confidence: 0.50(현물), 0.40(선물 V2), MIN_ACTIVE_WEIGHT: 0.12 (crash=0.06)

### Signal Combiner (현물 전용)
- HOLD = 기권 (투표 미참여). BUY/SELL만 경쟁.
- active_weight로 정규화 → min_confidence 이상만 실행.
- 거래소별 독립 인스턴스 (exchange_name 로그 구분).

### FuturesEngineV2 (선물)
- RegimeDetector: 1h ADX + BB Width + ATR% + EMA slope → 4 레짐 분류 (히스테리시스: 3h 최소 유지, 2회 확인)
- StrategySelector: 레짐 → 단일 전략 매핑 (TRENDING→TF, RANGING+VOLATILE→MR)
- 2 레짐 전략: MeanReversion(BB+RSI+1h RSI확인), TrendFollower(EMA+ADX+1h RSI방향확인, SL 2.0 ATR)
- SafeOrderPipeline: 주문 실행 + SL/TP/trailing 관리
- 백테스트: `backtest_v2.py` — Walk-Forward 검증, 전략별/레짐별 진단

### EngineConfig
- `EngineConfig.from_app_config(app_config, exchange_name)` 팩토리.
- 엔진 내부에서 `"binance"` 같은 문자열 비교 완전 제거 → `self._ec.*` 통합 참조.

### DB Convention
- 모든 테이블에 `exchange` 컬럼 (기본값 "bithumb").
- Position은 (symbol, exchange) 복합 유니크.
- DateTime: `DateTime(timezone=True)`, `_utcnow()` 헬퍼.
- PostgreSQL 16 (운영) / SQLite (테스트).

### Indicator Columns
라이브: `sma_20`, `rsi_14` (lowercase). 백테스트: `SMA_20`, `RSI_14` (uppercase).

---

## Directory Structure

```
backend/
├── main.py              # FastAPI lifespan, 쿼드 엔진 조립
├── config.py            # Pydantic Settings (AppConfig)
├── backtest.py          # Backtester V1 (현물/선물/로테이션/포트폴리오)
├── backtest_v2.py       # Backtester V2 (레짐 전략, Walk-Forward 검증)
├── core/                # models, schemas, enums, event_bus, error_classifier
├── exchange/            # base, bithumb_v2, binance_usdm, binance_spot, paper
├── strategies/          # 현물4 + 선물7(V1) + 레짐3(V2) + combiner + registry + ml_filter
├── engine/              # trading_engine, futures_engine_v2, surge_engine, regime_detector, strategy_selector, order_manager, portfolio_manager
├── agents/              # market_analysis, risk_management, trade_review, performance_analytics, strategy_advisor, diagnostic_agent
├── services/            # market_data, notification/, llm/, discord_bot/
├── api/                 # FastAPI routes (모든 엔드포인트 exchange 파라미터)
├── db/                  # SQLAlchemy async session
└── tests/               # pytest + 인메모리 SQLite

frontend/src/            # React 18, TypeScript, Vite, TailwindCSS
```

---

## Common Patterns

### 새 전략 추가
1. `strategies/new_strategy.py` — `@StrategyRegistry.register`, `BaseStrategy` 상속
2. `combiner.py` — 가중치 추가 (SPOT_WEIGHTS 또는 DEFAULT_WEIGHTS + ADAPTIVE_PROFILES)
3. `main.py` — import 추가
4. `backtest.py` — `ALL_STRATEGIES_N`, `WEIGHTS_N` 추가
5. **540일 백테스트 검증 필수**
6. **테스트 추가 필수**

### Exchange Adapter
- `ExchangeAdapter` ABC (base.py) 상속
- 선물 메서드: Optional (기본 NotImplementedError)
- Bithumb: ccxt public + aiohttp JWT private, `BTC/KRW` <-> `KRW-BTC`
- Binance: ccxt binanceusdm / binance, ccxt.pro WebSocket

### Config
- `config.py` Pydantic Settings, env_prefix 기반
- `TradingConfig` (TRADING_), `BinanceTradingConfig` (BINANCE_TRADING_), `BinanceSpotTradingConfig` (BINANCE_SPOT_TRADING_)
- `FuturesV2Config` (FUTURES_V2_), `SurgeTradingConfig` (SURGE_TRADING_), `DiscordBotConfig` (DISCORD_BOT_)

---

## Quick Commands

```bash
# 서버 실행
cd backend && .venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000

# 엔진 시작
curl -X POST http://localhost:8000/api/v1/engine/start
curl -X POST "http://localhost:8000/api/v1/engine/start?exchange=binance_futures"
curl -X POST "http://localhost:8000/api/v1/engine/start?exchange=binance_spot"

# 테스트
cd backend && .venv/bin/python -m pytest tests/ -x -q

# 백테스트 V1 (선물 7전략 앙상블)
cd backend && .venv/bin/python backtest.py --futures --portfolio --leverage 3 --trade-cooldown 6 --min-sell-weight 0.20 --dynamic-sl --short-all --days 540

# 백테스트 V2 (선물 레짐 전략, 라이브 파라미터 일치)
cd backend && .venv/bin/python backtest_v2.py --days 540 --leverage 3 --cooldown 312 --min-confidence 0.4 --eval-interval 48
```

---

## Environment & Config Reference

### Config Classes (config.py)
| 클래스 | env_prefix | 주요 필드 |
|--------|-----------|-----------|
| `TradingConfig` | `TRADING_` | mode, initial_balance, rotation_enabled, asymmetric_mode |
| `BinanceTradingConfig` | `BINANCE_TRADING_` | mode, initial_balance_usdt, eval_interval |
| `BinanceSpotTradingConfig` | `BINANCE_SPOT_TRADING_` | mode, initial_balance_usdt |
| `BinanceConfig` | `BINANCE_` | enabled, api_key, api_secret, testnet, default_leverage, spot_enabled, tracked_coins |
| `ExchangeConfig` | `EXCHANGE_` | api_key, api_secret (빗썸) |
| `RiskConfig` | `RISK_` | max_single_coin_pct, max_drawdown_pct, rebalancing |
| `LLMConfig` | `LLM_` | enabled, model, api_key, gemini_api_key |
| `FuturesV2Config` | `FUTURES_V2_` | enabled, eval_interval, cooldown, max_position_pct, regime_* |
| `SurgeTradingConfig` | `SURGE_TRADING_` | enabled, mode, leverage, sl/tp/trail |
| `DiscordBotConfig` | `DISCORD_BOT_` | enabled, bot_token, channel_id |

### 거래소별 모드 독립
```bash
TRADING_MODE=paper              # 빗썸 현물 (paper/live 독립)
BINANCE_TRADING_MODE=live       # 바이낸스 선물 (독립)
BINANCE_SPOT_TRADING_MODE=live  # 바이낸스 현물 (독립)
```
세 거래소가 각각 paper/live를 독립적으로 설정. 빗썸 paper + 바이낸스 live 조합 가능.

### 엔진 속성 접근
- 엔진 내부: `self._ec.*` (EngineConfig) — 거래소별 설정 통합 참조
- 외부에서 exchange 접근: `engine._exchange` (private) — `engine.exchange` 아님!
- 추적 코인: `engine.tracked_coins` (public property)

### 거래소별 통화
| 거래소 | 기준 통화 | 수수료 | 심볼 형식 |
|--------|----------|--------|-----------|
| 빗썸 | KRW | 0.25% | `BTC/KRW` |
| 바이낸스 현물 | USDT | 0.10% | `BTC/USDT` |
| 바이낸스 선물 | USDT | 0.04% | `BTC/USDT` |

### 프론트엔드 통화 판별
```typescript
exchange.startsWith('binance') ? 'USDT' : '원'
```

---

## Gotchas (자주 실수하는 부분)

### 선물 cash 관리
- **내부 장부 기반**: buy/sell 반영 + Income API 펀딩비 (8시간 폴링)
- `initialize_cash_from_exchange()`: 서버 시작 1회 초기화
- sync는 futures cash를 **덮어쓰지 않음** (audit log only)
```python
# WRONG: USDT.free에 unrealizedPnL이 포함되어 이중계산됨
cash = balance['USDT']['free']

# RIGHT: walletBalance에서 totalMargin 차감
wallet = balance['USDT']['total'] - unrealized_pnl
cash = wallet - total_margin
```

### 선물 수수료
- CCXT `create_order()` 응답에 fee 정보 미포함 (항상 0).
- `_parse_order()`에서 `cost * 0.04%`로 추정 계산 (binance_usdm_adapter.py).

### reconcile 동작 차이
- **현물 paper**: `reconcile_cash_from_db()` 적용 — DB 포지션 기반 현금 재계산
- **현물 live**: reconcile 비활성 — sync 잔고 존중
- **선물**: reconcile 비활성 — 내부 장부 기반. `sync_exchange_positions()`(2분)은 포지션 감사만

### Bithumb API
```python
# POST: json=params (form data 아님)
# query_hash: SHA512(urlencode(params))
# 심볼: BTC/KRW ↔ KRW-BTC (ccxt ↔ API)
# 시장가 매수: order_type="price" (KRW 금액 기준)
# 시장가 매도: order_type="market" (코인 수량 기준)
```

### 백테스트 vs 라이브 컬럼명
```python
# 백테스트 (pandas-ta 기본): SMA_20, RSI_14, ATRr_14
# 라이브 (MarketDataService): sma_20, rsi_14, atr_14
# 전략에서 df['sma_20'] 사용 — 백테스트에서는 rename 필요
```

### DB 마이그레이션
- 새 컬럼: `db/migrate.py`에 `add_column_if_not_exists()` 추가
- Alembic도 있지만 수동 migrate.py와 혼재 — 둘 다 확인 필요
- 테스트는 인메모리 SQLite → 마이그레이션 불필요 (create_all)

### Position Tracker
- SL/TP/trailing 상태: Position 테이블에 영속화 (7컬럼)
- 재시작 시 `_check_stop_conditions()`에서 자동 복원
- `entry_price=0` 가드: avg_buy_price fallback, 불가 시 SL/TP 체크 스킵

### 워시아웃 (재매수 방지)
- `_last_sell_time[symbol]`: **인메모리** dict — 재시작 시 소실
- `Position.last_sell_at`: **DB** — `_restore_trade_timestamps()`로 재시작 시 복원
- 청산 포지션(qty=0)은 복원 건너뜀
- 강제 청산(에러 기반)은 쿨다운 면제: `_last_sell_time` 삭제

---

## Reference Docs
| 문서 | 내용 |
|------|------|
| `PROGRESS.md` | 운영 참조 — 현재 설정, API, 남은 과제 |
| `CHANGELOG.md` | 버전 이력 |
| `DEVELOPMENT.md` | 개발 규칙 — 테스트, 백테스트, 코드 컨벤션 |
| `DEPLOYMENT.md` | 배포 프로세스 — 라즈베리파이, systemd, nginx |
| `README.md` | 프로젝트 소개 + Quick Start |
