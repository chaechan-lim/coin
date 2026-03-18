"""COIN-24: 현물 매수 시 DB 포지션 수량이 실제 체결 수량과 불일치 수정 테스트.

근본 원인: 매수 시 요청 수량(amount_krw / price)을 DB에 저장하지만,
실제 거래소 체결 수량은 stepSize 내림, 수수료 차감 등으로 달라짐.
매도 시 DB 수량을 사용하면 insufficient balance 에러 발생.

수정:
1. 매수 체결 후 order.executed_quantity / executed_price 로 DB 갱신
2. 매도 시 거래소 실잔고와 비교하여 클램핑
3. sync_exchange_positions 에서 1% 이상 차이 시 거래소 기준 보정 (기존)
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from config import AppConfig
from core.enums import SignalType
from core.models import Position
from strategies.base import Signal
from strategies.combiner import CombinedDecision
from engine.trading_engine import TradingEngine, PositionTracker


# ── 헬퍼 ──────────────────────────────────────────────────────────


def _make_balance(free: float) -> MagicMock:
    bal = MagicMock()
    bal.free = free
    bal.total = free
    return bal


def _make_config() -> AppConfig:
    cfg = AppConfig()
    cfg.trading.mode = "live"
    cfg.trading.asymmetric_mode = True
    cfg.trading.min_combined_confidence = 0.50
    cfg.trading.daily_buy_limit = 20
    cfg.trading.max_daily_coin_buys = 3
    cfg.trading.min_trade_interval_sec = 3600
    cfg.trading.cooldown_after_sell_sec = 14400
    cfg.risk.max_trade_size_pct = 0.20
    return cfg


def _make_engine(
    config,
    mock_exchange,
    mock_market_data,
    mock_order_mgr,
    mock_pm,
    exchange_name="binance_spot",
):
    combiner = MagicMock()
    engine = TradingEngine(
        config=config,
        exchange=mock_exchange,
        market_data=mock_market_data,
        order_manager=mock_order_mgr,
        portfolio_manager=mock_pm,
        combiner=combiner,
        exchange_name=exchange_name,
    )
    engine._market_state = "sideways"
    engine._market_confidence = 0.5
    engine._is_running = True
    return engine


def _make_buy_decision(confidence=0.65):
    signal = Signal(
        strategy_name="test_strategy",
        signal_type=SignalType.BUY,
        confidence=confidence,
        reason="test buy",
    )
    return CombinedDecision(
        action=SignalType.BUY,
        combined_confidence=confidence,
        contributing_signals=[signal],
        final_reason="test buy decision",
    )


def _make_sell_decision():
    signal = Signal(
        strategy_name="test_strategy",
        signal_type=SignalType.SELL,
        confidence=0.70,
        reason="test sell",
    )
    return CombinedDecision(
        action=SignalType.SELL,
        combined_confidence=0.70,
        contributing_signals=[signal],
        final_reason="test sell decision",
    )


def _make_session_no_position():
    """Simulate DB session returning no existing position."""
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=None)
    result.scalars = MagicMock(
        return_value=MagicMock(
            first=MagicMock(return_value=None),
            all=MagicMock(return_value=[]),
        )
    )
    session.execute = AsyncMock(return_value=result)
    return session


def _make_session_with_position(qty=0.00074272, avg_price=67500):
    """Simulate DB session returning existing position."""
    pos = MagicMock(spec=Position)
    pos.quantity = qty
    pos.average_buy_price = avg_price
    pos.total_invested = qty * avg_price
    pos.exchange = "binance_spot"
    pos.symbol = "BTC/USDT"
    pos.last_trade_at = None

    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=pos)
    result.scalars = MagicMock(
        return_value=MagicMock(
            first=MagicMock(return_value=None),
            all=MagicMock(return_value=[]),
        )
    )
    session.execute = AsyncMock(return_value=result)
    return session, pos


# ── Fix 1: 매수 시 체결 수량 사용 ──────────────────────────────────


class TestBuyUsesExecutedQuantity:
    """매수 후 DB 포지션에 거래소 체결 수량(executed_quantity)이 저장되는지 검증."""

    @pytest.mark.asyncio
    async def test_buy_stores_executed_quantity_not_requested(self):
        """요청 수량 0.00074272 → 체결 수량 0.00074 → DB에 0.00074 저장."""
        config = _make_config()
        mock_exchange = AsyncMock()
        mock_exchange.fetch_ticker = AsyncMock(
            return_value=MagicMock(last=67500, ask=67500)
        )
        mock_exchange.fetch_balance = AsyncMock(
            return_value={"BTC": _make_balance(100.0)}
        )

        mock_md = AsyncMock()
        mock_md.get_current_price = AsyncMock(return_value=67500)
        mock_md.get_ticker = AsyncMock(return_value=MagicMock(last=67500, ask=67500))
        mock_md.get_candles = AsyncMock(side_effect=Exception("no candles"))

        mock_om = AsyncMock()
        # 거래소 응답: stepSize 내림 + 수수료 차감된 수량
        filled_order = MagicMock()
        filled_order.status = "filled"
        filled_order.fee = 0.05  # USDT
        filled_order.id = 1
        filled_order.exchange_order_id = "binance-123"
        filled_order.executed_quantity = 0.00074  # stepSize 내림
        filled_order.executed_price = 67500.0
        mock_om.create_order = AsyncMock(return_value=filled_order)

        mock_pm = MagicMock()
        mock_pm.cash_balance = 100.0  # 100 USDT
        mock_pm._sync_lock = asyncio.Lock()
        mock_pm._is_paper = False
        mock_pm._exchange_name = "binance_spot"
        mock_pm.update_position_on_buy = AsyncMock()
        mock_pm.update_position_on_sell = AsyncMock()
        mock_pm.reconcile_cash_from_db = AsyncMock()
        mock_pm.get_portfolio_summary = AsyncMock(
            return_value={
                "total_value_krw": 100,
                "cash_balance_krw": 100,
            }
        )
        mock_pm.take_snapshot = AsyncMock(return_value=None)

        engine = _make_engine(config, mock_exchange, mock_md, mock_om, mock_pm)
        session = _make_session_no_position()
        decision = _make_buy_decision(confidence=0.65)

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            await engine._process_decision(session, "BTC/USDT", decision)

        # update_position_on_buy에 전달된 인자 검증
        mock_pm.update_position_on_buy.assert_called_once()
        call_args = mock_pm.update_position_on_buy.call_args

        # 두 번째 인자 (quantity): 체결 수량이어야 함
        actual_qty = call_args[0][2]
        assert actual_qty == 0.00074, (
            f"DB에 요청 수량이 아닌 체결 수량이 저장되어야 함: got {actual_qty}"
        )

        # 세 번째 인자 (price): 체결 가격이어야 함
        actual_price = call_args[0][3]
        assert actual_price == 67500.0

        # 네 번째 인자 (cost): executed_qty * executed_price
        actual_cost = call_args[0][4]
        assert actual_cost == pytest.approx(0.00074 * 67500.0, rel=1e-6)

    @pytest.mark.asyncio
    async def test_buy_tracker_uses_executed_price(self):
        """PositionTracker가 체결 가격(executed_price)으로 생성됨."""
        config = _make_config()
        mock_exchange = AsyncMock()
        mock_exchange.fetch_ticker = AsyncMock(
            return_value=MagicMock(last=67500, ask=67500)
        )
        mock_exchange.fetch_balance = AsyncMock(
            return_value={"BTC": _make_balance(100.0)}
        )

        mock_md = AsyncMock()
        mock_md.get_current_price = AsyncMock(return_value=67500)
        mock_md.get_ticker = AsyncMock(return_value=MagicMock(last=67500, ask=67500))
        mock_md.get_candles = AsyncMock(side_effect=Exception("no candles"))

        mock_om = AsyncMock()
        filled_order = MagicMock()
        filled_order.status = "filled"
        filled_order.fee = 0.05
        filled_order.id = 1
        filled_order.exchange_order_id = "binance-123"
        # 체결 가격이 요청 가격과 다름 (시장가 슬리피지)
        filled_order.executed_quantity = 0.00074
        filled_order.executed_price = 67600.0  # 요청 67500 → 체결 67600
        mock_om.create_order = AsyncMock(return_value=filled_order)

        mock_pm = MagicMock()
        mock_pm.cash_balance = 100.0
        mock_pm._sync_lock = asyncio.Lock()
        mock_pm._is_paper = False
        mock_pm._exchange_name = "binance_spot"
        mock_pm.update_position_on_buy = AsyncMock()
        mock_pm.update_position_on_sell = AsyncMock()
        mock_pm.reconcile_cash_from_db = AsyncMock()
        mock_pm.get_portfolio_summary = AsyncMock(
            return_value={
                "total_value_krw": 100,
                "cash_balance_krw": 100,
            }
        )
        mock_pm.take_snapshot = AsyncMock(return_value=None)

        engine = _make_engine(config, mock_exchange, mock_md, mock_om, mock_pm)
        session = _make_session_no_position()
        decision = _make_buy_decision(confidence=0.65)

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            await engine._process_decision(session, "BTC/USDT", decision)

        assert "BTC/USDT" in engine._position_trackers
        tracker = engine._position_trackers["BTC/USDT"]
        assert tracker.entry_price == 67600.0, "체결 가격으로 트래커 생성"
        assert tracker.extreme_price == 67600.0

    @pytest.mark.asyncio
    async def test_buy_fallback_when_executed_fields_none(self):
        """executed_quantity/price가 None일 때 요청값으로 폴백."""
        config = _make_config()
        mock_exchange = AsyncMock()
        mock_exchange.fetch_ticker = AsyncMock(
            return_value=MagicMock(last=67500, ask=67500)
        )
        mock_exchange.fetch_balance = AsyncMock(
            return_value={"BTC": _make_balance(100.0)}
        )

        mock_md = AsyncMock()
        mock_md.get_current_price = AsyncMock(return_value=67500)
        mock_md.get_ticker = AsyncMock(return_value=MagicMock(last=67500, ask=67500))
        mock_md.get_candles = AsyncMock(side_effect=Exception("no candles"))

        mock_om = AsyncMock()
        filled_order = MagicMock()
        filled_order.status = "filled"
        filled_order.fee = 0.05
        filled_order.id = 1
        filled_order.exchange_order_id = "binance-123"
        filled_order.executed_quantity = None  # 일부 거래소에서 None 가능
        filled_order.executed_price = None
        mock_om.create_order = AsyncMock(return_value=filled_order)

        mock_pm = MagicMock()
        mock_pm.cash_balance = 100.0
        mock_pm._sync_lock = asyncio.Lock()
        mock_pm._is_paper = False
        mock_pm._exchange_name = "binance_spot"
        mock_pm.update_position_on_buy = AsyncMock()
        mock_pm.update_position_on_sell = AsyncMock()
        mock_pm.reconcile_cash_from_db = AsyncMock()
        mock_pm.get_portfolio_summary = AsyncMock(
            return_value={
                "total_value_krw": 100,
                "cash_balance_krw": 100,
            }
        )
        mock_pm.take_snapshot = AsyncMock(return_value=None)

        engine = _make_engine(config, mock_exchange, mock_md, mock_om, mock_pm)
        session = _make_session_no_position()
        decision = _make_buy_decision(confidence=0.65)

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            await engine._process_decision(session, "BTC/USDT", decision)

        # None 폴백 → 요청값(ticker price=67500) 사용
        mock_pm.update_position_on_buy.assert_called_once()
        call_args = mock_pm.update_position_on_buy.call_args
        actual_price = call_args[0][3]
        assert actual_price == 67500  # 폴백 가격


# ── Fix 2: 매도 시 실잔고 클램핑 ──────────────────────────────────


class TestSellClampsToBalance:
    """매도 시 DB 수량이 거래소 실잔고보다 클 경우 실잔고로 클램핑."""

    @pytest.mark.asyncio
    async def test_stop_sell_clamps_to_exchange_balance(self):
        """DB qty(0.00074272) > 실잔고(0.00073926) → 실잔고로 매도."""
        config = _make_config()

        # 거래소 실잔고: 0.00073926 (DB보다 적음)
        mock_exchange = AsyncMock()
        mock_exchange.fetch_balance = AsyncMock(
            return_value={
                "BTC": _make_balance(free=0.00073926),
            }
        )

        mock_md = AsyncMock()
        mock_md.get_current_price = AsyncMock(return_value=67500)

        mock_om = AsyncMock()
        filled_order = MagicMock()
        filled_order.status = "filled"
        filled_order.fee = 0.03
        filled_order.id = 1
        filled_order.exchange_order_id = "sell-123"
        filled_order.executed_quantity = 0.00073926
        filled_order.executed_price = 67500
        mock_om.create_order = AsyncMock(return_value=filled_order)

        mock_pm = MagicMock()
        mock_pm.cash_balance = 50.0
        mock_pm._sync_lock = asyncio.Lock()
        mock_pm._is_paper = False
        mock_pm._exchange_name = "binance_spot"
        mock_pm.update_position_on_buy = AsyncMock()
        mock_pm.update_position_on_sell = AsyncMock()
        mock_pm.reconcile_cash_from_db = AsyncMock()
        mock_pm.get_portfolio_summary = AsyncMock(
            return_value={
                "total_value_krw": 100,
                "cash_balance_krw": 50,
            }
        )
        mock_pm.take_snapshot = AsyncMock(return_value=None)

        engine = _make_engine(config, mock_exchange, mock_md, mock_om, mock_pm)
        # DB qty: 0.00074272 (실잔고보다 큼 — 원래 버그 시나리오)
        position = MagicMock(spec=Position)
        position.quantity = 0.00074272
        position.average_buy_price = 67500
        position.total_invested = 0.00074272 * 67500

        session = AsyncMock()
        engine._position_trackers["BTC/USDT"] = PositionTracker(
            entry_price=67500,
            extreme_price=67500,
        )

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            await engine._execute_stop_sell(
                session,
                "BTC/USDT",
                position,
                67500,
                "stop_loss: -3%",
            )

        # create_order에 실잔고(0.00073926)가 전달됨
        mock_om.create_order.assert_called_once()
        call_args = mock_om.create_order.call_args
        sell_qty = call_args[0][3]
        assert sell_qty == 0.00073926, f"실잔고로 클램핑되어야 함: got {sell_qty}"

    @pytest.mark.asyncio
    async def test_stop_sell_no_clamp_when_balance_sufficient(self):
        """실잔고 >= DB qty → 클램핑 없이 DB 수량으로 매도."""
        config = _make_config()

        mock_exchange = AsyncMock()
        mock_exchange.fetch_balance = AsyncMock(
            return_value={
                "BTC": _make_balance(free=0.001),  # 충분한 잔고
            }
        )

        mock_md = AsyncMock()
        mock_md.get_current_price = AsyncMock(return_value=67500)

        mock_om = AsyncMock()
        filled_order = MagicMock()
        filled_order.status = "filled"
        filled_order.fee = 0.03
        filled_order.id = 1
        filled_order.exchange_order_id = "sell-123"
        filled_order.executed_quantity = 0.00074
        filled_order.executed_price = 67500
        mock_om.create_order = AsyncMock(return_value=filled_order)

        mock_pm = MagicMock()
        mock_pm.cash_balance = 50.0
        mock_pm._sync_lock = asyncio.Lock()
        mock_pm._is_paper = False
        mock_pm._exchange_name = "binance_spot"
        mock_pm.update_position_on_buy = AsyncMock()
        mock_pm.update_position_on_sell = AsyncMock()
        mock_pm.reconcile_cash_from_db = AsyncMock()
        mock_pm.get_portfolio_summary = AsyncMock(
            return_value={
                "total_value_krw": 100,
                "cash_balance_krw": 50,
            }
        )
        mock_pm.take_snapshot = AsyncMock(return_value=None)

        engine = _make_engine(config, mock_exchange, mock_md, mock_om, mock_pm)
        position = MagicMock(spec=Position)
        position.quantity = 0.00074
        position.average_buy_price = 67500
        position.total_invested = 0.00074 * 67500

        session = AsyncMock()
        engine._position_trackers["BTC/USDT"] = PositionTracker(
            entry_price=67500,
            extreme_price=67500,
        )

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            await engine._execute_stop_sell(
                session,
                "BTC/USDT",
                position,
                67500,
                "stop_loss: -3%",
            )

        call_args = mock_om.create_order.call_args
        sell_qty = call_args[0][3]
        assert sell_qty == 0.00074, "잔고 충분 시 DB 수량 유지"

    @pytest.mark.asyncio
    async def test_stop_sell_zero_balance_returns_early(self):
        """실잔고 0 → 매도 스킵 (RuntimeError 발생하지 않음)."""
        config = _make_config()

        mock_exchange = AsyncMock()
        mock_exchange.fetch_balance = AsyncMock(
            return_value={
                "BTC": _make_balance(free=0.0),
            }
        )

        mock_md = AsyncMock()
        mock_om = AsyncMock()
        mock_pm = MagicMock()
        mock_pm.cash_balance = 50.0
        mock_pm._sync_lock = asyncio.Lock()
        mock_pm._is_paper = False
        mock_pm._exchange_name = "binance_spot"
        mock_pm.update_position_on_buy = AsyncMock()
        mock_pm.update_position_on_sell = AsyncMock()
        mock_pm.reconcile_cash_from_db = AsyncMock()

        engine = _make_engine(config, mock_exchange, mock_md, mock_om, mock_pm)
        position = MagicMock(spec=Position)
        position.quantity = 0.001
        position.average_buy_price = 67500
        session = AsyncMock()

        # 예외 없이 정상 반환
        await engine._execute_stop_sell(
            session,
            "BTC/USDT",
            position,
            67500,
            "stop_loss",
        )
        mock_om.create_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_sell_fetch_balance_failure_uses_db_qty(self):
        """fetch_balance 실패 시 DB 수량으로 폴백 (기존 동작 유지)."""
        config = _make_config()

        mock_exchange = AsyncMock()
        mock_exchange.fetch_balance = AsyncMock(side_effect=Exception("network error"))

        mock_md = AsyncMock()
        mock_om = AsyncMock()
        filled_order = MagicMock()
        filled_order.status = "filled"
        filled_order.fee = 0.03
        filled_order.id = 1
        filled_order.exchange_order_id = "sell-123"
        filled_order.executed_quantity = 0.001
        filled_order.executed_price = 67500
        mock_om.create_order = AsyncMock(return_value=filled_order)

        mock_pm = MagicMock()
        mock_pm.cash_balance = 50.0
        mock_pm._sync_lock = asyncio.Lock()
        mock_pm._is_paper = False
        mock_pm._exchange_name = "binance_spot"
        mock_pm.update_position_on_buy = AsyncMock()
        mock_pm.update_position_on_sell = AsyncMock()
        mock_pm.reconcile_cash_from_db = AsyncMock()
        mock_pm.get_portfolio_summary = AsyncMock(
            return_value={
                "total_value_krw": 100,
                "cash_balance_krw": 50,
            }
        )
        mock_pm.take_snapshot = AsyncMock(return_value=None)

        engine = _make_engine(config, mock_exchange, mock_md, mock_om, mock_pm)
        position = MagicMock(spec=Position)
        position.quantity = 0.001
        position.average_buy_price = 67500
        position.total_invested = 0.001 * 67500
        session = AsyncMock()
        engine._position_trackers["BTC/USDT"] = PositionTracker(
            entry_price=67500,
            extreme_price=67500,
        )

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            await engine._execute_stop_sell(
                session,
                "BTC/USDT",
                position,
                67500,
                "stop_loss",
            )

        # fetch_balance 실패 → DB 수량(0.001)으로 폴백
        call_args = mock_om.create_order.call_args
        sell_qty = call_args[0][3]
        assert sell_qty == 0.001, "fetch_balance 실패 시 DB 수량 유지"

    @pytest.mark.asyncio
    async def test_normal_sell_clamps_to_exchange_balance(self):
        """일반 매도(전략 SELL 시그널)에서도 실잔고 클램핑."""
        config = _make_config()

        # 실잔고가 DB보다 적음
        mock_exchange = AsyncMock()
        mock_exchange.fetch_ticker = AsyncMock(
            return_value=MagicMock(last=67500, ask=67500)
        )
        mock_exchange.fetch_balance = AsyncMock(
            return_value={
                "BTC": _make_balance(free=0.0009),
            }
        )

        mock_md = AsyncMock()
        mock_md.get_current_price = AsyncMock(return_value=67500)

        mock_om = AsyncMock()
        filled_order = MagicMock()
        filled_order.status = "filled"
        filled_order.fee = 0.03
        filled_order.id = 1
        filled_order.exchange_order_id = "sell-456"
        filled_order.executed_quantity = 0.0009
        filled_order.executed_price = 67500
        mock_om.create_order = AsyncMock(return_value=filled_order)

        mock_pm = MagicMock()
        mock_pm.cash_balance = 50.0
        mock_pm._sync_lock = asyncio.Lock()
        mock_pm._is_paper = False
        mock_pm._exchange_name = "binance_spot"
        mock_pm.update_position_on_buy = AsyncMock()
        mock_pm.update_position_on_sell = AsyncMock()
        mock_pm.reconcile_cash_from_db = AsyncMock()
        mock_pm.get_portfolio_summary = AsyncMock(
            return_value={
                "total_value_krw": 100,
                "cash_balance_krw": 50,
            }
        )
        mock_pm.take_snapshot = AsyncMock(return_value=None)

        engine = _make_engine(config, mock_exchange, mock_md, mock_om, mock_pm)
        decision = _make_sell_decision()

        pos = MagicMock()
        pos.quantity = 0.001  # DB: 0.001
        pos.average_buy_price = 67500
        pos.total_invested = 67.5
        session = _make_session_no_position()

        async def sell_execute(*args, **kwargs):
            result = MagicMock()
            result.scalar_one_or_none = MagicMock(return_value=pos)
            result.scalars = MagicMock(
                return_value=MagicMock(
                    first=MagicMock(return_value=None),
                    all=MagicMock(return_value=[]),
                )
            )
            return result

        session.execute = AsyncMock(side_effect=sell_execute)

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            await engine._process_decision(session, "BTC/USDT", decision)

        mock_om.create_order.assert_called_once()
        call_args = mock_om.create_order.call_args
        sell_qty = call_args[0][3]
        assert sell_qty == 0.0009, f"일반 매도도 실잔고 클램핑: got {sell_qty}"


# ── Fix 1 추가: 로테이션(서지) 매수도 체결 수량 사용 ──────────────


class TestRotationBuyUsesExecutedQuantity:
    """로테이션 매수도 거래소 체결 수량/가격을 DB에 저장하는지 검증."""

    @pytest.mark.asyncio
    async def test_rotation_buy_stores_executed_quantity(self):
        """rotation buy → executed_qty로 포지션 업데이트."""
        config = _make_config()

        mock_exchange = AsyncMock()
        mock_exchange.fetch_ticker = AsyncMock(
            return_value=MagicMock(last=100, ask=100)
        )
        mock_exchange.fetch_balance = AsyncMock(
            return_value={"SOL": _make_balance(100.0)}
        )

        mock_md = AsyncMock()
        mock_md.get_current_price = AsyncMock(return_value=100)
        mock_md.get_candles = AsyncMock(side_effect=Exception("no candles"))
        mock_md.get_ticker = AsyncMock(return_value=MagicMock(last=100, ask=100))

        mock_om = AsyncMock()
        filled_order = MagicMock()
        filled_order.status = "filled"
        filled_order.fee = 0.01
        filled_order.id = 1
        filled_order.exchange_order_id = "rot-123"
        filled_order.executed_quantity = 0.49  # stepSize 내림
        filled_order.executed_price = 100.5  # 슬리피지
        mock_om.create_order = AsyncMock(return_value=filled_order)

        mock_pm = MagicMock()
        mock_pm.cash_balance = 100.0
        mock_pm._sync_lock = asyncio.Lock()
        mock_pm._is_paper = False
        mock_pm._exchange_name = "binance_spot"
        mock_pm.update_position_on_buy = AsyncMock()
        mock_pm.update_position_on_sell = AsyncMock()
        mock_pm.reconcile_cash_from_db = AsyncMock()
        mock_pm.get_portfolio_summary = AsyncMock(
            return_value={
                "total_value_krw": 100,
                "cash_balance_krw": 100,
            }
        )
        mock_pm.take_snapshot = AsyncMock(return_value=None)

        combiner = MagicMock()
        engine = TradingEngine(
            config=config,
            exchange=mock_exchange,
            market_data=mock_md,
            order_manager=mock_om,
            portfolio_manager=mock_pm,
            combiner=combiner,
            exchange_name="binance_spot",
        )
        engine._market_state = "sideways"
        engine._market_confidence = 0.5
        engine._is_running = True

        session = _make_session_no_position()

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            await engine._execute_rotation_buy(
                session,
                "SOL/USDT",
                2.5,
                0.8,
            )

        mock_pm.update_position_on_buy.assert_called_once()
        call_args = mock_pm.update_position_on_buy.call_args

        actual_qty = call_args[0][2]
        assert actual_qty == 0.49, f"로테이션 매수도 체결 수량 사용: got {actual_qty}"

        actual_price = call_args[0][3]
        assert actual_price == 100.5, (
            f"로테이션 매수도 체결 가격 사용: got {actual_price}"
        )

        actual_cost = call_args[0][4]
        assert actual_cost == pytest.approx(0.49 * 100.5, rel=1e-6)


# ── _clamp_sell_qty_to_balance 유닛 테스트 ──────────────────────


class TestClampSellQty:
    """_clamp_sell_qty_to_balance 메서드 직접 테스트."""

    @pytest.mark.asyncio
    async def test_clamp_when_balance_less(self):
        """실잔고 < 요청 → 실잔고 반환."""
        config = _make_config()
        mock_exchange = AsyncMock()
        mock_exchange.fetch_balance = AsyncMock(
            return_value={
                "ETH": _make_balance(free=1.5),
            }
        )
        mock_md = AsyncMock()
        mock_om = AsyncMock()
        mock_pm = MagicMock()
        mock_pm.cash_balance = 100
        mock_pm._sync_lock = asyncio.Lock()
        mock_pm._is_paper = False
        mock_pm._exchange_name = "binance_spot"
        mock_pm.reconcile_cash_from_db = AsyncMock()

        engine = _make_engine(config, mock_exchange, mock_md, mock_om, mock_pm)
        result = await engine._clamp_sell_qty_to_balance("ETH/USDT", 2.0)
        assert result == 1.5

    @pytest.mark.asyncio
    async def test_no_clamp_when_balance_sufficient(self):
        """실잔고 >= 요청 → 요청 수량 유지."""
        config = _make_config()
        mock_exchange = AsyncMock()
        mock_exchange.fetch_balance = AsyncMock(
            return_value={
                "ETH": _make_balance(free=5.0),
            }
        )
        mock_md = AsyncMock()
        mock_om = AsyncMock()
        mock_pm = MagicMock()
        mock_pm.cash_balance = 100
        mock_pm._sync_lock = asyncio.Lock()
        mock_pm._is_paper = False
        mock_pm._exchange_name = "binance_spot"
        mock_pm.reconcile_cash_from_db = AsyncMock()

        engine = _make_engine(config, mock_exchange, mock_md, mock_om, mock_pm)
        result = await engine._clamp_sell_qty_to_balance("ETH/USDT", 2.0)
        assert result == 2.0

    @pytest.mark.asyncio
    async def test_clamp_unknown_symbol_no_clamp(self):
        """잔고에 해당 심볼 없음 → 클램핑 없이 요청 수량 유지."""
        config = _make_config()
        mock_exchange = AsyncMock()
        mock_exchange.fetch_balance = AsyncMock(
            return_value={
                "BTC": _make_balance(free=1.0),
            }
        )
        mock_md = AsyncMock()
        mock_om = AsyncMock()
        mock_pm = MagicMock()
        mock_pm.cash_balance = 100
        mock_pm._sync_lock = asyncio.Lock()
        mock_pm._is_paper = False
        mock_pm._exchange_name = "binance_spot"
        mock_pm.reconcile_cash_from_db = AsyncMock()

        engine = _make_engine(config, mock_exchange, mock_md, mock_om, mock_pm)
        # ETH not in balances
        result = await engine._clamp_sell_qty_to_balance("ETH/USDT", 2.0)
        assert result == 2.0

    @pytest.mark.asyncio
    async def test_clamp_fetch_failure_returns_original(self):
        """fetch_balance 예외 → 요청 수량 유지."""
        config = _make_config()
        mock_exchange = AsyncMock()
        mock_exchange.fetch_balance = AsyncMock(side_effect=Exception("timeout"))
        mock_md = AsyncMock()
        mock_om = AsyncMock()
        mock_pm = MagicMock()
        mock_pm.cash_balance = 100
        mock_pm._sync_lock = asyncio.Lock()
        mock_pm._is_paper = False
        mock_pm._exchange_name = "binance_spot"
        mock_pm.reconcile_cash_from_db = AsyncMock()

        engine = _make_engine(config, mock_exchange, mock_md, mock_om, mock_pm)
        result = await engine._clamp_sell_qty_to_balance("ETH/USDT", 2.0)
        assert result == 2.0


# ── End-to-end: 매수 → 매도 수량 일관성 ──────────────────────────


class TestEndToEndQuantityConsistency:
    """매수 체결 수량이 DB에 저장되면 매도 시 일관성 유지."""

    @pytest.mark.asyncio
    async def test_buy_then_sell_consistent_quantity(self):
        """매수 체결 수량 저장 → 매도 시 해당 수량 사용 (불일치 없음)."""
        config = _make_config()

        mock_exchange = AsyncMock()
        mock_exchange.fetch_ticker = AsyncMock(
            return_value=MagicMock(last=67500, ask=67500)
        )
        mock_exchange.fetch_balance = AsyncMock(
            return_value={
                "BTC": _make_balance(free=0.00074),  # 체결 수량과 동일
            }
        )

        mock_md = AsyncMock()
        mock_md.get_current_price = AsyncMock(return_value=67500)
        mock_md.get_ticker = AsyncMock(return_value=MagicMock(last=67500, ask=67500))
        mock_md.get_candles = AsyncMock(side_effect=Exception("no candles"))

        # 매수 주문
        buy_order = MagicMock()
        buy_order.status = "filled"
        buy_order.fee = 0.05
        buy_order.id = 1
        buy_order.exchange_order_id = "buy-123"
        buy_order.executed_quantity = 0.00074  # stepSize 내림
        buy_order.executed_price = 67500

        # 매도 주문
        sell_order = MagicMock()
        sell_order.status = "filled"
        sell_order.fee = 0.03
        sell_order.id = 2
        sell_order.exchange_order_id = "sell-123"
        sell_order.executed_quantity = 0.00074
        sell_order.executed_price = 68000

        mock_om = AsyncMock()
        mock_om.create_order = AsyncMock(side_effect=[buy_order, sell_order])

        # 실제 PortfolioManager 대신 mock PM 사용하여 전달 인자만 검증
        mock_pm = MagicMock()
        mock_pm.cash_balance = 100.0
        mock_pm._sync_lock = asyncio.Lock()
        mock_pm._is_paper = False
        mock_pm._exchange_name = "binance_spot"
        mock_pm.update_position_on_buy = AsyncMock()
        mock_pm.update_position_on_sell = AsyncMock()
        mock_pm.reconcile_cash_from_db = AsyncMock()
        mock_pm.get_portfolio_summary = AsyncMock(
            return_value={
                "total_value_krw": 100,
                "cash_balance_krw": 50,
            }
        )
        mock_pm.take_snapshot = AsyncMock(return_value=None)

        engine = _make_engine(config, mock_exchange, mock_md, mock_om, mock_pm)

        # 1) 매수 실행
        buy_session = _make_session_no_position()
        buy_decision = _make_buy_decision(confidence=0.65)
        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            await engine._process_decision(buy_session, "BTC/USDT", buy_decision)

        # 매수 시 DB에 0.00074 저장 확인
        buy_call = mock_pm.update_position_on_buy.call_args
        buy_qty = buy_call[0][2]
        assert buy_qty == 0.00074

        # 2) 매도 실행 (stop sell)
        position = MagicMock(spec=Position)
        position.quantity = 0.00074  # DB에 올바른 값 저장됨
        position.average_buy_price = 67500
        position.total_invested = 0.00074 * 67500

        sell_session = AsyncMock()
        engine._position_trackers["BTC/USDT"] = PositionTracker(
            entry_price=67500,
            extreme_price=68000,
        )

        with patch("engine.trading_engine.emit_event", new_callable=AsyncMock):
            await engine._execute_stop_sell(
                sell_session,
                "BTC/USDT",
                position,
                68000,
                "take_profit",
            )

        # 매도 시 실잔고(0.00074) == DB(0.00074) → 클램핑 불필요
        sell_call = mock_om.create_order.call_args_list[1]
        sell_qty = sell_call[0][3]
        assert sell_qty == 0.00074, "매수/매도 수량 일관성"
