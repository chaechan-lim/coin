"""
R&D 포지션 감사 — 거래소 vs 엔진 메모리 비교 테스트.
"""
from unittest.mock import AsyncMock, MagicMock
import pytest

from services.rnd_position_audit import (
    diff_positions, _collect_engine_positions,
    PositionDelta, run_position_audit,
)


def _ex_pos(symbol: str, contracts: float, position_side: str):
    """Mock CCXT 거래소 포지션 dict."""
    return {
        "symbol": symbol,
        "contracts": contracts,
        "info": {"positionSide": position_side},
    }


def _engine_status(positions=None, position=None, coin_a=None, coin_b=None):
    out = {"is_running": True, "leverage": 2}
    if positions is not None:
        out["positions"] = positions
    if position is not None:
        out["position"] = position
    if coin_a:
        out["coin_a"] = coin_a
        out["coin_b"] = coin_b
    return out


def _registry(engines: dict[str, dict]):
    """exchange_name → status 매핑으로 mock registry 생성."""
    reg = MagicMock()
    def _get(name):
        if name not in engines:
            return None
        eng = MagicMock()
        eng.get_status = MagicMock(return_value=engines[name])
        return eng
    reg.get_engine = _get
    return reg


def test_diff_no_mismatch():
    """거래소와 엔진이 일치하면 deltas 비어있음."""
    ex_pos = [
        _ex_pos("BTC/USDT:USDT", 0.01, "LONG"),
        _ex_pos("ETH/USDT:USDT", 0.5, "SHORT"),
    ]
    engine_map = {
        ("BTC/USDT", "LONG"): [("binance_hmm", 0.01)],
        ("ETH/USDT", "SHORT"): [("binance_pairs", 0.5)],
    }
    deltas = diff_positions(ex_pos, engine_map)
    assert deltas == []


def test_diff_orphan_position():
    """거래소에만 있고 엔진엔 없으면 orphan delta."""
    ex_pos = [_ex_pos("LINK/USDT:USDT", 3.23, "LONG")]
    engine_map = {}
    deltas = diff_positions(ex_pos, engine_map)
    assert len(deltas) == 1
    assert deltas[0].symbol == "LINK/USDT"
    assert deltas[0].position_side == "LONG"
    assert deltas[0].diff_qty == 3.23


def test_diff_missing_position():
    """엔진은 알지만 거래소엔 없으면 missing delta."""
    ex_pos = []
    engine_map = {("BTC/USDT", "SHORT"): [("binance_hmm", 0.005)]}
    deltas = diff_positions(ex_pos, engine_map)
    assert len(deltas) == 1
    assert deltas[0].symbol == "BTC/USDT"
    assert deltas[0].diff_qty == -0.005


def test_diff_quantity_mismatch():
    """심볼/방향 일치하지만 수량 다르면 delta."""
    ex_pos = [_ex_pos("ATOM/USDT:USDT", 88.09, "LONG")]
    engine_map = {("ATOM/USDT", "LONG"): [("binance_momentum", 64.45)]}
    deltas = diff_positions(ex_pos, engine_map)
    assert len(deltas) == 1
    assert deltas[0].diff_qty == pytest.approx(23.64, abs=0.01)


def test_diff_multiple_engines_summed():
    """같은 심볼+방향을 여러 엔진이 보유 → 합산."""
    ex_pos = [_ex_pos("ETH/USDT:USDT", 0.3, "LONG")]
    engine_map = {
        ("ETH/USDT", "LONG"): [
            ("binance_hmm", 0.2),
            ("binance_pairs", 0.1),
        ]
    }
    deltas = diff_positions(ex_pos, engine_map)
    assert deltas == []  # 합산 0.3 = 거래소 0.3


def test_diff_hedge_mode_long_short_same_symbol():
    """Hedge mode: ETH LONG + ETH SHORT 별도 처리."""
    ex_pos = [
        _ex_pos("ETH/USDT:USDT", 0.1, "LONG"),
        _ex_pos("ETH/USDT:USDT", 0.2, "SHORT"),
    ]
    engine_map = {
        ("ETH/USDT", "LONG"): [("binance_pairs", 0.1)],
        ("ETH/USDT", "SHORT"): [("binance_hmm", 0.2)],
    }
    deltas = diff_positions(ex_pos, engine_map)
    assert deltas == []


