# 코인 자동 매매 시스템 — 구현 진행 현황

> 최종 업데이트: 2026-02-28

---

## 개요

빗썸(Bithumb) 현물 + 바이낸스(Binance) USDM 선물 **듀얼 엔진** 24시간 자동 암호화폐 트레이딩 시스템.
6개 전략 가중 투표 (HOLD=기권 방식) + 거래량 서지 매수 + 5요소 시장 감지, AI 에이전트(시장 분석 + 리스크 관리 + 거래 리뷰), React 대시보드(7탭, 거래소 전환) 포함.
**현재 라이브 운영 중**: 빗썸 현물 (~548K KRW) + 바이낸스 선물 (~174.92 USDT, 3x 레버리지), **PostgreSQL 16** (docker compose), **라즈베리파이 배포 완료**.
**듀얼 엔진 아키텍처**: 빗썸 TradingEngine + 바이낸스 BinanceFuturesEngine 독립 병렬 실행, EngineRegistry 중앙 관리, exchange 컬럼 기반 데이터 격리.
**바이낸스 선물 라이브**: 독립 모드 분리 (빗썸 paper/live + 바이낸스 paper/live 별도), 시장가 주문, 실제 USDT 잔고 조회.
**에이전트 심볼 분기**: MarketAnalysisAgent `market_symbol` 파라미터 — 빗썸 BTC/KRW, 바이낸스 BTC/USDT 자동 분기.
**선물 Graceful Stop**: 포지션 보유 시 경고, `force=true`로 강제 중지.

---

## 기술 스택

| 영역 | 기술 |
|---|---|
| 백엔드 | Python 3.12, FastAPI, SQLAlchemy (async), APScheduler |
| 프론트엔드 | React 18, TypeScript, Vite, TailwindCSS, Recharts, lightweight-charts |
| DB | **PostgreSQL 16** (docker compose) / SQLite (테스트 폴백) |
| Cache / PubSub | Redis 7 (Docker, 선택) |
| 거래소 연동 | Bithumb V2 (ccxt+JWT), Binance USDM 선물 (ccxt binanceusdm), 듀얼 엔진 EngineRegistry |
| 기술적 지표 | pandas + pandas-ta |
| 배포 | Docker Compose (restart: always → 24/7) |

---

## 프로젝트 구조

