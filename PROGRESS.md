# 코인 자동 매매 시스템 — 구현 진행 현황

> 최종 업데이트: 2026-02-25

---

## 개요

빗썸(Bithumb) 거래소 기반의 24시간 자동 암호화폐 트레이딩 시스템.
5개 활성 전략 가중 투표 + 거래량 서지 로테이션, AI 에이전트(시장 분석 + 리스크 관리 + 거래 리뷰), React 대시보드(7탭) 포함.
**현재 라이브 운영 중** (BTC/KRW, 500K KRW, SQLite).

---

## 기술 스택

| 영역 | 기술 |
|---|---|
| 백엔드 | Python 3.12, FastAPI, SQLAlchemy (async), APScheduler |
| 프론트엔드 | React 18, TypeScript, Vite, TailwindCSS, Recharts, lightweight-charts |
| DB | SQLite (개발/현재) / PostgreSQL 16 (프로덕션) |
| Cache / PubSub | Redis 7 (Docker, 선택) |
| 거래소 연동 | Bithumb V2 API (ccxt public + aiohttp JWT private) |
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
│   │   ├── event_bus.py         ✅ 완료 (서버 이벤트 DB 기록 + WS 브로드캐스트)
│   │   ├── exceptions.py        ✅ 완료
│   │   ├── models.py            ✅ 완료
│   │   └── schemas.py           ✅ 완료
│   ├── db/
│   │   ├── __init__.py          ✅
│   │   └── session.py           ✅ 완료
│   ├── exchange/
│   │   ├── __init__.py          ✅
│   │   ├── base.py              ✅ 완료
│   │   ├── bithumb_adapter.py   ✅ 완료 (V1, 미사용)
│   │   ├── bithumb_v2_adapter.py ✅ 완료 (V2, 현재 라이브)
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
│   │   ├── grid_trading.py      ✅ 완료
│   │   └── dca_momentum.py      ✅ 완료
│   ├── agents/
│   │   ├── __init__.py          ✅
│   │   ├── market_analysis.py   ✅ 완료
│   │   ├── risk_management.py   ✅ 완료
│   │   ├── trade_review.py      ✅ 완료 (24h 거래 리뷰)
│   │   └── coordinator.py       ✅ 완료
│   ├── engine/
│   │   ├── __init__.py          ✅
│   │   ├── trading_engine.py    ✅ 완료
│   │   ├── order_manager.py     ✅ 완료
│   │   ├── portfolio_manager.py ✅ 완료
│   │   └── scheduler.py         ✅ 완료
│   ├── api/
│   │   ├── __init__.py          ✅
│   │   ├── router.py            ✅ 완료
│   │   ├── dashboard.py         ✅ 완료
│   │   ├── events.py            ✅ 완료 (서버 이벤트 조회 + 건수)
│   │   ├── portfolio.py         ✅ 완료
│   │   ├── trades.py            ✅ 완료
│   │   ├── strategies.py        ✅ 완료
│   │   └── websocket.py         ✅ 완료
│   └── tests/
│       └── __init__.py          ✅
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
| DB 세션 | `db/session.py` (async SQLAlchemy) | ✅ |
| Pydantic 스키마 | `core/schemas.py` | ✅ |

### ✅ Phase 2 — 거래소 어댑터 + 시장 데이터 (완료)

| 항목 | 파일 | 상태 |
|---|---|---|
| 거래소 추상 인터페이스 | `exchange/base.py` | ✅ |
| 빗썸 어댑터 (ccxt) | `exchange/bithumb_adapter.py` | ✅ |
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
| 전략 6: 그리드 | `strategies/grid_trading.py` | ✅ |
| 전략 7: DCA+모멘텀 | `strategies/dca_momentum.py` | ✅ |
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
| 대시보드 + 탭 네비 | `frontend/src/components/Dashboard.tsx` | ✅ |
| 포트폴리오 요약 + 포지션 | `frontend/src/components/PortfolioSummary.tsx` | ✅ |
| 포트폴리오 추이 차트 | `frontend/src/components/PortfolioChart.tsx` | ✅ |
| 거래 이력 (전략 귀속 상세) | `frontend/src/components/TradeHistory.tsx` | ✅ |
| 전략 성과 비교 | `frontend/src/components/StrategyPerformance.tsx` | ✅ |
| 전략 신호 로그 (회고용) | `frontend/src/components/OrderLog.tsx` | ✅ |
| 에이전트 상태 + 가중치 시각화 | `frontend/src/components/AgentStatus.tsx` | ✅ |
| 엔진 제어 + 실시간 이벤트 피드 | `frontend/src/components/EngineControl.tsx` | ✅ |
| 로테이션 모니터 (서지 바 차트) | `frontend/src/components/RotationMonitor.tsx` | ✅ |
| 시스템 이벤트 로그 (필터+페이징) | `frontend/src/components/SystemLog.tsx` | ✅ |
| 프론트엔드 Dockerfile | `frontend/Dockerfile` | ✅ |

