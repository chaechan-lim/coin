from datetime import datetime, timezone
from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    DateTime,
    Boolean,
    ForeignKey,
    Text,
    Index,
)
from sqlalchemy import JSON
from sqlalchemy.orm import DeclarativeBase, relationship


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
    created_at = Column(DateTime, default=datetime.utcnow)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_symbol_created", "symbol", "created_at"),
        Index("ix_orders_status", "status"),
        Index("ix_orders_strategy", "strategy_name"),
    )

    id = Column(Integer, primary_key=True)
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

    # Strategy attribution
    strategy_name = Column(String(50), nullable=False)
    signal_confidence = Column(Float)
    signal_reason = Column(Text)
    combined_score = Column(Float, nullable=True)
    contributing_strategies = Column(JSON, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    filled_at = Column(DateTime, nullable=True)

    trades = relationship("Trade", back_populates="order")


class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = (
        Index("ix_trades_symbol_executed", "symbol", "executed_at"),
    )

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    symbol = Column(String(20), nullable=False)
    side = Column(String(4), nullable=False)
    price = Column(Float, nullable=False)
    quantity = Column(Float, nullable=False)
    cost = Column(Float, nullable=False)
    fee = Column(Float, default=0.0)
    is_paper = Column(Boolean, default=True)
    executed_at = Column(DateTime, default=datetime.utcnow)

    order = relationship("Order", back_populates="trades")


class Position(Base):
    __tablename__ = "positions"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), unique=True, nullable=False)
    quantity = Column(Float, default=0.0)
    average_buy_price = Column(Float, default=0.0)
    total_invested = Column(Float, default=0.0)
    current_value = Column(Float, default=0.0)
    unrealized_pnl = Column(Float, default=0.0)
    unrealized_pnl_pct = Column(Float, default=0.0)
    is_paper = Column(Boolean, default=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"
    __table_args__ = (
        Index("ix_portfolio_snapshots_at", "snapshot_at"),
    )

    id = Column(Integer, primary_key=True)
    total_value_krw = Column(Float, nullable=False)
    cash_balance_krw = Column(Float, nullable=False)
    invested_value_krw = Column(Float, nullable=False)
    realized_pnl = Column(Float, default=0.0)
    unrealized_pnl = Column(Float, default=0.0)
    peak_value = Column(Float, default=0.0)
    drawdown_pct = Column(Float, default=0.0)
    snapshot_at = Column(DateTime, default=datetime.utcnow)


class StrategyLog(Base):
    __tablename__ = "strategy_logs"
    __table_args__ = (
        Index("ix_strategy_logs_lookup", "strategy_name", "symbol", "logged_at"),
    )

    id = Column(Integer, primary_key=True)
    strategy_name = Column(String(50), nullable=False)
    symbol = Column(String(20), nullable=False)
    signal_type = Column(String(4))  # BUY/SELL/HOLD
    confidence = Column(Float)
    reason = Column(Text)
    indicators = Column(JSON)  # {"rsi": 28.5, "ma_short": 45000000}
    was_executed = Column(Boolean, default=False)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=True)
    logged_at = Column(DateTime, default=datetime.utcnow)


class AgentAnalysisLog(Base):
    __tablename__ = "agent_analysis_logs"
    __table_args__ = (
        Index("ix_agent_logs_name_at", "agent_name", "analyzed_at"),
    )

    id = Column(Integer, primary_key=True)
    agent_name = Column(String(50), nullable=False)
    analysis_type = Column(String(30))
    result = Column(JSON, nullable=False)
    recommended_weights = Column(JSON, nullable=True)
    risk_level = Column(String(20), nullable=True)
    analyzed_at = Column(DateTime, default=datetime.utcnow)


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
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
