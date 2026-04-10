import pytest

from engine.futures_rnd_coordinator import FuturesRndCoordinator


@pytest.mark.asyncio
async def test_symbol_reservation_blocks_other_engine():
    coord = FuturesRndCoordinator(global_capital_usdt=150.0)
    await coord.register_engine("binance_donchian_futures", 100.0)
    await coord.register_engine("binance_pairs", 50.0)

    ok1, reason1, token1 = await coord.request_reservation(
        "binance_donchian_futures", 100.0, ["BTC/USDT"], 20.0
    )
    assert ok1 is True
    assert reason1 == "reserved"
    assert token1 is not None

    ok2, reason2, token2 = await coord.request_reservation(
        "binance_pairs", 50.0, ["BTC/USDT", "ETH/USDT"], 25.0
    )
    assert ok2 is False
    assert reason2.startswith("symbol_reserved:")
    assert token2 is None


@pytest.mark.asyncio
async def test_global_margin_budget_blocks_excess_entry():
    coord = FuturesRndCoordinator(global_capital_usdt=100.0)
    await coord.register_engine("binance_donchian_futures", 70.0)
    await coord.register_engine("binance_pairs", 50.0)

    ok1, _, token1 = await coord.request_reservation(
        "binance_donchian_futures", 70.0, ["SOL/USDT"], 60.0
    )
    assert ok1 is True
    await coord.sync_engine_state(
        "binance_donchian_futures",
        symbols=["SOL/USDT"],
        reserved_margin=60.0,
        cumulative_pnl=0.0,
        daily_pnl=0.0,
        capital_limit=70.0,
        reservation_token=token1,
    )

    ok2, reason2, token2 = await coord.request_reservation(
        "binance_pairs", 50.0, ["BTC/USDT", "ETH/USDT"], 45.0
    )
    assert ok2 is False
    assert reason2 == "global_cap_exceeded"
    assert token2 is None


@pytest.mark.asyncio
async def test_global_loss_limit_pauses_new_entries():
    coord = FuturesRndCoordinator(global_capital_usdt=100.0, daily_loss_limit_pct=0.05, total_loss_limit_pct=0.10)
    await coord.register_engine("binance_pairs", 50.0)
    await coord.note_pnl("binance_pairs", -6.0)

    ok, reason, token = await coord.request_reservation(
        "binance_pairs", 50.0, ["BTC/USDT", "ETH/USDT"], 20.0
    )
    assert ok is False
    assert reason == "global_loss_limit_reached"
    assert token is None

    status = await coord.get_status()
    assert status["entry_paused"] is True
    assert status["global_daily_pnl"] == -6.0
