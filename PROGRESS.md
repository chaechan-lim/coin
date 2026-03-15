# 코인 자동 매매 시스템 — 운영 참조

> 최종 업데이트: 2026-03-14
> 완료된 Phase 1-5 상세 및 버전 이력은 `CHANGELOG.md` 참고.

---

## 개요

빗썸(현물, 비활성) + 바이낸스 현물(live) + 바이낸스 USDM 선물(live, 3x) + 서지 **쿼드 엔진** 24시간 자동 트레이딩 시스템.
가중 투표 (HOLD=기권) + ML 시그널 필터 + 5요소 시장 감지 + 적응형 가중치, AI 에이전트 5종, Discord 봇(자연어 제어), React 대시보드(8탭).
**현물 4전략** (BNF이격도, CIS모멘텀, 래리윌리엄스, 돈치안채널) + **선물 7전략** (MA, RSI, MACD, 볼린저RSI, 스토캐스틱RSI, OBV, BB스퀴즈).
**자기 치유 엔진** (에러 분류 → 자동 복구 → LLM 진단), **979 유닛 테스트**.

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
│   ├── services/      (market_data, notification/, llm/, discord_bot/)
│   ├── strategies/    (11전략 + combiner + registry + ml_filter)
│   ├── agents/        (market_analysis, risk_management, trade_review, performance_analytics, strategy_advisor, diagnostic_agent, coordinator)
│   ├── engine/        (trading_engine, futures_engine, surge_engine, order_manager, portfolio_manager, recovery, health_monitor, capital_sync, scheduler)
│   ├── api/           (router, dependencies, dashboard, portfolio, trades, strategies, events, capital, websocket)
│   └── tests/         (979 tests)
└── frontend/
    └── src/           (Dashboard, 8탭 컴포넌트, hooks, types)
