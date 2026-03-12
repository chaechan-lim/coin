# Changelog

> Phase 1-5 완료 항목 및 버전별 이력. 현재 운영 참조는 PROGRESS.md 참고.

---

## Phase 1 — 인프라 기반 (완료)
- 프로젝트 스캐폴딩, 패키지 의존성, 설정 시스템 (Pydantic Settings)
- Docker 구성, 환경 변수, DB ORM 모델 (8개 테이블), DB 세션 (async SQLAlchemy + WAL)

## Phase 2 — 거래소 어댑터 + 시장 데이터 (완료)
- 거래소 추상 인터페이스, 빗썸 어댑터 (ccxt), 페이퍼 트레이딩 어댑터
- 시장 데이터 서비스 (OHLCV + 지표)

## Phase 3 — 전략 엔진 + AI 에이전트 (완료)
- 11전략 구현: 현물 4 (BNF이격도, CIS모멘텀, 래리윌리엄스, 돈치안채널) + 선물 7 (MA, RSI, MACD, Boll+RSI, StochRSI, OBV, BB Squeeze) + 독립 2 (Grid, DCA)
- Signal Combiner (가중 투표, HOLD=기권), 주문/포트폴리오 관리자, 트레이딩 엔진
- AI 에이전트: 시장 분석, 리스크 관리, 거래 리뷰, 에이전트 조율자

## Phase 4 — REST API + React 대시보드 (완료)
- REST API 전체 (포트폴리오, 거래, 전략, 엔진, 에이전트, 이벤트)
- WebSocket 실시간 스트림, React 18 대시보드 (8탭, 모바일 반응형)
- APScheduler, 알림 서비스, Alembic, Docker

## Phase 5 — 안정화 (완료)
- 구조화 로깅 (structlog), 에러 핸들링, 멀티 알림 (Telegram/Discord/Slack)
- PositionTracker DB 영속화, 가짜 스파이크 6겹 방어, 교차 거래소 충돌 차단
- 쿼드 엔진 (빗썸+바이낸스현물+바이낸스선물+서지), WebSocket 실시간 동기화
- P1 최적화, 현물 4h 전환, 현물 4전략 전환, 비대칭 전략
- 자기 치유 엔진 (ErrorClassifier → RecoveryManager → DiagnosticAgent)
- EngineConfig 거래소 추상화, Order PnL 기록
- 숏 가중치 최적화: ADAPTIVE_PROFILES downtrend/crash에서 MA+MACD 가중치 상향
- 백테스트 개선: 동적 포트폴리오, 롱/숏 PnL 분리, 선물 4h 자동보정
- 선물 cash 스파이크 근본 수정 (notional→margin+PnL 정산)
- ML Signal Filter (LightGBM), 서지 엔진, Discord 봇 (자연어 → tool_use)
- AI 에이전트 5종: 시장분석, 리스크관리, 거래리뷰, 성과분석, 전략조언
- 773 유닛 테스트

---

## Version History

