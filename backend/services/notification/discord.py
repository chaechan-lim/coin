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
        if category == "strategy" and level == "info":
            return self._format_strategy(title, detail, meta)
        if category == "balance_guard":
            return self._format_balance_guard(level, title, detail, meta)
        if category == "surge_trade" and level == "info":
            return self._format_surge_trade(title, detail, meta)
        if category == "safe_order" and level == "critical":
            return self._format_safe_order(title, detail, meta)
        if category in ("rnd_trade", "donchian_futures_trade", "pairs_trade") and level == "info":
            return self._format_rnd_trade(title, detail, meta)
        if category in ("rnd_trade", "donchian_futures_trade", "pairs_trade") and level in ("warning", "error"):
            return self._format_rnd_trade_warning(title, detail, meta)
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

    def _format_strategy(self, title: str, detail: str | None, meta: dict) -> dict:
        """AI 에이전트 전략 분석 알림 (시장 분석, 매매 회고, 성과 분석, 전략 어드바이저)."""
        # 매매 회고 — 풍부한 컨텍스트 포함
        if meta.get("review_kind") == "trade_review":
            return self._format_trade_review(title, meta)
        if meta.get("review_kind") == "performance_analytics":
            return self._format_performance_analytics(title, meta)
        if meta.get("review_kind") == "strategy_advisor":
            return self._format_strategy_advisor(title, meta)

        fields = []
        if meta.get("state"):
            fields.append({"name": "시장 상태", "value": meta["state"], "inline": True})
        if meta.get("confidence") is not None:
            fields.append({"name": "신뢰도", "value": f"{meta['confidence']:.0%}", "inline": True})
        if meta.get("exchange"):
            fields.append({"name": "거래소", "value": meta["exchange"], "inline": True})
        if meta.get("total_trades") is not None:
            fields.append({"name": "거래 수", "value": str(meta["total_trades"]), "inline": True})
        if meta.get("win_rate") is not None:
            fields.append({"name": "승률", "value": f"{meta['win_rate']:.0%}", "inline": True})
        if meta.get("pnl") is not None:
            pnl = meta["pnl"]
            sign = "+" if pnl >= 0 else ""
            fields.append({"name": "실현 손익", "value": f"{sign}{pnl:,.0f}", "inline": True})
        # 레짐 변경 필드
        if meta.get("prev_regime") and meta.get("new_regime"):
            fields.append({"name": "이전 레짐", "value": meta["prev_regime"], "inline": True})
            fields.append({"name": "새 레짐", "value": meta["new_regime"], "inline": True})
        if meta.get("adx") is not None:
            fields.append({"name": "ADX", "value": f"{meta['adx']:.1f}", "inline": True})
        desc = detail[:500] if detail else None
        return {"title": f"🧠 {title}", "description": desc, "color": COLOR_PURPLE, "fields": fields}

    def _format_trade_review(self, title: str, meta: dict) -> dict:
        """매매 회고 — insights/recommendations/strategy/symbol 분석 포함."""
        is_usdt = "binance" in meta.get("exchange", "")

        def _fmt(n: float) -> str:
            return f"{n:+,.2f} USDT" if is_usdt else f"{n:+,.0f} ₩"

        total_trades = meta.get("total_trades", 0)
        pnl = meta.get("pnl", 0)
        color = COLOR_GREEN if pnl > 0 else COLOR_RED if pnl < 0 else COLOR_PURPLE

        fields = []
        # 핵심 지표
        wr = meta.get("win_rate", 0) * 100
        wins = meta.get("win_count", 0)
        losses = meta.get("loss_count", 0)
        fields.append({
            "name": "거래 / 승률",
            "value": f"{total_trades}건 (매수 {meta.get('buy_count',0)} / 매도 {meta.get('sell_count',0)})\n승률 {wr:.0f}% ({wins}승 {losses}패)",
            "inline": True,
        })
        pf = meta.get("profit_factor", 0)
        pf_str = f"{pf:.2f}x" if pf < 99 else "∞ (무손실)"
        fields.append({
            "name": "수익 지표",
            "value": f"PnL {_fmt(pnl)}\nProfit Factor {pf_str}",
            "inline": True,
        })

        # 최대 거래
        lw = meta.get("largest_win", 0)
        ll = meta.get("largest_loss", 0)
        if lw or ll:
            fields.append({
                "name": "극단치",
                "value": f"최대 수익 {_fmt(lw)}\n최대 손실 {_fmt(ll)}",
                "inline": True,
            })

        # 전략별
        by_strat = meta.get("by_strategy") or {}
        if by_strat:
            lines = []
            for name, st in sorted(by_strat.items(), key=lambda x: x[1].get("total_pnl", 0), reverse=True):
                t = st.get("trades", 0)
                w = st.get("win_rate", 0) * 100
                p = st.get("total_pnl", 0)
                lines.append(f"**{name}** {t}건 {w:.0f}% {_fmt(p)}")
            if lines:
                fields.append({"name": "전략별", "value": "\n".join(lines[:6]), "inline": False})

        # 코인별 (상위 5)
        by_sym = meta.get("by_symbol") or {}
        if by_sym:
            sorted_syms = sorted(by_sym.items(), key=lambda x: abs(x[1].get("total_pnl", 0)), reverse=True)
            sym_lines = []
            for sym, st in sorted_syms[:5]:
                coin = sym.split("/")[0]
                t = st.get("trades", 0)
                w = st.get("win_rate", 0) * 100
                p = st.get("total_pnl", 0)
                sym_lines.append(f"**{coin}** {t}건 {w:.0f}% {_fmt(p)}")
            if sym_lines:
                fields.append({"name": "코인별 (상위 5)", "value": "\n".join(sym_lines), "inline": False})

        # 보유 포지션
        open_pos = meta.get("open_positions") or []
        if open_pos:
            pos_lines = []
            for p in open_pos[:5]:
                sym = (p.get("symbol") or "").split("/")[0]
                side = p.get("direction", "long")
                upnl = p.get("unrealized_pnl", 0)
                upct = p.get("unrealized_pnl_pct", 0)
                pos_lines.append(f"**{sym}** {side} {_fmt(upnl)} ({upct:+.2f}%)")
            if pos_lines:
                fields.append({"name": "보유 포지션", "value": "\n".join(pos_lines), "inline": False})

        # 인사이트 (LLM 분석)
        insights = meta.get("insights") or []
        if insights:
            ins_text = "\n".join(f"• {s}" for s in insights[:3])
            if len(ins_text) > 1024:
                ins_text = ins_text[:1020] + "..."
            fields.append({"name": "인사이트", "value": ins_text, "inline": False})

        # 추천
        recs = meta.get("recommendations") or []
        if recs:
            rec_text = "\n".join(f"• {s}" for s in recs[:3])
            if len(rec_text) > 1024:
                rec_text = rec_text[:1020] + "..."
            fields.append({"name": "추천", "value": rec_text, "inline": False})

        return {"title": f"🧠 {title}", "color": color, "fields": fields}

    def _format_performance_analytics(self, title: str, meta: dict) -> dict:
        """일일 성과 분석 — 7d/14d/30d 윈도우, 전략별 성과, 알림."""
        is_usdt = "binance" in meta.get("exchange", "")

        def _fmt(n: float) -> str:
            return f"{n:+,.2f} USDT" if is_usdt else f"{n:+,.0f} ₩"

        fields = []
        windows = meta.get("windows") or {}
        # 윈도우 비교 (7d/14d/30d)
        if windows:
            lines = []
            for key in ["7d", "14d", "30d"]:
                w = windows.get(key)
                if not w or w.get("total_trades", 0) == 0:
                    continue
                wr = (w.get("win_rate", 0) or 0) * 100
                pf = w.get("profit_factor", 0) or 0
                pf_str = f"{pf:.2f}x" if pf < 99 else "∞"
                lines.append(
                    f"**{key}** {w.get('total_trades',0)}건 · 승률 {wr:.0f}% · PF {pf_str} · {_fmt(w.get('total_pnl',0))}"
                )
            if lines:
                fields.append({"name": "기간별 성과", "value": "\n".join(lines), "inline": False})

        # 30d 추가 디테일
        w30 = windows.get("30d") or {}
        if w30 and w30.get("total_trades", 0) > 0:
            extras = []
            if w30.get("largest_win") is not None:
                extras.append(f"최대 수익 {_fmt(w30['largest_win'])}")
            if w30.get("largest_loss") is not None:
                extras.append(f"최대 손실 {_fmt(w30['largest_loss'])}")
            if extras:
                fields.append({"name": "30일 극단치", "value": " · ".join(extras), "inline": False})

        # 전략별 (PnL 기준 상위)
        by_strat = meta.get("by_strategy") or {}
        if by_strat:
            sorted_strats = sorted(by_strat.items(), key=lambda x: x[1].get("pnl_30d", 0), reverse=True)
            lines = []
            for name, st in sorted_strats[:6]:
                t30 = st.get("trades_30d", 0)
                w30v = (st.get("win_rate_30d", 0) or 0) * 100
                p30 = st.get("pnl_30d", 0) or 0
                contrib = st.get("pnl_contribution_pct", 0) or 0
                trend = st.get("trend", "stable")
                trend_emoji = "📈" if trend == "improving" else "📉" if trend == "declining" else "➡️"
                lines.append(f"{trend_emoji} **{name}** {t30}건 {w30v:.0f}% {_fmt(p30)} ({contrib:+.1f}%)")
            if lines:
                fields.append({"name": "전략별 30일", "value": "\n".join(lines), "inline": False})

        # 코인별 (PnL 기준 상위)
        by_sym = meta.get("by_symbol") or {}
        if by_sym:
            sorted_syms = sorted(by_sym.items(), key=lambda x: abs(x[1].get("pnl_30d", 0)), reverse=True)
            lines = []
            for sym, st in sorted_syms[:5]:
                coin = sym.split("/")[0]
                t = st.get("trades_30d", 0)
                wr = (st.get("win_rate_30d", 0) or 0) * 100
                p = st.get("pnl_30d", 0)
                cl = st.get("consecutive_losses", 0)
                cl_str = f" 🚨연패{cl}" if cl >= 3 else ""
                lines.append(f"**{coin}** {t}건 {wr:.0f}% {_fmt(p)}{cl_str}")
            if lines:
                fields.append({"name": "코인별 30일 (상위 5)", "value": "\n".join(lines), "inline": False})

        # 성과 저하 알림
        alerts = meta.get("degradation_alerts") or []
        if alerts:
            lines = [f"⚠️ {a}" for a in alerts[:5]]
            fields.append({"name": "성과 저하 경고", "value": "\n".join(lines), "inline": False})

        # 인사이트 + 추천
        insights = meta.get("insights") or []
        if insights:
            ins_text = "\n".join(f"• {s}" for s in insights[:3])
            if len(ins_text) > 1024:
                ins_text = ins_text[:1020] + "..."
            fields.append({"name": "인사이트", "value": ins_text, "inline": False})
        recs = meta.get("recommendations") or []
        if recs:
            rec_text = "\n".join(f"• {s}" for s in recs[:3])
            if len(rec_text) > 1024:
                rec_text = rec_text[:1020] + "..."
            fields.append({"name": "추천", "value": rec_text, "inline": False})

        # 색상: 30d 수익률 기준
        pnl_30d = (windows.get("30d") or {}).get("total_pnl", 0) or 0
        color = COLOR_GREEN if pnl_30d > 0 else COLOR_RED if pnl_30d < 0 else COLOR_GOLD
        return {"title": f"📊 {title}", "color": color, "fields": fields}

    def _format_strategy_advisor(self, title: str, meta: dict) -> dict:
        """주간 전략 어드바이저 — 청산/방향/파라미터 분석 + LLM 제안."""
        fields = []

        # 분석 요약
        summary = meta.get("analysis_summary")
        if summary:
            text = summary[:1024]
            fields.append({"name": "분석 요약", "value": text, "inline": False})

        # 청산 분석
        exit_a = meta.get("exit_analysis") or {}
        if exit_a:
            lines = []
            for k, v in list(exit_a.items())[:6]:
                if isinstance(v, (int, float)):
                    lines.append(f"**{k}**: {v:.2f}" if isinstance(v, float) else f"**{k}**: {v}")
                else:
                    lines.append(f"**{k}**: {str(v)[:80]}")
            if lines:
                fields.append({"name": "청산 분석", "value": "\n".join(lines), "inline": False})

        # 방향 분석 (long/short)
        dir_a = meta.get("direction_analysis") or {}
        if dir_a:
            lines = []
            for direction in ["long", "short"]:
                d = dir_a.get(direction)
                if d and isinstance(d, dict):
                    t = d.get("trades", 0)
                    wr = (d.get("win_rate", 0) or 0) * 100
                    pnl = d.get("total_pnl", 0) or 0
                    sign = "+" if pnl >= 0 else ""
                    lines.append(f"**{direction.upper()}** {t}건 · 승률 {wr:.0f}% · {sign}{pnl:.2f}")
            if lines:
                fields.append({"name": "방향별 성과", "value": "\n".join(lines), "inline": False})

        # 파라미터 민감도 (top 3)
        params = meta.get("param_sensitivities") or []
        if params:
            lines = []
            for p in params[:3]:
                name = p.get("param_name", "?")
                current = p.get("current_value")
                opt = p.get("optimal_value")
                impact = p.get("expected_pnl_change", 0) or 0
                lines.append(f"**{name}** 현재 {current} → 권장 {opt} (예상 영향 {impact:+.2f})")
            if lines:
                fields.append({"name": "파라미터 민감도", "value": "\n".join(lines), "inline": False})

        # LLM 제안 (핵심)
        suggestions = meta.get("suggestions") or []
        if suggestions:
            sug_text = "\n".join(f"• {s}" for s in suggestions[:5])
            if len(sug_text) > 1024:
                sug_text = sug_text[:1020] + "..."
            fields.append({"name": "💡 제안", "value": sug_text, "inline": False})

        return {"title": f"🎯 {title}", "color": COLOR_PURPLE, "fields": fields}

    def _format_balance_guard(self, level: str, title: str, detail: str | None, meta: dict) -> dict:
        """잔고 무결성 감시 알림 (잔고 괴리 경고, 자동 재동기화, 자동 재개)."""
        if level == "critical":
            color = COLOR_RED
            icon = "🚨"
        elif level == "warning":
            color = COLOR_ORANGE
            icon = "⚠️"
        else:
            color = COLOR_GREEN
            icon = "✅"
        fields = []
        if meta.get("divergence_pct") is not None:
            fields.append({"name": "괴리율", "value": f"{meta['divergence_pct']:.2f}%", "inline": True})
        if meta.get("exchange_balance") is not None:
            fields.append({"name": "거래소 잔고", "value": f"{meta['exchange_balance']:,.4f} USDT", "inline": True})
        if meta.get("resync_count") is not None:
            fields.append({"name": "재동기화 횟수", "value": str(meta["resync_count"]), "inline": True})
        desc = detail[:500] if detail else None
        return {"title": f"{icon} {title}", "description": desc, "color": color, "fields": fields}

    def _format_surge_trade(self, title: str, detail: str | None, meta: dict) -> dict:
        """서지 매매 알림 (진입/청산)."""
        is_close = "CLOSED" in title or "청산" in title
        if is_close:
            pnl_pct = meta.get("pnl_pct")
            color = (COLOR_GREEN if pnl_pct is not None and pnl_pct >= 0 else COLOR_RED)
        elif "LONG" in title:
            color = COLOR_GREEN
        elif "SHORT" in title:
            color = COLOR_RED
        else:
            color = COLOR_BLUE
        fields = []
        if meta.get("symbol"):
            fields.append({"name": "심볼", "value": meta["symbol"], "inline": True})
        if meta.get("direction"):
            fields.append({"name": "방향", "value": meta["direction"].upper(), "inline": True})
        if meta.get("price") is not None:
            v = meta["price"]
            fmt = f"{v:,.2f}" if v >= 10 else f"{v:,.4f}"
            fields.append({"name": "가격", "value": f"{fmt} USDT", "inline": True})
        if meta.get("score") is not None:
            fields.append({"name": "서지 점수", "value": f"{meta['score']:.3f}", "inline": True})
        if meta.get("size_usdt") is not None:
            fields.append({"name": "포지션", "value": f"{meta['size_usdt']:.1f} USDT", "inline": True})
        if meta.get("leverage") is not None:
            fields.append({"name": "레버리지", "value": f"{meta['leverage']}x", "inline": True})
        if meta.get("pnl_pct") is not None:
            pnl = meta["pnl_pct"]
            sign = "+" if pnl >= 0 else ""
            fields.append({"name": "PnL", "value": f"{sign}{pnl:.1f}%", "inline": True})
        if meta.get("pnl_usdt") is not None:
            p = meta["pnl_usdt"]
            sign = "+" if p >= 0 else ""
            fields.append({"name": "손익", "value": f"{sign}{p:.2f} USDT", "inline": True})
        if meta.get("reason"):
            fields.append({"name": "사유", "value": meta["reason"], "inline": True})
        if meta.get("hold_min") is not None:
            fields.append({"name": "보유 시간", "value": f"{meta['hold_min']:.0f}분", "inline": True})
        return {"title": f"⚡ {title}", "description": detail, "color": color, "fields": fields}

    def _format_safe_order(self, title: str, detail: str | None, meta: dict) -> dict:
        """SafeOrder 치명적 오류 알림 (거래소 실행 후 DB 기록 실패)."""
        fields = []
        if meta.get("symbol"):
            fields.append({"name": "심볼", "value": meta["symbol"], "inline": True})
        if meta.get("side"):
            fields.append({"name": "방향", "value": meta["side"].upper(), "inline": True})
        desc = detail[:500] if detail else None
        return {"title": f"🚨 {title}", "description": desc, "color": COLOR_RED, "fields": fields}

    def _format_rnd_trade(self, title: str, detail: str | None, meta: dict) -> dict:
        """R&D 엔진 거래 알림 (진입/청산/리밸런싱)."""
        is_close = any(k in title for k in ("청산", "exit", "close", "매도"))
        is_short = any(k in title for k in ("숏", "short", "SHORT"))
        pnl = meta.get("pnl_pct") or meta.get("realized_pnl")
        if is_close and pnl is not None:
            color = COLOR_GREEN if pnl >= 0 else COLOR_RED
        elif is_short:
            color = COLOR_RED
        else:
            color = COLOR_GREEN
        fields = []
        if meta.get("symbol"):
            fields.append({"name": "심볼", "value": meta["symbol"], "inline": True})
        if meta.get("direction"):
            fields.append({"name": "방향", "value": meta["direction"].upper(), "inline": True})
        if meta.get("engine"):
            fields.append({"name": "엔진", "value": meta["engine"], "inline": True})
        if meta.get("price") is not None:
            v = meta["price"]
            fmt = f"{v:,.2f}" if v >= 10 else f"{v:,.4f}"
            fields.append({"name": "가격", "value": f"{fmt} USDT", "inline": True})
        if meta.get("entry_price") is not None:
            v = meta["entry_price"]
            fmt = f"{v:,.2f}" if v >= 10 else f"{v:,.4f}"
            fields.append({"name": "진입가", "value": f"{fmt} USDT", "inline": True})
        if meta.get("quantity") is not None:
            fields.append({"name": "수량", "value": f"{meta['quantity']:.6f}", "inline": True})
        if meta.get("leverage"):
            fields.append({"name": "레버리지", "value": f"{meta['leverage']}x", "inline": True})
        if meta.get("pnl_pct") is not None:
            p = meta["pnl_pct"]
            sign = "+" if p >= 0 else ""
            fields.append({"name": "PnL", "value": f"{sign}{p:.2f}%", "inline": True})
        if meta.get("realized_pnl") is not None:
            p = meta["realized_pnl"]
            sign = "+" if p >= 0 else ""
            fields.append({"name": "손익", "value": f"{sign}{p:.2f} USDT", "inline": True})
        if meta.get("reason"):
            fields.append({"name": "사유", "value": meta["reason"], "inline": False})
        desc = detail[:500] if detail else None
        return {"title": f"🔬 {title}", "description": desc, "color": color, "fields": fields}

    def _format_rnd_trade_warning(self, title: str, detail: str | None, meta: dict) -> dict:
        """R&D 엔진 거래 경고 (손실 한도, 오류 등)."""
        fields = []
        if meta.get("engine"):
            fields.append({"name": "엔진", "value": meta["engine"], "inline": True})
        if meta.get("symbol"):
            fields.append({"name": "심볼", "value": meta["symbol"], "inline": True})
        if meta.get("reason"):
            fields.append({"name": "사유", "value": meta["reason"], "inline": False})
        desc = detail[:500] if detail else None
        return {"title": f"⚠️ {title}", "description": desc, "color": COLOR_ORANGE, "fields": fields}

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