```

---

## 남은 과제 (Phase 6)

### 완료
| 항목 | 상세 |
|---|---|
| WS 자동 재연결 | 선물 엔진: 3회 연속 실패 → 지수 백오프 재연결 (5s→300s), 성공 시 폴백 해제 |
| Discord 봇 대화 컨텍스트 | 채널별 최근 10턴 히스토리, 1시간 만료, 후속 질문 지원 |
| 메모리 최적화 | Spot USDT 마켓만, WS markets 공유, gc.collect() (~200MB) |
| DB 자동 정리 | 매일 13:00 KST — strategy_logs 30d, snapshots 60d, agent_logs 60d, orders 90d |
| extreme_price 의미 명확화 | PositionTracker.extreme_price: 롱=최고가, 숏=최저가. DB 컬럼 호환 유지 |
| 일일 매수 카운터 DB 복원 | 엔진 재시작 시 Order 테이블에서 오늘 매수 수 복원 |
| Discord 봇 도구 4종 | get_health_status, get_funding_rates, get_system_stats, close_position (총 18개) |
| Discord 봇 선제 알림 | event_bus → bot.send_alert() (health/engine/risk 이벤트 자동 알림) |
| 구조화된 /health | 엔진 상세, 메모리, uptime, DB 연결 상태, WS 상태 |
| lifespan() 리팩토링 | _create_agent_stack, _sync_live_state, _create_self_healing 추출 (-93줄) |
| 포지션 상세 모달 | 가격 분포 시각화, SL/TP/트레일링 상세, 선물 청산 거리 |
| API 타임아웃+서킷브레이커 | 30초 타임아웃, 5회 연속 실패 → 60초 차단, 바이낸스 양쪽 어댑터 |
| 스케줄러 작업 타임아웃 | 5분 제한, hung job 방지 |
| 전략 루프 에러 추적 | 연속 5회 에러 → 60초 일시 중지 + 이벤트 알림 |
| 마켓 데이터 재시도+LRU | 지수 백오프 3회 재시도, LRU 캐시 (OHLCV 100, ticker 50) |
| N+1 쿼리 최적화 | _fast_stop_check_loop 배치 DB 조회 |
| 엔진 종료 태스크 정리 | stop() 시 task cancel + await (graceful shutdown) |
| Config 검증 | Pydantic field_validator (mode, confidence, pct 범위) |
| API exchange 검증 | validate_exchange() 유효 거래소 이름 검증 |
| create_task 이름 부여 | 전체 asyncio.create_task에 name= 파라미터 적용 |
| 평가 사이클 타이밍 로그 | elapsed_ms 구조화 로깅 |
| 다운타임 포지션 감사 | 서버 재시작 시 사라진 포지션 감지(청산 추정), 즉시 SL/TP 체크, emit_event 알림 |
| systemd 안정성 개선 | RestartSec=20, StartLimitBurst=10/600s, pg_isready 대기, ExecStopPost fuser |
| PostgreSQL 메모리 제한 | shared_buffers=128MB, max_conn=30, Docker 512MB 제한 |
| 프론트엔드 조건부 빌드 | src 변경 시만 npm build (506MB 피크 메모리 절감) |
| delisted 심볼 필터 | JEX 등 삭제 심볼 즉시 실패 + rotation 제외, position_sync 60→120초 |
| 교차 거래소 포지션 전환 | 높은 신뢰도(>=0.65) 반대 신호 시 기존 포지션 청산 후 새 방향 진입 |
| 선물 총 자산 과소계상 버그 수정 | `_merge_surge_positions`: `total_value_krw += unrealized` → `+= current_value` (surge 마진이 총 자산에서 누락되던 문제) |
| MIN_SELL_ACTIVE_WEIGHT | 단일 전략 숏 진입 방지 옵션 (기본 0.0=비활성, backtest --min-sell-weight) |
| 선물 트레일링 스탑 알림 스팸 수정 | `_check_futures_stop_conditions`에 5분 per-symbol 쿨다운 추가 (`_last_stop_event_time` 재사용). 청산 완료 시 쿨다운 해제. (COIN-6) |
| 선물 구조 최적화 (v0.37→v0.38) | 3x 레버리지, 쿨다운 cd6(24h), 7전략(bb_squeeze 추가), ML Signal Filter |
| 선물 쿨다운 구현 | futures_engine에 쿨다운 체크 추가, last_sell_at DB 기록 |
| ML Signal Filter (v0.37) | LightGBM 23피처, 선물 시그널 사전 필터링 (strategies/ml_filter.py) |
| 서지 엔진 (v0.37) | 거래량 급등 감지 단기 매매, 선물 PM 잔고 공유, exchange="binance_surge" |
| 버그 수정 11건 (v0.38) | entry_price=0 가드, cash race condition, DB 인덱스, API 검증, fire-and-forget 에러 핸들링 |
| 현물 Optuna 바이낸스 재최적화 (v0.39) | 빗썸 KRW→바이낸스 USDT 데이터로 재최적화 (PF 1.56, +22.48%), cis_momentum 지배적, SL5/TP14/trail3-1.5, cd15(60h) |
| 트레일링 스탑 알림 반복 버그 수정 | `_execute_stop_sell`: 미체결 주문 시 `return` → `raise RuntimeError` — cooldown pop 오동작 방지, 30초마다 알림 폭주 해결 |
| 서지 엔진 거래량 감지 수정 (v0.40) | ticker 24h volume→5m 캔들 OHLCV 기반, 배치 ticker(USDM 키 정규화), 좀비 포지션 자동 청산 |
| 현물 좀비 포지션 감지 버그 수정 | `sync_exchange_positions`: dust 잔고(가치 미만)가 exchange_symbols에 포함돼 좀비 탐지가 누락되던 버그 수정. exchange_symbols를 dust 필터 통과 심볼만 수집하도록 변경 |
| 선물 Optuna 최적화 도구 (v0.40) | optimize.py --futures 지원, 백테스트 적응형 가중치 bb_squeeze 누락 수정 |
| v2 백테스터 + 레짐 전략 개선 | backtest_v2.py: Walk-Forward 검증, 레짐 적응형 선물 엔진, 쿨다운/신뢰도/레짐 필터/평가 주기 |
| v2 레짐 전략 최적화 (2026-03-14) | MR: 1h RSI 반전 필수(88.5% WR), VB: KC 2.2/VOL 2.0/RSI필수(PF 1.54), TF: 상승 진입 비활성(하락만 PF 3.27). Tier1 SL/TP 버그 수정. 540d: **PF 2.17, WR 63.4%, MDD 5.42%**, WF 4/4 PASS |

| 프론트엔드 탭 수정 (COIN-7) | `FuturesEngineV2`에 `strategies` + `rotation_status` 프로퍼티 추가 → 전략 성과/종목·로테이션 탭 500 에러 해결. `list_strategies` getattr 폴백. `StrategyPerformance.tsx` bb_squeeze + v2 전략 이름 추가. `RotationMonitor.tsx` v2 레짐 상태 레이블 추가 |

### 낮은 우선순위

| 항목 | 상세 |
|---|---|
| ~~시장 상태별 전략 on/off~~ | ~~ADAPTIVE_PROFILES로 대체 (v0.32)~~ |
| ~~Alembic 마이그레이션 정리~~ | ~~현 구조로 안정 운영 중~~ |
| 로그 로테이션/모니터링 | systemd journal 기반, 별도 관리 미설정 |
| ~~nginx 직접 서빙~~ | ~~완료 (v0.32)~~ |
| ~~포지션 상세 모달~~ | ~~완료 (v0.35)~~ |

---

## 핵심 설계 결정

### 전략 신호 결합 (HOLD=기권)
```
전략들 → Signal(type, confidence, reason)
         ↓
  SignalCombiner (가중 투표, HOLD=기권)
  BUY/SELL만 경쟁, active_weight < 0.12 → HOLD
  임계값(0.55) 이상만 실행
         ↓
  5요소 시장 감지 → 적응형 가중치
  confidence < 0.35 → 임계값 +0.10
  crash=25% / downtrend=50% / 나머지=100% 사이징
