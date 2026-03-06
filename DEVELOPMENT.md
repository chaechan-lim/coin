# Development Guide

---

## 1. Testing Rules

### 원칙
- **모든 코드 변경에 테스트 추가/수정 필수.** 예외 없음.
- 변경 전 전체 테스트 통과 확인 → 변경 후 다시 통과 확인.
- 테스트 수는 늘어야지 줄어들면 안 됨 (현재 519+개).

### 실행
```bash
cd backend

# 전체 테스트
.venv/bin/python -m pytest tests/ -x -q

# 특정 파일/클래스
.venv/bin/python -m pytest tests/test_combiner.py -x -q
.venv/bin/python -m pytest tests/test_order_pnl.py::TestOrderPnlCalculation -x -q

# 커버리지 (선택)
.venv/bin/python -m pytest tests/ --cov=. --cov-report=term-missing
```

### 테스트 구조
```
tests/
├── test_strategies.py           # 전략 48개 — 각 전략 analyze() 시그널 검증
├── test_combiner.py             # 시그널 결합 19개 — 가중투표, HOLD=기권, 적응형
├── test_portfolio.py            # 포트폴리오 62개 — 포지션/잔고/스냅샷
├── test_can_trade.py            # 매매 제한 21개 — 쿨다운, 교차충돌, 일일한도
├── test_futures.py              # 선물 11개 — 롱/숏, 레버리지, 청산가
├── test_buy_execution.py        # 매수 실행 35개 — 최소금액, cash 검증
├── test_order_pnl.py            # 주문 PnL 10개 — 현물/선물 손익 계산
├── test_engine_config.py        # EngineConfig 25개 — 거래소 추상화
├── test_realtime_sync.py        # 실시간 동기화 46개 — WebSocket, 스냅샷
├── test_error_classifier.py     # 에러 분류 21개
├── test_recovery.py             # 복구 매니저 14개
├── test_health_monitor.py       # 헬스 모니터 19개
├── test_diagnostic_agent.py     # LLM 진단 17개
├── test_code_safety.py          # 코드 안전성 29개
└── ... (기타)
```

### 테스트 작성 가이드
- **DB**: 인메모리 SQLite + aiosqlite. 외부 DB 의존 금지.
- **외부 API**: Mock 필수 (exchange, LLM 등). `unittest.mock.AsyncMock` 사용.
- **새 기능**: 최소 정상 케이스 + 에러 케이스 + 경계값 테스트.
- **버그 수정**: 버그를 재현하는 테스트를 먼저 작성 → 수정 → 통과 확인.
- **전략**: `test_strategies.py`에 analyze() 결과 검증 추가.

---

## 2. Backtest Process

### 원칙
- **전략 파라미터 변경 시 반드시 540일 장기 백테스트 검증.**
- 단기(30d, 90d) 결과만으로 판단 금지 — 장기에서 악화하는 경우 다수.
- 백테스트 결과는 **성공/실패 모두** `backtest-analysis.md`(메모리)에 기록.
- 작업 전 기존 실패 이력 확인 → 동일 실수 반복 방지.

### 실행 모드
```bash
cd backend

# 현물 단일코인 (기본: BTC/KRW, 540일, 4h)
.venv/bin/python backtest.py --days 540

# 현물 포트폴리오 (멀티코인 동시 운용)
.venv/bin/python backtest.py --portfolio --days 540

# 선물 (롱/숏 + 레버리지)
.venv/bin/python backtest.py --futures --leverage 3 --days 540

# 선물 포트폴리오 (P1 최적화 설정)
.venv/bin/python backtest.py --futures --portfolio --leverage 3 --short-all --dynamic-sl --days 540

# 로테이션 (서지 매수)
.venv/bin/python backtest.py --rotation --dynamic-rotation --days 180

# 특정 코인
.venv/bin/python backtest.py --symbol ETH/KRW --days 365
.venv/bin/python backtest.py --futures --symbol ETH/USDT --days 365

# 리스크/매매제한 포함 (옵트인)
.venv/bin/python backtest.py --futures --portfolio --risk --trade-limits --days 540
```

