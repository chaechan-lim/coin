"""LLM 진단 에이전트 테스트.

DiagnosticAgent가 에러를 LLM에 진단 요청하고,
허용된 액션만 실행하는지 검증.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agents.diagnostic_agent import DiagnosticAgent, ALLOWED_ACTIONS


@pytest.fixture
def mock_engine():
    eng = MagicMock()
    eng.suppress_buys = MagicMock()
    eng.pause_buying = MagicMock()
    eng.resume_buying = MagicMock()
    eng._eval_error_counts = {"BTC/USDT": 2}
    return eng


@pytest.fixture
def mock_pm():
    pm = MagicMock()
    pm.cash_balance = 100.0
    pm.reconcile_cash_from_db = AsyncMock()
    pm.sync_exchange_positions = AsyncMock()
    return pm


@pytest.fixture
def mock_exchange():
    return AsyncMock()


@pytest.fixture
def agent(mock_engine, mock_pm, mock_exchange):
    with patch("agents.diagnostic_agent.get_config") as mock_config:
        cfg = MagicMock()
        cfg.llm.enabled = False
        cfg.llm.api_key = ""
        mock_config.return_value = cfg
        return DiagnosticAgent(
            engine=mock_engine,
            portfolio_manager=mock_pm,
            exchange_adapter=mock_exchange,
            exchange_name="test_exchange",
            tracked_coins=["BTC/USDT"],
        )


class TestDiagnosticLLMDisabled:
    """LLM 비활성 시 skip 반환."""

    @pytest.mark.asyncio
    async def test_skip_when_llm_disabled(self, agent):
        result = await agent.diagnose_and_recover(
            error=Exception("test error"),
            context="buy_order",
            symbol="BTC/USDT",
            rule_based_result="reconcile_cash: 잔고 여전히 부족",
        )
        assert result.suggested_action == "skip"
        assert result.action_executed is False
        assert "비활성" in result.diagnosis


class TestDiagnosticDailyLimit:
    """일일 호출 한도."""

    @pytest.mark.asyncio
    async def test_daily_limit_exceeded(self, agent):
        agent._llm_client = MagicMock()  # LLM 활성
        agent._daily_call_count = DiagnosticAgent.MAX_DAILY_LLM_CALLS

        result = await agent.diagnose_and_recover(
            error=Exception("test"),
            context="buy_order",
            symbol="BTC/USDT",
            rule_based_result="failed",
        )
        assert result.suggested_action == "skip"
        assert "한도 초과" in result.diagnosis


class TestParseResponse:
    """LLM 응답 파싱."""

    def test_parse_valid_response(self, agent):
        text = "ACTION: reconcile_cash\nDIAGNOSIS: 잔고 DB 불일치로 reconcile 필요"
        action, diagnosis = agent._parse_response(text)
        assert action == "reconcile_cash"
        assert "잔고" in diagnosis

    def test_parse_skip(self, agent):
        text = "ACTION: skip\nDIAGNOSIS: 일시적 타임아웃, 자동 해결 예상"
        action, diagnosis = agent._parse_response(text)
        assert action == "skip"

    def test_parse_invalid_action_defaults_skip(self, agent):
        text = "ACTION: delete_database\nDIAGNOSIS: 삭제하자"
        action, diagnosis = agent._parse_response(text)
        assert action == "skip"  # 미허용 액션

    def test_parse_no_action_line(self, agent):
        text = "잘 모르겠습니다. 에러를 더 조사해야 합니다."
        action, diagnosis = agent._parse_response(text)
        assert action == "skip"
        assert len(diagnosis) > 0  # fallback to text[:200]

    def test_parse_all_allowed_actions(self, agent):
        for action_name in ALLOWED_ACTIONS:
            text = f"ACTION: {action_name}\nDIAGNOSIS: test"
            action, _ = agent._parse_response(text)
            assert action == action_name


class TestExecuteAction:
    """허용된 액션 실행."""

    @pytest.mark.asyncio
    async def test_reconcile_cash(self, agent, mock_pm):
        with patch("db.session.get_session_factory") as mock_sf:
            mock_session = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
            mock_ctx.__aexit__ = AsyncMock(return_value=False)
            mock_sf.return_value = MagicMock(return_value=mock_ctx)

            result = await agent._execute_action("reconcile_cash", None)
        assert result is True
        mock_pm.reconcile_cash_from_db.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_positions(self, agent, mock_pm):
        with patch("db.session.get_session_factory") as mock_sf:
            mock_session = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
            mock_ctx.__aexit__ = AsyncMock(return_value=False)
            mock_sf.return_value = MagicMock(return_value=mock_ctx)

            result = await agent._execute_action("sync_positions", None)
        assert result is True
        mock_pm.sync_exchange_positions.assert_called_once()

    @pytest.mark.asyncio
    async def test_suppress_coin(self, agent, mock_engine):
        result = await agent._execute_action("suppress_coin", "LUNA/USDT")
        assert result is True
        mock_engine.suppress_buys.assert_called_once_with(["LUNA/USDT"])

    @pytest.mark.asyncio
    async def test_suppress_coin_no_symbol(self, agent):
        result = await agent._execute_action("suppress_coin", None)
        assert result is False

    @pytest.mark.asyncio
    async def test_pause_buying(self, agent, mock_engine):
        result = await agent._execute_action("pause_buying", None)
        assert result is True
        mock_engine.pause_buying.assert_called_once()

    @pytest.mark.asyncio
    async def test_resume_buying(self, agent, mock_engine):
        result = await agent._execute_action("resume_buying", None)
        assert result is True
        mock_engine.resume_buying.assert_called_once()

    @pytest.mark.asyncio
    async def test_skip(self, agent):
        result = await agent._execute_action("skip", None)
        assert result is True

    @pytest.mark.asyncio
    async def test_action_failure_handled(self, agent, mock_pm):
        mock_pm.reconcile_cash_from_db = AsyncMock(side_effect=Exception("DB error"))
        with patch("db.session.get_session_factory") as mock_sf:
            mock_session = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
            mock_ctx.__aexit__ = AsyncMock(return_value=False)
            mock_sf.return_value = MagicMock(return_value=mock_ctx)

            result = await agent._execute_action("reconcile_cash", None)
        assert result is False


class TestPromptBuild:
    """프롬프트 구성 검증."""

    def test_prompt_contains_error_info(self, agent):
        prompt = agent._build_prompt(
            error=Exception("connection timeout"),
            context="buy_order",
            symbol="BTC/USDT",
            rule_based_result="backoff_wait: 실패",
            portfolio_state={"cash": 100, "total_value": 500, "position_count": 3, "drawdown_pct": 2.5},
        )
        assert "connection timeout" in prompt
        assert "buy_order" in prompt
        assert "BTC/USDT" in prompt
        assert "backoff_wait" in prompt
        assert "100" in prompt  # cash
        assert "reconcile_cash" in prompt  # allowed actions listed
        assert "skip" in prompt

    def test_prompt_includes_error_counts(self, agent, mock_engine):
        mock_engine._eval_error_counts = {"BTC/USDT": 3, "ETH/USDT": 1}
        prompt = agent._build_prompt(
            error=Exception("test"),
            context="evaluation",
            symbol=None,
            rule_based_result="failed",
            portfolio_state=None,
        )
        assert "BTC/USDT" in prompt
        assert "3회" in prompt
