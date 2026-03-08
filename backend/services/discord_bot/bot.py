"""
Discord 트레이딩 봇 — 자연어 → LLM tool_use → 응답.

discord.py Client가 FastAPI와 같은 이벤트 루프에서 동작.
Multi-provider LLM (Anthropic + Gemini fallback) 지원.
"""
from __future__ import annotations

import json
import asyncio
from collections import deque
from typing import Any

import discord
import structlog

from services.llm import LLMClient
from services.discord_bot.tools import (
    TOOL_DEFINITIONS,
    WRITE_TOOLS,
    ToolContext,
    execute_tool,
    load_memories,
)

logger = structlog.get_logger(__name__)

# 대화 컨텍스트 설정
MAX_CONTEXT_TURNS = 10  # 채널당 보관할 최근 대화 턴 수 (user+assistant 쌍)
MAX_CONTEXT_AGE_SEC = 3600  # 1시간 이상 지난 컨텍스트는 만료

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

메모리:
- 사용자가 '기억해', '메모해', '잊지마' 등 요청하면 save_memory 도구로 저장하세요.
- 저장된 메모리는 아래에 표시됩니다. 이미 알고 있는 사실처럼 자연스럽게 활용하세요.
- 사용자가 '잊어', '삭제해' 등 요청하면 delete_memory 도구를 사용하세요.
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

        # LLM client (multi-provider)
        llm_cfg = config.llm
        self._llm = LLMClient(llm_cfg)
        self._model = bot_cfg.model or llm_cfg.model

        # Tool context
        self._tool_ctx = ToolContext(
            engine_registry=engine_registry,
            session_factory=session_factory,
        )

        # 채널별 대화 히스토리: channel_id → deque of (timestamp, messages)
        self._conversations: dict[int, deque] = {}

        # 선제 알림 설정
        self._alert_channel_id = bot_cfg.channel_id  # 알림 전송 채널

        # Discord client
        intents = discord.Intents.default()
        intents.message_content = True
        self._client = discord.Client(intents=intents)
        self._setup_events()

    def _setup_events(self):
        @self._client.event
        async def on_ready():
            guilds = self._client.guilds
            guild_names = [g.name for g in guilds]
            logger.info("discord_bot_ready",
                        user=str(self._client.user),
                        guild_count=len(guilds),
                        guilds=guild_names)

        @self._client.event
        async def on_message(message: discord.Message):
            await self._on_message(message)

    async def _on_message(self, message: discord.Message):
        """메시지 수신 처리."""
        # 메시지 수신 디버그 로깅 (봇 자신 제외)
        if message.author != self._client.user:
            logger.debug("discord_bot_raw_message",
                         author=str(message.author),
                         channel=message.channel.id,
                         content_len=len(message.content))

        # 자기 메시지 무시
        if message.author == self._client.user:
            return

        # 봇 멘션 여부 판단
        is_mention = self._client.user in message.mentions
        is_target_channel = bool(self._channel_id) and message.channel.id == self._channel_id

        # 채널 제한: 지정 채널 외에서는 멘션만 응답
        if self._channel_id and message.channel.id != self._channel_id:
            if not is_mention:
                return

        # 멘션 또는 지정 채널에서만 응답
        if not is_mention and not is_target_channel:
            return

        logger.info("discord_bot_message_received",
                     author=str(message.author),
                     channel=message.channel.id,
                     is_mention=is_mention,
                     content=message.content[:80])

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
                response_text = await self._process_message(text, user_id, channel_id=message.channel.id)
            await self._send_response(message, response_text)
        except Exception as e:
            logger.error("discord_bot_message_error", error=str(e), exc_info=True)
            error_msg = f"⚠️ 처리 중 오류가 발생했습니다.\n```\n{type(e).__name__}: {str(e)[:300]}\n```"
            try:
                await message.reply(error_msg)
            except Exception:
                pass

    def _build_system_prompt(self) -> str:
        """시스템 프롬프트 + 저장된 메모리 결합."""
        memories = load_memories()
        if not memories:
            return SYSTEM_PROMPT
        memory_lines = [f"- {m['content']}" for m in memories]
        return SYSTEM_PROMPT + "\n저장된 메모리:\n" + "\n".join(memory_lines) + "\n"

    def _get_context(self, channel_id: int) -> list[dict]:
        """채널의 최근 대화 히스토리를 LLM 메시지 형식으로 반환."""
        if channel_id not in self._conversations:
            return []
        import time
        now = time.time()
        history = self._conversations[channel_id]
        # 만료된 항목 제거
        while history and (now - history[0][0]) > MAX_CONTEXT_AGE_SEC:
            history.popleft()
        # 메시지 목록 결합
        msgs = []
        for _, turn_msgs in history:
            msgs.extend(turn_msgs)
        return msgs

    def _save_context(self, channel_id: int, turn_messages: list[dict]) -> None:
        """대화 턴을 히스토리에 저장."""
        import time
        if channel_id not in self._conversations:
            self._conversations[channel_id] = deque(maxlen=MAX_CONTEXT_TURNS)
        self._conversations[channel_id].append((time.time(), turn_messages))

    async def _process_message(self, text: str, user_id: int, channel_id: int = 0) -> str:
        """사용자 메시지 → LLM API → tool_use 루프 → 최종 텍스트."""
        # 이전 대화 컨텍스트 로드
        context = self._get_context(channel_id) if channel_id else []
        messages = context + [{"role": "user", "content": text}]
        system = self._build_system_prompt()

        logger.info("discord_bot_processing", text=text[:100], user_id=user_id,
                     context_turns=len(context) // 2 if context else 0)

        response = await self._llm.generate_with_tools(
            messages=messages,
            tools=TOOL_DEFINITIONS,
            max_tokens=self._max_tokens,
            system=system,
            model=self._model,
        )

        # tool_use 루프
        max_iterations = 10
        iteration = 0
        while response.stop_reason == "tool_use" and iteration < max_iterations:
            iteration += 1
            tool_results = []

            for tc in response.tool_calls:
                logger.debug("discord_bot_tool_call", tool=tc.name, input=tc.arguments)

                # write 도구 권한 체크
                if tc.name in WRITE_TOOLS:
                    if self._allowed_users and user_id not in self._allowed_users:
                        tool_results.append({
                            "tool_call_id": tc.id,
                            "content": json.dumps(
                                {"error": "권한 없음: 이 작업은 허가된 사용자만 실행할 수 있습니다."},
                                ensure_ascii=False,
                            ),
                        })
                        continue

                result = await execute_tool(self._tool_ctx, tc.name, tc.arguments)
                tool_results.append({
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                })

            # Build messages for next turn
            asst_msg, user_msg = self._llm.format_tool_loop_messages(response, tool_results)
            messages.append(asst_msg)
            messages.append(user_msg)

            response = await self._llm.generate_with_tools(
                messages=messages,
                tools=TOOL_DEFINITIONS,
                max_tokens=self._max_tokens,
                system=system,
                model=self._model,
            )

        # 최종 텍스트 추출
        result = response.text or "응답을 생성할 수 없습니다."
        logger.info("discord_bot_response", length=len(result), iterations=iteration)

        # 대화 컨텍스트 저장 (user 질문 + assistant 최종 응답만, tool 루프 중간 과정 제외)
        if channel_id:
            self._save_context(channel_id, [
                {"role": "user", "content": text},
                {"role": "assistant", "content": result},
            ])

        return result

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

    # ── 선제 알림 ──────────────────────────────────────────────

    # 알림 대상 이벤트: (level, category) 쌍
    _ALERT_EVENTS = {
        ("warning", "health"),
        ("critical", "health"),
        ("warning", "engine"),
        ("critical", "engine"),
        ("error", "engine"),
        ("warning", "risk"),
        ("info", "recovery"),
    }

    async def send_alert(self, level: str, category: str, title: str,
                         detail: str = "", **kwargs) -> None:
        """이벤트 버스에서 호출 — 중요 이벤트를 채널에 선제 알림."""
        if not self._alert_channel_id:
            return
        if (level, category) not in self._ALERT_EVENTS:
            return
        if self._client.is_closed() or not self._client.is_ready():
            return

        icons = {"critical": "🚨", "error": "❌", "warning": "⚠️", "info": "ℹ️"}
        icon = icons.get(level, "📢")

        msg = f"{icon} **[{category.upper()}]** {title}"
        if detail:
            msg += f"\n```\n{detail[:500]}\n```"

        try:
            channel = self._client.get_channel(self._alert_channel_id)
            if channel:
                await channel.send(msg[:MAX_MESSAGE])
        except Exception as e:
            logger.debug("bot_alert_send_error", error=str(e))

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
