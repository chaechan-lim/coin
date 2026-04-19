from __future__ import annotations

from dataclasses import dataclass


RESEARCH_STAGES: tuple[str, ...] = (
    "research",
    "candidate",
    "shadow",
    "live_rnd",
    "production",
    "hold",
)

EXECUTION_ALLOWED_STAGES: frozenset[str] = frozenset({"live_rnd", "production"})
SIGNAL_ONLY_STAGES: frozenset[str] = frozenset({"shadow"})


@dataclass(frozen=True)
class StageRule:
    stage: str
    description: str
    promotion_criteria: tuple[str, ...]
    demotion_criteria: tuple[str, ...]
    next_stages: tuple[str, ...]


@dataclass(frozen=True)
class ResearchCandidate:
    key: str
    title: str
    market: str
    directionality: str
    stage: str
    venue: str
    stage_managed: bool
    status: str
    objective: str
    rationale: str
    recommended_next_step: str


STAGE_RULES: tuple[StageRule, ...] = (
    StageRule(
        stage="research",
        description="아이디어 검증 단계. 백테스트/구조 타당성 확인 전.",
        promotion_criteria=(
            "비용 반영 백테스트 수익률이 0 초과",
            "Sharpe, MDD, 거래 수가 최소 기준 충족",
            "최근 구간과 장기 구간 모두 완전히 붕괴하지 않음",
            "파라미터 민감도가 과도하지 않음",
        ),
        demotion_criteria=(
            "핵심 가설이 백테스트에서 재현되지 않음",
            "거래 수 부족으로 통계 해석 불가",
            "비용 반영 후 edge 소멸",
        ),
        next_stages=("candidate", "hold"),
    ),
    StageRule(
        stage="candidate",
        description="라이브 적용 후보. OOS와 운영 설계 검토 중.",
        promotion_criteria=(
            "walk-forward 또는 OOS 검증 통과",
            "체제별 성과 분해가 가능",
            "리스크 룰과 충돌 방지 설계 완료",
            "기존 포트폴리오와 분산 효과 확인",
        ),
        demotion_criteria=(
            "walk-forward 재현 실패",
            "체제 전환 시 성과 붕괴",
            "운영 복잡도 대비 기대효과 부족",
        ),
        next_stages=("shadow", "hold"),
    ),
    StageRule(
        stage="shadow",
        description="실거래 미집행 상태로 라이브 시그널 추적.",
        promotion_criteria=(
            "shadow 성과가 백테스트와 크게 괴리 나지 않음",
            "실시간 시그널/포지션 관리 로직 정상 동작",
            "슬리피지와 수수료 반영 후 기대수익 유지",
        ),
        demotion_criteria=(
            "실시간 신호 품질이 백테스트와 구조적으로 다름",
            "오주문/중복진입/포지션 충돌 발생",
            "슬리피지 반영 후 edge 소멸",
        ),
        next_stages=("live_rnd", "candidate", "hold"),
    ),
    StageRule(
        stage="live_rnd",
        description="소액 실거래 R&D 단계.",
        promotion_criteria=(
            "일정 기간 실거래 데이터에서 기대 범위 내 성과 확인",
            "자동 중지/리스크 한도 포함 운영 안정성 확인",
            "중단 사유 없이 반복 실행 가능",
        ),
        demotion_criteria=(
            "live MDD 또는 손실 한도 초과",
            "최근 실거래 성과가 shadow 기대치 대비 과도하게 저조",
            "운영 장애 또는 체결 품질 문제 발생",
        ),
        next_stages=("production", "shadow", "hold"),
    ),
    StageRule(
        stage="production",
        description="정식 운영 단계.",
        promotion_criteria=(),
        demotion_criteria=(
            "성과 열화가 구조적으로 확인됨",
            "리스크 기준 초과",
            "더 우수한 대안 전략으로 대체 필요",
        ),
        next_stages=("live_rnd", "hold"),
    ),
    StageRule(
        stage="hold",
        description="보류 단계. 재설계 또는 재검증 전까지 비활성.",
        promotion_criteria=(
            "가설 수정 또는 시장 체제 변화로 재검토 필요",
            "새 파라미터/새 구조에서 edge 재확인",
        ),
        demotion_criteria=(),
        next_stages=("research",),
    ),
)


