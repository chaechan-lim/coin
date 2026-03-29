"""PositionStateTracker 테스트."""
import pytest
import pytest_asyncio
from datetime import datetime, timezone

from engine.position_state_tracker import PositionStateTracker, PositionState
from core.enums import Direction
from core.models import Position


@pytest.fixture
def tracker():
    return PositionStateTracker()


def make_state(
    symbol="BTC/USDT",
    direction=Direction.LONG,
    entry_price=80000.0,
    quantity=0.01,
    margin=100.0,
    leverage=3,
    **kwargs,
) -> PositionState:
    return PositionState(
        symbol=symbol,
        direction=direction,
        quantity=quantity,
        entry_price=entry_price,
        margin=margin,
        leverage=leverage,
        extreme_price=kwargs.get("extreme_price", entry_price),
        stop_loss_atr=kwargs.get("stop_loss_atr", 1.5),
        take_profit_atr=kwargs.get("take_profit_atr", 3.0),
        trailing_activation_atr=kwargs.get("trailing_activation_atr", 2.0),
        trailing_stop_atr=kwargs.get("trailing_stop_atr", 1.0),
        tier=kwargs.get("tier", "tier1"),
        strategy_name=kwargs.get("strategy_name", "trend_follower"),
        confidence=kwargs.get("confidence", 0.7),
    )


class TestPositionState:
    def test_is_long(self):
        state = make_state(direction=Direction.LONG)
        assert state.is_long is True
        assert state.is_short is False

    def test_is_short(self):
        state = make_state(direction=Direction.SHORT)
        assert state.is_long is False
        assert state.is_short is True


class TestStopLoss:
    def test_long_sl_not_hit(self):
        state = make_state(entry_price=80000.0, stop_loss_atr=1.5)
        atr = 1000.0  # SL at 80000 - 1500 = 78500
        assert state.check_stop_loss(79000.0, atr) is False

    def test_long_sl_hit(self):
        state = make_state(entry_price=80000.0, stop_loss_atr=1.5)
        atr = 1000.0  # SL at 78500
        assert state.check_stop_loss(78400.0, atr) is True

    def test_short_sl_not_hit(self):
        state = make_state(direction=Direction.SHORT, entry_price=80000.0, stop_loss_atr=1.5)
        atr = 1000.0  # SL at 81500
        assert state.check_stop_loss(81000.0, atr) is False

    def test_short_sl_hit(self):
        state = make_state(direction=Direction.SHORT, entry_price=80000.0, stop_loss_atr=1.5)
        atr = 1000.0  # SL at 81500
        assert state.check_stop_loss(81600.0, atr) is True

    def test_zero_atr_safe(self):
        state = make_state()
        assert state.check_stop_loss(0.0, 0.0) is False

    def test_zero_entry_safe(self):
        state = make_state(entry_price=0.0)
        assert state.check_stop_loss(80000.0, 1000.0) is False


class TestTakeProfit:
    def test_long_tp_hit(self):
        state = make_state(entry_price=80000.0, take_profit_atr=3.0)
        atr = 1000.0  # TP at 83000
        assert state.check_take_profit(83100.0, atr) is True

    def test_long_tp_not_hit(self):
        state = make_state(entry_price=80000.0, take_profit_atr=3.0)
        atr = 1000.0  # TP at 83000
        assert state.check_take_profit(82000.0, atr) is False

    def test_short_tp_hit(self):
        state = make_state(direction=Direction.SHORT, entry_price=80000.0, take_profit_atr=3.0)
        atr = 1000.0  # TP at 77000
        assert state.check_take_profit(76900.0, atr) is True

    def test_short_tp_not_hit(self):
        state = make_state(direction=Direction.SHORT, entry_price=80000.0, take_profit_atr=3.0)
        atr = 1000.0  # TP at 77000
        assert state.check_take_profit(78000.0, atr) is False


