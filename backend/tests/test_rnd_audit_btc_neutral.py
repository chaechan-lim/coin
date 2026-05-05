"""
R&D position audit — BTC-Neutral MR 의 alt_symbol/btc_qty 인식 검증.

증상: BTC-Neutral 포지션 (SOL long + BTC short) 이 audit 에서 좀비로 잘못 감지.
원인: _collect_engine_positions 가 standard symbol/side 키만 인식, alt_symbol/btc_qty 미지원.
"""
from unittest.mock import MagicMock

from services.rnd_position_audit import _collect_engine_positions


def _registry(engines: dict[str, dict]):
    reg = MagicMock()
    def _get(name):
        if name not in engines:
            return None
        eng = MagicMock()
        eng.get_status = MagicMock(return_value=engines[name])
        return eng
    reg.get_engine = _get
    return reg


def test_btc_neutral_alt_long_btc_short():
    """BTC-Neutral 의 SOL long + BTC short 페어가 정확히 2 레그로 인식돼야 한다."""
    reg = _registry({
        "binance_btc_neutral": {
            "is_running": True,
            "leverage": 2,
            "positions": [
                {
                    "alt_symbol": "SOL/USDT",
                    "alt_side": "long",
                    "alt_qty": 1.42,
                    "alt_entry": 84.29,
                    "btc_side": "short",
                    "btc_qty": 0.001,
                    "btc_entry": 80153.9,
                    "entry_z": -2.5,
                }
            ],
        },
    })
    em = _collect_engine_positions(reg)
    assert ("SOL/USDT", "LONG") in em
    assert em[("SOL/USDT", "LONG")][0] == ("binance_btc_neutral", 1.42)
    assert ("BTC/USDT", "SHORT") in em
    assert em[("BTC/USDT", "SHORT")][0] == ("binance_btc_neutral", 0.001)


def test_btc_neutral_alt_short_btc_long():
    """반대 방향 (alt short + BTC long) 도 정확히 인식."""
    reg = _registry({
        "binance_btc_neutral": {
            "positions": [
                {
                    "alt_symbol": "ETH/USDT",
                    "alt_side": "short",
                    "alt_qty": 0.5,
                    "alt_entry": 2400,
                    "btc_side": "long",
                    "btc_qty": 0.002,
                    "btc_entry": 80000,
                }
            ],
        },
    })
    em = _collect_engine_positions(reg)
    assert ("ETH/USDT", "SHORT") in em
    assert ("BTC/USDT", "LONG") in em


def test_btc_neutral_zero_qty_skipped():
    """수량 0 (좀비도 아닌 무효 데이터) 은 스킵."""
    reg = _registry({
        "binance_btc_neutral": {
            "positions": [
                {"alt_symbol": "SOL/USDT", "alt_side": "long", "alt_qty": 0,
                 "btc_side": "short", "btc_qty": 0}
            ],
        },
    })
    em = _collect_engine_positions(reg)
    assert em == {}
