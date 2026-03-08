"""Discord 봇 메시지 처리 테스트."""
import pytest
import json
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock
from dataclasses import dataclass


# ── Mock classes for Anthropic response ─────────────────────

@dataclass
class TextBlock:
    type: str = "text"
    text: str = ""


@dataclass
class ToolUseBlock:
    type: str = "tool_use"
    id: str = "tool_1"
    name: str = ""
    input: dict = None

    def __post_init__(self):
        if self.input is None:
            self.input = {}


@dataclass
class MockResponse:
    content: list = None
    stop_reason: str = "end_turn"

    def __post_init__(self):
        if self.content is None:
            self.content = [TextBlock(text="안녕하세요!")]


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
        with patch("services.discord_bot.bot.AsyncAnthropic") as mock_anthropic:
            from services.discord_bot.bot import TradingBot
            b = TradingBot(
                config=mock_config,
                engine_registry=mock_registry,
                session_factory=session_factory,
            )
            b._anthropic = mock_anthropic.return_value
            return b


@pytest.mark.asyncio
async def test_process_simple_message(bot):
    """단순 텍스트 응답."""
    bot._anthropic.messages.create = AsyncMock(
        return_value=MockResponse(
            content=[TextBlock(text="현재 시스템 정상 운영 중입니다.")],
            stop_reason="end_turn",
        )
    )
    result = await bot._process_message("현재 상태 알려줘", user_id=123)
    assert "정상 운영" in result


@pytest.mark.asyncio
async def test_process_with_tool_use(bot):
    """tool_use → 결과 → 최종 응답."""
    # 1차 응답: tool_use
    tool_response = MockResponse(
        content=[ToolUseBlock(
            id="tool_1",
            name="get_engine_status",
            input={},
        )],
        stop_reason="tool_use",
    )
    # 2차 응답: 텍스트
    final_response = MockResponse(
        content=[TextBlock(text="모든 엔진이 정상 작동 중입니다.")],
        stop_reason="end_turn",
    )
    bot._anthropic.messages.create = AsyncMock(
        side_effect=[tool_response, final_response]
    )

    result = await bot._process_message("엔진 상태", user_id=123)
    assert "정상 작동" in result
    assert bot._anthropic.messages.create.call_count == 2


@pytest.mark.asyncio
async def test_write_tool_permission_denied(bot):
    """허가되지 않은 사용자의 write 도구 호출."""
    bot._allowed_users = {999}  # 다른 사용자만 허용

    tool_response = MockResponse(
        content=[ToolUseBlock(
            id="tool_1",
            name="start_engine",
            input={"exchange": "bithumb"},
        )],
        stop_reason="tool_use",
    )
    final_response = MockResponse(
        content=[TextBlock(text="권한이 없습니다.")],
        stop_reason="end_turn",
    )
    bot._anthropic.messages.create = AsyncMock(
        side_effect=[tool_response, final_response]
    )

    result = await bot._process_message("엔진 시작해줘", user_id=123)
    # Claude가 권한 에러를 받고 응답
    assert bot._anthropic.messages.create.call_count == 2


@pytest.mark.asyncio
async def test_write_tool_permission_allowed(bot):
    """허가된 사용자의 write 도구 호출."""
    bot._allowed_users = {123}

    tool_response = MockResponse(
        content=[ToolUseBlock(
            id="tool_1",
            name="start_engine",
            input={"exchange": "bithumb"},
        )],
        stop_reason="tool_use",
    )
    final_response = MockResponse(
        content=[TextBlock(text="빗썸 엔진을 시작했습니다.")],
        stop_reason="end_turn",
    )
    bot._anthropic.messages.create = AsyncMock(
        side_effect=[tool_response, final_response]
    )

    result = await bot._process_message("빗썸 엔진 시작", user_id=123)
    assert "시작" in result


@pytest.mark.asyncio
async def test_write_tool_no_whitelist(bot):
    """화이트리스트 없으면 모든 사용자 허용."""
    bot._allowed_users = set()  # 빈 = 제한 없음

    tool_response = MockResponse(
        content=[ToolUseBlock(
            id="tool_1",
            name="stop_engine",
            input={"exchange": "bithumb"},
        )],
        stop_reason="tool_use",
    )
    final_response = MockResponse(
        content=[TextBlock(text="엔진 중지됨.")],
        stop_reason="end_turn",
    )

    eng = bot._tool_ctx.engine_registry.get_engine("bithumb")
    eng.stop = AsyncMock()

    bot._anthropic.messages.create = AsyncMock(
        side_effect=[tool_response, final_response]
    )

    result = await bot._process_message("엔진 멈춰", user_id=456)
    assert "중지" in result


@pytest.mark.asyncio
async def test_max_iterations_protection(bot):
    """무한 루프 방지 (10회 반복 후 종료)."""
    # 항상 tool_use를 반환하는 악의적 응답
    tool_response = MockResponse(
        content=[ToolUseBlock(
            id="tool_1",
            name="get_engine_status",
            input={},
        )],
        stop_reason="tool_use",
    )
    final_response = MockResponse(
        content=[TextBlock(text="종료.")],
        stop_reason="end_turn",
    )
    # 10번 tool_use + 1번 final
    bot._anthropic.messages.create = AsyncMock(
        side_effect=[tool_response] * 10 + [final_response]
    )

    result = await bot._process_message("테스트", user_id=123)
    # 10번까지만 반복 후 마지막 응답에서 텍스트 추출 시도
    assert bot._anthropic.messages.create.call_count == 11


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
