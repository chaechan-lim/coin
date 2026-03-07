"""일일 요약 이벤트 생성 — 스케줄러 잡."""
import structlog

logger = structlog.get_logger(__name__)


async def send_daily_summary(engine_registry) -> None:
    """일일 요약을 emit_event로 전송하는 스케줄러 잡.

    포트폴리오 + 매매 회고 데이터를 수집하여 daily_summary 이벤트 발행.
    실제 전송은 NotificationDispatcher → 각 어댑터가 담당.
    """
    from core.event_bus import emit_event
    from db.session import get_session_factory
    from config import get_config

    session_factory = get_session_factory()
    config = get_config()

    for exchange_name in engine_registry.available_exchanges:
        if exchange_name == "bithumb" and config.trading.mode != "live":
            continue
        pm = engine_registry.get_portfolio_manager(exchange_name)
        eng = engine_registry.get_engine(exchange_name)
        if not pm or not eng:
            continue

        try:
            is_usdt = "binance" in exchange_name
            currency = "USDT" if is_usdt else "KRW"

            async with session_factory() as session:
                summary = await pm.get_portfolio_summary(session)

            total_value = summary.get("total_value_krw", 0)
            initial = summary.get("initial_balance_krw", 0)
            return_pct = ((total_value - initial) / initial * 100) if initial > 0 else 0

            review_data = None
            coord = engine_registry.get_coordinator(exchange_name)
            if coord and coord.last_trade_review:
                r = coord.last_trade_review
                review_data = {
                    "total_trades": r.total_trades,
                    "buy_count": r.buy_count,
                    "sell_count": r.sell_count,
                    "win_count": r.win_count,
                    "loss_count": r.loss_count,
                    "win_rate": r.win_rate,
                    "profit_factor": r.profit_factor,
                    "by_strategy": r.by_strategy,
                    "by_symbol": getattr(r, "by_symbol", {}),
                    "largest_win": getattr(r, "largest_win", 0),
                    "largest_loss": getattr(r, "largest_loss", 0),
                    "insights": r.insights[:3] if r.insights else [],
                    "recommendations": r.recommendations[:2] if r.recommendations else [],
                }

            detail_parts = [f"총 자산: {total_value:,.2f} {currency}" if is_usdt
                            else f"총 자산: {total_value:,.0f} {currency}"]
            if return_pct != 0:
                sign = "+" if return_pct >= 0 else ""
                detail_parts.append(f"원금 대비 {sign}{return_pct:.2f}%")

            await emit_event(
                "info", "daily_summary",
                f"일일 요약 [{exchange_name}]",
                detail=" | ".join(detail_parts),
                metadata={
                    "exchange": exchange_name,
                    "total_value": round(total_value, 4 if is_usdt else 0),
                    "return_pct": round(return_pct, 2),
                    "realized_pnl": round(summary.get("realized_pnl", 0), 4 if is_usdt else 0),
                    "unrealized_pnl": round(summary.get("unrealized_pnl", 0), 4 if is_usdt else 0),
                    "total_fees": round(summary.get("total_fees", 0), 4 if is_usdt else 0),
                    "drawdown_pct": round(summary.get("drawdown_pct", 0), 2),
                    "positions": len(summary.get("positions", [])),
                    "cash": round(summary.get("cash_balance_krw", 0), 4 if is_usdt else 0),
                    "trades_today": getattr(eng, "_daily_trade_count", 0),
                    "review": review_data,
                },
            )
        except Exception as e:
            logger.warning("daily_summary_error", exchange=exchange_name, error=str(e))