class TestTrailingStop:
    def test_not_activated_yet(self):
        state = make_state(entry_price=80000.0, trailing_activation_atr=2.0)
        atr = 1000.0  # activation at 82000
        assert state.check_trailing_stop(81000.0, atr) is False
        assert state.trailing_active is False

    def test_activation(self):
        state = make_state(
            entry_price=80000.0,
            extreme_price=82500.0,
            trailing_activation_atr=2.0,
            trailing_stop_atr=1.0,
        )
        atr = 1000.0  # activation at 82000, trail at extreme - 1000
        # 가격 82500 → 활성화, 트레일링 가격 81500, 현재 82500 > 81500 → 미히트
        assert state.check_trailing_stop(82500.0, atr) is False
        assert state.trailing_active is True

    def test_trailing_hit(self):
        state = make_state(
            entry_price=80000.0,
            extreme_price=83000.0,
            trailing_activation_atr=2.0,
            trailing_stop_atr=1.0,
        )
        state.trailing_active = True
        atr = 1000.0  # trail price: 83000 - 1000 = 82000
        assert state.check_trailing_stop(81900.0, atr) is True

    def test_short_trailing_activation(self):
        state = make_state(
            direction=Direction.SHORT,
            entry_price=80000.0,
            extreme_price=77500.0,
            trailing_activation_atr=2.0,
            trailing_stop_atr=1.0,
        )
        atr = 1000.0  # activation at 78000, 현재 77500 < 78000 → 활성화
        state.check_trailing_stop(77500.0, atr)
        assert state.trailing_active is True

    def test_short_trailing_hit(self):
        state = make_state(
            direction=Direction.SHORT,
            entry_price=80000.0,
            extreme_price=76000.0,
            trailing_activation_atr=2.0,
            trailing_stop_atr=1.0,
        )
        state.trailing_active = True
        atr = 1000.0  # trail price: 76000 + 1000 = 77000
        assert state.check_trailing_stop(77100.0, atr) is True


class TestUpdateExtreme:
    def test_long_higher(self):
        state = make_state(extreme_price=80000.0)
        state.update_extreme(81000.0)
        assert state.extreme_price == 81000.0

    def test_long_lower_no_update(self):
        state = make_state(extreme_price=80000.0)
        state.update_extreme(79000.0)
        assert state.extreme_price == 80000.0

    def test_short_lower(self):
        state = make_state(direction=Direction.SHORT, extreme_price=80000.0)
        state.update_extreme(79000.0)
        assert state.extreme_price == 79000.0

    def test_short_higher_no_update(self):
        state = make_state(direction=Direction.SHORT, extreme_price=80000.0)
        state.update_extreme(81000.0)
        assert state.extreme_price == 80000.0


class TestSLTPPrice:
    def test_long_sl_price(self):
        state = make_state(entry_price=80000.0, stop_loss_atr=1.5)
        assert state.sl_price(1000.0) == 78500.0

    def test_short_sl_price(self):
        state = make_state(direction=Direction.SHORT, entry_price=80000.0, stop_loss_atr=1.5)
        assert state.sl_price(1000.0) == 81500.0

    def test_long_tp_price(self):
        state = make_state(entry_price=80000.0, take_profit_atr=3.0)
        assert state.tp_price(1000.0) == 83000.0

    def test_short_tp_price(self):
        state = make_state(direction=Direction.SHORT, entry_price=80000.0, take_profit_atr=3.0)
        assert state.tp_price(1000.0) == 77000.0

    def test_zero_atr_returns_none(self):
        state = make_state()
        assert state.sl_price(0.0) is None
        assert state.tp_price(0.0) is None


