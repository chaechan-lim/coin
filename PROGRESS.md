# 코인 자동 매매 시스템 — 운영 참조

> 최종 업데이트: 2026-03-06
> 완료된 Phase 1-5 상세 및 버전 이력은 `CHANGELOG.md` 참고.

---

## 개요

빗썸(현물, paper) + 바이낸스 현물(live) + 바이낸스 USDM 선물(live, 3x) **트리플 엔진** 24시간 자동 트레이딩 시스템.
가중 투표 (HOLD=기권) + 거래량 서지 매수 + 5요소 시장 감지, AI 에이전트(시장 분석 + 리스크 관리 + 거래 리뷰), React 대시보드(8탭, 거래소 전환).
**현물 4전략** (BNF이격도, CIS모멘텀, 래리윌리엄스, 돈치안채널) + **선물 6전략** (MA, RSI, MACD, 볼린저RSI, 스토캐스틱RSI, OBV).
**자기 치유 엔진** (에러 분류 → 자동 복구 → LLM 진단), **533+ 유닛 테스트**.

---

## 기술 스택

| 영역 | 기술 |
|---|---|
| 백엔드 | Python 3.12, FastAPI, SQLAlchemy (async), APScheduler |
| 프론트엔드 | React 18, TypeScript, Vite, TailwindCSS, Recharts |
| DB | PostgreSQL 16 (docker compose) / SQLite (테스트) |
| 거래소 | Bithumb V2 (ccxt+JWT), Binance 현물/선물 (ccxt/ccxt.pro) |
| 지표 | pandas + pandas-ta |
| 배포 | Raspberry Pi, systemd, nginx HTTPS |

---

## 프로젝트 구조

```
coin/
├── CLAUDE.md                    <- AI 어시스턴트 지침 (자동 로드)
├── PROGRESS.md                  <- 이 파일 (운영 참조)
├── CHANGELOG.md                 <- 버전 이력
├── DEVELOPMENT.md               <- 개발 규칙 (테스트, 백테스트, 컨벤션)
├── DEPLOYMENT.md                <- 배포 프로세스 (라즈베리파이)
├── backend/
│   ├── main.py, config.py, backtest.py
│   ├── core/          (models, schemas, enums, event_bus, error_classifier)
│   ├── db/            (session, migrate)
│   ├── exchange/      (base, bithumb_v2, binance_usdm, binance_spot, paper)
│   ├── services/      (market_data, notification, discord_event_handler)
│   ├── strategies/    (10전략 + combiner + registry)
│   ├── agents/        (market_analysis, risk_management, trade_review, diagnostic_agent, coordinator)
│   ├── engine/        (trading_engine, futures_engine, order_manager, portfolio_manager, recovery, health_monitor, capital_sync, scheduler)
│   ├── api/           (router, dependencies, dashboard, portfolio, trades, strategies, events, capital, websocket)
│   └── tests/         (533+ tests)
└── frontend/
    └── src/           (Dashboard, 8탭 컴포넌트, hooks, types)
```

---

## 남은 과제 (Phase 6)

### 중간 우선순위

(현재 없음 — 모두 완료 또는 구조적 정상 확인)

### 낮은 우선순위

| 항목 | 상세 |
|---|---|
| 시장 상태별 전략 on/off | 횡보 시 추세추종 완전 비활성 |
| Alembic 마이그레이션 정리 | 초기 마이그레이션 + 수동 migrate.py 혼재 |
| 로그 로테이션/모니터링 | systemd journal 기반, 별도 관리 미설정 |
| nginx 직접 서빙 | serve→nginx static, 메모리 170MB 절감 |
| 포지션 상세 모달 | 진입 시그널, 전략 기여도, SL/TP 차트 오버레이 |

---

## 핵심 설계 결정

### 전략 신호 결합 (HOLD=기권)
```
전략들 → Signal(type, confidence, reason)
         ↓
  SignalCombiner (가중 투표, HOLD=기권)
  BUY/SELL만 경쟁, active_weight < 0.12 → HOLD
  임계값(0.50) 이상만 실행
         ↓
  5요소 시장 감지 → 적응형 가중치
  confidence < 0.35 → 임계값 +0.10
  crash=25% / downtrend=50% / 나머지=100% 사이징
```