```
coin/
├── PROGRESS.md                  ← 이 파일
├── docker-compose.yml           ✅ 완료
├── .env.example                 ✅ 완료
├── setup.sh                     ✅ 완료 (WSL 초기 환경 세팅 스크립트)
├── dev.sh                       ✅ 완료 (WSL 로컬 개발 서버 실행 스크립트)
├── backend/
│   ├── main.py                  ✅ 완료
│   ├── config.py                ✅ 완료
│   ├── requirements.txt         ✅ 완료
│   ├── Dockerfile               ✅ 완료
│   ├── alembic.ini              ✅ 완료
│   ├── alembic/                 ✅ 완료
│   ├── core/
│   │   ├── __init__.py          ✅
│   │   ├── enums.py             ✅ 완료
│   │   ├── event_bus.py         ✅ 완료 (서버 이벤트 DB 기록 + WS 브로드캐스트 + 재시도)
│   │   ├── exceptions.py        ✅ 완료
│   │   ├── models.py            ✅ 완료
│   │   └── schemas.py           ✅ 완료
│   ├── db/
│   │   ├── __init__.py          ✅
│   │   ├── session.py           ✅ 완료
│   │   └── migrate.py           ✅ 완료 (exchange 컬럼 마이그레이션)
│   ├── exchange/
│   │   ├── __init__.py          ✅
│   │   ├── base.py              ✅ 완료
│   │   ├── bithumb_adapter.py   ✅ 완료 (V1, 미사용)
│   │   ├── bithumb_v2_adapter.py ✅ 완료 (V2, 현재 라이브)
│   │   ├── binance_usdm_adapter.py ✅ 완료 (USDM 선물)
│   │   ├── paper_adapter.py     ✅ 완료
│   │   └── data_models.py       ✅ 완료
│   ├── services/
│   │   ├── __init__.py          ✅
│   │   ├── market_data.py       ✅ 완료
│   │   └── notification.py      ✅ 완료
│   ├── strategies/
│   │   ├── __init__.py          ✅
│   │   ├── base.py              ✅ 완료
│   │   ├── registry.py          ✅ 완료
│   │   ├── combiner.py          ✅ 완료
│   │   ├── volatility_breakout.py ✅ 완료
│   │   ├── ma_crossover.py      ✅ 완료
│   │   ├── rsi_strategy.py      ✅ 완료
│   │   ├── macd_crossover.py    ✅ 완료
│   │   ├── bollinger_rsi.py     ✅ 완료
│   │   ├── stochastic_rsi.py   ✅ 완료
│   │   ├── obv_divergence.py   ✅ 완료
│   │   ├── supertrend.py       ✅ 완료
│   │   ├── grid_trading.py      ✅ 완료 (독립 관리형, combiner 미사용)
│   │   └── dca_momentum.py      ✅ 완료 (독립 관리형, combiner 미사용)
│   ├── agents/
│   │   ├── __init__.py          ✅
│   │   ├── market_analysis.py   ✅ 완료
│   │   ├── risk_management.py   ✅ 완료
│   │   ├── trade_review.py      ✅ 완료 (24h 거래 리뷰)
│   │   └── coordinator.py       ✅ 완료
│   ├── engine/
│   │   ├── __init__.py          ✅
│   │   ├── trading_engine.py    ✅ 완료
│   │   ├── futures_engine.py    ✅ 완료 (BinanceFuturesEngine 서브클래스)
│   │   ├── order_manager.py     ✅ 완료
│   │   ├── portfolio_manager.py ✅ 완료
│   │   └── scheduler.py         ✅ 완료
│   ├── api/
│   │   ├── __init__.py          ✅
│   │   ├── router.py            ✅ 완료
│   │   ├── dependencies.py      ✅ 완료 (EngineRegistry 싱글턴)
│   │   ├── dashboard.py         ✅ 완료
│   │   ├── events.py            ✅ 완료 (서버 이벤트 조회 + 건수)
│   │   ├── portfolio.py         ✅ 완료
│   │   ├── trades.py            ✅ 완료
│   │   ├── strategies.py        ✅ 완료
│   │   └── websocket.py         ✅ 완료
│   ├── tests/
│   │   ├── __init__.py          ✅
│   │   ├── conftest.py          ✅ 완료 (인메모리 SQLite 픽스처)
│   │   ├── test_api_strategies.py ✅ 완료 (7 tests)
│   │   ├── test_api_trades.py   ✅ 완료 (5 tests)
│   │   ├── test_api_portfolio.py ✅ 완료 (4 tests)
│   │   ├── test_portfolio_manager.py ✅ 완료 (9 tests)
│   │   ├── test_risk_management.py ✅ 완료 (5 tests)
│   │   ├── test_exchange_filter.py ✅ 완료 (5 tests, 거래소 격리)
│   │   └── test_futures_engine.py ✅ 완료 (11 tests, 선물 엔진)
│   └── pytest.ini               ✅ 완료
└── frontend/
    ├── package.json             ✅ 완료
    ├── tsconfig.json            ✅ 완료
    ├── vite.config.ts           ✅ 완료
    ├── Dockerfile               ✅ 완료
    ├── nginx.conf               ✅ 완료
    ├── tailwind.config.js       ✅ 완료
    └── src/
        ├── main.tsx             ✅ 완료
        ├── index.css            ✅ 완료
        ├── api/
        │   └── client.ts        ✅ 완료
        ├── components/
        │   ├── Dashboard.tsx    ✅ 완료
        │   ├── PortfolioSummary.tsx ✅ 완료
        │   ├── PortfolioChart.tsx   ✅ 완료
        │   ├── TradeHistory.tsx ✅ 완료
        │   ├── StrategyPerformance.tsx ✅ 완료
        │   ├── OrderLog.tsx     ✅ 완료
        │   ├── AgentStatus.tsx  ✅ 완료
        │   ├── EngineControl.tsx ✅ 완료
        │   ├── RotationMonitor.tsx ✅ 완료
        │   └── SystemLog.tsx      ✅ 완료 (서버 이벤트 타임라인)
        ├── hooks/
        │   ├── useWebSocket.ts  ✅ 완료
        │   └── usePortfolio.ts  ✅ 완료
        └── types/
            └── index.ts         ✅ 완료
```

---

## 구현 단계별 현황

### ✅ Phase 1 — 인프라 기반 (완료)

| 항목 | 파일 | 상태 |
|---|---|---|
| 프로젝트 스캐폴딩 | 전체 디렉토리 구조 | ✅ |
| 패키지 의존성 | `backend/requirements.txt` | ✅ |
| 설정 시스템 | `backend/config.py` (Pydantic Settings) | ✅ |
| Docker 구성 | `docker-compose.yml` | ✅ |
| 환경 변수 템플릿 | `.env.example` | ✅ |
| DB ORM 모델 | `core/models.py` (8개 테이블) | ✅ |
| 열거형/예외 | `core/enums.py`, `core/exceptions.py` | ✅ |
| DB 세션 | `db/session.py` (async SQLAlchemy + WAL) | ✅ |
| Pydantic 스키마 | `core/schemas.py` | ✅ |

### ✅ Phase 2 — 거래소 어댑터 + 시장 데이터 (완료)

| 항목 | 파일 | 상태 |
|---|---|---|
| 거래소 추상 인터페이스 | `exchange/base.py` | ✅ |
| 빗썸 어댑터 (ccxt) | `exchange/bithumb_adapter.py` (fetch_tickers 포함) | ✅ |
| 페이퍼 트레이딩 어댑터 | `exchange/paper_adapter.py` | ✅ |
| 데이터 모델 (DTO) | `exchange/data_models.py` | ✅ |
| 시장 데이터 서비스 | `services/market_data.py` (OHLCV + 지표) | ✅ |

### ✅ Phase 3 — 전략 엔진 (완료)