### 평가 지표 (우선순위)
1. **Profit Factor (PF)** — 총 수익 / 총 손실. PF > 1이면 수익 시스템.
2. **Alpha** — B&H 대비 초과 수익률. 양수여야 존재 가치 있음.
3. **MDD** — 최대 낙폭. 20% 이하 권장.
4. **거래 횟수 + 수수료** — 과매매 여부 판단.
5. 승률은 참고만 — 30%라도 win/loss ratio가 좋으면 OK.

### 검증된 실패 패턴 (반복 금지)
1. 개별 전략 미세조정 — 단기 개선이 540일에서 악화
2. 방향별 가중치 분리 — 정규화 문제로 약한 신호 증폭
3. 동적 포지션 사이징 — 고정 35%가 최적
4. 1h 타임프레임 — 노이즈+수수료 폭증 (현물/선물 모두)
5. 추세추종+평균회귀 혼합 — 시그널 상쇄
6. 과도한 할인 (0.4x) — 0.5x 이하 권장

---

## 3. Code Conventions

### Python
- Python 3.12, async/await 기반 (FastAPI + SQLAlchemy async).
- 로깅: `structlog` — `logger.info("event_name", key=value)` 형식.
- Config: Pydantic Settings, env_prefix 기반 `.env` 매핑.
- 타입 힌트 사용 (기존 코드 스타일 따름).

### 전략 패턴
```python
@StrategyRegistry.register
class MyStrategy(BaseStrategy):
    name = "my_strategy"
    applicable_market_types = ["trending", "sideways"]

    async def analyze(self, df: pd.DataFrame, ticker: Ticker) -> Signal:
        return Signal(
            strategy_name=self.name,
            signal_type=SignalType.BUY,
            confidence=0.7,
            reason="설명",
        )
```

### DB
- ORM: SQLAlchemy 2.0 async (core/models.py).
- 세션: `async with get_session() as session:` 패턴.
- 마이그레이션: `db/migrate.py` (수동) + Alembic.
- 새 컬럼 추가 시 `migrate.py`에 `add_column_if_not_exists()` 추가.

### Exchange Adapter
- `ExchangeAdapter` ABC 상속, 선물 메서드는 Optional.
- API 호출: `_call()` 래퍼 (rate limit + 에러 변환).
- 에러: `core/exceptions.py`의 커스텀 예외로 변환.

### API
- 모든 엔드포인트에 `exchange` 쿼리 파라미터.
- EngineRegistry에서 엔진/PM/콤바이너 조회.
- Pydantic response schema (core/schemas.py).

---

## 4. Workflow

### 일반 버그 수정
1. 버그 재현 테스트 작성
2. 코드 수정
3. 테스트 통과 확인 (전체)
4. 커밋 (`fix: 설명`)
5. PROGRESS.md + MEMORY.md 업데이트 (해당 시)
6. 서버 재시작 + 엔진 start

### 전략 변경
1. `backtest-analysis.md` 기존 이력 확인
2. 백테스트 (540일) 실행 + 결과 기록
3. PF/Alpha/MDD 검증
4. 코드 수정 + 테스트 추가
5. 커밋 + 문서 업데이트 + 배포

### 새 기능 추가
1. 기능 구현
2. 테스트 작성 (정상/에러/경계값)
3. 전체 테스트 통과 확인
4. 커밋 + 문서 업데이트 + 배포

---

## 5. Environment

### Dependencies
```bash
cd backend
.venv/bin/pip install -r requirements.txt

cd frontend
npm install
```

### 환경 변수 (.env)
- `TRADING_MODE`: paper / live (빗썸)
- `BINANCE_ENABLED`: true / false
- `BINANCE_TRADING_MODE`: paper / live (선물, 독립)
- `BINANCE_SPOT_ENABLED`: true / false
- `BINANCE_SPOT_TRADING_MODE`: paper / live (현물, 독립)
- `DB_URL`: PostgreSQL 연결 문자열
- `NOTIFY_ENABLED`: true → Discord 알림 활성화
- `NOTIFY_DISCORD_WEBHOOK_URL`: Discord webhook URL
