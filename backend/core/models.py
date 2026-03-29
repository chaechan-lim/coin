from datetime import datetime, timezone
from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    Boolean,
    ForeignKey,
    Text,
    Date,
    Index,
    UniqueConstraint,
    text,
)
from sqlalchemy import JSON, DateTime as _DateTime
from sqlalchemy.orm import DeclarativeBase, relationship

# PostgreSQL: TIMESTAMP WITH TIME ZONE 사용
DateTime = _DateTime(timezone=True)


def _utcnow():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class CoinConfig(Base):
    __tablename__ = "coin_configs"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), unique=True, nullable=False)  # "BTC/KRW"
    display_name = Column(String(50))
    is_active = Column(Boolean, default=True)
    strategies = Column(JSON, default=list)  # ["volatility_breakout", "rsi"]
    min_order_krw = Column(Float, default=5000)
    max_position_pct = Column(Float, default=0.30)
    created_at = Column(DateTime, default=_utcnow)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_symbol_created", "symbol", "created_at"),
        Index("ix_orders_status", "status"),
        Index("ix_orders_strategy", "strategy_name"),
        Index("ix_orders_exchange_created", "exchange", "created_at"),
    )

    id = Column(Integer, primary_key=True)
    exchange = Column(String(20), nullable=False, default="bithumb", server_default="bithumb")
    exchange_order_id = Column(String(100), nullable=True)
    symbol = Column(String(20), nullable=False)
    side = Column(String(4), nullable=False)  # "buy" / "sell"
    order_type = Column(String(10), default="limit")
    status = Column(String(20), default="pending")
    requested_price = Column(Float)
    executed_price = Column(Float, nullable=True)
    requested_quantity = Column(Float)
    executed_quantity = Column(Float, nullable=True)
    fee = Column(Float, default=0.0)
    fee_currency = Column(String(10), default="KRW")
    is_paper = Column(Boolean, default=True)

    # Futures-specific fields
    direction = Column(String(5), nullable=True)  # "long" / "short"
    leverage = Column(Integer, nullable=True)
    margin_used = Column(Float, nullable=True)

    # PnL (sell/close orders only)
    entry_price = Column(Float, nullable=True)         # 진입 평단가
    realized_pnl = Column(Float, nullable=True)        # 실현 손익 (금액)
    realized_pnl_pct = Column(Float, nullable=True)    # 실현 손익 (%)

    # Strategy attribution
    strategy_name = Column(String(50), nullable=False)
    signal_confidence = Column(Float)
    signal_reason = Column(Text)
    combined_score = Column(Float, nullable=True)
    contributing_strategies = Column(JSON, nullable=True)

    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)
    filled_at = Column(DateTime, nullable=True)

    trades = relationship("Trade", back_populates="order")


class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = (
        Index("ix_trades_symbol_executed", "symbol", "executed_at"),
        Index("ix_trades_exchange", "exchange"),
    )

    id = Column(Integer, primary_key=True)
    exchange = Column(String(20), nullable=False, default="bithumb", server_default="bithumb")
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    symbol = Column(String(20), nullable=False)
    side = Column(String(4), nullable=False)
    price = Column(Float, nullable=False)
    quantity = Column(Float, nullable=False)
    cost = Column(Float, nullable=False)
    fee = Column(Float, default=0.0)
    is_paper = Column(Boolean, default=True)
    executed_at = Column(DateTime, default=_utcnow)

    order = relationship("Order", back_populates="trades")


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("symbol", "exchange", name="uq_position_symbol_exchange"),
    )

    id = Column(Integer, primary_key=True)
    exchange = Column(String(20), nullable=False, default="bithumb", server_default="bithumb")
    symbol = Column(String(20), nullable=False)
    quantity = Column(Float, default=0.0)
    average_buy_price = Column(Float, default=0.0)
    total_invested = Column(Float, default=0.0)
    current_value = Column(Float, default=0.0)
    unrealized_pnl = Column(Float, default=0.0)
    unrealized_pnl_pct = Column(Float, default=0.0)
    is_paper = Column(Boolean, default=True)
    is_surge = Column(Boolean, default=False)
    # Futures-specific fields
    direction = Column(String(5), default="long")
    leverage = Column(Integer, default=1)
    liquidation_price = Column(Float, nullable=True)
    margin_used = Column(Float, default=0.0)
    entered_at = Column(DateTime, nullable=True)
    # PositionTracker 영속화 (SL/TP/trailing 재시작 복원)
    stop_loss_pct = Column(Float, nullable=True)
    take_profit_pct = Column(Float, nullable=True)
    trailing_activation_pct = Column(Float, nullable=True)
    trailing_stop_pct = Column(Float, nullable=True)
    trailing_active = Column(Boolean, default=False)
    highest_price = Column(Float, nullable=True)
    lowest_price = Column(Float, nullable=True)   # 숏 포지션 extreme_price (최저가)
    max_hold_hours = Column(Float, nullable=True)
    # 진입 전략 (페어링 매도: 진입 전략의 SELL만 허용)
    strategy_name = Column(String(50), nullable=True)
    # 매매 타이밍 추적 (재시작 시 쿨다운/washout 복원)
    last_trade_at = Column(DateTime, nullable=True)
    last_sell_at = Column(DateTime, nullable=True)
    last_sell_direction = Column(String(10), nullable=True)  # "long"/"short" (COIN-41)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"
    __table_args__ = (
        Index("ix_portfolio_snapshots_at", "snapshot_at"),
    )

    id = Column(Integer, primary_key=True)
    exchange = Column(String(20), nullable=False, default="bithumb", server_default="bithumb")
    total_value_krw = Column(Float, nullable=False)
    cash_balance_krw = Column(Float, nullable=False)
    invested_value_krw = Column(Float, nullable=False)
    realized_pnl = Column(Float, default=0.0)
    unrealized_pnl = Column(Float, default=0.0)
    peak_value = Column(Float, default=0.0)
    drawdown_pct = Column(Float, default=0.0)
    snapshot_at = Column(DateTime, default=_utcnow)


