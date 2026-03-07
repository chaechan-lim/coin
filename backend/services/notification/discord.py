"""
Discord 알림 어댑터 — 이벤트를 Discord Embed로 변환 + 웹훅 전송
"""
import time
import asyncio
from collections import deque
from typing import Any

import httpx
import structlog

from services.notification.base import NotificationAdapter

logger = structlog.get_logger(__name__)

# 색상 코드 (Discord Embed)
COLOR_GREEN = 0x2ECC71   # 매수 / 롱
COLOR_RED = 0xE74C3C     # 매도 / 숏 / 청산
COLOR_ORANGE = 0xF39C12  # 경고
COLOR_BLUE = 0x3498DB    # 시스템
COLOR_PURPLE = 0x9B59B6  # 시그널
COLOR_GOLD = 0xF1C40F    # 일일 요약
COLOR_TEAL = 0x1ABC9C    # 헬스체크
COLOR_CYAN = 0x00BCD4    # 복구

# 레이트 리밋: 5건/5초 (Discord 429 방지)
RATE_LIMIT_WINDOW = 5.0
RATE_LIMIT_MAX = 5


class DiscordAdapter(NotificationAdapter):
    """이벤트를 Discord Embed로 변환 + 웹훅 전송."""

    def __init__(self, webhook_url: str):
        self._webhook_url = webhook_url
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
        """이벤트 → Discord Embed 변환 → 전송. 절대 예외 미전파."""
        try:
            embed = self._format_event(level, category, title, detail, metadata)
            if embed is None:
                return

            if not self._check_rate_limit():
                logger.debug("discord_rate_limited", title=title)
                return

            await self._send_embed(embed)
        except Exception as e:
            logger.warning("discord_adapter_error", error=str(e), title=title)

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
    ) -> dict | None:
        """카테고리별 Discord Embed 생성. 불필요한 이벤트는 None 반환."""
        meta = metadata or {}

        if category == "trade" and level == "info":
            return self._format_trade(title, meta)
        if category == "trade" and level == "warning":
            return self._format_stop(title, detail, meta)
        if category == "futures_trade" and level == "warning":
            return self._format_futures_stop(title, detail, meta)
        if category == "futures_trade" and level == "info":
            return self._format_futures_trade(title, meta)
        if category == "rotation" and level == "info":
            return self._format_rotation(title, meta)
        if category == "risk" and level == "warning":
            return self._format_risk(title, detail, meta)
        if category == "system":
            return self._format_system(title, detail, meta)
        if category == "engine" and level == "info":
            return self._format_engine_lifecycle(title, detail, meta)
        if category == "engine" and level in ("warning", "critical", "error"):
            return self._format_engine_error(level, title, detail, meta)
        if category == "signal":
            return self._format_signal(title, detail, meta)
        if category == "daily_summary":
            return self._format_daily_summary(title, detail, meta)
        if category == "health":
            return self._format_health(level, title, detail, meta)
        if category == "recovery":
            return self._format_recovery(title, detail, meta)
        return None

    # ── 포맷 함수들 ──────────────────────────────────────────

    def _format_trade(self, title: str, meta: dict) -> dict:
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
        fields = []
        if meta.get("price"):
            fields.append({"name": "가격", "value": f"{meta['price']:,}", "inline": True})
        if meta.get("pnl_pct") is not None:
            pnl = meta["pnl_pct"]
            sign = "+" if pnl >= 0 else ""
            fields.append({"name": "PnL", "value": f"{sign}{pnl:.2f}%", "inline": True})
        if meta.get("reason"):
            fields.append({"name": "사유", "value": meta["reason"], "inline": False})
        return {"title": f"⚠️ {title}", "description": detail, "color": COLOR_ORANGE, "fields": fields}

    def _format_futures_stop(self, title: str, detail: str | None, meta: dict) -> dict:
        pnl = meta.get("pnl_pct", 0)
        color = COLOR_GREEN if pnl >= 0 else COLOR_RED
        icon = "🟢" if pnl >= 0 else "🔴"
        fields = []
        if meta.get("price"):
            v = meta["price"]
            fmt = f"{v:,.2f}" if v >= 10 else f"{v:,.4f}"
            fields.append({"name": "현재가", "value": f"{fmt} USDT", "inline": True})
        if meta.get("entry_price"):
            v = meta["entry_price"]
            fmt = f"{v:,.2f}" if v >= 10 else f"{v:,.4f}"
            fields.append({"name": "진입가", "value": f"{fmt} USDT", "inline": True})
        if meta.get("direction"):
            fields.append({"name": "방향", "value": meta["direction"].upper(), "inline": True})
        if pnl is not None:
            sign = "+" if pnl >= 0 else ""
            fields.append({"name": "PnL", "value": f"{sign}{pnl:.2f}%", "inline": True})
        if meta.get("leveraged_pnl_pct") is not None:
            lp = meta["leveraged_pnl_pct"]
            sign = "+" if lp >= 0 else ""
            fields.append({"name": "레버리지 PnL", "value": f"{sign}{lp:.2f}%", "inline": True})
        if meta.get("loss_amount") is not None:
            fields.append({"name": "손익 금액", "value": f"{meta['loss_amount']:,.2f} USDT", "inline": True})
        if meta.get("leverage"):
            fields.append({"name": "레버리지", "value": f"{meta['leverage']}x", "inline": True})
        if meta.get("reason"):
            fields.append({"name": "사유", "value": meta["reason"], "inline": False})
        return {"title": f"{icon} {title}", "description": detail, "color": color, "fields": fields}

    def _format_futures_trade(self, title: str, meta: dict) -> dict:
        is_long = "롱" in title or "Long" in title
        is_close = "청산" in title or "Close" in title
        pnl = meta.get("pnl_pct")
        if is_close:
            color = COLOR_GREEN if pnl and pnl > 0 else COLOR_RED
        elif is_long:
            color = COLOR_GREEN
        else:
            color = COLOR_RED
        fields = []
        if meta.get("price"):
            v = meta["price"]
            fmt = f"{v:,.2f}" if v >= 10 else f"{v:,.4f}"
            fields.append({"name": "가격", "value": f"{fmt} USDT", "inline": True})
        if meta.get("entry_price"):
            v = meta["entry_price"]
            fmt = f"{v:,.2f}" if v >= 10 else f"{v:,.4f}"
            fields.append({"name": "진입가", "value": f"{fmt} USDT", "inline": True})
        if meta.get("direction"):
            fields.append({"name": "방향", "value": meta["direction"].upper(), "inline": True})
        if meta.get("strategy"):
            fields.append({"name": "전략", "value": meta["strategy"], "inline": True})
        if meta.get("confidence"):
            fields.append({"name": "신뢰도", "value": f"{meta['confidence']:.0%}", "inline": True})
        if pnl is not None:
            sign = "+" if pnl >= 0 else ""
            fields.append({"name": "PnL", "value": f"{sign}{pnl:.2f}%", "inline": True})
        if meta.get("leveraged_pnl_pct") is not None:
            lp = meta["leveraged_pnl_pct"]
            sign = "+" if lp >= 0 else ""
            fields.append({"name": "레버리지 PnL", "value": f"{sign}{lp:.2f}%", "inline": True})
        if meta.get("loss_amount") is not None:
            la = meta["loss_amount"]
            sign = "+" if la >= 0 else ""
            fields.append({"name": "손익 금액", "value": f"{sign}{la:,.2f} USDT", "inline": True})
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
        if meta.get("reason"):
            fields.append({"name": "사유", "value": meta["reason"], "inline": False})
        return {"title": title, "color": color, "fields": fields}

    def _format_rotation(self, title: str, meta: dict) -> dict:
        fields = []
        if meta.get("price"):
            fields.append({"name": "가격", "value": f"{meta['price']:,.0f}", "inline": True})
        if meta.get("surge_ratio"):
            fields.append({"name": "서지", "value": f"{meta['surge_ratio']:.1f}x", "inline": True})
        if meta.get("amount_krw"):
            fields.append({"name": "금액", "value": f"{meta['amount_krw']:,.0f} KRW", "inline": True})
        return {"title": f"🚀 {title}", "color": COLOR_GOLD, "fields": fields}

    def _format_risk(self, title: str, detail: str | None, meta: dict) -> dict:
        fields = []
        if meta.get("drawdown_pct"):
            fields.append({"name": "드로다운", "value": f"{meta['drawdown_pct']:.2f}%", "inline": True})
        if meta.get("daily_loss_pct"):
            fields.append({"name": "일일 손실", "value": f"{meta['daily_loss_pct']:.2f}%", "inline": True})
        return {"title": f"🚨 {title}", "description": detail, "color": COLOR_ORANGE, "fields": fields}

    def _format_system(self, title: str, detail: str | None, meta: dict) -> dict:
        is_start = "시작" in title
        icon = "🚀" if is_start else "🛑"
        fields = []
        if meta.get("spot_coins"):
            fields.append({"name": "현물 추적", "value": ", ".join(meta["spot_coins"]), "inline": False})
        if meta.get("futures_coins"):
            fields.append({"name": "선물 추적", "value": ", ".join(meta["futures_coins"]), "inline": False})
        if meta.get("positions_summary"):
            fields.append({"name": "포지션", "value": meta["positions_summary"], "inline": False})
        return {"title": f"{icon} {title}", "description": detail, "color": COLOR_BLUE, "fields": fields}

    def _format_engine_lifecycle(self, title: str, detail: str | None, meta: dict) -> dict:
        is_start = "시작" in title
        icon = "▶️" if is_start else "⏹️"
        fields = []
        if meta.get("exchange"):
            fields.append({"name": "거래소", "value": meta["exchange"], "inline": True})
        if meta.get("mode"):
            fields.append({"name": "모드", "value": meta["mode"], "inline": True})
        return {"title": f"{icon} {title}", "description": detail, "color": COLOR_BLUE, "fields": fields}

    def _format_engine_error(self, level: str, title: str, detail: str | None, meta: dict) -> dict:
        color = COLOR_RED if level == "critical" else COLOR_ORANGE
        icon = "🚨" if level == "critical" else "⚠️"
        fields = []
        if meta.get("symbol"):
            fields.append({"name": "심볼", "value": meta["symbol"], "inline": True})
        if meta.get("exchange"):
            fields.append({"name": "거래소", "value": meta["exchange"], "inline": True})
        if meta.get("consecutive_errors"):
            fields.append({"name": "연속 실패", "value": str(meta["consecutive_errors"]), "inline": True})
        if meta.get("reason"):
            fields.append({"name": "사유", "value": meta["reason"], "inline": False})
        if meta.get("confidence"):
            fields.append({"name": "신뢰도", "value": f"{meta['confidence']:.0%}", "inline": True})
        desc = detail[:500] if detail else None
        return {"title": f"{icon} {title}", "description": desc, "color": color, "fields": fields}

    def _format_signal(self, title: str, detail: str | None, meta: dict) -> dict:
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
        return {"title": f"📊 {title}", "description": detail, "color": COLOR_PURPLE, "fields": fields}

    def _format_daily_summary(self, title: str, detail: str | None, meta: dict) -> dict:
        is_usdt = "binance" in meta.get("exchange", "")

        def _fmt(n: float) -> str:
            return f"{n:,.2f} USDT" if is_usdt else f"{n:,.0f} ₩"

        def _pct(n: float) -> str:
            return f"{'+' if n >= 0 else ''}{n:.2f}%"

        fields = []
        if meta.get("total_value") is not None:
            fields.append({"name": "총 자산", "value": _fmt(meta["total_value"]), "inline": True})
        if meta.get("return_pct") is not None:
            fields.append({"name": "원금 대비", "value": _pct(meta["return_pct"]), "inline": True})
        if meta.get("drawdown_pct") is not None:
            dd = meta["drawdown_pct"]
            fields.append({"name": "고점 대비", "value": f"-{dd:.2f}%" if dd > 0 else "0%", "inline": True})
        if meta.get("realized_pnl") is not None:
            fields.append({"name": "실현 손익", "value": _fmt(meta["realized_pnl"]), "inline": True})
        if meta.get("unrealized_pnl") is not None:
            fields.append({"name": "미실현 손익", "value": _fmt(meta["unrealized_pnl"]), "inline": True})
        if meta.get("total_fees") is not None:
            fields.append({"name": "수수료", "value": _fmt(meta["total_fees"]), "inline": True})

        review = meta.get("review")
        if review:
            trade_line = f"{review['total_trades']}건 (매수 {review['buy_count']} / 매도 {review['sell_count']})"
            fields.append({"name": "24h 거래", "value": trade_line, "inline": True})
            if review.get("win_rate") is not None:
                wr = review["win_rate"] * 100
                fields.append({"name": "승률", "value": f"{wr:.0f}% ({review['win_count']}승 {review['loss_count']}패)", "inline": True})
            if review.get("profit_factor") is not None:
                fields.append({"name": "Profit Factor", "value": f"{review['profit_factor']:.2f}x", "inline": True})

            by_strat = review.get("by_strategy", {})
            if by_strat:
                lines = []
                for name, stats in sorted(by_strat.items(), key=lambda x: x[1]["total_pnl"], reverse=True):
                    pnl_str = _fmt(stats["total_pnl"])
                    wr_str = f"{stats['win_rate'] * 100:.0f}%"
                    lines.append(f"**{name.replace('_', ' ')}** {stats['trades']}건 {wr_str} {pnl_str}")
                if lines:
                    fields.append({"name": "전략별 성과", "value": "\n".join(lines[:6]), "inline": False})

            by_sym = review.get("by_symbol", {})
            if by_sym:
                sorted_syms = sorted(by_sym.items(), key=lambda x: x[1].get("total_pnl", 0), reverse=True)
                sym_lines = []
                for sym, stats in sorted_syms[:3]:
                    coin = sym.split("/")[0]
                    pnl = stats.get("total_pnl", 0)
                    wr = stats.get("win_rate", 0) * 100
                    cnt = stats.get("trades", 0)
                    sym_lines.append(f"**{coin}** {cnt}건 {wr:.0f}% {_fmt(pnl)}")
                if sym_lines:
                    fields.append({"name": "코인별 성과", "value": "\n".join(sym_lines), "inline": False})

            largest_win = review.get("largest_win", 0)
            largest_loss = review.get("largest_loss", 0)
            if largest_win > 0 or largest_loss < 0:
                wl_parts = []
                if largest_win > 0:
                    wl_parts.append(f"최대 수익: {_fmt(largest_win)}")
                if largest_loss < 0:
                    wl_parts.append(f"최대 손실: {_fmt(largest_loss)}")
                fields.append({"name": "최대 거래", "value": " | ".join(wl_parts), "inline": False})

            insights = review.get("insights", [])
            if insights:
                fields.append({"name": "인사이트", "value": "\n".join(f"• {s}" for s in insights[:3]), "inline": False})

            recs = review.get("recommendations", [])
            if recs:
                fields.append({"name": "추천", "value": "\n".join(f"• {s}" for s in recs[:2]), "inline": False})
        else:
            if meta.get("positions"):
                fields.append({"name": "포지션", "value": str(meta["positions"]), "inline": True})
            if meta.get("trades_today"):
                fields.append({"name": "금일 거래", "value": str(meta["trades_today"]), "inline": True})

        return {"title": f"📋 {title}", "description": detail, "color": COLOR_GOLD, "fields": fields}

    def _format_health(self, level: str, title: str, detail: str | None, meta: dict) -> dict:
        color = COLOR_RED if level == "critical" else COLOR_TEAL
        icon = "🚨" if level == "critical" else "🏥"
        fields = []
        if meta.get("exchange"):
            fields.append({"name": "거래소", "value": meta["exchange"], "inline": True})
        if meta.get("issues"):
            fields.append({"name": "이상 항목", "value": ", ".join(meta["issues"]), "inline": False})
        if meta.get("auto_fixed"):
            fields.append({"name": "자동 수정", "value": ", ".join(meta["auto_fixed"]), "inline": False})
        if meta.get("fail_streak"):
            fields.append({"name": "연속 실패", "value": str(meta["fail_streak"]), "inline": True})
        desc = detail[:500] if detail else None
        return {"title": f"{icon} {title}", "description": desc, "color": color, "fields": fields}

    def _format_recovery(self, title: str, detail: str | None, meta: dict) -> dict:
        fields = []
        if meta.get("exchange"):
            fields.append({"name": "거래소", "value": meta["exchange"], "inline": True})
        if meta.get("action"):
            fields.append({"name": "액션", "value": meta["action"], "inline": True})
        if meta.get("symbol"):
            fields.append({"name": "심볼", "value": meta["symbol"], "inline": True})
        if meta.get("old_cash") is not None and meta.get("new_cash") is not None:
            fields.append({"name": "잔고 변화", "value": f"{meta['old_cash']:.2f} → {meta['new_cash']:.2f}", "inline": True})
        desc = detail[:500] if detail else None
        return {"title": f"🔧 {title}", "description": desc, "color": COLOR_CYAN, "fields": fields}

    # ── 전송 ───────────────────────────────────────────────────

    async def _send_embed(self, embed: dict) -> None:
        payload = {"embeds": [embed]}
        try:
            resp = await self._client.post(self._webhook_url, json=payload)
            if resp.status_code == 429:
                try:
                    retry_after = resp.json().get("retry_after", 5)
                except Exception:
                    retry_after = 5
                logger.warning("discord_rate_limited_429", retry_after=retry_after)
                await asyncio.sleep(retry_after)
                try:
                    await self._client.post(self._webhook_url, json=payload)
                except Exception as e2:
                    logger.warning("discord_retry_failed", error=str(e2))
            elif resp.status_code not in (200, 204):
                logger.warning("discord_webhook_failed", status=resp.status_code)
        except Exception as e:
            logger.warning("discord_send_error", error=str(e))
