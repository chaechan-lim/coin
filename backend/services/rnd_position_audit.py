"""
R&D 포지션 감사 서비스.

목적: 거래소 실제 포지션과 R&D 엔진 메모리(DB 기반) 포지션을 주기적으로 비교.
- 거래소에만 있고 엔진이 모르는 포지션 → "고아 포지션" → critical 알림
- 엔진은 알지만 거래소에 없는 포지션 → DB 부정합 → warning 알림

자동 청산은 하지 않음 (사용자 판단 필요). 알림으로만 보고.
"""
from __future__ import annotations
from dataclasses import dataclass

import structlog

from core.event_bus import emit_event
from api.dependencies import EngineRegistry

logger = structlog.get_logger(__name__)


@dataclass
class PositionDelta:
    symbol: str
    position_side: str       # "LONG" or "SHORT" (Hedge mode)
    exchange_qty: float       # 거래소 실제 수량
    engine_qty: float         # 엔진 메모리 합산 수량
    diff_qty: float           # exchange - engine (양수면 고아)
    engines_claiming: list[str]


# R&D 엔진 exchange 이름 — 이 엔진들이 관리하는 포지션만 정상으로 인정
RND_FUTURES_ENGINES = (
    "binance_donchian_futures", "binance_pairs", "binance_momentum",
    "binance_hmm", "binance_breakout_pb", "binance_vol_mom", "binance_btc_neutral",
)


def _collect_engine_positions(registry: EngineRegistry) -> dict[tuple[str, str], list[tuple[str, float]]]:
    """엔진별 보유 포지션을 (symbol, positionSide)로 정규화하여 합산.

    Returns:
        {(symbol, "LONG"|"SHORT"): [(engine_name, qty), ...]}
    """
    out: dict[tuple[str, str], list[tuple[str, float]]] = {}
    for ex in RND_FUTURES_ENGINES:
        eng = registry.get_engine(ex)
        if eng is None or not hasattr(eng, "get_status"):
            continue
        try:
            status = eng.get_status()
        except Exception as e:
            logger.warning("audit_get_status_failed", exchange=ex, error=str(e))
            continue

        positions: list[dict] = []
        # positions (list) 또는 position (단일)
        raw = status.get("positions") or []
        single = status.get("position")
        if single and isinstance(single, dict):
            raw = [single]
        for p in raw:
            if not isinstance(p, dict):
                continue
            # Pairs 는 pair_direction 으로 다리 분리 (symbol 키 없음)
            if "pair_direction" in p:
                coin_a = status.get("coin_a")
                coin_b = status.get("coin_b")
                pd_ = p.get("pair_direction", "long_a")
                side_a = "LONG" if "long_a" in pd_ else "SHORT"
                side_b = "SHORT" if "long_a" in pd_ else "LONG"
                qa = float(p.get("qty_a", 0) or 0)
                qb = float(p.get("qty_b", 0) or 0)
                if coin_a and qa > 0:
                    out.setdefault((coin_a, side_a), []).append((ex, qa))
                if coin_b and qb > 0:
                    out.setdefault((coin_b, side_b), []).append((ex, qb))
                continue

            # BTC-Neutral MR: alt + BTC 2 레그 분리 (alt_symbol/alt_side/alt_qty + btc_side/btc_qty)
            if "alt_symbol" in p:
                alt_sym = p.get("alt_symbol")
                alt_side = (p.get("alt_side") or "").lower()
                alt_qty = float(p.get("alt_qty", 0) or 0)
                btc_side = (p.get("btc_side") or "").lower()
                btc_qty = float(p.get("btc_qty", 0) or 0)
                alt_ps = "LONG" if alt_side == "long" else "SHORT" if alt_side == "short" else None
                btc_ps = "LONG" if btc_side == "long" else "SHORT" if btc_side == "short" else None
                if alt_sym and alt_ps and alt_qty > 0:
                    out.setdefault((alt_sym, alt_ps), []).append((ex, alt_qty))
                if btc_ps and btc_qty > 0:
                    out.setdefault(("BTC/USDT", btc_ps), []).append((ex, btc_qty))
                continue

            sym = p.get("symbol")
            if not sym:
                continue
            side = (p.get("side") or p.get("direction") or "").lower()
            qty = float(p.get("qty") or p.get("quantity") or 0)
            if qty <= 0:
                continue
            ps = "LONG" if side == "long" else "SHORT" if side == "short" else None
            if not ps:
                continue
            out.setdefault((sym, ps), []).append((ex, qty))
    return out