| 항목 | 파일 | 상태 |
|---|---|---|
| 전략 기반 클래스 | `strategies/base.py` | ✅ |
| 전략 레지스트리 | `strategies/registry.py` | ✅ |
| 신호 결합기 | `strategies/combiner.py` (가중 투표) | ✅ |
| 전략 1: 변동성 돌파 | `strategies/volatility_breakout.py` | ✅ |
| 전략 2: 이동평균 크로스 | `strategies/ma_crossover.py` | ✅ |
| 전략 3: RSI | `strategies/rsi_strategy.py` | ✅ |
| 전략 4: MACD | `strategies/macd_crossover.py` | ✅ |
| 전략 5: 볼린저+RSI | `strategies/bollinger_rsi.py` | ✅ |
| 전략 6: Stochastic RSI | `strategies/stochastic_rsi.py` | ✅ |
| 전략 7: OBV 다이버전스 | `strategies/obv_divergence.py` | ✅ |
| 전략 8: Supertrend | `strategies/supertrend.py` | ✅ |
| (독립) 그리드 | `strategies/grid_trading.py` | ✅ (combiner 미사용) |
| (독립) DCA+모멘텀 | `strategies/dca_momentum.py` | ✅ (combiner 미사용) |
| 주문 관리자 | `engine/order_manager.py` | ✅ |
| 포트폴리오 관리자 | `engine/portfolio_manager.py` | ✅ |
| 트레이딩 엔진 | `engine/trading_engine.py` | ✅ |

### ✅ Phase 3 — AI 에이전트 (완료)

| 항목 | 파일 | 상태 |
|---|---|---|
| 시장 분석 에이전트 | `agents/market_analysis.py` | ✅ |
| 리스크 관리 에이전트 | `agents/risk_management.py` | ✅ |
| 에이전트 조율자 | `agents/coordinator.py` | ✅ |

### ✅ Phase 4 — REST API (완료)

| 항목 | 파일 | 상태 |
|---|---|---|
| 라우터 통합 | `api/router.py` | ✅ |
| 포트폴리오 API | `api/portfolio.py` | ✅ |
| 거래 이력 API | `api/trades.py` | ✅ |
| 전략 API | `api/strategies.py` | ✅ |
| 대시보드/엔진 API | `api/dashboard.py` | ✅ |
| WebSocket | `api/websocket.py` | ✅ |

### ✅ Phase 4 마무리 — 앱 진입점 + 스케줄러 (완료)

| 항목 | 파일 | 상태 |
|---|---|---|
| FastAPI 앱 진입점 | `backend/main.py` | ✅ |
| APScheduler | `engine/scheduler.py` | ✅ |
| 알림 서비스 | `services/notification.py` (텔레그램) | ✅ |
| Alembic 설정 | `alembic.ini` + `alembic/env.py` | ✅ |
| 백엔드 Dockerfile | `backend/Dockerfile` | ✅ |

### ✅ Phase 4 — React 대시보드 UI (완료)

| 항목 | 파일 | 상태 |
|---|---|---|
| 패키지 설정 | `frontend/package.json`, `tsconfig.json`, `vite.config.ts` | ✅ |
| Tailwind + PostCSS + nginx | `tailwind.config.js`, `nginx.conf` | ✅ |
| 타입 정의 | `frontend/src/types/index.ts` | ✅ |
| API 클라이언트 | `frontend/src/api/client.ts` | ✅ |
| WebSocket 훅 | `frontend/src/hooks/useWebSocket.ts` | ✅ |
| 포트폴리오 훅 | `frontend/src/hooks/usePortfolio.ts` | ✅ |
| 앱 진입점 | `frontend/src/main.tsx` | ✅ |
| 대시보드 + 탭 네비 (모바일 스크롤) | `frontend/src/components/Dashboard.tsx` | ✅ |
| 포트폴리오 요약 + 포지션 (모바일 카드) | `frontend/src/components/PortfolioSummary.tsx` | ✅ |
| 포트폴리오 추이 차트 | `frontend/src/components/PortfolioChart.tsx` | ✅ |
| 거래 이력 (전략 귀속 상세) | `frontend/src/components/TradeHistory.tsx` | ✅ |
| 전략 성과 비교 | `frontend/src/components/StrategyPerformance.tsx` | ✅ |
| 전략 신호 로그 (회고용) | `frontend/src/components/OrderLog.tsx` | ✅ |
| 에이전트 상태 + 가중치 시각화 | `frontend/src/components/AgentStatus.tsx` | ✅ |
| 엔진 제어 + 실시간 이벤트 피드 | `frontend/src/components/EngineControl.tsx` | ✅ |
| 로테이션 모니터 (서지 바 차트) | `frontend/src/components/RotationMonitor.tsx` | ✅ |
| 시스템 이벤트 로그 (필터+페이징) | `frontend/src/components/SystemLog.tsx` | ✅ |
| 프론트엔드 Dockerfile | `frontend/Dockerfile` | ✅ |

### 🔄 Phase 5 — 안정화 (진행 중)

