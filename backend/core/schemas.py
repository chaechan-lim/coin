from pydantic import BaseModel
from datetime import datetime
from typing import Optional


# -- Portfolio --
class PositionResponse(BaseModel):
    symbol: str
    quantity: float
    average_buy_price: float
    current_price: float
    current_value: float
    unrealized_pnl: float
    unrealized_pnl_pct: float
    # Futures-specific (optional)
    direction: Optional[str] = None
    leverage: Optional[int] = None
    liquidation_price: Optional[float] = None


class PortfolioSummaryResponse(BaseModel):
    exchange: str = "bithumb"
    total_value_krw: float
    cash_balance_krw: float
    invested_value_krw: float
    initial_balance_krw: float
    realized_pnl: float
    unrealized_pnl: float
    total_pnl: float
    total_pnl_pct: float
    total_fees: float
    trade_count: int
    peak_value: float
    drawdown_pct: float
    positions: list[PositionResponse]


class PortfolioHistoryPoint(BaseModel):
    timestamp: datetime
    total_value: float
    cash_balance: float
    unrealized_pnl: float
    drawdown_pct: float


# -- Trades --
class TradeResponse(BaseModel):
    id: int
    order_id: int
    symbol: str
    side: str
    price: float
    quantity: float
    cost: float
    fee: float
    is_paper: bool
    executed_at: datetime


class OrderResponse(BaseModel):
    id: int
    exchange: str = "bithumb"
    symbol: str
    side: str
    order_type: str
    status: str
    requested_price: Optional[float]
    executed_price: Optional[float]
    requested_quantity: float
    executed_quantity: Optional[float]
    fee: float
    is_paper: bool
    # Futures-specific
    direction: Optional[str] = None
    leverage: Optional[int] = None
    margin_used: Optional[float] = None
    # Strategy attribution
    strategy_name: str
    signal_confidence: Optional[float]
    signal_reason: Optional[str]
    combined_score: Optional[float]
    contributing_strategies: Optional[list]
    created_at: datetime
    filled_at: Optional[datetime]


# -- Strategies --
class StrategyResponse(BaseModel):
    name: str
    display_name: str
    applicable_market_types: list[str]
    default_coins: list[str]
    required_timeframe: str
    params: dict
    current_weight: float


class StrategyPerformanceResponse(BaseModel):
    strategy_name: str
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    total_pnl: float
    avg_return_pct: float


class StrategyParamsUpdate(BaseModel):
    params: dict


class StrategyWeightUpdate(BaseModel):
    weight: float


# -- Strategy Logs --
class StrategyLogResponse(BaseModel):
    id: int
    strategy_name: str
    symbol: str
    signal_type: Optional[str]
    confidence: Optional[float]
    reason: Optional[str]
    indicators: Optional[dict]
    was_executed: bool
    order_id: Optional[int]
    logged_at: datetime


# -- Agents --
class MarketAnalysisResponse(BaseModel):
    state: str
    confidence: float
    volatility_level: str
    recommended_weights: dict
    reasoning: str
    analyzed_at: datetime


class RiskAlertResponse(BaseModel):
    level: str
    message: str
    action: str
    affected_coins: list[str]
    details: dict


class AgentLogResponse(BaseModel):
    id: int
    agent_name: str
    analysis_type: Optional[str]
    result: dict
    risk_level: Optional[str]
    analyzed_at: datetime


# -- Settings --
class EngineStatusResponse(BaseModel):
    exchange: str = "bithumb"
    is_running: bool
    mode: str
    evaluation_interval_sec: int
    tracked_coins: list[str]
    daily_trade_count: int
    strategies_active: list[str]


class ModeUpdate(BaseModel):
    mode: str  # "paper" or "live"


# -- Server Events --
class ServerEventResponse(BaseModel):
    id: int
    level: str
    category: str
    title: str
    detail: Optional[str]
    metadata: Optional[dict]
    created_at: datetime


# -- Rotation Monitor --
# -- Capital Transactions --
class CapitalTransactionCreate(BaseModel):
    exchange: str
    tx_type: str  # "deposit" / "withdrawal"
    amount: float
    note: str | None = None


class CapitalTransactionResponse(BaseModel):
    id: int
    exchange: str
    tx_type: str
    amount: float
    currency: str
    note: str | None
    source: str
    confirmed: bool
    created_at: datetime


class CapitalSummaryResponse(BaseModel):
    exchange: str
    total_deposits: float
    total_withdrawals: float
    net_capital: float
    currency: str
    transaction_count: int


class SurgeScoreItem(BaseModel):
    symbol: str
    score: float
    above_threshold: bool


class RotationStatusResponse(BaseModel):
    exchange: str = "bithumb"
    rotation_enabled: bool
    surge_threshold: float
    market_state: str
    current_surge_symbol: Optional[str]
    last_rotation_time: Optional[datetime]
    last_scan_time: Optional[datetime]
    rotation_cooldown_sec: int
    tracked_coins: list[str]
    rotation_coins: list[str]
    surge_scores: list[SurgeScoreItem]
