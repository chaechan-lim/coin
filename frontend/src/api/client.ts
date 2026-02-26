import axios from 'axios'
import type {
  ExchangeName,
  ExchangeInfo,
  PortfolioSummary,
  PortfolioHistoryPoint,
  Order,
  TradeSummary,
  Strategy,
  StrategyPerformance,
  StrategyLog,
  MarketAnalysis,
  RiskAlert,
  AgentLog,
  EngineStatus,
  RotationStatus,
  ServerEvent,
} from '../types'

const api = axios.create({
  baseURL: '/api/v1',
  timeout: 10_000,
})

// ── Exchanges ───────────────────────────────────────────────
export const getExchanges = () =>
  api.get<ExchangeInfo>('/exchanges').then((r) => r.data)

// ── Portfolio ────────────────────────────────────────────────
export const getPortfolioSummary = (exchange: ExchangeName = 'bithumb') =>
  api.get<PortfolioSummary>('/portfolio/summary', { params: { exchange } }).then((r) => r.data)

export const getPortfolioHistory = (period = '7d', exchange: ExchangeName = 'bithumb') =>
  api.get<PortfolioHistoryPoint[]>('/portfolio/history', { params: { period, exchange } }).then((r) => r.data)

// ── Trades ───────────────────────────────────────────────────
export const getTrades = (params?: {
  page?: number
  size?: number
  symbol?: string
  strategy?: string
  side?: string
  exchange?: ExchangeName
}) => api.get<Order[]>('/trades', { params: { exchange: 'bithumb', ...params } }).then((r) => r.data)

export const getTradeDetail = (id: number) =>
  api.get<Order>(`/trades/${id}`).then((r) => r.data)

export const getTradeSummary = (period = '7d', exchange: ExchangeName = 'bithumb') =>
  api.get<TradeSummary>('/trades/summary', { params: { period, exchange } }).then((r) => r.data)

// ── Strategies ───────────────────────────────────────────────
export const getStrategies = (exchange: ExchangeName = 'bithumb') =>
  api.get<Strategy[]>('/strategies', { params: { exchange } }).then((r) => r.data)

export const getStrategyPerformance = (name: string, period = '30d', exchange: ExchangeName = 'bithumb') =>
  api.get<StrategyPerformance>(`/strategies/${name}/performance`, { params: { period, exchange } }).then((r) => r.data)

export const compareStrategies = (period = '30d', exchange: ExchangeName = 'bithumb') =>
  api.get<StrategyPerformance[]>('/strategies/comparison', { params: { period, exchange } }).then((r) => r.data)

export const updateStrategyParams = (name: string, params: Record<string, number>) =>
  api.put(`/strategies/${name}/params`, { params }).then((r) => r.data)

export const updateStrategyWeight = (name: string, weight: number) =>
  api.put(`/strategies/${name}/weight`, { weight }).then((r) => r.data)

export const getStrategyLogs = (params?: {
  symbol?: string
  strategy?: string
  page?: number
  size?: number
  exchange?: ExchangeName
}) => api.get<StrategyLog[]>('/strategies/logs', { params: { exchange: 'bithumb', ...params } }).then((r) => r.data)

// ── Engine ───────────────────────────────────────────────────
export const getEngineStatus = (exchange: ExchangeName = 'bithumb') =>
  api.get<EngineStatus>('/engine/status', { params: { exchange } }).then((r) => r.data)

export const startEngine = (exchange: ExchangeName = 'bithumb') =>
  api.post('/engine/start', null, { params: { exchange } }).then((r) => r.data)

export const stopEngine = (exchange: ExchangeName = 'bithumb') =>
  api.post('/engine/stop', null, { params: { exchange } }).then((r) => r.data)

export const getRotationStatus = (exchange: ExchangeName = 'bithumb') =>
  api.get<RotationStatus>('/engine/rotation-status', { params: { exchange } }).then((r) => r.data)

// ── Agents ───────────────────────────────────────────────────
export const getMarketAnalysis = (exchange: ExchangeName = 'bithumb') =>
  api.get<MarketAnalysis>('/agents/market-analysis/latest', { params: { exchange } }).then((r) => r.data)

export const getMarketAnalysisHistory = (limit = 50, exchange: ExchangeName = 'bithumb') =>
  api.get<AgentLog[]>('/agents/market-analysis/history', { params: { limit, exchange } }).then((r) => r.data)

export const getRiskAlerts = (exchange: ExchangeName = 'bithumb') =>
  api.get<RiskAlert[]>('/agents/risk/alerts', { params: { exchange } }).then((r) => r.data)

export const getRiskHistory = (limit = 50, exchange: ExchangeName = 'bithumb') =>
  api.get<AgentLog[]>('/agents/risk/history', { params: { limit, exchange } }).then((r) => r.data)

// ── Trade Review Agent ────────────────────────────────────────
export const getTradeReview = (exchange: ExchangeName = 'bithumb') =>
  api.get('/agents/trade-review/latest', { params: { exchange } }).then((r) => r.data)

export const triggerTradeReview = (exchange: ExchangeName = 'bithumb') =>
  api.post('/agents/trade-review/run', null, { params: { exchange } }).then((r) => r.data)

export const getTradeReviewHistory = (limit = 50, exchange: ExchangeName = 'bithumb') =>
  api.get<AgentLog[]>('/agents/trade-review/history', { params: { limit, exchange } }).then((r) => r.data)

// ── Server Events ───────────────────────────────────────────
export const getServerEvents = (params?: {
  page?: number
  size?: number
  level?: string
  category?: string
}) => api.get<ServerEvent[]>('/events', { params }).then((r) => r.data)

export const getServerEventCounts = () =>
  api.get<Record<string, number>>('/events/counts').then((r) => r.data)