| 항목 | 상태 |
|---|---|
| 구조화된 로깅 (structlog) | ✅ 코드 내 적용 완료 |
| 에러 핸들링 / 재연결 | ✅ 기본 구현 완료 |
| 텔레그램 알림 | ✅ 구현 완료 (설정만 필요) |
| SQLite WAL 모드 | ✅ 동시 접근 안정화 |
| 주문 fill 폴링 | ✅ 지정가 주문 체결/수수료 추적 |
| emit_event 재시도 | ✅ DB locked 3회 재시도 |
| Signal Combiner 개선 | ✅ HOLD=기권 방식 |
| 5요소 시장 감지 | ✅ backtest + live 동기화 |
| 서지 매수 방식 개선 | ✅ 전량매도→현금매수로 변경 |
| 서지 매도 프로필 | ✅ 타이트 단타 (SL2.5/TP5/트레일1.5-2/24h) |
| 서지 평가 버그 수정 | ✅ held_symbols 포함 |
| 수수료 추적 UI | ✅ 개요 페이지 수수료 지출 카드 |
| 낙폭 UI 개선 | ✅ 고점 대비 음수 표시 + 3단계 색상 |
| 리스크 에이전트 낙폭 수정 | ✅ peak = MAX(total_value_krw), 단계별 대응 (WARNING/CRITICAL) |
| 매수 차단 시 매도 허용 | ✅ buying_paused 상태에서도 SELL 신호 실행 |
| 시장 상태 동기화 | ✅ 엔진↔에이전트 즉시 동기화 |
| 미체결 주문 표시 수정 | ✅ was_executed = (status == FILLED) |
| 매수 비중 상향 | ✅ max_trade_size_pct 0.30 → 0.50 |
| 원금 대비 수익 표시 | ✅ initial_balance_krw + total_pnl_pct 원금 기준 |
| 전략 성과 P&L 수정 | ✅ FIFO 원가 매칭 (기존: sell.requested_price 비교 → 오계산) |
| 모바일 반응형 UI | ✅ 탭 스크롤, 테이블→카드, 터치 타겟, 전 컴포넌트 |
| 단위 테스트 | ✅ 125개 (pytest + 인메모리 SQLite) |
| 거래 기본 필터 | ✅ 체결(filled)만 기본 표시, status 파라미터 |
| 시작 시 현금 보정 | ✅ reconcile_cash_from_db at startup (peak 오염 방지) |
| 0% 승률 전략 제거 | ✅ volatility_breakout/supertrend 비활성 → 6전략 체제 |
| 진입 기준 상향 | ✅ min_confidence 0.25→0.50, 쿨다운 3→12캔들 |
| 동적 로테이션 코인 | ✅ 빗썸 전체 마켓 스캔 → 거래대금 10B+ 자동 선정 (6시간 갱신) |
| 스마트 매매 제한 | ✅ 매수만 카운트 (매도 무제한), 일일 매수 20회 + 코인당 3회 |
| **듀얼 엔진 아키텍처** | ✅ 빗썸 + 바이낸스 병렬 실행, EngineRegistry |
| DB exchange 컬럼 | ✅ 6테이블 exchange 컬럼 + 마이그레이션 |
| BinanceFuturesEngine | ✅ 롱/숏 양방향, 레버리지, 청산가 감시, 펀딩비 |
| API 거래소 라우팅 | ✅ 모든 엔드포인트 exchange 파라미터 |
| 프론트엔드 거래소 전환 | ✅ Dashboard 거래소 탭, 모든 컴포넌트 exchange prop |
| 거래소 격리 테스트 | ✅ 5 tests + 선물 엔진 11 tests |
| 바이낸스 선물 라이브 | ✅ 독립 모드 분리, 시장가 주문, 실제 USDT 잔고 조회 |
| WebSocket 가격 모니터 | ✅ ccxt.pro 실시간 SL/TP/청산가 체크 (~1초), 5분 폴링 fallback |
| **P1 최적화** | ✅ 4h 타임프레임, 동적 SL, 숏 전면 허용, PF 1.80 |
| 가격 0원 fallback | ✅ fetch_ticker last=None → bid/ask 중간값 → orderbook fallback |
| **자동 리밸런싱** | ✅ 비중 40% 초과 → 35%까지 자동 부분 매도 (현물+선물, 1시간 쿨다운) |
| 바이낸스 현물 연동 | ⬜ BinanceSpotAdapter (ccxt binance), 현물 TradingEngine 추가 (계획) |
| 시장 상태별 전략 on/off | ⬜ 횡보 시 추세추종 완전 비활성 (향후) |
| 라즈베리파이 배포 | ✅ 완료 (192.168.50.244, systemd) |
| 매매 회고 선물 인식 | ✅ 방향/레버리지/마진/청산가, LLM 선물 컨텍스트, 통화 자동 전환 |
| 선물 레버리지 sync | ✅ ccxt leverage=None fallback, fetch_balance 미포함 포지션 보정 |
| 선물 포지션 최적화 | ✅ 사이징 35%, conf 0.55 (MDD 5.42%, PF 1.80) |
| **현물 비대칭 전략** | ✅ 하락장 매수 차단, 상승장 공격적 매수 (알파 +25.38%) |

---

## 핵심 설계 결정 사항

### 전략 신호 결합 방식 (HOLD=기권)
```
6개 전략 → Signal(type, confidence, reason)
                    ↓
           SignalCombiner (가중 투표, HOLD=기권)
           BUY/SELL만 경쟁 — HOLD는 투표 미참여
           참여 가중치(active_weight)로 정규화
           active_weight < 0.12 → 의견 부족 HOLD
           임계값(0.25) 이상만 실행
                    ↓
           5요소 시장 감지 → 적응형 가중치 자동 적용
           market_confidence < 0.35 → 임계값 +0.10 상향
           crash=25% 사이징 / downtrend=50% / 나머지=100%
```

### 시장 상태별 전략 가중치 프로필 (6전략)

> Grid/DCA는 독립 관리형, volatility_breakout/supertrend는 0% 승률로 비활성. 6개 활성 전략만 사용.

| 시장 상태 | MA | RSI | MACD | Boll+RSI | StochRSI | OBV |
|---|---|---|---|---|---|---|
| 강한 상승장 | 0.12 | 0.20 | 0.15 | 0.22 | 0.15 | 0.16 |
| 상승장 | 0.10 | 0.22 | 0.14 | 0.25 | 0.14 | 0.15 |
| 횡보장 (기본) | 0.08 | 0.25 | 0.12 | 0.27 | 0.15 | 0.13 |
| 하락장 | 0.07 | 0.26 | 0.11 | 0.28 | 0.15 | 0.13 |
| 폭락장 | 0.05 | 0.28 | 0.10 | 0.30 | 0.14 | 0.13 |

