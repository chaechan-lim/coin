"""
LLM 진단 에이전트 — 규칙 기반 복구 실패 시 LLM이 에러 진단 + 복구 액션 제안

2단 복구 체계:
1차: RecoveryManager (규칙 기반, 즉시)
2차: DiagnosticAgent (LLM 분석, 규칙 실패 시 에스컬레이션)

허용된 액션만 실행 (안전):
- reconcile_cash: 잔고 재계산
- sync_positions: 거래소 동기화
- suppress_coin: 코인 매수 억제
- pause_buying: 전체 매수 일시중지
- resume_buying: 매수 재개
- skip: 아무것도 안 함
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

import structlog

from config import get_config
from core.event_bus import emit_event

logger = structlog.get_logger(__name__)

# LLM이 제안할 수 있는 허용된 액션들
ALLOWED_ACTIONS = {
    "reconcile_cash",
    "sync_positions",
    "suppress_coin",
    "pause_buying",
    "resume_buying",
    "skip",
}


@dataclass
class DiagnosticResult:
    diagnosis: str          # LLM의 진단 요약
    suggested_action: str   # 제안된 액션
    action_executed: bool   # 액션 실행 여부
    detail: str             # 추가 설명


class DiagnosticAgent:
    """LLM 기반 에러 진단 + 복구 액션 실행.

    Parameters
    ----------
    engine : TradingEngine
        suppress_buys, pause_buying, resume_buying 호출
    portfolio_manager : PortfolioManager
        reconcile_cash, sync_exchange_positions 호출
    exchange_adapter : ExchangeAdapter
        sync_exchange_positions에 전달
    exchange_name : str
        컨텍스트 정보
    tracked_coins : list[str]
        sync에 전달
    """

    MAX_DAILY_LLM_CALLS = 20  # 일일 LLM 호출 제한 (비용 절감)

    def __init__(
        self,
        engine,
        portfolio_manager,
        exchange_adapter,
        exchange_name: str,
        tracked_coins: list[str],
    ):
        self._engine = engine
        self._pm = portfolio_manager
        self._exchange = exchange_adapter
        self._exchange_name = exchange_name
        self._tracked_coins = tracked_coins

        self._llm_client = None
        self._llm_config = None
        self._daily_call_count = 0
        self._reset_date = datetime.now(timezone.utc).date()
        self._init_llm()

    def _init_llm(self) -> None:
        """LLM 클라이언트 초기화."""
        try:
            config = get_config()
            self._llm_config = config.llm
            if self._llm_config.enabled and self._llm_config.api_key:
                import anthropic
                self._llm_client = anthropic.AsyncAnthropic(
                    api_key=self._llm_config.api_key,
                )
                logger.info("diagnostic_agent_llm_enabled",
                            model=self._llm_config.model,
                            exchange=self._exchange_name)
            else:
                logger.info("diagnostic_agent_llm_disabled",
                            exchange=self._exchange_name)
        except Exception as e:
            logger.warning("diagnostic_agent_init_failed", error=str(e))
            self._llm_client = None

    async def diagnose_and_recover(
        self,
        error: Exception,
        context: str,
        symbol: str | None,
        rule_based_result: str,
        portfolio_state: dict | None = None,
    ) -> DiagnosticResult:
        """LLM에 에러 컨텍스트 전달 → 진단 + 액션 제안 → 실행.

        Parameters
        ----------
        error : 원본 예외
        context : "buy_order", "sell_order", "price_fetch", "health_check" 등
        symbol : 문제 코인 심볼
        rule_based_result : 규칙 기반 복구 결과 요약
        portfolio_state : 포트폴리오 상태 정보 (cash, positions 등)
        """
        self._maybe_reset_daily()

        if not self._llm_client:
            return DiagnosticResult(
                diagnosis="LLM 비활성",
                suggested_action="skip",
                action_executed=False,
                detail="LLM_ENABLED=false 또는 API 키 미설정",
            )

        if self._daily_call_count >= self.MAX_DAILY_LLM_CALLS:
            return DiagnosticResult(
                diagnosis="일일 LLM 호출 한도 초과",
                suggested_action="skip",
                action_executed=False,
                detail=f"{self._daily_call_count}/{self.MAX_DAILY_LLM_CALLS}",
            )

        self._daily_call_count += 1

        # 프롬프트 구성
        prompt = self._build_prompt(error, context, symbol, rule_based_result, portfolio_state)

        # LLM 호출
        action, diagnosis = await self._call_llm(prompt)

        if action == "skip" or action not in ALLOWED_ACTIONS:
            return DiagnosticResult(
                diagnosis=diagnosis,
                suggested_action=action,
                action_executed=False,
                detail="액션 불필요 또는 미허용",
            )

        # 허용된 액션 실행
        executed = await self._execute_action(action, symbol)

        # 이벤트 발행
        await emit_event(
            "info", "recovery",
            f"LLM 진단 복구 [{self._exchange_name}]",
            detail=f"진단: {diagnosis[:200]}\n액션: {action}",
            metadata={
                "exchange": self._exchange_name,
                "action": action,
                "symbol": symbol,
                "diagnosis": diagnosis[:500],
                "context": context,
                "llm_call_count": self._daily_call_count,
            },
        )

        return DiagnosticResult(
            diagnosis=diagnosis,
            suggested_action=action,
            action_executed=executed,
            detail=f"LLM 제안 '{action}' 실행{'됨' if executed else ' 실패'}",
        )

    def _build_prompt(
        self,
        error: Exception,
        context: str,
        symbol: str | None,
        rule_based_result: str,
        portfolio_state: dict | None,
    ) -> str:
        is_futures = "futures" in self._exchange_name
        currency = "USDT" if "binance" in self._exchange_name else "KRW"

        state_text = ""
        if portfolio_state:
            state_text = f"""
