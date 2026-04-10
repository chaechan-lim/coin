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
    ResearchCandidate(
        key="donchian_daily_spot",
        title="Donchian Daily Ensemble",
        market="spot",
        directionality="long_only",
        stage="live_rnd",
        venue="binance_donchian",
        stage_managed=True,
        status="running",
        objective="약세장에서 무리한 진입을 피하면서 추세 복귀를 포착",
        rationale="일봉 돌파 기반이라 거래 빈도가 낮고, 현재 시스템에 가장 안전하게 병렬 운영 가능",
        recommended_next_step="실거래 데이터 축적 후 월 단위 성과/진입 빈도 점검",
    ),
    ResearchCandidate(
        key="pairs_trading_futures",
        title="Pairs Trading",
        market="futures",
        directionality="long_short",
        stage="candidate",
        venue="binance_pairs",
        stage_managed=True,
        status="candidate",
        objective="시장 방향성과 무관한 델타 중립 알파 확보",
        rationale="최근 180일 수익성은 확인됐지만 walk-forward 승률이 낮아 체제 필터 보강이 필요",
        recommended_next_step="walk-forward 범위를 넓히고 레짐 필터를 결합해 라이브 조건 재검증",
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
        rationale="최근 단기 구간에서는 전 조합이 손실이라 현 시점 시스템 개선 우선순위가 낮음",
        recommended_next_step="선물 short 확장판 또는 체제 전환형 버전으로 재설계",
    ),
    ResearchCandidate(
        key="funding_arb",
        title="Funding Arbitrage",
        market="cross_market",
        directionality="market_neutral",
        stage="candidate",
        venue="binance_spot+futures",
        stage_managed=False,
        status="candidate",
        objective="방향 노출 없이 펀딩/베이시스 수익 확보",
        rationale="이미 시스템과 시장 구조가 맞지만 음수 펀딩 회피와 수수료 계층 반영이 남아 있음",
        recommended_next_step="음수 펀딩 필터와 VIP 수수료 시나리오를 반영한 보수적 백테스트",
    ),
    ResearchCandidate(
        key="donchian_futures_bi",
        title="Donchian Futures Bi-Directional",
        market="futures",
        directionality="long_short",
        stage="research",
        venue="binance_donchian_futures",
        stage_managed=True,
        status="planned",
        objective="약세장에서도 신고가/신저가 돌파를 모두 수익화",
        rationale="현재 spot Donchian의 철학을 유지하면서 하락 추세 short까지 포함할 수 있음",
        recommended_next_step="spot Donchian 로직을 재사용해 short breakout/리스크 한도 설계",
    ),
    ResearchCandidate(
        key="hmm_regime_detection",
        title="HMM Regime Detection",
        market="meta_strategy",
        directionality="adaptive",
        stage="research",
        venue="binance_futures",
        stage_managed=False,
        status="planned",
        objective="시장 체제를 더 정교하게 분류해 전략 선택/사이징 품질 향상",
        rationale="현재 rule-based regime detector를 대체 또는 보강할 수 있는 상위 메타 레이어 후보",
        recommended_next_step="기존 RegimeDetector와 병행 백테스트 후 체제 분류 일치율 및 성과 비교",
    ),
    ResearchCandidate(
        key="volatility_adaptive_trend",
        title="Volatility Adaptive Trend Following",
        market="futures",
        directionality="long_short",
        stage="research",
        venue="binance_futures",
        stage_managed=False,
        status="planned",
        objective="변동성 수준에 따라 추세 추종 진입/청산 민감도 자동 조절",
        rationale="단일 고정 파라미터 trend 전략보다 체제 적응력이 높을 가능성이 있음",
        recommended_next_step="기존 trend_follower와 비교 가능한 단독 백테스트 스크립트 작성",
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
