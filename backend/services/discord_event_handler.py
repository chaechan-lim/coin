"""
Discord Event Handler — event_bus 이벤트를 Discord Embed로 전송

event_bus.set_notification() 콜백으로 등록되어,
모든 emit_event() 호출 시 카테고리별 필터링 후 Discord 웹훅으로 전송.
"""
import time
import asyncio
from collections import deque
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

# 색상 코드 (Discord Embed)
COLOR_GREEN = 0x2ECC71   # 매수 / 롱
COLOR_RED = 0xE74C3C     # 매도 / 숏 / 청산
COLOR_ORANGE = 0xF39C12  # 경고
COLOR_BLUE = 0x3498DB    # 시스템
COLOR_PURPLE = 0x9B59B6  # 시그널
COLOR_GOLD = 0xF1C40F    # 일일 요약

# 레이트 리밋: 5건/5초 (Discord 429 방지)
RATE_LIMIT_WINDOW = 5.0
RATE_LIMIT_MAX = 5


class DiscordEventHandler:
    """event_bus 이벤트를 Discord Embed로 변환 + 웹훅 전송."""

    def __init__(self, webhook_url: str):
        self._webhook_url = webhook_url
        self._client = httpx.AsyncClient(timeout=10)
        self._timestamps: deque[float] = deque(maxlen=RATE_LIMIT_MAX)

    async def close(self) -> None:
        """HTTP 클라이언트 종료."""
        await self._client.aclose()

    async def handle_event(
        self,
        level: str,
        category: str,
        title: str,
        detail: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """event_bus 콜백 — 필터링 + 전송. 절대 예외 미전파."""
        try:
            embed = self._format_event(level, category, title, detail, metadata)
            if embed is None:
                return

            if not self._check_rate_limit():
                logger.debug("discord_rate_limited", title=title)
                return

            await self._send_embed(embed)
        except Exception as e:
            logger.warning("discord_event_handler_error", error=str(e), title=title)

    def _check_rate_limit(self) -> bool:
        """5초 윈도우 내 5건 제한. 초과 시 False."""
        now = time.monotonic()
        # 윈도우 밖 타임스탬프 제거
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
    ) -> dict | None:
        """카테고리별 Discord Embed 생성. 불필요한 이벤트는 None 반환."""
        meta = metadata or {}

        # ── 매수/매도 (현물) ─────────────────────────────
        if category == "trade" and level == "info":
            return self._format_trade(title, meta)

        # ── SL/TP/Trailing 청산 ──────────────────────────
        if category == "trade" and level == "warning":
            return self._format_stop(title, detail, meta)

        # ── 선물 매매 ─────────────────────────────────────
        if category == "futures_trade" and level == "info":
            return self._format_futures_trade(title, meta)

        # ── 로테이션 (서지 매수) ──────────────────────────
        if category == "rotation" and level == "info":
            return self._format_rotation(title, meta)

        # ── 리스크 경고 ───────────────────────────────────
        if category == "risk" and level == "warning":
            return self._format_risk(title, detail, meta)

        # ── 시스템 (시작/종료) ────────────────────────────
        if category == "system":
            return self._format_system(title, detail)

        # ── 통합 시그널 ───────────────────────────────────
        if category == "signal":
            return self._format_signal(title, detail, meta)

        # ── 일일 요약 ────────────────────────────────────
        if category == "daily_summary":
            return self._format_daily_summary(title, detail, meta)

        # 그 외 무시
        return None

    # ── 포맷 함수들 ──────────────────────────────────────────

    def _format_trade(self, title: str, meta: dict) -> dict:
        """현물 매수/매도 embed."""
        is_buy = "매수" in title
        color = COLOR_GREEN if is_buy else COLOR_RED
        fields = []
        if meta.get("price"):
            fields.append({"name": "가격", "value": f"{meta['price']:,.0f}", "inline": True})
        if meta.get("amount_krw"):
            fields.append({"name": "금액", "value": f"{meta['amount_krw']:,.0f} KRW", "inline": True})
        if meta.get("strategy"):
            fields.append({"name": "전략", "value": meta["strategy"], "inline": True})
        if meta.get("confidence"):
            fields.append({"name": "신뢰도", "value": f"{meta['confidence']:.0%}", "inline": True})
        if meta.get("pnl_pct") is not None:
            pnl = meta["pnl_pct"]
            sign = "+" if pnl >= 0 else ""
            fields.append({"name": "PnL", "value": f"{sign}{pnl:.2f}%", "inline": True})
        if meta.get("sl_price") is not None:
            fields.append({"name": "손절가", "value": f"{meta['sl_price']:,.0f}", "inline": True})
        if meta.get("tp_price") is not None:
            fields.append({"name": "익절가", "value": f"{meta['tp_price']:,.0f}", "inline": True})
        if meta.get("market_state"):
            fields.append({"name": "시장", "value": meta["market_state"], "inline": True})
        return {"title": title, "color": color, "fields": fields}

    def _format_stop(self, title: str, detail: str | None, meta: dict) -> dict:
        """SL/TP/Trailing 청산 embed."""
        fields = []
        if meta.get("price"):
            fields.append({"name": "가격", "value": f"{meta['price']:,}", "inline": True})
        if meta.get("pnl_pct") is not None:
            pnl = meta["pnl_pct"]
            sign = "+" if pnl >= 0 else ""
            fields.append({"name": "PnL", "value": f"{sign}{pnl:.2f}%", "inline": True})
        if meta.get("reason"):
            fields.append({"name": "사유", "value": meta["reason"], "inline": False})
        return {
            "title": f"⚠️ {title}",
            "description": detail,
            "color": COLOR_ORANGE,
            "fields": fields,
        }

    def _format_futures_trade(self, title: str, meta: dict) -> dict:
        """선물 롱/숏/청산 embed."""
        is_long = "롱" in title or "Long" in title
        is_close = "청산" in title or "Close" in title
        if is_close:
            color = COLOR_RED
        elif is_long:
            color = COLOR_GREEN
        else:
            color = COLOR_RED
        fields = []
        if meta.get("price"):
            fields.append({"name": "가격", "value": f"{meta['price']:,.2f} USDT", "inline": True})
        if meta.get("strategy"):
            fields.append({"name": "전략", "value": meta["strategy"], "inline": True})
        if meta.get("confidence"):
            fields.append({"name": "신뢰도", "value": f"{meta['confidence']:.0%}", "inline": True})
        if meta.get("pnl_pct") is not None:
            pnl = meta["pnl_pct"]
            sign = "+" if pnl >= 0 else ""
            fields.append({"name": "PnL", "value": f"{sign}{pnl:.2f}%", "inline": True})
        if meta.get("leverage"):
            fields.append({"name": "레버리지", "value": f"{meta['leverage']}x", "inline": True})
        if meta.get("sl_price") is not None:
            v = meta["sl_price"]
            fmt = f"{v:,.2f}" if v >= 10 else f"{v:,.4f}"
            fields.append({"name": "손절가", "value": f"{fmt} USDT", "inline": True})
        if meta.get("tp_price") is not None:
            v = meta["tp_price"]
            fmt = f"{v:,.2f}" if v >= 10 else f"{v:,.4f}"
            fields.append({"name": "익절가", "value": f"{fmt} USDT", "inline": True})
        return {"title": title, "color": color, "fields": fields}

    def _format_rotation(self, title: str, meta: dict) -> dict:
        """로테이션 서지 매수 embed."""
        fields = []
        if meta.get("price"):
            fields.append({"name": "가격", "value": f"{meta['price']:,.0f}", "inline": True})
        if meta.get("surge_ratio"):
            fields.append({"name": "서지", "value": f"{meta['surge_ratio']:.1f}x", "inline": True})
        if meta.get("amount_krw"):
            fields.append({"name": "금액", "value": f"{meta['amount_krw']:,.0f} KRW", "inline": True})
        return {"title": f"🚀 {title}", "color": COLOR_GOLD, "fields": fields}

    def _format_risk(self, title: str, detail: str | None, meta: dict) -> dict:
        """리스크 경고 embed."""
        fields = []
        if meta.get("drawdown_pct"):
            fields.append({"name": "드로다운", "value": f"{meta['drawdown_pct']:.2f}%", "inline": True})
        if meta.get("daily_loss_pct"):
            fields.append({"name": "일일 손실", "value": f"{meta['daily_loss_pct']:.2f}%", "inline": True})
        return {
            "title": f"🚨 {title}",
            "description": detail,
            "color": COLOR_ORANGE,
            "fields": fields,
        }

    def _format_system(self, title: str, detail: str | None) -> dict:
        """시스템 시작/종료 embed."""
        return {
            "title": f"⚙️ {title}",
            "description": detail,
            "color": COLOR_BLUE,
        }

    def _format_signal(self, title: str, detail: str | None, meta: dict) -> dict:
        """통합 시그널 embed."""
        fields = []
        if meta.get("action"):
            fields.append({"name": "판단", "value": meta["action"], "inline": True})
        if meta.get("confidence"):
            fields.append({"name": "신뢰도", "value": f"{meta['confidence']:.0%}", "inline": True})
        if meta.get("strategies"):
            strats = meta["strategies"]
            if isinstance(strats, list):
                strats = ", ".join(strats)
            fields.append({"name": "참여 전략", "value": strats, "inline": False})
        return {
            "title": f"📊 {title}",
            "description": detail,
            "color": COLOR_PURPLE,
            "fields": fields,
        }

    def _format_daily_summary(self, title: str, detail: str | None, meta: dict) -> dict:
        """일일 요약 embed."""
        fields = []
        if meta.get("total_value"):
            fields.append({"name": "총 자산", "value": f"{meta['total_value']:,.0f}", "inline": True})
        if meta.get("daily_pnl_pct") is not None:
            pnl = meta["daily_pnl_pct"]
            sign = "+" if pnl >= 0 else ""
            fields.append({"name": "일일 수익", "value": f"{sign}{pnl:.2f}%", "inline": True})
        if meta.get("positions"):
            fields.append({"name": "포지션", "value": str(meta["positions"]), "inline": True})
        if meta.get("trades_today"):
            fields.append({"name": "금일 거래", "value": str(meta["trades_today"]), "inline": True})
        return {
            "title": f"📋 {title}",
            "description": detail,
            "color": COLOR_GOLD,
            "fields": fields,
        }

    # ── 전송 ───────────────────────────────────────────────────

    async def _send_embed(self, embed: dict) -> None:
        """Discord 웹훅으로 embed 전송."""
        payload = {"embeds": [embed]}
        try:
            resp = await self._client.post(self._webhook_url, json=payload)
            if resp.status_code == 429:
                # Rate limited by Discord — back off
                retry_after = resp.json().get("retry_after", 5)
                logger.warning("discord_rate_limited_429", retry_after=retry_after)
                await asyncio.sleep(retry_after)
                await self._client.post(self._webhook_url, json=payload)
            elif resp.status_code not in (200, 204):
                logger.warning("discord_webhook_failed", status=resp.status_code)
        except Exception as e:
            logger.warning("discord_send_error", error=str(e))