def _normalize_symbol(s: str) -> str:
    """거래소 심볼 'BTC/USDT:USDT' → 'BTC/USDT'."""
    return s.split(":")[0]


def diff_positions(
    exchange_positions: list[dict],
    engine_map: dict[tuple[str, str], list[tuple[str, float]]],
    tolerance: float = 1e-6,
) -> list[PositionDelta]:
    """거래소 포지션과 엔진 합산을 비교."""
    deltas: list[PositionDelta] = []
    seen: set[tuple[str, str]] = set()

    for p in exchange_positions:
        contracts = float(p.get("contracts") or 0)
        if contracts <= 0:
            continue
        sym = _normalize_symbol(p.get("symbol") or "")
        ps = (p.get("info") or {}).get("positionSide") or ""
        if not sym or ps not in ("LONG", "SHORT"):
            continue
        key = (sym, ps)
        seen.add(key)

        engine_legs = engine_map.get(key, [])
        engine_qty = sum(q for _, q in engine_legs)
        engines = [name for name, _ in engine_legs]
        diff = contracts - engine_qty
        if abs(diff) > tolerance:
            deltas.append(PositionDelta(
                symbol=sym, position_side=ps,
                exchange_qty=contracts, engine_qty=engine_qty,
                diff_qty=diff, engines_claiming=engines,
            ))

    # 엔진은 있는데 거래소에 없는 포지션
    for key, legs in engine_map.items():
        if key in seen:
            continue
        sym, ps = key
        engine_qty = sum(q for _, q in legs)
        if engine_qty <= tolerance:
            continue
        deltas.append(PositionDelta(
            symbol=sym, position_side=ps,
            exchange_qty=0.0, engine_qty=engine_qty,
            diff_qty=-engine_qty,
            engines_claiming=[name for name, _ in legs],
        ))

    return deltas


async def run_position_audit(registry: EngineRegistry, exchange) -> list[PositionDelta]:
    """포지션 감사 1회 실행. 불일치 발견 시 emit_event 알림.

    exchange는 BinanceUSDMAdapter 또는 ccxt 인스턴스. adapter일 경우
    내부 ccxt._exchange로 fallback.
    """
    if exchange is None:
        return []
    try:
        # ccxt 직접 노출 (binance_usdm_adapter는 _exchange 속성)
        ccxt_ex = getattr(exchange, "_exchange", None) or exchange
        positions = await ccxt_ex.fetch_positions()
    except Exception as e:
        logger.warning("audit_fetch_positions_failed", error=str(e))
        return []

    engine_map = _collect_engine_positions(registry)
    deltas = diff_positions(positions, engine_map)

    if not deltas:
        logger.info("rnd_position_audit_clean")
        return []

    # 알림: 거래소에만 있는 (고아) vs 엔진에만 있는 (DB 부정합)
    orphan_deltas = [d for d in deltas if d.diff_qty > 0]
    missing_deltas = [d for d in deltas if d.diff_qty < 0]

    if orphan_deltas:
        lines = [
            f"{d.symbol} {d.position_side} 거래소 {d.exchange_qty} / 엔진 {d.engine_qty} (차이 +{d.diff_qty:.6f})"
            for d in orphan_deltas[:10]
        ]
        await emit_event(
            "critical", "balance_guard",
            f"R&D 고아 포지션 {len(orphan_deltas)}개 감지",
            detail="거래소에만 있는 포지션. 수동 확인 필요.\n" + "\n".join(lines),
            metadata={
                "orphan_count": len(orphan_deltas),
                "missing_count": len(missing_deltas),
                "orphan_symbols": [f"{d.symbol}:{d.position_side}" for d in orphan_deltas],
            },
        )

    if missing_deltas:
        lines = [
            f"{d.symbol} {d.position_side} 엔진 {d.engine_qty} / 거래소 0 (engines: {', '.join(d.engines_claiming)})"
            for d in missing_deltas[:10]
        ]
        await emit_event(
            "warning", "balance_guard",
            f"R&D 엔진 포지션 부정합 {len(missing_deltas)}개",
            detail="엔진은 보유로 기록하지만 거래소엔 없음. DB 보정 필요.\n" + "\n".join(lines),
            metadata={
                "missing_count": len(missing_deltas),
                "missing_symbols": [f"{d.symbol}:{d.position_side}" for d in missing_deltas],
            },
        )

    logger.warning("rnd_position_audit_mismatch",
                   orphan=len(orphan_deltas), missing=len(missing_deltas))
    return deltas
