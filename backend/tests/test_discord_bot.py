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


# ── Conversation context tests ──────────────────────────────

@pytest.mark.asyncio
async def test_conversation_context_saved(bot):
    """대화 후 컨텍스트가 저장되는지 확인."""
    bot._llm.generate_with_tools = AsyncMock(
        return_value=LLMResponse(
            text="BTC 가격은 65,000 USDT입니다.",
            stop_reason="end_turn",
            model="claude-haiku-4-5-20251001",
        )
    )
    await bot._process_message("BTC 가격", user_id=123, channel_id=100)

    # 컨텍스트에 저장됨
    assert 100 in bot._conversations
    assert len(bot._conversations[100]) == 1


@pytest.mark.asyncio
async def test_conversation_context_used_in_followup(bot):
    """후속 질문 시 이전 컨텍스트가 LLM에 전달되는지 확인."""
    # 1차 대화
    bot._llm.generate_with_tools = AsyncMock(
        return_value=LLMResponse(
            text="BTC 65,000 USDT.",
            stop_reason="end_turn",
            model="claude-haiku-4-5-20251001",
        )
    )
    await bot._process_message("BTC 가격", user_id=123, channel_id=100)

    # 2차 대화 (후속)
    bot._llm.generate_with_tools = AsyncMock(
        return_value=LLMResponse(
            text="ETH는 3,200 USDT입니다.",
            stop_reason="end_turn",
            model="claude-haiku-4-5-20251001",
        )
    )
    await bot._process_message("ETH는?", user_id=123, channel_id=100)

    # LLM에 전달된 messages에 이전 대화 포함
    call_args = bot._llm.generate_with_tools.call_args
    messages = call_args.kwargs.get("messages") or call_args[1].get("messages")
    # 이전 대화(user+assistant) + 현재 질문 = 3개
    assert len(messages) == 3
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "BTC 가격"
    assert messages[1]["role"] == "assistant"
    assert messages[2]["content"] == "ETH는?"


@pytest.mark.asyncio
async def test_conversation_context_no_channel_id(bot):
    """channel_id=0이면 컨텍스트 저장/사용 안 함."""
    bot._llm.generate_with_tools = AsyncMock(
        return_value=LLMResponse(
            text="응답",
            stop_reason="end_turn",
            model="claude-haiku-4-5-20251001",
        )
    )
    await bot._process_message("테스트", user_id=123, channel_id=0)

    assert 0 not in bot._conversations


@pytest.mark.asyncio
async def test_conversation_context_separate_channels(bot):
    """채널별로 독립 컨텍스트 유지."""
    bot._llm.generate_with_tools = AsyncMock(
        return_value=LLMResponse(
            text="응답",
            stop_reason="end_turn",
            model="claude-haiku-4-5-20251001",
        )
    )
    await bot._process_message("채널A 질문", user_id=123, channel_id=100)
    await bot._process_message("채널B 질문", user_id=123, channel_id=200)

    assert len(bot._conversations[100]) == 1
    assert len(bot._conversations[200]) == 1


def test_get_context_empty(bot):
    """컨텍스트 없는 채널은 빈 리스트."""
    result = bot._get_context(999)
    assert result == []


def test_save_and_get_context(bot):
    """저장 후 조회."""
    bot._save_context(100, [
        {"role": "user", "content": "질문"},
        {"role": "assistant", "content": "답변"},
    ])
    ctx = bot._get_context(100)
    assert len(ctx) == 2
    assert ctx[0]["content"] == "질문"
    assert ctx[1]["content"] == "답변"


def test_context_max_turns(bot):
    """MAX_CONTEXT_TURNS 초과 시 오래된 대화 제거."""
    from services.discord_bot.bot import MAX_CONTEXT_TURNS
    for i in range(MAX_CONTEXT_TURNS + 5):
        bot._save_context(100, [
            {"role": "user", "content": f"질문 {i}"},
            {"role": "assistant", "content": f"답변 {i}"},
        ])
    assert len(bot._conversations[100]) == MAX_CONTEXT_TURNS


def test_context_expiry(bot):
    """오래된 컨텍스트 만료."""
    import time
    from services.discord_bot.bot import MAX_CONTEXT_AGE_SEC
    bot._conversations[100] = __import__("collections").deque(maxlen=10)
    # 2시간 전 대화 추가
    old_time = time.time() - MAX_CONTEXT_AGE_SEC - 100
    bot._conversations[100].append((old_time, [
        {"role": "user", "content": "오래된 질문"},
        {"role": "assistant", "content": "오래된 답변"},
    ]))

    ctx = bot._get_context(100)
    assert len(ctx) == 0  # 만료됨