### 전략 가중치 프로필

**선물** (6전략, 시장 상태별):

| 시장 상태 | MA | RSI | MACD | Boll+RSI | StochRSI | OBV |
|---|---|---|---|---|---|---|
| 강한 상승 | 0.12 | 0.18 | 0.12 | 0.28 | 0.15 | 0.15 |
| 상승 | 0.10 | 0.22 | 0.10 | 0.28 | 0.15 | 0.15 |
| 횡보 (기본) | 0.08 | 0.25 | 0.08 | 0.31 | 0.15 | 0.13 |
| 하락 | 0.06 | 0.27 | 0.08 | 0.32 | 0.15 | 0.12 |
| 폭락 | 0.04 | 0.28 | 0.06 | 0.34 | 0.15 | 0.13 |

**현물** (4전략, 고정 SPOT_WEIGHTS):

| 전략 | 가중치 |
|---|---|
| cis_momentum | 0.32 |
| larry_williams | 0.32 |
| donchian_channel | 0.26 |
| bnf_deviation | 0.10 |

### 리스크 설정

| 설정 | 값 |
|---|---|
| 단일 코인 최대 비중 | 40% (초과 시 35%까지 자동 매도) |
| 일일 매수 상한 | 20건 (매도 무제한) |
| 코인당 매수 상한 | 3건/일, 1시간 간격 |
| 매도 후 재매수 대기 | 4시간 (washout) |
| 교차 거래소 충돌 | 현물 롱↔선물 숏 동시 진입 차단 |
| 현물 비대칭 | crash/downtrend 매수 차단, uptrend 공격적 매수 |

### 서지 로테이션

| 항목 | 추적 코인 | 서지 코인 |
|---|---|---|
| 손절 | 5% (ATR) | 4% |
| 익절 | 10% | 8% |
| 트레일링 | 5%/4% | 1.5%/2% |
| 최대 보유 | 무제한 | 48시간 |
| 진입 | BUY 필요 | BUY만 (HOLD 거부) |

---

## API 엔드포인트

> 모든 엔드포인트: `?exchange=bithumb|binance_futures|binance_spot` (기본: bithumb)

### REST (prefix: /api/v1)

| Method | Path | 설명 |
|---|---|---|
| GET | /portfolio/summary | 자산, P&L, 낙폭 |
| GET | /portfolio/positions | 코인별 보유 현황 |
| GET | /portfolio/history | 자산 추이 차트 |
| GET | /portfolio/daily-pnl | 일별 손익 통계 |
| GET | /trades | 거래 이력 (페이징+필터) |
| GET | /trades/summary | 승률, 수익 요약 |
| GET | /strategies | 전략 목록 + 가중치 |
| GET | /strategies/{name}/performance | 전략별 성과 |
| GET | /strategies/comparison | 전략 간 비교 |
| GET | /engine/status | 엔진 상태 |
| POST | /engine/start | 엔진 시작 |
| POST | /engine/stop | 엔진 중지 |
| GET | /engine/rotation-status | 서지 점수 |
| GET | /agents/market-analysis/latest | 시장 분석 |
| GET | /agents/risk/alerts | 리스크 경고 |
| GET | /agents/trade-review/latest | 거래 리뷰 |
| GET | /exchanges | 거래소 목록 |
| GET | /events | 서버 이벤트 로그 |

### WebSocket
- `WS /ws/dashboard` — 실시간 이벤트 (portfolio_update, trade_executed, strategy_signal, agent_alert, price_update, server_event)

---

## 실행 방법

> 상세 배포 절차: `DEPLOYMENT.md`, 개발 규칙: `DEVELOPMENT.md`, AI 지침: `CLAUDE.md`

```bash
# 서버 실행
cd backend && .venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000

# 엔진 시작 (재시작 후 반드시 호출)
curl -X POST http://localhost:8000/api/v1/engine/start?exchange=binance_futures
curl -X POST http://localhost:8000/api/v1/engine/start?exchange=binance_spot
curl -X POST http://localhost:8000/api/v1/engine/start

# 테스트
cd backend && .venv/bin/python -m pytest tests/ -x -q
```