### 5요소 시장 상태 감지 (Agent-style)

| 요소 | 판단 기준 |
|---|---|
| Price vs SMA20 거리 | >5% 상회→strong_up, 상회→up, <5% 하회→down |
| SMA20 vs SMA50 정렬 | 위→up, 아래→down |
| RSI | >70→strong, >55→up, <30→down, <45→down, else→sideways |
| 7일 가격변동 | >10%→strong, >3%→up, <-10%→down, <-3%→down |
| 거래량/SMA20 | >2x→strong+down 동시 (변동성) |

### 서지 로테이션 (현금 매수 방식)

서지 발견 시 기존 포지션을 유지하고 현금의 15%로 서지 코인 매수:
1. 이미 보유 코인이면 스킵
2. 전략 확인: **BUY만 허용** (HOLD/SELL 거부) — 백테스트 결과 엄격 확인이 최적
3. 현금 < 5,000원이면 스킵
4. 쿨다운: `rotation_cooldown_sec` (기본 7200초 = 2시간)

### 서지 코인 매도 프로필 (백테스트 C — 엄격 확인)

서지 코인은 추적 코인보다 별도의 매도 프로필 적용 (3조합 비교 후 C 채택):

| 항목 | 추적 코인 | 서지 코인 (C) |
|---|---|---|
| 손절 (SL) | 5% (또는 동적 ATR) | **4%** |
| 익절 (TP) | 10% | **8%** |
| 트레일링 활성화 | 3% | **1.5%** |
| 트레일링 스탑 | 3% | **2%** |
| 최대 보유 시간 | 무제한 | **48시간** |
| 전략 확인 | BUY 필요 | **BUY만 허용 (HOLD 거부)** |
| 서지 임계값 | - | **3.0x** |

- 시간 초과 시 수익/손실 무관 강제 청산
- 보유 포지션은 평가 사이클에서 tracked_coins + held_symbols 모두 평가
- 백테스트 180일 하락장: -4.27% (BTC B&H -37.71%, **알파 +33.44%**)
- PF 1.04, 승률 44.8%, MDD 10.44%

### 리스크 관리 — 낙폭 단계별 대응

| 낙폭 수준 | 레벨 | 동작 |
|---|---|---|
| 10-20% | WARNING | `reduce_buying` (경고 로그만, 매수 차단 안 함) |
| 20-50% | CRITICAL | `stop_buying` (해당 코인 매수 차단, **매도는 허용**) |
| 50%+ | CRITICAL | `emergency_sell` (전량 청산) |

- peak 계산: `MAX(PortfolioSnapshot.total_value_krw)` (인메모리 peak_value 대신 DB 기반)
- 매수 차단 시에도 SELL 신호는 통과 (`_can_trade=False`여도 SELL 실행)

### 자동 포트폴리오 리밸런싱 (v0.15)

매 평가 사이클(5분)마다 비중 체크 → `max_single_coin_pct`(40%) 초과 시 자동 부분 매도:

| 항목 | 값 | 비고 |
|------|------|------|
| 트리거 | 비중 > 40% | `RiskConfig.max_single_coin_pct` |
| 목표 비중 | 35% | `RiskConfig.rebalancing_target_pct` (5% 버퍼) |
| 주문 유형 | 시장가 | 리스크 제어 우선 |
| 쿨다운 | 1시간 | 동일 코인 연속 리밸런싱 방지 |
| 서지 포지션 | 스킵 | 별도 SL/TP/max_hold 관리 |
| 선물 처리 | direction 반영 | 롱→sell, 숏→buy, `_close_lock` 내 실행 |
| 일일 제한 | 면제 | 매도는 원래 무제한 |
| 환경변수 | `RISK_REBALANCING_ENABLED`, `RISK_REBALANCING_TARGET_PCT` | |

### 과매매 방지 레이어 (다중 장치)
1. **코인당 최소 간격**: 매수 1시간 이상 (매도는 간격 제한 없음)
2. **매수 후 쿨다운**: 30분 (손절 제외)
3. **일일 매수 상한**: 최대 20건 (**매도는 무제한** — 손절/익절 보장)
4. **코인당 일일 매수 상한**: 최대 3회 (동일 코인 과매매 방지)
5. **신뢰도 임계값**: 결합 신뢰도 0.50 이상만 실행
6. **시장 신뢰도 게이팅**: confidence < 0.35 → 임계값 +0.10
7. **시장 상태 사이징**: crash 25%, downtrend 50%
8. **비대칭 전략** (현물): crash/downtrend 매수 완전 차단, 상승장 신뢰도 완화 + 풀 사이징

### 현물 비대칭 전략 (v0.17.3)

하락장에서 최대한 손실을 방어하고 상승장에서 이득을 극대화:

| 시장 상태 | 매수 허용 | 신뢰도 임계값 | 사이징 |
|---|---|---|---|
| crash | 차단 | - | 0% |
| downtrend | 차단 | - | 0% |
| sideways | 허용 | base + 0.05 | 50% |
| uptrend | 허용 | base - 0.10 | 80% |
| strong_uptrend | 허용 | base - 0.15 | 100% |