def test_collect_engine_positions_pairs_trading():
    """Pairs 엔진의 pair_direction 을 LONG/SHORT 다리로 변환."""
    reg = _registry({
        "binance_pairs": _engine_status(
            position={
                "pair_direction": "short_a_long_b",
                "qty_a": 0.003,
                "qty_b": 0.123,
            },
            coin_a="BTC/USDT", coin_b="ETH/USDT",
        ),
    })
    em = _collect_engine_positions(reg)
    assert ("BTC/USDT", "SHORT") in em
    assert em[("BTC/USDT", "SHORT")][0][1] == 0.003
    assert ("ETH/USDT", "LONG") in em
    assert em[("ETH/USDT", "LONG")][0][1] == 0.123


def test_collect_engine_positions_pairs_long_a():
    """Pairs long_a → coin_a LONG, coin_b SHORT."""
    reg = _registry({
        "binance_pairs": _engine_status(
            position={
                "pair_direction": "long_a_short_b",
                "qty_a": 0.005,
                "qty_b": 0.05,
            },
            coin_a="BTC/USDT", coin_b="ETH/USDT",
        ),
    })
    em = _collect_engine_positions(reg)
    assert em[("BTC/USDT", "LONG")][0][1] == 0.005
    assert em[("ETH/USDT", "SHORT")][0][1] == 0.05


def test_collect_engine_positions_standard():
    """일반 엔진의 positions list (HMM, Momentum 등)."""
    reg = _registry({
        "binance_momentum": _engine_status(
            positions=[
                {"symbol": "ATOM/USDT", "side": "long", "qty": 64.45, "entry": 2.0},
                {"symbol": "DOT/USDT", "side": "short", "qty": 102.5, "entry": 1.27},
            ]
        ),
    })
    em = _collect_engine_positions(reg)
    assert em[("ATOM/USDT", "LONG")][0] == ("binance_momentum", 64.45)
    assert em[("DOT/USDT", "SHORT")][0] == ("binance_momentum", 102.5)


def test_collect_skips_zero_quantity():
    """수량 0 포지션은 무시."""
    reg = _registry({
        "binance_hmm": _engine_status(
            positions=[{"symbol": "BTC/USDT", "side": "long", "qty": 0}]
        ),
    })
    em = _collect_engine_positions(reg)
    assert em == {}


def test_collect_handles_missing_engines():
    """등록 안 된 엔진은 skip."""
    reg = _registry({})  # 모든 엔진이 None 반환
    em = _collect_engine_positions(reg)
    assert em == {}


@pytest.mark.asyncio
async def test_audit_emits_orphan_alert():
    """고아 포지션 감지 시 critical 알림 발송."""
    reg = _registry({})
    exchange = MagicMock()
    exchange.fetch_positions = AsyncMock(return_value=[
        _ex_pos("LINK/USDT:USDT", 3.23, "LONG"),
    ])
    from unittest.mock import patch
    with patch("services.rnd_position_audit.emit_event", new_callable=AsyncMock) as emit:
        deltas = await run_position_audit(reg, exchange)
    assert len(deltas) == 1
    emit.assert_called()
    # critical level + balance_guard category
    call = emit.call_args_list[0]
    assert call.args[0] == "critical"
    assert call.args[1] == "balance_guard"
    assert "고아 포지션" in call.args[2]


@pytest.mark.asyncio
async def test_audit_no_alert_when_clean():
    """일치 시 알림 없음."""
    reg = _registry({
        "binance_hmm": _engine_status(positions=[
            {"symbol": "BTC/USDT", "side": "long", "qty": 0.01, "entry": 80000}
        ])
    })
    exchange = MagicMock()
    exchange.fetch_positions = AsyncMock(return_value=[
        _ex_pos("BTC/USDT:USDT", 0.01, "LONG"),
    ])
    from unittest.mock import patch
    with patch("services.rnd_position_audit.emit_event", new_callable=AsyncMock) as emit:
        deltas = await run_position_audit(reg, exchange)
    assert deltas == []
    emit.assert_not_called()


@pytest.mark.asyncio
async def test_audit_handles_fetch_failure():
    """거래소 API 실패 시 빈 리스트 + 예외 미전파."""
    reg = _registry({})
    exchange = MagicMock()
    exchange.fetch_positions = AsyncMock(side_effect=Exception("network"))
    deltas = await run_position_audit(reg, exchange)
    assert deltas == []