### ⬜ Phase 5 — 안정화 (다음 단계)

| 항목 | 상태 |
|---|---|
| 구조화된 로깅 (structlog) | 코드 내 적용 완료 |
| 에러 핸들링 / 재연결 | 기본 구현 완료 |
| 텔레그램 알림 | ✅ 구현 완료 (설정만 필요) |
| 단위 테스트 | ⬜ 작성 필요 |
| 페이퍼 트레이딩 검증 | ⬜ 실행 후 검증 필요 |

---

## 핵심 설계 결정 사항

### 전략 신호 결합 방식
```
각 전략 → Signal(type, confidence, reason)
                  ↓
         SignalCombiner (가중 투표)
         score = Σ(weight × confidence) per signal type
         임계값(0.4) 이상만 실행
                  ↓
         Market Analysis Agent가 가중치 자동 조정
         Risk Management Agent가 실행 필터링
```

### 시장 상태별 전략 가중치 프로필

> Grid/DCA는 독립 관리형이라 combiner에서 제외. 5개 활성 전략만 사용.

| 시장 상태 | RSI | 볼린저+RSI | MACD | 변동성돌파 | MA크로스 |
|---|---|---|---|---|---|
| 강한 상승장 | 0.20 | 0.20 | 0.20 | 0.20 | 0.20 |
| 상승장 | 0.25 | 0.30 | 0.15 | 0.15 | 0.15 |
| 횡보장 (기본) | 0.30 | 0.35 | 0.15 | 0.10 | 0.10 |
| 하락장 | 0.35 | 0.40 | 0.10 | 0.05 | 0.10 |

### 과매매 방지 레이어 (다중 장치)
1. **코인당 최소 간격**: 1시간 이상 (설정 가능)
2. **매수 후 쿨다운**: 30분 (손절 제외)
3. **일일 매매 상한**: 최대 10건
4. **신뢰도 임계값**: 결합 신뢰도 0.4 이상만 실행
5. **수수료 대비 수익성**: 왕복 수수료(~1%) 이상 기대수익 시만 실행

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
| GET | /events | 서버 이벤트 로그 (페이징+필터) |
| GET | /events/counts | 레벨별 이벤트 건수 |

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
| 단일 거래 최대 크기 | 20% (잔액) | 포지션 사이징 기준 |
| 최대 일일 거래 수 | 10건 | 과매매 방지 |
| 코인당 최소 매매 간격 | 1시간 | 과매매 방지 |

---

## 버전 이력

| 버전 | 날짜 | 내용 |
|---|---|---|
| v0.1 | 2026-02-24 | 초기 구현: 백엔드 전체 + API + React 대시보드 |
| v0.2 | 2026-02-24 | 라이브 전환: Bithumb V2 어댑터, SL/TP/trailing, 동적 손절, 거래량 로테이션 |
| v0.3 | 2026-02-24 | 백테스트-라이브 패리티 수정, crash→downtrend 통합, UTC→KST 수정 |
| v0.4 | 2026-02-25 | 서지 임계값 2.0x, 추적코인 5종 축소, 로테이션 모니터 프론트엔드 탭 |
| v0.5 | 2026-02-25 | 서버 이벤트 로그 시스템 (DB + API + WS + 시스템 로그 탭), UTC→로컬 타임존 수정 |
| v0.6 | 예정 | 단위 테스트 + 안정화 |
| v1.0 | 예정 | 장기 운영 안정화 |