## 포트폴리오 상태
- 현금: {portfolio_state.get('cash', 'N/A')} {currency}
- 총 자산: {portfolio_state.get('total_value', 'N/A')} {currency}
- 보유 포지션 수: {portfolio_state.get('position_count', 'N/A')}
- 드로다운: {portfolio_state.get('drawdown_pct', 'N/A')}%
"""

        error_counts = ""
        if hasattr(self._engine, '_eval_error_counts') and self._engine._eval_error_counts:
            counts = self._engine._eval_error_counts
            error_counts = f"\n## 연속 에러 현황\n" + "\n".join(
                f"- {sym}: {c}회 연속" for sym, c in counts.items()
            )

        return f"""당신은 암호화폐 자동매매 시스템의 에러 진단 전문가입니다.
아래 에러 상황을 분석하고, 복구 액션을 제안해주세요.

## 거래소
{self._exchange_name} ({'선물' if is_futures else '현물'}, 통화: {currency})

## 에러 정보
- 컨텍스트: {context}
- 심볼: {symbol or '없음'}
- 에러 타입: {type(error).__name__}
- 에러 메시지: {str(error)[:500]}

## 규칙 기반 복구 결과
{rule_based_result}
{state_text}{error_counts}
---

## 허용된 복구 액션 (이 중 하나만 선택):
- reconcile_cash: DB 기반 잔고 재계산 (잔고 불일치 시)
- sync_positions: 거래소 실제 포지션과 DB 동기화 (포지션 불일치 시)
- suppress_coin: 특정 코인 매수 억제 (상폐, API 404 반복 시)
- pause_buying: 전체 매수 일시중지 (API 장애, 시스템 불안정 시)
- resume_buying: 매수 재개 (장애 복구 확인 후)
- skip: 액션 불필요 (일시적 에러, 자동 해결 예상)

## 주의사항
- 보수적으로 판단하세요. 확신 없으면 'skip'을 선택하세요.
- pause_buying은 전체 매수를 중단하므로 신중하게 선택하세요.
- suppress_coin은 해당 코인의 매수만 차단합니다.
- 에러가 일시적(타임아웃, 네트워크)이면 'skip'이 적절합니다.
- 반복적인 에러 패턴이면 적극적 액션을 선택하세요.

다음 형식으로 **정확히** 응답하세요:

ACTION: <액션명>
DIAGNOSIS: <1-2줄 진단 요약>"""

    async def _call_llm(self, prompt: str) -> tuple[str, str]:
        """LLM 호출 → (action, diagnosis) 반환."""
        models = [self._llm_config.model]
        if self._llm_config.fallback_model:
            models.append(self._llm_config.fallback_model)

        for model in models:
            for attempt in range(2):  # 모델당 2회 시도
                try:
                    response = await self._llm_client.messages.create(
                        model=model,
                        max_tokens=256,
                        messages=[{"role": "user", "content": prompt}],
                    )
                    text = response.content[0].text
                    return self._parse_response(text)
                except Exception as e:
                    wait = 2 ** attempt * 2
                    logger.warning(
                        "diagnostic_llm_failed",
                        model=model, attempt=attempt + 1,
                        error=str(e), exchange=self._exchange_name,
                    )
                    if attempt < 1:
                        await asyncio.sleep(wait)

        logger.error("diagnostic_llm_all_failed", exchange=self._exchange_name)
        return "skip", "LLM 호출 실패 — 기본 skip"

    def _parse_response(self, text: str) -> tuple[str, str]:
        """LLM 응답에서 ACTION과 DIAGNOSIS 파싱."""
        action = "skip"
        diagnosis = ""

        for line in text.strip().split("\n"):
            line = line.strip()
            if line.upper().startswith("ACTION:"):
                raw = line.split(":", 1)[1].strip().lower()
                if raw in ALLOWED_ACTIONS:
                    action = raw
            elif line.upper().startswith("DIAGNOSIS:"):
                diagnosis = line.split(":", 1)[1].strip()

        if not diagnosis:
            diagnosis = text[:200]

        return action, diagnosis

    async def _execute_action(self, action: str, symbol: str | None) -> bool:
        """허용된 액션 실행."""
        try:
            if action == "reconcile_cash":
                from db.session import get_session_factory
                sf = get_session_factory()
                async with sf() as session:
                    await self._pm.reconcile_cash_from_db(session)
                return True

            elif action == "sync_positions":
                from db.session import get_session_factory
                sf = get_session_factory()
                async with sf() as session:
                    await self._pm.sync_exchange_positions(
                        session, self._exchange, self._tracked_coins,
                    )
                return True

            elif action == "suppress_coin":
                if symbol:
                    self._engine.suppress_buys([symbol])
                    return True
                return False

            elif action == "pause_buying":
                self._engine.pause_buying(self._tracked_coins)
                return True

            elif action == "resume_buying":
                self._engine.resume_buying()
                return True

            elif action == "skip":
                return True

        except Exception as e:
            logger.error(
                "diagnostic_action_failed",
                action=action, symbol=symbol,
                error=str(e), exchange=self._exchange_name,
            )
            return False

        return False

    def _maybe_reset_daily(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self._reset_date:
            self._daily_call_count = 0
            self._reset_date = today