@pytest.mark.asyncio
async def test_context_includes_tool_summary(bot):
    """도구 사용 시 컨텍스트에 조회 요약이 포함되는지 확인."""
    tool_response = LLMResponse(
        text=None,
        tool_calls=[ToolCall(id="tc_1", name="get_portfolio_summary", arguments={"exchange": "binance_futures"})],
        stop_reason="tool_use",
        model="claude-haiku-4-5-20251001",
    )
    final_response = LLMResponse(
        text="선물 포트폴리오: BTC +3.2%",
        stop_reason="end_turn",
        model="claude-haiku-4-5-20251001",
    )
    bot._llm.generate_with_tools = AsyncMock(
        side_effect=[tool_response, final_response]
    )
    bot._llm.format_tool_loop_messages = MagicMock(
        return_value=(
            {"role": "assistant", "content": [{"type": "tool_use", "id": "tc_1", "name": "get_portfolio_summary", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tc_1", "content": "{}"}]},
        )
    )

    await bot._process_message("포트폴리오", user_id=123, channel_id=100)

    ctx = bot._get_context(100)
    assert len(ctx) == 2
    # assistant 컨텍스트에 도구 요약 포함
    assert "[조회: get_portfolio_summary]" in ctx[1]["content"]
    assert "BTC +3.2%" in ctx[1]["content"]


@pytest.mark.asyncio
async def test_context_no_tool_summary_when_no_tools(bot):
    """도구 미사용 시 조회 요약 없음."""
    bot._llm.generate_with_tools = AsyncMock(
        return_value=LLMResponse(
            text="안녕하세요!",
            stop_reason="end_turn",
            model="claude-haiku-4-5-20251001",
        )
    )
    await bot._process_message("안녕", user_id=123, channel_id=100)

    ctx = bot._get_context(100)
    assert ctx[1]["content"] == "안녕하세요!"
    assert "[조회:" not in ctx[1]["content"]


@pytest.mark.asyncio
async def test_context_dedup_tool_names(bot):
    """같은 도구를 여러 번 호출해도 요약에서 중복 제거."""
    tool_resp_1 = LLMResponse(
        text=None,
        tool_calls=[
            ToolCall(id="tc_1", name="get_portfolio_summary", arguments={"exchange": "binance_futures"}),
            ToolCall(id="tc_2", name="get_portfolio_summary", arguments={"exchange": "binance_spot"}),
        ],
        stop_reason="tool_use",
        model="claude-haiku-4-5-20251001",
    )
    final_response = LLMResponse(
        text="선물: +3%, 현물: +1%",
        stop_reason="end_turn",
        model="claude-haiku-4-5-20251001",
    )
    bot._llm.generate_with_tools = AsyncMock(
        side_effect=[tool_resp_1, final_response]
    )
    bot._llm.format_tool_loop_messages = MagicMock(
        return_value=({"role": "assistant", "content": []}, {"role": "user", "content": []})
    )

    await bot._process_message("전체 포트폴리오", user_id=123, channel_id=100)

    ctx = bot._get_context(100)
    # 중복 제거됨
    assert ctx[1]["content"].count("get_portfolio_summary") == 1


# ── Proactive alerts tests ───────────────────────────────────

@pytest.mark.asyncio
async def test_send_alert_warning_health(bot):
    """헬스 경고 이벤트가 채널에 전송됨."""
    bot._alert_channel_id = 12345
    bot._client.is_closed = MagicMock(return_value=False)
    bot._client.is_ready = MagicMock(return_value=True)
    mock_channel = AsyncMock()
    bot._client.get_channel = MagicMock(return_value=mock_channel)

    await bot.send_alert("warning", "health", "Cash 불일치", "내부 vs 거래소 차이")
    mock_channel.send.assert_awaited_once()
    sent = mock_channel.send.call_args[0][0]
    assert "HEALTH" in sent
    assert "Cash 불일치" in sent


@pytest.mark.asyncio
async def test_send_alert_ignored_for_non_target_events(bot):
    """대상이 아닌 이벤트는 무시됨."""
    bot._alert_channel_id = 12345
    bot._client.is_closed = MagicMock(return_value=False)
    bot._client.is_ready = MagicMock(return_value=True)
    mock_channel = AsyncMock()
    bot._client.get_channel = MagicMock(return_value=mock_channel)

    await bot.send_alert("info", "trade", "BTC 매수 완료")
    mock_channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_alert_no_channel(bot):
    """채널 미설정 시 무시."""
    bot._alert_channel_id = 0
    await bot.send_alert("warning", "health", "테스트")  # 에러 없이 종료