class TestTracker:
    def test_open_and_get(self, tracker):
        state = make_state()
        tracker.open_position(state)
        assert tracker.has_position("BTC/USDT")
        assert tracker.get("BTC/USDT") is state

    def test_close_position(self, tracker):
        state = make_state()
        tracker.open_position(state)
        closed = tracker.close_position("BTC/USDT")
        assert closed is state
        assert tracker.has_position("BTC/USDT") is False

    def test_close_nonexistent(self, tracker):
        assert tracker.close_position("ETH/USDT") is None

    def test_active_count(self, tracker):
        tracker.open_position(make_state(symbol="BTC/USDT", tier="tier1"))
        tracker.open_position(make_state(symbol="ETH/USDT", tier="tier1"))
        tracker.open_position(make_state(symbol="DOGE/USDT", tier="tier2"))
        assert tracker.active_count() == 3
        assert tracker.active_count("tier1") == 2
        assert tracker.active_count("tier2") == 1

    def test_all_symbols(self, tracker):
        tracker.open_position(make_state(symbol="BTC/USDT"))
        tracker.open_position(make_state(symbol="ETH/USDT"))
        assert set(tracker.all_symbols()) == {"BTC/USDT", "ETH/USDT"}

    def test_update_direction(self, tracker):
        state = make_state(direction=Direction.LONG)
        tracker.open_position(state)
        tracker.update_direction("BTC/USDT", Direction.SHORT)
        assert tracker.get("BTC/USDT").direction == Direction.SHORT


class TestDBRestore:
    @pytest.mark.asyncio
    async def test_restore_from_db(self, session, tracker):
        """DB에서 포지션 복원."""
        pos = Position(
            exchange="binance_futures",
            symbol="BTC/USDT",
            quantity=0.01,
            average_buy_price=80000.0,
            total_invested=100.0,
            is_paper=False,
            direction="long",
            leverage=3,
            stop_loss_pct=1.5,
            take_profit_pct=3.0,
            trailing_activation_pct=2.0,
            trailing_stop_pct=1.0,
            highest_price=82000.0,
            entered_at=datetime.now(timezone.utc),
        )
        session.add(pos)
        await session.flush()

        count = await tracker.restore_from_db(session, "binance_futures")
        assert count == 1
        state = tracker.get("BTC/USDT")
        assert state is not None
        assert state.direction == Direction.LONG
        assert state.entry_price == 80000.0
        assert state.extreme_price == 82000.0
        assert state.stop_loss_atr == 1.5

    @pytest.mark.asyncio
    async def test_restore_short_position(self, session, tracker):
        pos = Position(
            exchange="binance_futures",
            symbol="ETH/USDT",
            quantity=0.5,
            average_buy_price=3000.0,
            total_invested=50.0,
            is_paper=False,
            direction="short",
            leverage=3,
            entered_at=datetime.now(timezone.utc),
        )
        session.add(pos)
        await session.flush()

        count = await tracker.restore_from_db(session, "binance_futures")
        assert count == 1
        assert tracker.get("ETH/USDT").direction == Direction.SHORT

    @pytest.mark.asyncio
    async def test_restore_skips_zero_quantity(self, session, tracker):
        pos = Position(
            exchange="binance_futures",
            symbol="XRP/USDT",
            quantity=0,
            average_buy_price=0.5,
            total_invested=0,
            is_paper=False,
        )
        session.add(pos)
        await session.flush()

        count = await tracker.restore_from_db(session, "binance_futures")
        assert count == 0

    @pytest.mark.asyncio
    async def test_restore_surge_as_tier2(self, session, tracker):
        pos = Position(
            exchange="binance_futures",
            symbol="DOGE/USDT",
            quantity=100.0,
            average_buy_price=0.15,
            total_invested=5.0,
            is_paper=False,
            is_surge=True,
            entered_at=datetime.now(timezone.utc),
        )
        session.add(pos)
        await session.flush()

        await tracker.restore_from_db(session, "binance_futures")
        assert tracker.get("DOGE/USDT").tier == "tier2"