class StrategyLog(Base):
    __tablename__ = "strategy_logs"
    __table_args__ = (
        Index("ix_strategy_logs_lookup", "strategy_name", "symbol", "logged_at"),
    )

    id = Column(Integer, primary_key=True)
    exchange = Column(String(20), nullable=False, default="bithumb", server_default="bithumb")
    strategy_name = Column(String(50), nullable=False)
    symbol = Column(String(20), nullable=False)
    signal_type = Column(String(4))  # BUY/SELL/HOLD
    confidence = Column(Float)
    reason = Column(Text)
    indicators = Column(JSON)  # {"rsi": 28.5, "ma_short": 45000000}
    was_executed = Column(Boolean, default=False)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=True)
    logged_at = Column(DateTime, default=_utcnow)


class AgentAnalysisLog(Base):
    __tablename__ = "agent_analysis_logs"
    __table_args__ = (
        Index("ix_agent_logs_name_at", "agent_name", "analyzed_at"),
    )

    id = Column(Integer, primary_key=True)
    exchange = Column(String(20), nullable=False, default="bithumb", server_default="bithumb")
    agent_name = Column(String(50), nullable=False)
    analysis_type = Column(String(30))
    result = Column(JSON, nullable=False)
    recommended_weights = Column(JSON, nullable=True)
    risk_level = Column(String(20), nullable=True)
    analyzed_at = Column(DateTime, default=_utcnow)


class CapitalTransaction(Base):
    __tablename__ = "capital_transactions"
    __table_args__ = (
        Index("ix_capital_tx_exchange_at", "exchange", "created_at"),
        # exchange_tx_id가 NULL이 아닌 행만 (exchange, exchange_tx_id) 중복 방지
        # → 동시 스케줄러 틱에서 같은 tranId가 이중 삽입될 때 IntegrityError로 차단
        # Index(unique=True, postgresql_where=): PostgreSQL 부분 유니크 인덱스
        # SQLite: NULL!=NULL이므로 비부분 unique index와 실질적으로 동일하게 동작
        Index(
            "uq_capital_tx_exchange_txid",
            "exchange", "exchange_tx_id",
            unique=True,
            postgresql_where=text("exchange_tx_id IS NOT NULL"),
        ),
    )

    id = Column(Integer, primary_key=True)
    exchange = Column(String(20), nullable=False)        # "bithumb" / "binance_futures"
    tx_type = Column(String(15), nullable=False)         # "deposit" / "withdrawal"
    amount = Column(Float, nullable=False)               # KRW or USDT
    currency = Column(String(10), nullable=False)        # "KRW" / "USDT"
    note = Column(Text, nullable=True)
    source = Column(String(20), default="manual")        # "manual" / "auto_detected" / "seed"
    confirmed = Column(Boolean, default=True)
    exchange_tx_id = Column(String(100), nullable=True)  # 거래소 tx ID (자동 감지 시)
    created_at = Column(DateTime, default=_utcnow)


class DailyPnL(Base):
    __tablename__ = "daily_pnl"
    __table_args__ = (
        UniqueConstraint("exchange", "date", name="uq_daily_pnl_exchange_date"),
        Index("ix_daily_pnl_exchange_date", "exchange", "date"),
    )

    id = Column(Integer, primary_key=True)
    exchange = Column(String(20), nullable=False)
    date = Column(Date, nullable=False)
    open_value = Column(Float)
    close_value = Column(Float)
    daily_pnl = Column(Float, default=0.0)
    daily_pnl_pct = Column(Float, default=0.0)
    realized_pnl = Column(Float, default=0.0)
    total_fees = Column(Float, default=0.0)
    trade_count = Column(Integer, default=0)
    buy_count = Column(Integer, default=0)
    sell_count = Column(Integer, default=0)
    win_count = Column(Integer, default=0)
    loss_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=_utcnow)


class RegimeLog(Base):
    """레짐 변경 이력 (선물 v2)."""
    __tablename__ = "regime_logs"
    __table_args__ = (
        Index("ix_regime_logs_at", "detected_at"),
    )

    id = Column(Integer, primary_key=True)
    exchange = Column(String(20), default="binance_futures")
    symbol = Column(String(20), default="BTC/USDT")
    regime = Column(String(20), nullable=False)
    prev_regime = Column(String(20), nullable=True)
    confidence = Column(Float)
    adx = Column(Float)
    bb_width = Column(Float)
    atr_pct = Column(Float)
    volume_ratio = Column(Float)
    detected_at = Column(DateTime, default=_utcnow)


class ServerEvent(Base):
    __tablename__ = "server_events"
    __table_args__ = (
        Index("ix_server_events_created", "created_at"),
        Index("ix_server_events_level_cat", "level", "category", "created_at"),
    )

    id = Column(Integer, primary_key=True)
    level = Column(String(10), nullable=False, default="info")
    category = Column(String(20), nullable=False, default="system")
    title = Column(String(200), nullable=False)
    detail = Column(Text, nullable=True)
    metadata_ = Column("metadata", JSON, nullable=True)
    created_at = Column(DateTime, default=_utcnow)