- 설정: `TradingConfig.asymmetric_mode = True` (env: `TRADING_ASYMMETRIC_MODE`)
- 백테스트 540일: -10.06% (B&H -35.44%, **알파 +25.38%**)
- 기존 대비 손실 44% 감소 (-17.85% → -10.06%)

### 듀얼 엔진 아키텍처 (v0.13)

```
EngineRegistry (싱글턴)
├── "bithumb"
│   ├── TradingEngine (현물, exchange_name="bithumb")
│   ├── PortfolioManager (KRW)
│   ├── SignalCombiner + AgentCoordinator
│   └── 서지 로테이션 활성
└── "binance_futures"
    ├── BinanceFuturesEngine(TradingEngine) (선물, exchange_name="binance_futures")
    ├── PortfolioManager (USDT)
    ├── SignalCombiner + AgentCoordinator
    └── 롱/숏 양방향, 레버리지, 청산가 감시
```

**데이터 격리**: 6테이블(Order, Trade, Position, PortfolioSnapshot, StrategyLog, AgentAnalysisLog)에 `exchange` 컬럼 추가 (기본값 "bithumb"). Position은 (symbol, exchange) 복합 유니크.

**BinanceFuturesEngine 주요 기능**:
- 롱/숏 양방향 매매 (**전체 시장 숏 허용** — P1 백테스트 결과)
- **4h 타임프레임**: 전략 시그널 4h 캔들 기반 (노이즈 감소, PF 1.80)
- SL 8%/TP 16%/트레일 5%/3.5%: `/ sqrt(leverage)` 자동 축소 (P1 최적화)
- **동적 SL**: ATR 기반 + 시장 상태별 프로필 (crash=3~5%, uptrend=4~10%)
- 포지션 사이징: 35% (v0.17.3 백테스트 최적화, PF 1.80, +15.48%, MDD 5.42%)
- min_confidence: 0.55 (v0.17.2)
- 청산가 2% 이내 긴급 청산
- 펀딩비 30분 주기 조회
- 로테이션 비활성 (선물 전용)
- **WebSocket 실시간 가격 모니터**: ccxt.pro → ~1초 SL/TP/청산가 체크 (5분 폴링 fallback 이중 체크)
- **듀얼 타임프레임 시장 감지**: 4h(장기) + 1h(단기) 결합, 10분 갱신

### DB 주문 테이블 핵심 컬럼 (전략 귀속)
```sql
orders
  strategy_name         -- 주문을 촉발한 전략명
  signal_confidence     -- 해당 전략의 신뢰도 (0~1)
  signal_reason         -- 사람이 읽을 수 있는 사유 (회고용)
  combined_score        -- 결합 신뢰도 (여러 전략 조합 시)
  contributing_strategies -- JSON: 투표에 참여한 모든 전략 정보
```

---

## API 엔드포인트 목록

### REST (prefix: /api/v1)

| Method | Path | 설명 |
|---|---|---|
| GET | /portfolio/summary | 현재 자산, P&L, 낙폭 |
| GET | /portfolio/positions | 코인별 보유 현황 |
| GET | /portfolio/history | 기간별 자산 추이 (차트용) |
| GET | /trades | 거래 이력 (페이지네이션 + 필터) |
| GET | /trades/summary | 기간별 승률, 수익 요약 |
| GET | /trades/{id} | 개별 주문 상세 (전략 귀속 포함) |
| GET | /strategies | 전략 목록 + 현재 가중치 |
| GET | /strategies/{name}/performance | 전략별 성과 지표 |
| GET | /strategies/comparison | 전략 간 비교 |
| PUT | /strategies/{name}/params | 전략 파라미터 실시간 변경 |
| PUT | /strategies/{name}/weight | 전략 가중치 수동 조정 |
| GET | /strategies/logs | 전략 신호 이력 (회고 분석용) |
| GET | /engine/status | 엔진 상태 |
| POST | /engine/start | 엔진 시작 |
| POST | /engine/stop | 엔진 중지 |
| GET | /engine/rotation-status | 로테이션 상태 + 서지 점수 |
| GET | /agents/market-analysis/latest | 최신 시장 분석 결과 |
| GET | /agents/market-analysis/history | 시장 분석 이력 |
| GET | /agents/risk/alerts | 현재 리스크 경고 |
| GET | /agents/risk/history | 리스크 경고 이력 |
| GET | /agents/trade-review/latest | 최근 거래 리뷰 |
| POST | /agents/trade-review/run | 수동 거래 리뷰 실행 |
| GET | /agents/trade-review/history | 거래 리뷰 이력 |
| GET | /exchanges | 사용 가능 거래소 목록 |
| GET | /events | 서버 이벤트 로그 (페이징+필터) |
| GET | /events/counts | 레벨별 이벤트 건수 |

> **거래소 파라미터**: 모든 엔드포인트에 `?exchange=bithumb|binance_futures` 쿼리 파라미터 지원 (기본값: bithumb)

### WebSocket

| Endpoint | 설명 |
|---|---|
| WS /ws/dashboard | 실시간 이벤트 스트림 |

**WebSocket 이벤트 타입:**
- `portfolio_update` — 포트폴리오 총 자산/수익률
- `trade_executed` — 주문 체결 (전략명, 사유 포함)
- `strategy_signal` — 전략 신호 발생 (체결 미포함)
- `agent_alert` — 에이전트 경고/분석
- `price_update` — 실시간 가격
- `server_event` — 서버 이벤트 (엔진/트레이드/리스크/로테이션/전략/시스템)

