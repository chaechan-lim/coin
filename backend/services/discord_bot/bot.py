"""
Discord 트레이딩 봇 — 자연어 → Claude tool_use → 응답.

discord.py Client가 FastAPI와 같은 이벤트 루프에서 동작.
"""
from __future__ import annotations

import json
import asyncio
from typing import Any

import discord
import structlog
from anthropic import AsyncAnthropic

from services.discord_bot.tools import (
    TOOL_DEFINITIONS,
    WRITE_TOOLS,
    ToolContext,
    execute_tool,
)

logger = structlog.get_logger(__name__)

SYSTEM_PROMPT = """\
당신은 암호화폐 자동매매 시스템의 대화형 어시스턴트입니다.
사용자의 질문에 한국어로 간결하게 답변하세요.

시스템 개요:
- 트리플 엔진: 빗썸(현물 KRW), 바이낸스 현물(USDT), 바이낸스 선물(USDT, 3x 레버리지)
- 현물 4전략, 선물 6전략, 가중 투표 기반 시그널 결합
- AI 에이전트: 시장 분석, 리스크 관리, 성과 분석, 전략 조언

규칙:
- 도구(tool)를 사용하여 실시간 데이터를 조회한 후 답변하세요.
- 거래소를 명시하지 않으면 활성화된 모든 거래소를 조회하세요.
- 금액: 빗썸은 KRW(원), 바이낸스는 USDT.
- 숫자는 읽기 쉽게 천 단위 구분자를 사용하세요.
- 엔진 시작/중지 같은 위험한 작업은 사용자에게 확인 후 실행하세요.
- 답변은 Discord에 표시되므로 마크다운 형식으로 작성하세요.
"""

# Discord embed character limits
MAX_EMBED_DESC = 4096
MAX_MESSAGE = 2000


class TradingBot:
    """자연어 Discord 봇.

    Parameters
    ----------
    config : AppConfig
        discord_bot, llm 설정 참조
    engine_registry : EngineRegistry
        엔진/PM/코디네이터 접근
    session_factory : async_sessionmaker
        DB 세션 생성
    """

    def __init__(self, config, engine_registry, session_factory):
        self._config = config
        bot_cfg = config.discord_bot
        self._bot_token = bot_cfg.bot_token
        self._channel_id = bot_cfg.channel_id
        self._allowed_users = set(bot_cfg.allowed_user_ids) if bot_cfg.allowed_user_ids else set()
        self._max_tokens = bot_cfg.max_response_tokens

        # Claude API
        llm_cfg = config.llm
        self._anthropic = AsyncAnthropic(api_key=llm_cfg.api_key)
        self._model = bot_cfg.model or llm_cfg.model

        # Tool context
        self._tool_ctx = ToolContext(
            engine_registry=engine_registry,
            session_factory=session_factory,
        )

        # Discord client
        intents = discord.Intents.default()
        intents.message_content = True
        self._client = discord.Client(intents=intents)
        self._setup_events()

    def _setup_events(self):
        @self._client.event
        async def on_ready():
            logger.info("discord_bot_ready", user=str(self._client.user))

        @self._client.event
        async def on_message(message: discord.Message):
            await self._on_message(message)

    async def _on_message(self, message: discord.Message):
        """메시지 수신 처리."""
        # 자기 메시지 무시
        if message.author == self._client.user:
            return

        # 채널 제한
        if self._channel_id and message.channel.id != self._channel_id:
            # 멘션이면 채널 무관하게 응답
            if self._client.user not in message.mentions:
                return

        # 멘션 또는 지정 채널에서만 응답
        is_mention = self._client.user in message.mentions
        is_target_channel = self._channel_id and message.channel.id == self._channel_id
        if not is_mention and not is_target_channel:
            return

        # 텍스트 추출 (멘션 제거)
        text = message.content
        if self._client.user:
            text = text.replace(f"<@{self._client.user.id}>", "").strip()
        if not text:
            return

        # 권한 체크 (write tool은 나중에 실행 시 체크)
        user_id = message.author.id

        try:
            async with message.channel.typing():
                response_text = await self._process_message(text, user_id)
            await self._send_response(message, response_text)
        except Exception as e:
            logger.error("discord_bot_message_error", error=str(e), exc_info=True)
            await message.reply(f"오류가 발생했습니다: {str(e)[:200]}")

    async def _process_message(self, text: str, user_id: int) -> str:
        """사용자 메시지 → Claude API → tool_use 루프 → 최종 텍스트."""
        messages = [{"role": "user", "content": text}]

        response = await self._anthropic.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=SYSTEM_PROMPT,
            tools=TOOL_DEFINITIONS,
            messages=messages,
        )

        # tool_use 루프
        max_iterations = 10
        iteration = 0
        while response.stop_reason == "tool_use" and iteration < max_iterations:
            iteration += 1
            tool_results = []

            for block in response.content:
                if block.type == "tool_use":
                    # write 도구 권한 체크
                    if block.name in WRITE_TOOLS:
                        if self._allowed_users and user_id not in self._allowed_users:
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": json.dumps(
                                    {"error": "권한 없음: 이 작업은 허가된 사용자만 실행할 수 있습니다."},
                                    ensure_ascii=False,
                                ),
                            })
                            continue

                    result = await execute_tool(self._tool_ctx, block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, ensure_ascii=False, default=str),
                    })

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

            response = await self._anthropic.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=SYSTEM_PROMPT,
                tools=TOOL_DEFINITIONS,
                messages=messages,
            )

        # 최종 텍스트 추출
        text_parts = []
        for block in response.content:
            if hasattr(block, "text"):
                text_parts.append(block.text)
        return "\n".join(text_parts) or "응답을 생성할 수 없습니다."

    async def _send_response(self, message: discord.Message, text: str):
        """응답을 Discord embed 또는 일반 메시지로 전송."""
        if len(text) <= MAX_MESSAGE:
            await message.reply(text)
            return

        # 긴 텍스트는 여러 메시지로 분할
        chunks = self._split_text(text, MAX_MESSAGE)
        for chunk in chunks:
            await message.reply(chunk)

    @staticmethod
    def _split_text(text: str, max_len: int) -> list[str]:
        """텍스트를 max_len 이하 청크로 분할."""
        if len(text) <= max_len:
            return [text]

        chunks = []
        while text:
            if len(text) <= max_len:
                chunks.append(text)
                break
            # 줄바꿈에서 분할 시도
            split_at = text.rfind("\n", 0, max_len)
            if split_at == -1:
                split_at = max_len
            chunks.append(text[:split_at])
            text = text[split_at:].lstrip("\n")
        return chunks

    async def start(self):
        """봇 시작 (이벤트 루프에서 실행)."""
        try:
            await self._client.start(self._bot_token)
        except Exception as e:
            logger.error("discord_bot_start_failed", error=str(e))

    async def close(self):
        """봇 종료."""
        if not self._client.is_closed():
            await self._client.close()
            logger.info("discord_bot_closed")