class TestDBPersist:
    @pytest.mark.asyncio
    async def test_persist_to_db(self, session, tracker):
        """인메모리 상태를 DB에 영속화."""
        pos = Position(
            exchange="binance_futures",
            symbol="BTC/USDT",
            quantity=0.01,
            average_buy_price=80000.0,
            total_invested=100.0,
            is_paper=False,
            direction="long",
            leverage=3,
            entered_at=datetime.now(timezone.utc),
        )
        session.add(pos)
        await session.flush()

        state = make_state(
            symbol="BTC/USDT",
            stop_loss_atr=2.0,
            take_profit_atr=4.0,
            extreme_price=85000.0,
        )
        state.trailing_active = True
        tracker.open_position(state)

        count = await tracker.persist_to_db(session, "binance_futures")
        assert count == 1

        # DB 확인
        from sqlalchemy import select
        result = await session.execute(
            select(Position).where(Position.symbol == "BTC/USDT")
        )
        db_pos = result.scalar_one()
        assert db_pos.stop_loss_pct == 2.0
        assert db_pos.take_profit_pct == 4.0
        assert db_pos.highest_price == 85000.0
        assert db_pos.trailing_active is True

    @pytest.mark.asyncio
    async def test_persist_short_saves_lowest_price(self, session, tracker):
        """숏 방향: extreme_price → pos.lowest_price에 저장, pos.highest_price는 None으로 클리어."""
        pos = Position(
            exchange="binance_futures",
            symbol="BTC/USDT",
            quantity=0.01,
            average_buy_price=65000.0,
            total_invested=100.0,
            is_paper=False,
            direction="short",
            leverage=3,
            entered_at=datetime.now(timezone.utc),
            highest_price=70000.0,  # stale long-session value
        )
        session.add(pos)
        await session.flush()

        state = make_state(
            symbol="BTC/USDT",
            direction=Direction.SHORT,
            entry_price=65000.0,
            extreme_price=60000.0,  # 숏: 최저가
        )
        tracker.open_position(state)

        count = await tracker.persist_to_db(session, "binance_futures")
        assert count == 1

        from sqlalchemy import select
        result = await session.execute(select(Position).where(Position.symbol == "BTC/USDT"))
        db_pos = result.scalar_one()
        assert db_pos.lowest_price == pytest.approx(60000.0)
        assert db_pos.highest_price is None   # stale value cleared
        assert db_pos.direction == "short"

    @pytest.mark.asyncio
    async def test_persist_long_clears_stale_lowest_price(self, session, tracker):
        """롱 방향: extreme_price → pos.highest_price에 저장, pos.lowest_price는 None으로 클리어."""
        pos = Position(
            exchange="binance_futures",
            symbol="ETH/USDT",
            quantity=0.05,
            average_buy_price=3200.0,
            total_invested=160.0,
            is_paper=False,
            direction="long",
            leverage=3,
            entered_at=datetime.now(timezone.utc),
            lowest_price=2800.0,  # stale short-session value
        )
        session.add(pos)
        await session.flush()

        state = make_state(
            symbol="ETH/USDT",
            direction=Direction.LONG,
            entry_price=3200.0,
            extreme_price=3500.0,  # 롱: 최고가
        )
        tracker.open_position(state)

        await tracker.persist_to_db(session, "binance_futures")

        from sqlalchemy import select
        result = await session.execute(select(Position).where(Position.symbol == "ETH/USDT"))
        db_pos = result.scalar_one()
        assert db_pos.highest_price == pytest.approx(3500.0)
        assert db_pos.lowest_price is None   # stale value cleared
        assert db_pos.direction == "long"

    @pytest.mark.asyncio
    async def test_persist_none_direction_treated_as_long(self, session, tracker):
        """direction=None인 구 포지션은 long으로 취급해 highest_price에 저장하고 AttributeError를 일으키지 않는다."""
        pos = Position(
            exchange="binance_futures",
            symbol="BNB/USDT",
            quantity=0.1,
            average_buy_price=400.0,
            total_invested=40.0,
            is_paper=False,
            direction=None,  # pre-migration position
            leverage=3,
            entered_at=datetime.now(timezone.utc),
        )
        session.add(pos)
        await session.flush()

        state = make_state(symbol="BNB/USDT", extreme_price=450.0)
        tracker.open_position(state)
        state.direction = None  # simulate legacy in-memory state (direction lost after restart)

        await tracker.persist_to_db(session, "binance_futures")

        from sqlalchemy import select
        result = await session.execute(select(Position).where(Position.symbol == "BNB/USDT"))
        db_pos = result.scalar_one()
        assert db_pos.highest_price == pytest.approx(450.0)
        assert db_pos.lowest_price is None
        assert db_pos.direction == "long"  # defaulted from None