RESEARCH_CANDIDATES: tuple[ResearchCandidate, ...] = (
    # ── 선물 R&D (라이브 가동 중) ──
    ResearchCandidate(
        key="donchian_futures_bi",
        title="Donchian Futures Bi-Directional",
        market="futures",
        directionality="long_short",
        stage="live_rnd",
        venue="binance_donchian_futures",
        stage_managed=True,
        status="running",
        objective="일봉 Donchian 채널 돌파로 장단기 추세 모두 포착",
        rationale="앙상블 채널(10/20/40/55/90) 양방향 진입, 300 USDT 자본",
        recommended_next_step="1개월 실거래 데이터 축적 후 채널 조합별 기여도 분석",
    ),
    ResearchCandidate(
        key="pairs_trading_futures",
        title="Pairs Trading",
        market="futures",
        directionality="long_short",
        stage="live_rnd",
        venue="binance_pairs",
        stage_managed=True,
        status="running",
        objective="BTC-ETH z-score 기반 델타 중립 알파 확보",
        rationale="시장 방향과 무관한 스프레드 수익, 300 USDT 자본, 2x 레버리지",
        recommended_next_step="z-score 진입/청산 임계치 민감도 분석, 스프레드 안정성 모니터링",
    ),
    ResearchCandidate(
        key="momentum_rotation",
        title="Momentum Rotation",
        market="futures",
        directionality="long_short",
        stage="live_rnd",
        venue="binance_momentum",
        stage_managed=True,
        status="running",
        objective="주간 상대강약 기반 상위/하위 코인 롱숏 로테이션",
        rationale="24 알트코인 대상, 주간 리밸런싱, SL 8%/trailing 4%-2%, 400 USDT",
        recommended_next_step="리밸런싱 주기별 성과 비교, 종목 수 최적화",
    ),
    ResearchCandidate(
        key="hmm_regime",
        title="HMM Regime",
        market="futures",
        directionality="adaptive",
        stage="live_rnd",
        venue="binance_hmm",
        stage_managed=True,
        status="running",
        objective="4h HMM 3-state 체제전환 기반 BTC 롱/숏/플랫",
        rationale="70% 상태 확률 필터, 90일 학습, 300 USDT, 2x 레버리지",
        recommended_next_step="체제 전환 빈도와 각 상태별 수익 기여도 분석",
    ),
    ResearchCandidate(
        key="breakout_pullback",
        title="Breakout-Pullback",
        market="futures",
        directionality="long_short",
        stage="live_rnd",
        venue="binance_breakout_pb",
        stage_managed=True,
        status="running",
        objective="Donchian 돌파 후 풀백 진입으로 유리한 타점 확보",
        rationale="20일 채널 돌파 + 4% 풀백 대기, SL 5%/TP 8%, 400 USDT",
        recommended_next_step="풀백 비율/채널 길이 최적화, 진입 빈도 모니터링",
    ),
    ResearchCandidate(
        key="volume_momentum",
        title="Volume Momentum",
        market="futures",
        directionality="long_short",
        stage="live_rnd",
        venue="binance_vol_mom",
        stage_managed=True,
        status="running",
        objective="거래량 급등 + 모멘텀 동반 시 단기 진입",
        rationale="시간봉 평가, vol 2x 스파이크 + RSI 필터 + ATR 기반 SL/TP, 200 USDT",
        recommended_next_step="거래량 배수 임계치와 보유 시간 최적화",
    ),
    ResearchCandidate(
        key="btc_neutral_mr",
        title="BTC-neutral Mean Reversion",
        market="futures",
        directionality="market_neutral",
        stage="live_rnd",
        venue="binance_btc_neutral",
        stage_managed=True,
        status="running",
        objective="알트 vs BTC z-score 이탈 시 델타 중립 회귀 거래",
        rationale="일봉 평가, 최대 3쌍 동시 진입, 200 USDT",
        recommended_next_step="z-score 임계치, 보유 기간 한도 최적화",
    ),
    # ── 비활성 / 보류 ──
    ResearchCandidate(
        key="donchian_daily_spot",
        title="Donchian Daily Ensemble (Spot)",
        market="spot",
        directionality="long_only",
        stage="hold",
        venue="binance_donchian",
        stage_managed=True,
        status="hold",
        objective="현물 일봉 Donchian 앙상블 추세 추종",
        rationale="현물 전략 전체 알파 부재 확인 → 자본 선물 이전, 엔진 비활성",
        recommended_next_step="선물 전략에 집중, 현물 재개 시 재검토",
    ),
    ResearchCandidate(
        key="fear_greed_dca",
        title="Fear & Greed DCA (Spot)",
        market="spot",
        directionality="long_only",
        stage="hold",
        venue="binance_fgdca",
        stage_managed=True,
        status="hold",
        objective="RSI+30일 변동 기반 공포/탐욕 분할매수",
        rationale="현물 전략 전체 알파 부재 확인 → 자본 선물 이전, 엔진 비활성",
        recommended_next_step="선물 전략에 집중, 현물 재개 시 재검토",
    ),
    ResearchCandidate(
        key="dual_momentum_spot",
        title="Dual Momentum",
        market="spot",
        directionality="long_or_cash",
        stage="hold",
        venue="binance_spot",
        stage_managed=False,
        status="hold",
        objective="강한 코인만 보유하고 약세장에는 현금 회피",
        rationale="최근 전 조합 손실, 현물 알파 부재 → 보류",
        recommended_next_step="선물 short 확장판 또는 체제 전환형 버전으로 재설계",
    ),
    ResearchCandidate(
        key="funding_arb",
        title="Funding Arbitrage",
        market="cross_market",
        directionality="market_neutral",
        stage="hold",
        venue="binance_spot+futures",
        stage_managed=False,
        status="hold",
        objective="방향 노출 없이 펀딩/베이시스 수익 확보",
        rationale="백테스트에서 수수료 반영 시 edge 불충분, VIP 등급 없이는 비현실적",
        recommended_next_step="VIP 수수료 달성 후 재검토",
    ),
)

RESEARCH_CANDIDATE_BY_KEY: dict[str, ResearchCandidate] = {
    candidate.key: candidate for candidate in RESEARCH_CANDIDATES
}
RESEARCH_CANDIDATE_BY_VENUE: dict[str, ResearchCandidate] = {
    candidate.venue: candidate
    for candidate in RESEARCH_CANDIDATES
    if candidate.venue and candidate.stage_managed
}


def get_stage_rule(stage: str) -> StageRule:
    for rule in STAGE_RULES:
        if rule.stage == stage:
            return rule
    raise KeyError(f"Unknown research stage: {stage}")


def get_candidate(candidate_key: str) -> ResearchCandidate:
    try:
        return RESEARCH_CANDIDATE_BY_KEY[candidate_key]
    except KeyError as exc:
        raise KeyError(f"Unknown research candidate: {candidate_key}") from exc


def get_candidate_by_venue(venue: str) -> ResearchCandidate | None:
    return RESEARCH_CANDIDATE_BY_VENUE.get(venue)


def is_execution_allowed_stage(stage: str) -> bool:
    return stage in EXECUTION_ALLOWED_STAGES


def is_signal_only_stage(stage: str) -> bool:
    return stage in SIGNAL_ONLY_STAGES