```

### 전략 가중치 프로필

**선물** (7전략, 기본 DEFAULT_WEIGHTS):

| 전략 | 가중치 |
|---|---|
| bollinger_rsi | 0.26 |
| rsi | 0.21 |
| bb_squeeze | 0.15 |
| stochastic_rsi | 0.13 |
| obv_divergence | 0.11 |
| ma_crossover | 0.07 |
| macd_crossover | 0.07 |

시장 상태별 ADAPTIVE_PROFILES (7전략×5상태): `combiner.py` 참고.

**현물** (4전략, 고정 SPOT_WEIGHTS — Optuna 바이낸스 USDT 최적화 2026-03-13):

| 전략 | 가중치 |
|---|---|
| cis_momentum | 0.42 |
| bnf_deviation | 0.25 |
| donchian_channel | 0.24 |
| larry_williams | 0.10 |

### 리스크 설정

| 설정 | 값 |
|---|---|
| 단일 코인 최대 비중 | 40% (초과 시 35%까지 자동 매도) |
| 일일 매수 상한 | 20건 (매도 무제한) |
| 코인당 매수 상한 | 3건/일 |
| 매매 쿨다운 | **현물 60시간 (cd15)**, **선물 24시간 (cd6)** |
| 매도 후 재매수 대기 | **현물 60시간**, **선물 24시간** |
| 선물 레버리지 | **3x** |
| 선물 숏 허용 | 전체 시장 상태 (short-all), min_sell_wt=0.20 (2전략 합의) |
| 교차 거래소 충돌 | 현물 롱↔선물 숏: 낮은 신뢰도→차단, 높은 신뢰도(>=0.65)→방향 전환 |
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