---

## 실행 방법 (WSL2 기준)

> **환경**: Windows 11 + WSL2 (Ubuntu 22.04/24.04) + Docker Desktop (WSL2 백엔드 활성화)

---

### STEP 0 — WSL2 + Docker Desktop 설치 (최초 1회)

**Windows PowerShell (관리자)에서:**
```powershell
# WSL2 + Ubuntu 설치
wsl --install -d Ubuntu-22.04
# 설치 후 재부팅, Ubuntu 사용자 이름/비밀번호 설정
```

**Docker Desktop 설치:**
- https://www.docker.com/products/docker-desktop 에서 설치
- Settings → Resources → WSL Integration → Ubuntu-22.04 **체크**
- Apply & Restart

---

### STEP 1 — WSL 터미널 열고 프로젝트로 이동

```bash
# Windows 탐색기에서 폴더 열고 주소창에 "wsl" 입력하거나,
# 시작 메뉴에서 Ubuntu 실행 후:
cd /mnt/c/Users/chans/coin
```

---

### STEP 2 — 초기 환경 세팅 (최초 1회)

```bash
bash setup.sh
```

setup.sh가 자동으로 처리하는 것:
- Docker Engine / Compose 확인
- Python 3.12 설치 (deadsnakes PPA)
- Node.js 20 LTS 설치
- `.env.example` → `.env` 복사

> Docker 그룹 추가 후 "WSL 재시작이 필요하다"는 메시지가 나오면:
> WSL 터미널을 닫고 PowerShell에서 `wsl --shutdown` 후 다시 Ubuntu 실행, 그리고 `bash setup.sh` 재실행

---

### STEP 3 — 방법 A: Docker 전체 실행 (권장, 24/7 운영)

```bash
cd /mnt/c/Users/chans/coin

# 빌드 + 실행 (처음은 5~10분 소요)
docker compose up -d --build

# 로그 확인
docker compose logs -f backend
```

접속:
- 대시보드: http://localhost:3000
- API 문서: http://localhost:8000/docs

> **이후부터는 `docker compose up -d` 만 치면 됨.** `restart: always` 설정으로 WSL 재시작 시 자동 복구.

---

### STEP 3 — 방법 B: 로컬 개발 (코드 수정 → 즉시 반영)

```bash
cd /mnt/c/Users/chans/coin

# DB + Redis만 Docker로 실행
docker compose up -d postgres redis

# 백엔드 + 프론트엔드 로컬 실행 (한 번에)
bash dev.sh
```

