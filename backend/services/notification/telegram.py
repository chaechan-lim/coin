"""
Telegram 알림 어댑터 — 이벤트를 HTML 메시지로 변환 + Bot API 전송

Discord embed와 동일한 이벤트를 수신하되,
Telegram에 적합한 간결한 HTML 텍스트로 포맷.
"""
import asyncio
import time
from collections import deque
from typing import Any

import httpx
import structlog

from services.notification.base import NotificationAdapter

logger = structlog.get_logger(__name__)

# 레이트 리밋: 20건/60초 (Telegram 그룹 제한 대응)
RATE_LIMIT_WINDOW = 60.0
RATE_LIMIT_MAX = 20


class TelegramAdapter(NotificationAdapter):
    """이벤트를 Telegram HTML로 변환 + Bot API 전송."""

    def __init__(self, bot_token: str, chat_id: str):
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._client = httpx.AsyncClient(timeout=10)
        self._timestamps: deque[float] = deque(maxlen=RATE_LIMIT_MAX)

    async def close(self) -> None:
        await self._client.aclose()

    async def send(
        self,
        level: str,
        category: str,
        title: str,
        detail: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        try:
            text = self._format_event(level, category, title, detail, metadata)
            if text is None:
                return

            if not self._check_rate_limit():
                logger.debug("telegram_rate_limited", title=title)
                return

            await self._send_message(text)
        except Exception as e:
            logger.warning("telegram_adapter_error", error=str(e), title=title)

    def _check_rate_limit(self) -> bool:
        now = time.monotonic()
        while self._timestamps and now - self._timestamps[0] > RATE_LIMIT_WINDOW:
            self._timestamps.popleft()
        if len(self._timestamps) >= RATE_LIMIT_MAX:
            return False
        self._timestamps.append(now)
        return True

    def _format_event(
        self,
        level: str,
        category: str,
        title: str,
        detail: str | None,
        metadata: dict[str, Any] | None,
    ) -> str | None:
        """카테고리별 Telegram HTML 포맷. 불필요한 이벤트는 None."""
        meta = metadata or {}

        if category == "trade" and level == "info":
            return self._format_trade(title, meta)
        if category == "trade" and level == "warning":
            return self._format_stop(title, meta)
        if category == "futures_trade" and level == "info":
            return self._format_futures_trade(title, meta)
        if category == "futures_trade" and level == "warning":
            return self._format_futures_stop(title, meta)
        if category == "rotation" and level == "info":
            return self._format_rotation(title, meta)
        if category == "risk" and level == "warning":
            return self._format_risk(title, detail)
        if category == "system":
            return self._format_system(title, detail, meta)
        if category == "engine" and level == "info":
            return self._format_engine_lifecycle(title, meta)
        if category == "engine" and level == "critical":
            return self._format_engine_critical(title, detail, meta)
        if category == "daily_summary":
            return self._format_daily_summary(title, meta)
        # signal, health, recovery, engine warning/error → skip (noisy)
        return None

    # ── 포맷 함수들 ──────────────────────────────────────────

    def _format_trade(self, title: str, meta: dict) -> str:
        is_buy = "매수" in title
        emoji = "🟢" if is_buy else "🔴"
        lines = [f"{emoji} <b>{title}</b>"]
        if meta.get("price"):
            lines.append(f"💰 가격: {meta['price']:,.0f} KRW")
        if meta.get("strategy"):
            conf = f" ({meta['confidence']:.0%})" if meta.get("confidence") else ""
            lines.append(f"📊 전략: {meta['strategy']}{conf}")
        if meta.get("pnl_pct") is not None:
            pnl = meta["pnl_pct"]
            sign = "+" if pnl >= 0 else ""
            lines.append(f"📈 PnL: {sign}{pnl:.2f}%")
        if meta.get("sl_price") is not None and meta.get("tp_price") is not None:
            lines.append(f"🎯 SL: {meta['sl_price']:,.0f} | TP: {meta['tp_price']:,.0f}")
        return "\n".join(lines)

    def _format_stop(self, title: str, meta: dict) -> str:
        lines = [f"⚠️ <b>{title}</b>"]
        if meta.get("price"):
            lines.append(f"💰 가격: {meta['price']:,}")
        if meta.get("pnl_pct") is not None:
            pnl = meta["pnl_pct"]
            sign = "+" if pnl >= 0 else ""
            lines.append(f"📉 PnL: {sign}{pnl:.2f}%")
        if meta.get("reason"):
            lines.append(f"📌 사유: {meta['reason']}")
        return "\n".join(lines)

    def _format_futures_trade(self, title: str, meta: dict) -> str:
        is_long = "롱" in title or "Long" in title
        is_close = "청산" in title or "Close" in title
        pnl = meta.get("pnl_pct")
        if is_close:
            emoji = "🟢" if pnl and pnl > 0 else "🔴"
        elif is_long:
            emoji = "🟢"
        else:
            emoji = "🔴"
        lines = [f"{emoji} <b>{title}</b>"]
        if meta.get("price"):
            v = meta["price"]
            fmt = f"{v:,.2f}" if v >= 10 else f"{v:,.4f}"
            lev = f" | {meta['leverage']}x" if meta.get("leverage") else ""
            lines.append(f"💰 가격: {fmt} USDT{lev}")
        if meta.get("strategy"):
            conf = f" ({meta['confidence']:.0%})" if meta.get("confidence") else ""
            lines.append(f"📊 전략: {meta['strategy']}{conf}")
        if pnl is not None:
            sign = "+" if pnl >= 0 else ""
            pnl_str = f"PnL: {sign}{pnl:.2f}%"
            if meta.get("loss_amount") is not None:
                la = meta["loss_amount"]
                la_sign = "+" if la >= 0 else ""
                pnl_str += f" ({la_sign}{la:,.2f} USDT)"
            lines.append(f"📈 {pnl_str}")
        if meta.get("sl_price") is not None and meta.get("tp_price") is not None:
            sv, tv = meta["sl_price"], meta["tp_price"]
            sf = f"{sv:,.2f}" if sv >= 10 else f"{sv:,.4f}"
            tf = f"{tv:,.2f}" if tv >= 10 else f"{tv:,.4f}"
            lines.append(f"🎯 SL: {sf} | TP: {tf}")
        return "\n".join(lines)

    def _format_futures_stop(self, title: str, meta: dict) -> str:
        pnl = meta.get("pnl_pct", 0)
        emoji = "🟢" if pnl >= 0 else "🔴"
        lines = [f"{emoji} <b>{title}</b>"]
        if meta.get("price"):
            v = meta["price"]
            fmt = f"{v:,.2f}" if v >= 10 else f"{v:,.4f}"
            lines.append(f"💰 현재가: {fmt} USDT")
        if meta.get("direction"):
            lines.append(f"📌 방향: {meta['direction'].upper()}")
        sign = "+" if pnl >= 0 else ""
        pnl_str = f"PnL: {sign}{pnl:.2f}%"
        if meta.get("loss_amount") is not None:
            la = meta["loss_amount"]
            la_sign = "+" if la >= 0 else ""
            pnl_str += f" ({la_sign}{la:,.2f} USDT)"
        lines.append(f"📈 {pnl_str}")
        if meta.get("reason"):
            lines.append(f"📌 사유: {meta['reason']}")
        return "\n".join(lines)

    def _format_rotation(self, title: str, meta: dict) -> str:
        lines = [f"🚀 <b>{title}</b>"]
        if meta.get("price"):
            lines.append(f"💰 가격: {meta['price']:,.0f}")
        if meta.get("surge_ratio"):
            lines.append(f"📊 서지: {meta['surge_ratio']:.1f}x")
        return "\n".join(lines)

    def _format_risk(self, title: str, detail: str | None) -> str:
        lines = [f"🚨 <b>{title}</b>"]
        if detail:
            lines.append(detail[:200])
        return "\n".join(lines)

    def _format_system(self, title: str, detail: str | None, meta: dict) -> str:
        is_start = "시작" in title
        emoji = "🚀" if is_start else "🛑"
        lines = [f"{emoji} <b>{title}</b>"]
        if detail:
            lines.append(detail)
        if meta.get("positions_summary"):
            lines.append(meta["positions_summary"])
        return "\n".join(lines)

    def _format_engine_lifecycle(self, title: str, meta: dict) -> str:
        is_start = "시작" in title
        emoji = "▶️" if is_start else "⏹️"
        mode = f" ({meta['mode']})" if meta.get("mode") else ""
        return f"{emoji} <b>{title}</b>{mode}"

    def _format_engine_critical(self, title: str, detail: str | None, meta: dict) -> str:
        lines = [f"🚨 <b>{title}</b>"]
        if meta.get("symbol"):
            lines.append(f"심볼: {meta['symbol']}")
        if detail:
            lines.append(detail[:200])
        return "\n".join(lines)

    def _format_daily_summary(self, title: str, meta: dict) -> str:
        is_usdt = "binance" in meta.get("exchange", "")

        def _fmt(n: float) -> str:
            return f"{n:,.2f} USDT" if is_usdt else f"{n:,.0f} ₩"

        lines = [f"📋 <b>{title}</b>"]
        if meta.get("total_value") is not None:
            lines.append(f"총 자산: {_fmt(meta['total_value'])}")
        if meta.get("return_pct") is not None:
            r = meta["return_pct"]
            lines.append(f"원금 대비: {'+' if r >= 0 else ''}{r:.2f}%")

        review = meta.get("review")
        if review:
            lines.append(
                f"24h: {review['total_trades']}건 "
                f"(매수 {review['buy_count']}/매도 {review['sell_count']})"
            )
            if review.get("win_rate") is not None:
                wr = review["win_rate"] * 100
                lines.append(f"승률: {wr:.0f}% | PF: {review.get('profit_factor', 0):.2f}x")
        return "\n".join(lines)

    # ── 전송 ───────────────────────────────────────────────────

    async def _send_message(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        try:
            resp = await self._client.post(url, json=payload)
            if resp.status_code == 429:
                try:
                    retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
                except Exception:
                    retry_after = 5
                logger.warning("telegram_rate_limited", retry_after=retry_after)
                await asyncio.sleep(retry_after)
                try:
                    await self._client.post(url, json=payload)
                except Exception as e2:
                    logger.warning("telegram_retry_failed", error=str(e2))
            elif resp.status_code != 200:
                logger.warning("telegram_send_failed", status=resp.status_code)
        except Exception as e:
            logger.warning("telegram_send_error", error=str(e))