| 버전 | 날짜 | 내용 |
|---|---|---|
| v0.1 | 2026-02-24 | 초기 구현: 백엔드 전체 + API + React 대시보드 |
| v0.2 | 2026-02-24 | 라이브 전환: Bithumb V2, SL/TP/trailing, 거래량 로테이션 |
| v0.3 | 2026-02-24 | 백테스트-라이브 패리티, crash→downtrend 통합, UTC→KST |
| v0.4 | 2026-02-25 | 서지 임계값, 추적코인 축소, 로테이션 모니터 |
| v0.5 | 2026-02-25 | 서버 이벤트 로그 시스템 (DB + API + WS + 시스템 로그 탭) |
| v0.6 | 2026-02-25 | 8전략 combiner 개편 (HOLD=기권), 5요소 시장 감지 |
| v0.7 | 2026-02-25 | 수수료 추적, 주문 fill 폴링, 서지 매수 현금 방식 |
| v0.8 | 2026-02-25 | 서지 매도 프로필 C, 낙폭 UI 개선 |
| v0.9 | 2026-02-25 | 리스크 에이전트 수정, 시장 상태 동기화 |
| v0.10 | 2026-02-25 | 원금 대비 수익, max_trade_size 0.50 |
| v0.11 | 2026-02-26 | 모바일 반응형 UI, 전략 성과 FIFO, 30 tests |
| v0.12 | 2026-02-26 | 전략 개선: 8→6전략, min_confidence 0.50, 동적 로테이션 |
| v0.13 | 2026-02-26 | 듀얼 엔진: 바이낸스 선물 통합, EngineRegistry |
| v0.14 | 2026-02-26 | WebSocket 실시간 가격 모니터, P1 최적화 (PF 1.80) |
| v0.15 | 2026-02-27 | 시스템 로그 강화, LLM 매매 회고 |
| v0.16 | 2026-02-28 | 포트폴리오 상태 복원, 숏 P&L 수정, Crash 숏 진입 |
| v0.17 | 2026-02-28 | 입출금 추적, 동적 원금, HTTPS, 비대칭 전략, Discord 알림 |
| v0.18 | 2026-03-01 | 수동 매도 정리, 포지션 동기화 |
| v0.19 | 2026-03-02 | PositionTracker DB 영속화, 가짜 스파이크 방지, 교차 충돌 차단 |
| v0.21 | 2026-03-02 | 스파이크 3겹 방어, 현물 4h 전환 |
| v0.22 | 2026-03-03 | 바이낸스 현물 연동, 선물 알림 수정 |
| v0.23 | 2026-03-03 | 현물 4전략 전환 (PF 1.03→1.63), 거래소별 전략 분기 |
| v0.24 | 2026-03-03 | 역추세 매수 가드 (PF 1.10→1.22) |
| v0.25 | 2026-03-04 | 스파이크 6-Layer 방어 강화 |
| v0.26 | 2026-03-04 | 일일 손익 + 매매 안정성 + 에러 가시성 |
| v0.27 | 2026-03-04 | 실시간 동기화 강화 (WebSocket 잔고/포지션, 현물 30초 SL) |
| v0.28 | 2026-03-04 | 매수 실행 안정성 (최소 주문금액, cash 사전검증) |
| v0.29 | 2026-03-05 | 자기 치유 엔진 (에러 분류 → 자동 복구 → LLM 진단) |
| v0.30 | 2026-03-05 | 크리티컬 버그 수정 (현물 SL 무력화, 시장감지 하드코딩) |
| v0.31 | 2026-03-06 | EngineConfig 거래소 추상화, Order PnL 기록 |
| v0.32 | 2026-03-08 | 숏 가중치 최적화 (ADAPTIVE_PROFILES), 동적 포트폴리오 백테스트, 롱/숏 PnL 표시 |
| v0.33 | 2026-03-08 | 성과 분석 에이전트 + 전략 어드바이저 에이전트, 일일 통계 버그 수정, 571 테스트 |
| v0.34 | 2026-03-08 | Discord 봇 (자연어 → Claude tool_use → 시스템 제어), 600 테스트 |
| v0.35 | 2026-03-09 | 시스템 안정성 12개 개선 (API 타임아웃, 서킷브레이커, 에러 추적, graceful shutdown), Discord 봇 도구 18개, 포지션 상세 모달, 675 테스트 |
| v0.36 | 2026-03-09 | systemd 안정성 (RestartSec=20, pg_isready 대기), PostgreSQL 제한, 프론트엔드 조건부 빌드, delisted 심볼 필터 |
| v0.37 | 2026-03-10 | 선물 구조 최적화 (2x→3x ML, cd72→cd48→cd6), ML Signal Filter (LightGBM 23피처), 서지 엔진, 교차 거래소 포지션 전환, MIN_SELL_ACTIVE_WEIGHT, 페어링 매도, 현물 Optuna 가중치, 766 테스트 |
| v0.38 | 2026-03-12 | 선물 7전략 (bb_squeeze 추가), 쿨다운 cd6(24h), 버그 수정 11건 (entry_price=0 가드, cash race condition, DB 인덱스, API 검증, fire-and-forget 에러 핸들링), 773 테스트 |
