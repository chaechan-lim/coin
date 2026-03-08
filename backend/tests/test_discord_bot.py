"""Discord 봇 메시지 처리 테스트."""
import pytest
import json
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock
from dataclasses import dataclass

from services.llm.providers import LLMResponse, ToolCall


# ── Bot tests ──────────────────────────────────────────────

@pytest.fixture
def mock_config():
    config = MagicMock()
    config.discord_bot.bot_token = "test-token"
    config.discord_bot.channel_id = 0
    config.discord_bot.allowed_user_ids = []
    config.discord_bot.max_response_tokens = 1024
    config.discord_bot.model = ""
    config.llm.api_key = "test-key"
    config.llm.model = "claude-haiku-4-5-20251001"
    config.llm.fallback_model = ""
    config.llm.gemini_api_key = ""
    config.llm.gemini_fallback_model = ""
    return config


@pytest.fixture
def mock_registry():
    registry = MagicMock()
    registry.available_exchanges = ["bithumb"]
    eng = MagicMock()
    eng.is_running = True
    eng._ec.mode = "paper"
    eng.tracked_coins = ["BTC/KRW"]
    registry.get_engine = MagicMock(return_value=eng)
    pm = MagicMock()
    pm.cash_balance = 500_000
    pm.initial_balance = 1_000_000
    registry.get_portfolio_manager = MagicMock(return_value=pm)
    coord = MagicMock()
    coord.last_market_analysis = None
    registry.get_coordinator = MagicMock(return_value=coord)
    return registry


@pytest.fixture
def bot(mock_config, mock_registry, session_factory):
    with patch("services.discord_bot.bot.discord.Client"):
        with patch("services.discord_bot.bot.LLMClient") as mock_llm_cls:
            from services.discord_bot.bot import TradingBot
            b = TradingBot(
                config=mock_config,
                engine_registry=mock_registry,
                session_factory=session_factory,
            )
            # Replace with a fresh mock for test control
            b._llm = MagicMock()
            return b


@pytest.mark.asyncio
async def test_process_simple_message(bot):
    """단순 텍스트 응답."""
    bot._llm.generate_with_tools = AsyncMock(
        return_value=LLMResponse(
            text="현재 시스템 정상 운영 중입니다.",
            stop_reason="end_turn",
            model="claude-haiku-4-5-20251001",
        )
    )
    result = await bot._process_message("현재 상태 알려줘", user_id=123)
    assert "정상 운영" in result


