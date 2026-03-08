"""LLM provider implementations — Anthropic, Google Gemini."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog

logger = structlog.get_logger(__name__)


# ── Unified response types ──────────────────────────────────

@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMResponse:
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"  # "end_turn" | "tool_use" | "max_tokens"
    model: str = ""
    raw: Any = None  # provider-specific raw response


# ── Provider protocol ────────────────────────────────────────

class LLMProvider(Protocol):
    async def create(
        self,
        messages: list[dict],
        model: str,
        max_tokens: int,
        system: str | None,
        tools: list[dict] | None,
    ) -> LLMResponse: ...

    def format_tool_loop_messages(
        self,
        response: LLMResponse,
        tool_results: list[dict],
    ) -> tuple[dict, dict]:
        """Build (assistant_msg, user_msg) for multi-turn tool_use."""
        ...


# ── Anthropic provider ───────────────────────────────────────

class AnthropicProvider:
    def __init__(self, api_key: str):
        import anthropic
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    async def create(
        self,
        messages: list[dict],
        model: str,
        max_tokens: int,
        system: str | None,
        tools: list[dict] | None,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools

        response = await self._client.messages.create(**kwargs)

        # Map to unified response
        text_parts = []
        tool_calls = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.id,
                    name=block.name,
                    arguments=block.input,
                ))

        return LLMResponse(
            text="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            stop_reason=response.stop_reason,
            model=model,
            raw=response,
        )

    def format_tool_loop_messages(
        self,
        response: LLMResponse,
        tool_results: list[dict],
    ) -> tuple[dict, dict]:
        """Anthropic format: content blocks + tool_result blocks."""
        # Serialize assistant content
        assistant_content = []
        raw = response.raw
        for block in raw.content:
            if block.type == "text":
                assistant_content.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                assistant_content.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })

        # User message with tool_result blocks
        user_content = [
            {
                "type": "tool_result",
                "tool_use_id": tr["tool_call_id"],
                "content": tr["content"],
            }
            for tr in tool_results
        ]

        return (
            {"role": "assistant", "content": assistant_content},
            {"role": "user", "content": user_content},
        )


# ── Gemini provider ──────────────────────────────────────────

class GeminiProvider:
    def __init__(self, api_key: str):
        from google import genai
        self._client = genai.Client(api_key=api_key)
        self._genai = genai

    def _convert_tools(self, tools: list[dict]) -> list:
        """Convert Anthropic tool format → Gemini FunctionDeclaration."""
        from google.genai import types

        declarations = []
        for tool in tools:
            schema = tool.get("input_schema", {})
            declarations.append(types.FunctionDeclaration(
                name=tool["name"],
                description=tool.get("description", ""),
                parameters=schema if schema.get("properties") else None,
            ))
        return [types.Tool(function_declarations=declarations)]

    async def create(
        self,
        messages: list[dict],
        model: str,
        max_tokens: int,
        system: str | None,
        tools: list[dict] | None,
    ) -> LLMResponse:
        from google.genai import types

        # Convert messages to Gemini format
        gemini_contents = self._convert_messages(messages)

        # Build config
        config_kwargs: dict[str, Any] = {
            "max_output_tokens": max_tokens,
        }
        if system:
            config_kwargs["system_instruction"] = system

        config = types.GenerateContentConfig(**config_kwargs)

        # Build request kwargs
        req_kwargs: dict[str, Any] = {
            "model": model,
            "contents": gemini_contents,
            "config": config,
        }
        if tools:
            req_kwargs["config"] = types.GenerateContentConfig(
                **config_kwargs,
                tools=self._convert_tools(tools),
            )

        response = await self._client.aio.models.generate_content(**req_kwargs)

        # Map to unified response
        text_parts = []
        tool_calls = []
        if response.candidates and response.candidates[0].content:
            for part in response.candidates[0].content.parts:
                if part.text:
                    text_parts.append(part.text)
                elif part.function_call:
                    fc = part.function_call
                    tool_calls.append(ToolCall(
                        id=f"gemini_{uuid.uuid4().hex[:12]}",
                        name=fc.name,
                        arguments=dict(fc.args) if fc.args else {},
                    ))

        # Map stop reason
        stop_reason = "end_turn"
        if tool_calls:
            stop_reason = "tool_use"
        elif response.candidates:
            fr = response.candidates[0].finish_reason
            if fr and str(fr) == "MAX_TOKENS":
                stop_reason = "max_tokens"

        return LLMResponse(
            text="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            model=model,
            raw=response,
        )

    def _convert_messages(self, messages: list[dict]) -> list:
        """Convert Anthropic-style messages to Gemini Content format."""
        from google.genai import types

        contents = []
        for msg in messages:
            role = "model" if msg["role"] == "assistant" else "user"
            content = msg["content"]

            if isinstance(content, str):
                contents.append(types.Content(
                    role=role,
                    parts=[types.Part.from_text(text=content)],
                ))
            elif isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            parts.append(types.Part.from_text(text=block["text"]))
                        elif block.get("type") == "tool_use":
                            parts.append(types.Part.from_function_call(
                                name=block["name"],
                                args=block.get("input", {}),
                            ))
                        elif block.get("type") == "tool_result":
                            parts.append(types.Part.from_function_response(
                                name=block.get("_tool_name", "tool"),
                                response={"result": block.get("content", "")},
                            ))
                    elif isinstance(block, str):
                        parts.append(types.Part.from_text(text=block))
                if parts:
                    contents.append(types.Content(role=role, parts=parts))

        return contents

    def format_tool_loop_messages(
        self,
        response: LLMResponse,
        tool_results: list[dict],
    ) -> tuple[dict, dict]:
        """Gemini format: function_call + function_response parts."""
        # Assistant message with function_call parts
        assistant_parts = []
        if response.text:
            assistant_parts.append({"type": "text", "text": response.text})
        for tc in response.tool_calls:
            assistant_parts.append({
                "type": "tool_use",
                "id": tc.id,
                "name": tc.name,
                "input": tc.arguments,
            })

        # User message with function_response parts (encoded as tool_result)
        user_parts = []
        tc_map = {tc.id: tc.name for tc in response.tool_calls}
        for tr in tool_results:
            user_parts.append({
                "type": "tool_result",
                "tool_use_id": tr["tool_call_id"],
                "_tool_name": tc_map.get(tr["tool_call_id"], "tool"),
                "content": tr["content"],
            })

        return (
            {"role": "assistant", "content": assistant_parts},
            {"role": "user", "content": user_parts},
        )
