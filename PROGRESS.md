# 코인 자동 매매 시스템 — 운영 참조

> 최종 업데이트: 2026-03-19
> 완료된 Phase 1-5 상세 및 버전 이력은 `CHANGELOG.md` 참고.

---

## 개요

빗썸(현물, 비활성) + 바이낸스 현물(live) + 바이낸스 USDM 선물(live, 3x) + 서지 **쿼드 엔진** 24시간 자동 트레이딩 시스템.
가중 투표 (HOLD=기권) + ML 시그널 필터 + 5요소 시장 감지 + 적응형 가중치, AI 에이전트 5종, Discord 봇(자연어 제어), React 대시보드(8탭).
**현물 4전략** (BNF이격도, CIS모멘텀, 래리윌리엄스, 돈치안채널) + **선물 7전략** (MA, RSI, MACD, 볼린저RSI, 스토캐스틱RSI, OBV, BB스퀴즈).
**자기 치유 엔진** (에러 분류 → 자동 복구 → LLM 진단), **1366 유닛 테스트**.

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
│   └── tests/         (1366 tests)
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
| MarketAnalyzer 동점 편향 수정 | `_classify_market()` 동점 시 dict 순서 대신 현재가 vs SMA20 타이브레이크 (COIN-30) |
| 현물 Optuna 바이낸스 재최적화 (v0.39) | 빗썸 KRW→바이낸스 USDT 데이터로 재최적화 (PF 1.56, +22.48%), cis_momentum 지배적, SL5/TP14/trail3-1.5, cd15(60h) |
| 트레일링 스탑 알림 반복 버그 수정 | `_execute_stop_sell`: 미체결 주문 시 `return` → `raise RuntimeError` — cooldown pop 오동작 방지, 30초마다 알림 폭주 해결 |
| 서지 엔진 거래량 감지 수정 (v0.40) | ticker 24h volume→5m 캔들 OHLCV 기반, 배치 ticker(USDM 키 정규화), 좀비 포지션 자동 청산 |
| 현물 좀비 포지션 감지 버그 수정 | `sync_exchange_positions`: dust 잔고(가치 미만)가 exchange_symbols에 포함돼 좀비 탐지가 누락되던 버그 수정. exchange_symbols를 dust 필터 통과 심볼만 수집하도록 변경 |
| 선물 Optuna 최적화 도구 (v0.40) | optimize.py --futures 지원, 백테스트 적응형 가중치 bb_squeeze 누락 수정 |
| v2 백테스터 + 레짐 전략 개선 | backtest_v2.py: Walk-Forward 검증, 레짐 적응형 선물 엔진, 쿨다운/신뢰도/레짐 필터/평가 주기 |
| v2 레짐 전략 최적화 (2026-03-14) | MR: 1h RSI 반전 필수(88.5% WR), VB: KC 2.2/VOL 2.0/RSI필수(PF 1.54), TF: 상승 진입 비활성(하락만 PF 3.27). Tier1 SL/TP 버그 수정. 540d: **PF 2.17, WR 63.4%, MDD 5.42%**, WF 4/4 PASS |
| 에이전트 탭 개선 (2026-03-15) | 매매 회고 `analyzed_at` 타임스탬프 추가. 성과 분석/전략 어드바이저 바이낸스 스케줄 누락 수정 (선물·현물 각각 21:30 KST 매일, 일요일 22:00 KST 자동 실행). 프론트엔드 타임스탬프 표시 (KST). |