async def send_daily_summary(
    webhook_url: str,
    engine_registry,
) -> None:
    """일일 요약을 Discord로 전송하는 스케줄러 잡."""
    from core.event_bus import emit_event

    for exchange_name in engine_registry.available_exchanges:
        pm = engine_registry.get_portfolio_manager(exchange_name)
        eng = engine_registry.get_engine(exchange_name)
        if not pm or not eng:
            continue

        try:
            total_value = pm.cash_balance
            positions = 0
            # 포지션 가치 합산
            for sym, pos in pm.positions.items():
                if pos.get("quantity", 0) > 0:
                    positions += 1
                    total_value += pos.get("current_value", 0)

            currency = "USDT" if "binance" in exchange_name else "KRW"
            initial = pm.initial_balance
            daily_pnl_pct = ((total_value - initial) / initial * 100) if initial > 0 else 0

            await emit_event(
                "info", "daily_summary",
                f"일일 요약 [{exchange_name}]",
                detail=f"총 자산: {total_value:,.0f} {currency}",
                metadata={
                    "exchange": exchange_name,
                    "total_value": round(total_value, 0),
                    "daily_pnl_pct": round(daily_pnl_pct, 2),
                    "positions": positions,
                    "cash": round(pm.cash_balance, 0),
                    "trades_today": getattr(eng, "_daily_trade_count", 0),
                },
            )
        except Exception as e:
            logger.warning("daily_summary_error", exchange=exchange_name, error=str(e))