dev.sh가 자동으로 처리하는 것:
- Python 가상환경 생성/활성화 (`backend/.venv`)
- `pip install -r requirements.txt`
- `alembic upgrade head` (DB 마이그레이션)
- `npm install` (필요 시)
- 프론트엔드 백그라운드 실행 (http://localhost:5173)
- 백엔드 `--reload` 모드 실행 (http://localhost:8000)
- `Ctrl+C` 시 프론트엔드도 같이 종료

---

### STEP 4 — .env 설정 확인

```bash
nano /mnt/c/Users/chans/coin/.env
```

페이퍼 트레이딩은 API 키 없어도 됨. 기본값으로 바로 실행 가능:
```env
TRADING_MODE=paper          # paper = 가상 매매
TRADING_INITIAL_BALANCE_KRW=500000   # 시작 잔액 (가상)
```

---

### 자주 쓰는 명령어

```bash
# 전체 상태 확인
docker compose ps

# 백엔드 실시간 로그
docker compose logs -f backend

# 특정 서비스 재시작
docker compose restart backend

# 전체 종료 (데이터 유지)
docker compose down

# 전체 종료 + DB 초기화 (주의!)
docker compose down -v

# 프론트엔드 로그 (로컬 개발 시)
tail -f /tmp/frontend.log

# 단위 테스트 실행 (30개, ~1초)
cd backend && .venv/bin/python -m pytest tests/ -v
```

---

### STEP 5 — 페이퍼 → 실전 전환

```bash
nano /mnt/c/Users/chans/coin/.env
```

```env
TRADING_MODE=live
EXCHANGE_API_KEY=빗썸_API_키
EXCHANGE_API_SECRET=빗썸_API_시크릿
```

```bash
docker compose restart backend
```

---

## 리스크 설정 기본값

| 설정 | 값 | 설명 |
|---|---|---|
| 단일 코인 최대 비중 | 40% | 초과 시 WARNING → CRITICAL |
| 최대 낙폭 한도 | 10% | 초과 시 매수 중단 |
| 일일 손실 한도 | 3% | 초과 시 당일 매매 중단 |
| 단일 거래 최대 크기 | 50% (잔액) | 포지션 사이징 기준 (.env 설정) |
| 일일 매수 상한 | 20건 | 매수만 카운트 (매도 무제한) |
| 코인당 일일 매수 상한 | 3건 | 동일 코인 과매매 방지 |
| 코인당 최소 매수 간격 | 1시간 | 과매매 방지 |

---

## 버전 이력

| 버전 | 날짜 | 내용 |
|---|---|---|
| v0.1 | 2026-02-24 | 초기 구현: 백엔드 전체 + API + React 대시보드 |
| v0.2 | 2026-02-24 | 라이브 전환: Bithumb V2 어댑터, SL/TP/trailing, 동적 손절, 거래량 로테이션 |
| v0.3 | 2026-02-24 | 백테스트-라이브 패리티 수정, crash→downtrend 통합, UTC→KST 수정 |
| v0.4 | 2026-02-25 | 서지 임계값 2.0x, 추적코인 5종 축소, 로테이션 모니터 프론트엔드 탭 |
| v0.5 | 2026-02-25 | 서버 이벤트 로그 시스템 (DB + API + WS + 시스템 로그 탭), UTC→로컬 타임존 수정 |
| v0.6 | 2026-02-25 | 8전략 combiner 개편 (HOLD=기권), 5요소 시장 감지, Bithumb V2 API 수정, SQLite WAL |
| v0.7 | 2026-02-25 | 수수료 추적 UI, 주문 fill 폴링, 서지 매수→현금 방식, emit_event 재시도 |
| v0.8 | 2026-02-25 | 서지 매도 프로필 C (SL4/TP8/트레일1.5-2/48h/BUY확인/3.0x), 낙폭 UI 개선, 서지 평가 버그 수정 |
| v0.9 | 2026-02-25 | 리스크 에이전트 수정 (peak DB기반, 단계별 대응, SELL 허용), 시장 상태 동기화, was_executed 수정 |
| v0.10 | 2026-02-25 | 원금 대비 수익 표시, max_trade_size_pct 0.50, 거래 기본 필터 filled, 시작 시 cash reconcile |
| v0.11 | 2026-02-26 | 모바일 반응형 UI (전 컴포넌트), 전략 성과 P&L FIFO 원가 매칭 수정, 유닛 테스트 30개 |
| v0.12 | 2026-02-26 | 전략 개선: 0%승률 전략 제거(8→6전략), min_confidence 0.50, 쿨다운 12캔들, 동적 로테이션 코인 |
| v0.12.1 | 2026-02-26 | 스마트 매매 제한: 매수만 카운트(매도 무제한), 일일 매수 20회 + 코인당 3회 |
| v0.13 | 2026-02-26 | **듀얼 엔진**: 바이낸스 USDM 선물 통합 (DB exchange 컬럼, BinanceFuturesEngine, EngineRegistry, API 라우팅, 프론트엔드 거래소 전환), 111 테스트 |
| v0.13.1 | 2026-02-26 | **바이낸스 선물 라이브**: 독립 모드 분리(BinanceTradingConfig.mode), 시장가 주문, fee_currency, 실제 USDT 잔고 조회 |
| v0.14 | 2026-02-26 | **WebSocket 실시간 가격 모니터**: 선물 SL/TP/청산가 ~1초 체크 (ccxt.pro), 듀얼 루프 (WS 모니터 + 5분 전략 평가), 동시 청산 방지 Lock |
| v0.14.1 | 2026-02-27 | **P1 최적화**: 4h 타임프레임, SL8/TP16/트레일5-3.5%, 동적 SL(ATR), 숏 전면 허용, 포지션 35% (PF 1.80, 알파 +62%) |
| v0.14.2 | 2026-02-27 | 가격 0원 fallback: fetch_ticker last=None → bid/ask 중간값 → orderbook mid-price |
| v0.15 | 2026-02-27 | **시스템 로그 강화 + LLM 매매 회고**: 매수/매도 이벤트 상세화(전략/신뢰도/PnL), 에이전트 결과 시스템 로그 발행, Claude API(haiku) 일일 심층 매매 회고 |
| v0.16 | 2026-02-28 | **포트폴리오 상태 복원 + 숏 P&L 수정 + Crash 숏 진입**: DB 스냅샷에서 peak/realized_pnl 복원 (재시작 팽창 방지), 원금 고정, 숏 P&L direction-aware 계산, crash MIN_ACTIVE_WEIGHT 완화(0.06), min notional 검증 |
| v0.17 | 2026-02-28 | **입출금 추적 + 동적 원금 관리**: CapitalTransaction DB 모델, 수동/자동 입출금 기록, 바이낸스 USDT 자동 감지(30분), 빗썸 KRW 잔고 변동 감지(5분), 시드 deposit 자동 생성, initial_balance DB 기반 재계산, 신규 DB peak_value 실제 자산 초기화, 프론트 입출금 관리 모달 |
| v0.17.1 | 2026-02-28 | **매매 회고 선물 인식**: 방향/레버리지/마진/청산가 반영, 숏 P&L direction-aware, LLM 프롬프트 선물 컨텍스트, 통화 자동 전환 (USDT/KRW) |
| v0.17.2 | 2026-02-28 | **선물 레버리지 sync 수정 + 백테스트 최적화**: ccxt leverage=None fallback(notional/margin 계산), fetch_balance 미포함 포지션 메타데이터 보정, 선물 포지션 사이징 35%→25% + conf 0.55 (MDD 7.3%→3.9%, PF 1.25→1.38) |
| v0.17.3 | 2026-03-01 | **현물 비대칭 전략 + 선물 pos 35%**: 하락장 매수 완전 차단 + 상승장 공격적 매수 (알파 +25.38%), 선물 포지션 사이징 25%→35% (PF 1.80, +15.48%, MDD 5.42%) |
| v0.18 | 예정 | 바이낸스 현물 연동 (BinanceSpotAdapter) |
| v1.0 | 진행중 | **라즈베리파이 배포 완료**, 장기 운영 안정화 |
