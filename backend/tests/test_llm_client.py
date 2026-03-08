"""LLM 클라이언트 + 프로바이더 테스트."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass

from services.llm.client import LLMClient, LLMResponse, ToolCall
from services.llm.providers import AnthropicProvider


# ── Mock helpers ──────────────────────────────────────────────

@dataclass
class MockLLMConfig:
    enabled: bool = True
    api_key: str = "test-anthropic-key"
    model: str = "claude-haiku-4-5-20251001"
    fallback_model: str = "claude-sonnet-4-6"
    gemini_api_key: str = ""
    gemini_fallback_model: str = "gemini-2.5-flash"
    max_tokens: int = 4096
    diagnostic_max_tokens: int = 512
    daily_review_enabled: bool = True


def _make_anthropic_response(text="Hello", stop_reason="end_turn", tool_blocks=None):
    """Create a mock Anthropic API response."""
    content = []
    if text:
        block = MagicMock()
        block.type = "text"
        block.text = text
        content.append(block)
    if tool_blocks:
        for tb in tool_blocks:
            block = MagicMock()
            block.type = "tool_use"
            block.id = tb["id"]
            block.name = tb["name"]
            block.input = tb["input"]
            content.append(block)

    resp = MagicMock()
    resp.content = content
    resp.stop_reason = stop_reason
    return resp


def _make_client_with_mock_provider(config=None):
    """Create LLMClient with mocked AnthropicProvider (no real import)."""
    config = config or MockLLMConfig()
    with patch.object(AnthropicProvider, "__init__", return_value=None):
        client = LLMClient(config)
    # Replace with a fully controlled mock provider
    mock_provider = MagicMock()
    client._anthropic = mock_provider
    return client, mock_provider


# ── LLMClient tests ──────────────────────────────────────────

class TestLLMClientInit:
    """LLMClient 초기화."""

    def test_init_anthropic_only(self):
        client, _ = _make_client_with_mock_provider()
        assert client._anthropic is not None
        assert client._gemini is None

    def test_init_no_keys(self):
        config = MockLLMConfig(api_key="")
        client = LLMClient(config)
        assert client._anthropic is None
        assert client._gemini is None

    def test_init_with_gemini(self):
        config = MockLLMConfig(gemini_api_key="test-key")
        with patch.object(AnthropicProvider, "__init__", return_value=None):
            from services.llm.providers import GeminiProvider
            with patch.object(GeminiProvider, "__init__", return_value=None):
                client = LLMClient(config)
        assert client._anthropic is not None
        assert client._gemini is not None


class TestLLMClientFallbackChain:
    """Fallback chain 구성."""

    def test_chain_anthropic_only(self):
        client, _ = _make_client_with_mock_provider()
        chain = client._build_fallback_chain()
        assert len(chain) == 2  # haiku + sonnet
        assert chain[0][0] == "claude-haiku-4-5-20251001"
        assert chain[1][0] == "claude-sonnet-4-6"

    def test_chain_with_gemini(self):
        config = MockLLMConfig(gemini_api_key="test-key")
        client, _ = _make_client_with_mock_provider(config)
        client._gemini = MagicMock()
        chain = client._build_fallback_chain()
        assert len(chain) == 3
        assert chain[2][0] == "gemini-2.5-flash"

    def test_chain_with_model_override(self):
        client, _ = _make_client_with_mock_provider()
        chain = client._build_fallback_chain(model_override="claude-sonnet-4-6")
        assert chain[0][0] == "claude-sonnet-4-6"

    def test_chain_no_fallback(self):
        config = MockLLMConfig(fallback_model="")
        client, _ = _make_client_with_mock_provider(config)
        chain = client._build_fallback_chain()
        assert len(chain) == 1


class TestLLMClientGenerate:
    """generate() 메서드."""

    @pytest.mark.asyncio
    async def test_generate_success(self):
        client, provider = _make_client_with_mock_provider()
        mock_response = LLMResponse(text="분석 완료", model="claude-haiku-4-5-20251001")
        provider.create = AsyncMock(return_value=mock_response)

        result = await client.generate(
            messages=[{"role": "user", "content": "test"}],
        )
        assert result.text == "분석 완료"
        provider.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_generate_fallback_on_failure(self):
        client, provider = _make_client_with_mock_provider()

        mock_response = LLMResponse(text="fallback 응답", model="claude-sonnet-4-6")

        async def mock_create(**kwargs):
            if kwargs["model"] == "claude-haiku-4-5-20251001":
                raise Exception("API error")
            return mock_response

        provider.create = mock_create

        result = await client.generate(
            messages=[{"role": "user", "content": "test"}],
            retries=1,
        )
        assert result.text == "fallback 응답"

    @pytest.mark.asyncio
    async def test_generate_all_fail_raises(self):
        client, provider = _make_client_with_mock_provider()
        provider.create = AsyncMock(side_effect=Exception("fail"))

        with pytest.raises(RuntimeError, match="All LLM providers failed"):
            await client.generate(
                messages=[{"role": "user", "content": "test"}],
                retries=1,
            )

    @pytest.mark.asyncio
    async def test_generate_no_providers_raises(self):
        config = MockLLMConfig(api_key="", fallback_model="")
        client = LLMClient(config)

        with pytest.raises(RuntimeError, match="No LLM providers"):
            await client.generate(
                messages=[{"role": "user", "content": "test"}],
            )

    @pytest.mark.asyncio
    async def test_generate_cross_provider_fallback(self):
        """Anthropic 실패 → Gemini fallback."""
        config = MockLLMConfig(gemini_api_key="test-key")
        client, anthropic_provider = _make_client_with_mock_provider(config)

        # Anthropic fails
        anthropic_provider.create = AsyncMock(side_effect=Exception("Anthropic down"))

        # Gemini succeeds
        gemini_response = LLMResponse(text="Gemini 응답", model="gemini-2.5-flash")
        gemini_provider = MagicMock()
        gemini_provider.create = AsyncMock(return_value=gemini_response)
        client._gemini = gemini_provider

        result = await client.generate(
            messages=[{"role": "user", "content": "test"}],
            retries=1,
        )
        assert result.text == "Gemini 응답"
        assert result.model == "gemini-2.5-flash"


class TestLLMClientToolUse:
    """generate_with_tools() 메서드."""

    @pytest.mark.asyncio
    async def test_tool_use_response(self):
        client, provider = _make_client_with_mock_provider()

        tool_response = LLMResponse(
            text=None,
            tool_calls=[ToolCall(id="tc_1", name="get_status", arguments={"exchange": "binance_futures"})],
            stop_reason="tool_use",
            model="claude-haiku-4-5-20251001",
        )
        provider.create = AsyncMock(return_value=tool_response)

        result = await client.generate_with_tools(
            messages=[{"role": "user", "content": "상태 보여줘"}],
            tools=[{"name": "get_status", "description": "status", "input_schema": {}}],
        )
        assert result.stop_reason == "tool_use"
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "get_status"


class TestFormatToolLoopMessages:
    """format_tool_loop_messages() 메서드."""

    def test_format_delegates_to_provider(self):
        client, provider = _make_client_with_mock_provider()

        response = LLMResponse(
            text=None,
            tool_calls=[ToolCall(id="tc_1", name="test_tool", arguments={})],
            stop_reason="tool_use",
            model="claude-haiku-4-5-20251001",
        )

        provider.format_tool_loop_messages = MagicMock(
            return_value=({"role": "assistant"}, {"role": "user"})
        )

        asst, user = client.format_tool_loop_messages(
            response,
            [{"tool_call_id": "tc_1", "content": "result"}],
        )
        assert asst["role"] == "assistant"
        assert user["role"] == "user"
        provider.format_tool_loop_messages.assert_called_once()


# ── AnthropicProvider tests ───────────────────────────────────

class TestAnthropicProvider:
    """Anthropic provider 변환 로직."""

    @pytest.mark.asyncio
    async def test_create_text_response(self):
        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value = mock_client

            raw = _make_anthropic_response("테스트 응답", "end_turn")
            mock_client.messages.create = AsyncMock(return_value=raw)

            provider = AnthropicProvider(api_key="test")
            result = await provider.create(
                messages=[{"role": "user", "content": "hi"}],
                model="claude-haiku-4-5-20251001",
                max_tokens=100,
                system=None,
                tools=None,
            )

        assert result.text == "테스트 응답"
        assert result.stop_reason == "end_turn"
        assert len(result.tool_calls) == 0

    @pytest.mark.asyncio
    async def test_create_tool_use_response(self):
        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value = mock_client

            raw = _make_anthropic_response(
                text=None,
                stop_reason="tool_use",
                tool_blocks=[{"id": "tc_1", "name": "get_status", "input": {"ex": "a"}}],
            )
            mock_client.messages.create = AsyncMock(return_value=raw)

            provider = AnthropicProvider(api_key="test")
            result = await provider.create(
                messages=[{"role": "user", "content": "hi"}],
                model="claude-haiku-4-5-20251001",
                max_tokens=100,
                system="test system",
                tools=[{"name": "get_status"}],
            )

        assert result.stop_reason == "tool_use"
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "get_status"
        assert result.tool_calls[0].arguments == {"ex": "a"}

    def test_format_tool_loop_messages(self):
        with patch("anthropic.AsyncAnthropic"):
            provider = AnthropicProvider(api_key="test")

        raw = _make_anthropic_response(
            text="thinking...",
            stop_reason="tool_use",
            tool_blocks=[{"id": "tc_1", "name": "get_status", "input": {}}],
        )

        response = LLMResponse(
            text="thinking...",
            tool_calls=[ToolCall(id="tc_1", name="get_status", arguments={})],
            stop_reason="tool_use",
            model="test",
            raw=raw,
        )

        asst_msg, user_msg = provider.format_tool_loop_messages(
            response,
            [{"tool_call_id": "tc_1", "content": '{"status": "ok"}'}],
        )

        assert asst_msg["role"] == "assistant"
        assert len(asst_msg["content"]) == 2  # text + tool_use
        assert asst_msg["content"][0]["type"] == "text"
        assert asst_msg["content"][1]["type"] == "tool_use"

        assert user_msg["role"] == "user"
        assert user_msg["content"][0]["type"] == "tool_result"
        assert user_msg["content"][0]["tool_use_id"] == "tc_1"


# ── Integration-style test ────────────────────────────────────

class TestAgentLLMIntegration:
    """에이전트가 LLMClient를 올바르게 사용하는지 검증."""

    @pytest.mark.asyncio
    async def test_diagnostic_agent_uses_llm_client(self):
        """DiagnosticAgent가 LLMClient.generate()를 호출."""
        from agents.diagnostic_agent import DiagnosticAgent

        with patch("agents.diagnostic_agent.get_config") as mock_config:
            cfg = MagicMock()
            cfg.llm.enabled = True
            cfg.llm.api_key = "test-key"
            cfg.llm.model = "claude-haiku-4-5-20251001"
            cfg.llm.fallback_model = ""
            cfg.llm.gemini_api_key = ""
            cfg.llm.gemini_fallback_model = ""
            cfg.llm.diagnostic_max_tokens = 512
            mock_config.return_value = cfg

            with patch.object(AnthropicProvider, "__init__", return_value=None):
                agent = DiagnosticAgent(
                    engine=MagicMock(_eval_error_counts={}),
                    portfolio_manager=MagicMock(),
                    exchange_adapter=AsyncMock(),
                    exchange_name="test",
                    tracked_coins=["BTC/USDT"],
                )

        # Mock the LLM client's generate method
        mock_response = LLMResponse(
            text="ACTION: skip\nDIAGNOSIS: 일시적 에러",
            model="claude-haiku-4-5-20251001",
        )
        agent._llm_client.generate = AsyncMock(return_value=mock_response)

        result = await agent.diagnose_and_recover(
            error=Exception("test"),
            context="buy_order",
            symbol="BTC/USDT",
            rule_based_result="failed",
        )
        assert result.suggested_action == "skip"
        agent._llm_client.generate.assert_called_once()