| 프론트엔드 탭 수정 (COIN-7) | `FuturesEngineV2`에 `strategies` + `rotation_status` 프로퍼티 추가 → 전략 성과/종목·로테이션 탭 500 에러 해결. `list_strategies` getattr 폴백. `StrategyPerformance.tsx` bb_squeeze + v2 전략 이름 추가. `RotationMonitor.tsx` v2 레짐 상태 레이블 추가 |
| 신호 로그 최종 판단 (COIN-11) | `OrderLog.tsx`: 코인 헤더 우측에 가중 투표 기반 최종 판단 배지 추가 (BUY/SELL/HOLD + confidence %). `computeCombinedSignal()` 함수가 백엔드 SignalCombiner 로직을 미러링 (HOLD=기권, active_weight<0.12→HOLD, confidence<threshold→HOLD). 백엔드 계약 검증 테스트 6개 추가. |
| 전략 신호 로그 중복 표시 수정 (COIN-12) | `OrderLog.tsx`: 신호 로그 그룹핑을 `symbol` → `symbol + 1분 시간 버킷`으로 변경. 평가 사이클마다 7전략 로그가 DB에 쌓이면서 size=30 응답에 여러 사이클이 섞여 각 전략이 N번씩 표시되던 버그 수정. |
| v2 Tier2Scanner 레버리지 버그 수정 (COIN-13) | SafeOrderPipeline에 주문 전 set_leverage() 호출 추가 (Tier2 30코인 거래소 기본 레버리지 방지). Tier2 SL/TP에 레버리지 승수 적용 (pnl_pct *= leverage). FuturesEngineV2에 health_monitor 호환 속성 추가 (_eval_error_counts, _position_trackers, pause_buying, resume_buying). |
| 포지션 종료 사유 추적 (COIN-14) | `sync_exchange_positions`에서 사라진 포지션의 실제 청산 사유 판별. `_determine_close_reason()` 메서드 추가: Income API INSURANCE_CLEAR→강제청산, DB SL/TP 수준 비교→SL/TP/trailing stop, max_hold_hours→시간초과, PnL<-80%→강제청산(추정), 폴백→position_sync. strategy_name을 `stop_loss`/`take_profit`/`trailing_stop`/`forced_liquidation`/`time_expiry`/`position_sync`으로 세분화. |
| V2 Tier1 평가 사이클 관측성 (COIN-17) | `evaluation_cycle()` 완료 시 `tier1_cycle_complete` info 로그 추가 (coins_evaluated, hold/low_conf/cooldown/sl_tp/executed 카운트, regime, elapsed_ms). `CycleStats` 데이터클래스로 사이클 결과 구조화. `Tier1Manager.get_status()` 메서드 + `GET /api/v1/engine/v2/tier1-status` 엔드포인트 추가 (cycle_count, last_cycle_at, last_action_at, last_decisions, active_positions, regime). |
| BalanceGuard 자동 복구 (COIN-15) | 일시 정지 후 자동 복구 메커니즘 추가. `check_balance()`에서 divergence < warn_pct 3회 연속 안정 시 자동 `resume()`. `POST /api/v1/engine/balance-guard/resume` 수동 재개 엔드포인트 + `GET /api/v1/engine/balance-guard/status` 상태 조회 엔드포인트 추가. `get_status()`에 balance_guard 상세 정보 포함. |
| BalanceGuard 선물 잔고 계산 수정 (COIN-19) | `_fetch_exchange_balance()` 선물 잔고 계산을 USDT.free → wallet(total-unrealizedPnL)-totalMargin으로 수정. 내부 장부 자동 재동기화 메커니즘 추가 (5회 연속 critical → resync_callback → 자동 재개). FuturesEngineV2에 resync 콜백 연결. 테스트 26개 추가. |
| 선물 position_sync 잔고 불일치 수정 (COIN-18) | `sync_exchange_positions`에서 선물 포지션 청산 시 `invested + pnl_amount`을 `cash_balance`에 반환. 강제청산 시 `max(0)` 보호. `_realized_pnl`도 동기화. 거래소 SL/TP/트레일링으로 청산된 포지션의 마진이 내부 장부에 미반영되던 버그 해결. |
| V2 스냅샷·전략 로그 (COIN-21) | `_persist_loop`에 `take_snapshot()` 호출 추가 (5분 간격, daily_pnl 계산용). `Tier1Manager._evaluate_coin()`에 StrategyLog 기록 추가 (HOLD 포함 모든 판단, 레짐 정보 포함, was_executed 구분). 프론트엔드 선물 성과 차트/테이블 데이터 공백 해결. 테스트 17건 추가. |
| 서지 엔진 진입 필터 강화 (COIN-20) | 라이브 승률 38% 개선: min_score 0.40→0.55, RSI overbought 85→75/oversold 15→25, ATR 변동성 필터(min_atr_pct=0.5%), 연속 SL 쿨다운(2+연속→180분 차단), SL 2→2.5%/TP 4→3%/trailing 1→0.5%. 백테스트 CLI 파라미터 추가. 테스트 31개 추가(1178 total). |
| 서지 엔진 종료 후 쿨다운 미적용 수정 (COIN-22) | `_exit_position()`에서 포지션 삭제 후 `self._cooldowns[symbol]` 미설정 → 즉시 재진입 가능 버그 수정. TP/SL/Trailing/TimeExpiry 모든 종료에서 60분 쿨다운 적용. COIN-20 장기 쿨다운(180분) 보호. 테스트 7개 추가. |
| Tier2Scanner 진입 필터 + SL/TP 조정 (COIN-23) | surge_backtest 안전 필터 포팅: RSI 필터(75/25), ATR% 횡보 차단(0.5%), 가속도 25% 가중치, 소진 필터(8%+), 연속 SL 쿨다운(2연속→180분). 정규화 점수(vol*0.40+price*0.35+accel*0.25, min_score 0.55). SL 2→3.5%, TP 4→4.5%, trail 1.0/0.8→1.5/1.0, concurrent 5→3, cooldown 30→60분. 테스트 41개 추가(1245 total). |
| 현물 매수 DB 수량 불일치 수정 (COIN-24) | 매수 시 요청 수량 대신 `order.executed_quantity`/`executed_price`로 DB 포지션 갱신. 매도 시 `_clamp_sell_qty_to_balance()` 방어 로직 추가 (실잔고 < DB qty 시 실잔고 기준 매도). PositionTracker도 체결 가격으로 생성. 테스트 14개 추가(1259 total). |
| Tier1Manager 듀얼 이밸류에이터 (COIN-25) | `DirectionEvaluator` 프로토콜 + `DirectionDecision` 데이터클래스 신규. `RegimeLongEvaluator`/`RegimeShortEvaluator`가 StrategySelector를 방향별로 래핑. Tier1Manager가 `long_evaluator`/`short_evaluator` 주입받아 독립 평가. SAR 로직 제거, 충돌 시 confidence 높은 쪽 선택. 테스트 47개 추가(1298 total). |
| SpotLongEvaluator 현물 4전략 롱 경로 (COIN-26) | 현물 4전략(cis_momentum, bnf_deviation, donchian_channel, larry_williams)을 선물 롱 시그널 소스로 사용하는 `SpotLongEvaluator` 구현. 4h 캔들 기반, SignalCombiner(SPOT_WEIGHTS)로 가중 투표. BUY→롱 진입, SELL→롱 청산. FuturesEngineV2에서 long_evaluator로 주입. 파라미터: min_confidence 0.50, cooldown 60h, SL 5%/TP 14%/trail 3-1.5, eval 300s. FuturesV2Config에 tier1_long_* 필드 추가. 테스트 39개 추가(1366 total). |
| SAR + 방향별 쿨다운 (COIN-27) | Tier1Manager에 SAR(Stop And Reverse) 로직 추가: LONG 보유 중 short open → close LONG + open SHORT (역방향도 동일). SAR은 쿨다운 면제. 방향별 쿨다운: 롱 SL/TP → 12h 롱 재진입 금지, 숏 SL/TP → 26h 숏 재진입 금지 (반대 방향은 허용). FuturesV2Config에 tier1_sl_long/short_cooldown_hours 추가. FuturesEngineV2가 방향별 쿨다운을 Tier1Manager에 전달. 테스트 28개 추가(1405 total). |
| SpotEvaluator 양방향 확장 (COIN-28) | `SpotLongEvaluator` → `SpotEvaluator`로 리네이밍 및 양방향 지원. SELL 시그널+포지션 없음→SHORT 진입(spot_sell_short), SHORT 보유+BUY→숏 청산(spot_buy_close_short). 방향별 독립 쿨다운(_long_cooldowns, _short_cooldowns). SL/TP/trail 숏에도 동일 적용(5/14/3-1.5 ATR). FuturesEngineV2에서 SpotEvaluator 하나로 long+short evaluator 모두 담당. RegimeShortEvaluator는 유지(향후 재활용). backward compat alias(spot_long_evaluator.py). 테스트 21개 추가(1430 total). |
| SpotEvaluator 동일 인스턴스 중복 evaluate() 최적화 (COIN-29) | 포지션 보유 시 SAR 분기에서 동일 인스턴스(`long_evaluator is short_evaluator`) 감지 → SAR evaluate() 호출 스킵 (close 미달 시그널로 open도 불가). 포지션 없을 때 hold 로깅 중복 제거 (1회만 기록). 각 evaluate() = 4h 캔들+ticker+4전략 API 호출이므로 포지션 보유 시 ~50% API 절감. 다른 인스턴스(RegimeShortEvaluator) SAR 경로는 유지. 테스트 10개 추가(1440 total). |
| BTC 선물 최소 notional 보장 (COIN-31) | 바이낸스 USDM 최소 notional $100 미달로 BTC 주문 실패하던 버그 수정. 2중 방어: (1) Tier1Manager._calc_margin에 MIN_NOTIONAL/leverage 마진 하한 추가, (2) SafeOrderPipeline에 수량 precision 절삭 후 올림 보정 + notional 검증 추가. FuturesEngine V1 _get_min_notional 폴백 5.0→100.0 수정. 테스트 13개 추가(1453 total). |
| 프론트엔드 선물 V2 전략 표시 업데이트 (COIN-32) | STRATEGY_KR에 `spot_eval`(현물 시그널), `tier2_surge`(서지 스캐너) 추가. STRATEGY_COLORS에 V2 전략 6종 색상 추가(bb_squeeze, trend_follower, mean_reversion, vol_breakout, spot_eval, tier2_surge). TradeHistory 필터 드롭다운에 V2 전략 7종 추가. FuturesEngineV2.strategies 프로퍼티가 SpotEvaluator 현물 4전략을 포함하도록 수정 (주문 strategy_name과 일치 → /strategies/comparison 정확한 성과 데이터 반환). 테스트 2개 순증(1471 total). |
| 선물 신호 로그 사이클 그룹핑 + 비활성 전략 정리 (COIN-34) | FuturesEngineV2.strategies에서 비활성 V2 레짐 전략(trend_follower, mean_reversion, vol_breakout) 제거 → 활성 4전략만 노출. 새 API `GET /strategies/logs/grouped`: 평가 사이클 단위(1분 버킷+symbol)로 신호 로그 그룹핑, combined signal/confidence/개별 전략 판단 포함. 프론트엔드: TradeHistory 드롭다운 비활성 전략 제거, StrategyPerformance STRATEGY_KR 정리, OrderLog 사이클 그룹 카드 UI 개선(색상 보더, 전략 수 표시). 테스트 23개 추가(1494 total). |
| 전략 성과 FIFO→realized_pnl 전환 (COIN-35) | `GET /strategies/{name}/performance` FIFO 로트 매칭을 `realized_pnl` 기반 계산으로 교체. V1→V2 전환 시 고아 로트가 FIFO 큐를 오염하여 V2 전략 승/패가 누락되던 버그 수정. 청산 주문의 `realized_pnl`을 직접 사용하여 승/패 판정 → 고아 로트 문제 근본 해결. 테스트 5개 순증(1500 total). |
| 서지 엔진 양방향(숏) 활성화 (COIN-36) | `SurgeTradingConfig.long_only` 기본값 `True`→`False` 변경. 180일 백테스트 검증: 양방향 SL2.5% PnL +366%(롱온리 +215% 대비 +70%), MDD 1.6% 허용범위. 숏 진입/청산/트레일링 인프라 기존 완비, config 토글만 변경. 숏 진입/청산 유닛 테스트 + 양방향 동시 포지션 테스트 17개 추가(1517 total). |
| V2 ML Signal Filter 적용 (COIN-40) | V1에서 손실 거래 40-50% 차단하던 ML 시그널 필터가 V2 Tier1Manager에 미적용되던 버그 수정. Tier1Manager에 `_check_ml_filter()` 게이트 추가 (신규 진입+SAR만 필터링, 청산은 허용). SpotEvaluator가 open 결정에 signals+candle_row 전달. FuturesEngineV2에서 signal_filter.pkl 로드(min_win_prob 0.52). CycleStats에 ml_filtered_count 추가. 테스트 16개 추가(1563 total). |
| V2 리스크 관리 5종 포팅 (COIN-42) | V1 TradingEngine의 리스크 관리 기능 5종을 V2 Tier1Manager에 포팅. (1) 비대칭 모드: TRENDING_DOWN/VOLATILE(bearish) 시 신규 롱 차단. (2) 동적 SL: 레짐별 ATR mult 스케일링(floor/cap 포함). (3) ATR 레버리지 스케일링: 6단계 ATR%→레버리지 매핑(20%→1x~3%→5x). (4) 레짐별 포지션 사이징: TRENDING_DOWN→50%, VOLATILE→60%, RANGING→80%. (5) MIN_SELL_ACTIVE_WEIGHT: SignalCombiner에 숏 최소 참여 가중치 전달. FuturesV2Config에 4개 설정 추가. 테스트 35개 추가(1655 total). |

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
