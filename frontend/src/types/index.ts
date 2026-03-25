// ── Exchange ─────────────────────────────────────────────────
export type ExchangeName = 'binance_futures' | 'binance_spot' | 'binance_surge' | 'bithumb'

export interface ExchangeInfo {
  exchanges: ExchangeName[]
  default: ExchangeName
}

// ── Portfolio ────────────────────────────────────────────────
export interface Position {
  symbol: string
  quantity: number
  average_buy_price: number
  current_price: number
  current_value: number
  unrealized_pnl: number
  unrealized_pnl_pct: number
  total_invested?: number | null
  margin_used?: number | null
  entered_at?: string | null
  // Futures-specific
  direction?: string | null
  leverage?: number | null
  liquidation_price?: number | null
  // SL/TP target prices + tracker params
  stop_loss_price?: number | null
  take_profit_price?: number | null
  stop_loss_pct?: number | null
  take_profit_pct?: number | null
  trailing_activation_pct?: number | null
  trailing_stop_pct?: number | null
  trailing_active?: boolean | null
  highest_price?: number | null
  max_hold_hours?: number | null
  is_surge?: boolean | null
}

export interface PortfolioSummary {
  exchange?: ExchangeName
  total_value_krw: number
  cash_balance_krw: number
  invested_value_krw: number
  initial_balance_krw: number
  realized_pnl: number
  unrealized_pnl: number
  total_pnl: number
  total_pnl_pct: number
  total_fees: number
  trade_count: number
  peak_value: number
  drawdown_pct: number
  positions: Position[]
}

export interface PortfolioHistoryPoint {
  timestamp: string
  total_value: number
  cash_balance: number
  unrealized_pnl: number
  drawdown_pct: number
}

// ── Orders / Trades ──────────────────────────────────────────
export interface ContributingStrategy {
  name: string
  signal: string
  confidence: number
  reason: string
}

export interface Order {
  id: number
  exchange?: ExchangeName
  symbol: string
  side: 'buy' | 'sell'
  order_type: string
  status: string
  requested_price: number | null
  executed_price: number | null
  requested_quantity: number
  executed_quantity: number | null
  fee: number
  is_paper: boolean
  // Futures-specific
  direction?: string | null
  leverage?: number | null
  margin_used?: number | null
  // PnL (sell/close orders)
  entry_price?: number | null
  realized_pnl?: number | null
  realized_pnl_pct?: number | null
  // Strategy attribution
  strategy_name: string
  signal_confidence: number | null
  signal_reason: string | null
  combined_score: number | null
  contributing_strategies: ContributingStrategy[] | null
  created_at: string
  filled_at: string | null
}

export interface TradeSummary {
  period: string
  total_trades: number
  buy_count: number
  sell_count: number
  winning_trades: number
  losing_trades: number
  win_rate: number
  total_pnl: number
}

// ── Strategies ───────────────────────────────────────────────
export interface Strategy {
  name: string
  display_name: string
  applicable_market_types: string[]
  default_coins: string[]
  required_timeframe: string
  params: Record<string, number>
  current_weight: number
}

export interface StrategyPerformance {
  strategy_name: string
  total_trades: number
  winning_trades: number
  losing_trades: number
  win_rate: number
  total_pnl: number
  avg_return_pct: number
}

// ── Strategy Logs ────────────────────────────────────────────
export interface StrategyLog {
  id: number
  strategy_name: string
  symbol: string
  signal_type: 'BUY' | 'SELL' | 'HOLD' | null
  confidence: number | null
  reason: string | null
  indicators: Record<string, number | null> | null
  was_executed: boolean
  order_id: number | null
  logged_at: string
}

// ── Agents ───────────────────────────────────────────────────
export interface V2Regime {
  regime: string
  confidence: number
  adx: number
  atr_pct: number
  trend_direction: number
  timestamp: string
}

export interface MarketAnalysis {
  state: string
  confidence: number
  volatility_level: string
  recommended_weights: Record<string, number>
  reasoning: string
  v2_regime?: V2Regime
}

export interface RiskAlert {
  level: 'info' | 'warning' | 'critical'
  message: string
  action: string
  affected_coins: string[]
  details: Record<string, unknown>
}

export interface AgentLog {
  id: number
  agent_name: string
  analysis_type: string | null
  result: Record<string, unknown>
  risk_level: string | null
  analyzed_at: string
}

// ── Engine ───────────────────────────────────────────────────
export interface EngineStatus {
  exchange?: ExchangeName
  is_running: boolean
  mode: 'paper' | 'live'
  evaluation_interval_sec: number
  tracked_coins: string[]
  daily_trade_count: number
  strategies_active: string[]
  min_confidence?: number
}

// ── Rotation Monitor ────────────────────────────────────────
export interface SurgeScore {
  symbol: string
  score: number
  above_threshold: boolean
}

export interface RotationStatus {
  exchange?: ExchangeName
  rotation_enabled: boolean
  surge_threshold: number
  market_state: string
  current_surge_symbol: string | null
  last_rotation_time: string | null
  last_scan_time: string | null
  rotation_cooldown_sec: number
  tracked_coins: string[]
  rotation_coins: string[]
  surge_scores: SurgeScore[]
}

// ── Surge Scan ──────────────────────────────────────────────
export interface SurgeScanScore {
  symbol: string
  score: number
  vol_ratio: number
  price_chg: number
  rsi: number
  last_price: number
  has_position: boolean
  direction: string | null
  pnl_pct: number | null
}

export interface SurgeScanStatus {
  scan_symbols_count: number
  open_positions: number
  daily_trades: number
  daily_limit: number
  daily_losses: number
  consecutive_losses: number
  paused: boolean
  scan_interval_sec: number
  leverage: number
  last_scan_time: string | null
  scores: SurgeScanScore[]
}

// ── Server Events ───────────────────────────────────────────
export interface ServerEvent {
  id: number
  level: 'info' | 'warning' | 'error' | 'critical'
  category: 'engine' | 'trade' | 'futures_trade' | 'risk' | 'rotation' | 'strategy' | 'signal' | 'health' | 'recovery' | 'system'
  title: string
  detail: string | null
  metadata: Record<string, unknown> | null
  created_at: string
}

// ── Capital Transactions ─────────────────────────────────────
export interface CapitalTransaction {
  id: number
  exchange: ExchangeName
  tx_type: 'deposit' | 'withdrawal'
  amount: number
  currency: string
  note: string | null
  source: 'manual' | 'auto_detected' | 'seed'
  confirmed: boolean
  created_at: string
}

export interface CapitalSummary {
  exchange: string
  total_deposits: number
  total_withdrawals: number
  net_capital: number
  currency: string
  transaction_count: number
}

// ── Daily PnL ───────────────────────────────────────────────
export interface DailyPnL {
  date: string
  open_value: number
  close_value: number
  daily_pnl: number
  daily_pnl_pct: number
  realized_pnl: number
  total_fees: number
  trade_count: number
  buy_count: number
  sell_count: number
  win_count: number
  loss_count: number
}

// ── WebSocket Events ─────────────────────────────────────────
export type WsEvent =
  | { event: 'portfolio_update'; data: PortfolioSummary }
  | { event: 'trade_executed'; data: { symbol: string; side: string; price: number; strategy: string; confidence: number; reason: string } }
  | { event: 'strategy_signal'; data: { strategy: string; symbol: string; signal: string; confidence: number } }
  | { event: 'agent_alert'; data: { agent: string; level: string; message: string } }
  | { event: 'price_update'; data: { symbol: string; price: number; change_pct: number } }
  | { event: 'server_event'; data: ServerEvent }
  | { event: 'pong' }