@pytest.mark.asyncio
async def test_process_with_tool_use(bot):
    """tool_use → 결과 → 최종 응답."""
    # 1차 응답: tool_use
    tool_response = LLMResponse(
        text=None,
        tool_calls=[ToolCall(id="tc_1", name="get_engine_status", arguments={})],
        stop_reason="tool_use",
        model="claude-haiku-4-5-20251001",
    )
    # 2차 응답: 텍스트
    final_response = LLMResponse(
        text="모든 엔진이 정상 작동 중입니다.",
        stop_reason="end_turn",
        model="claude-haiku-4-5-20251001",
    )
    bot._llm.generate_with_tools = AsyncMock(
        side_effect=[tool_response, final_response]
    )
    bot._llm.format_tool_loop_messages = MagicMock(
        return_value=(
            {"role": "assistant", "content": [{"type": "tool_use", "id": "tc_1", "name": "get_engine_status", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tc_1", "content": "{}"}]},
        )
    )

    result = await bot._process_message("엔진 상태", user_id=123)
    assert "정상 작동" in result
    assert bot._llm.generate_with_tools.call_count == 2


@pytest.mark.asyncio
async def test_write_tool_permission_denied(bot):
    """허가되지 않은 사용자의 write 도구 호출."""
    bot._allowed_users = {999}  # 다른 사용자만 허용

    tool_response = LLMResponse(
        text=None,
        tool_calls=[ToolCall(id="tc_1", name="start_engine", arguments={"exchange": "bithumb"})],
        stop_reason="tool_use",
        model="claude-haiku-4-5-20251001",
    )
    final_response = LLMResponse(
        text="권한이 없습니다.",
        stop_reason="end_turn",
        model="claude-haiku-4-5-20251001",
    )
    bot._llm.generate_with_tools = AsyncMock(
        side_effect=[tool_response, final_response]
    )
    bot._llm.format_tool_loop_messages = MagicMock(
        return_value=({"role": "assistant", "content": []}, {"role": "user", "content": []})
    )

    result = await bot._process_message("엔진 시작해줘", user_id=123)
    # Claude가 권한 에러를 받고 응답
    assert bot._llm.generate_with_tools.call_count == 2


@pytest.mark.asyncio
async def test_write_tool_permission_allowed(bot):
    """허가된 사용자의 write 도구 호출."""
    bot._allowed_users = {123}

    tool_response = LLMResponse(
        text=None,
        tool_calls=[ToolCall(id="tc_1", name="start_engine", arguments={"exchange": "bithumb"})],
        stop_reason="tool_use",
        model="claude-haiku-4-5-20251001",
    )
    final_response = LLMResponse(
        text="빗썸 엔진을 시작했습니다.",
        stop_reason="end_turn",
        model="claude-haiku-4-5-20251001",
    )
    bot._llm.generate_with_tools = AsyncMock(
        side_effect=[tool_response, final_response]
    )
    bot._llm.format_tool_loop_messages = MagicMock(
        return_value=({"role": "assistant", "content": []}, {"role": "user", "content": []})
    )

    result = await bot._process_message("빗썸 엔진 시작", user_id=123)
    assert "시작" in result


@pytest.mark.asyncio
async def test_write_tool_no_whitelist(bot):
    """화이트리스트 없으면 모든 사용자 허용."""
    bot._allowed_users = set()  # 빈 = 제한 없음

    tool_response = LLMResponse(
        text=None,
        tool_calls=[ToolCall(id="tc_1", name="stop_engine", arguments={"exchange": "bithumb"})],
        stop_reason="tool_use",
        model="claude-haiku-4-5-20251001",
    )
    final_response = LLMResponse(
        text="엔진 중지됨.",
        stop_reason="end_turn",
        model="claude-haiku-4-5-20251001",
    )

    eng = bot._tool_ctx.engine_registry.get_engine("bithumb")
    eng.stop = AsyncMock()

    bot._llm.generate_with_tools = AsyncMock(
        side_effect=[tool_response, final_response]
    )
    bot._llm.format_tool_loop_messages = MagicMock(
        return_value=({"role": "assistant", "content": []}, {"role": "user", "content": []})
    )

    result = await bot._process_message("엔진 멈춰", user_id=456)
    assert "중지" in result


@pytest.mark.asyncio
async def test_max_iterations_protection(bot):
    """무한 루프 방지 (10회 반복 후 종료)."""
    # 항상 tool_use를 반환하는 응답
    tool_response = LLMResponse(
        text=None,
        tool_calls=[ToolCall(id="tc_1", name="get_engine_status", arguments={})],
        stop_reason="tool_use",
        model="claude-haiku-4-5-20251001",
    )
    final_response = LLMResponse(
        text="종료.",
        stop_reason="end_turn",
        model="claude-haiku-4-5-20251001",
    )
    # 10번 tool_use + 1번 final
    bot._llm.generate_with_tools = AsyncMock(
        side_effect=[tool_response] * 10 + [final_response]
    )
    bot._llm.format_tool_loop_messages = MagicMock(
        return_value=({"role": "assistant", "content": []}, {"role": "user", "content": []})
    )

    result = await bot._process_message("테스트", user_id=123)
    # 10번까지만 반복 후 마지막 응답에서 텍스트 추출 시도
    assert bot._llm.generate_with_tools.call_count == 11


def test_split_text_short(bot):
    """짧은 텍스트는 분할 안 됨."""
    chunks = bot._split_text("짧은 텍스트", 2000)
    assert len(chunks) == 1


def test_split_text_long(bot):
    """긴 텍스트는 줄바꿈에서 분할."""
    text = "라인 1\n" * 300
    chunks = bot._split_text(text, 100)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 100


def test_split_text_no_newline(bot):
    """줄바꿈 없는 긴 텍스트."""
    text = "a" * 5000
    chunks = bot._split_text(text, 2000)
    assert len(chunks) == 3
    assert len(chunks[0]) == 2000


def test_build_system_prompt_no_memories(bot):
    """메모리 없을 때 기본 프롬프트만."""
    with patch("services.discord_bot.bot.load_memories", return_value=[]):
        prompt = bot._build_system_prompt()
    assert "암호화폐 자동매매" in prompt
    # 메모리 항목이 추가되지 않음
    assert "- 빗썸" not in prompt


def test_build_system_prompt_with_memories(bot):
    """메모리 있을 때 프롬프트에 포함."""
    memories = [
        {"content": "빗썸은 비활성화됨", "created_at": "2026-03-08T00:00:00"},
        {"content": "선물 레버리지 3x", "created_at": "2026-03-08T00:00:00"},
    ]
    with patch("services.discord_bot.bot.load_memories", return_value=memories):
        prompt = bot._build_system_prompt()
    assert "빗썸은 비활성화됨" in prompt
    assert "선물 레버리지 3x" in prompt
