import axios from 'axios'
import type {
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
} from '../types'

const api = axios.create({
  baseURL: '/api/v1',
  timeout: 10_000,
})

// ── Portfolio ────────────────────────────────────────────────
export const getPortfolioSummary = () =>
  api.get<PortfolioSummary>('/portfolio/summary').then((r) => r.data)

export const getPortfolioHistory = (period = '7d') =>
  api.get<PortfolioHistoryPoint[]>(`/portfolio/history?period=${period}`).then((r) => r.data)

// ── Trades ───────────────────────────────────────────────────
export const getTrades = (params?: {
  page?: number
  size?: number
  symbol?: string
  strategy?: string
  side?: string
}) => api.get<Order[]>('/trades', { params }).then((r) => r.data)

export const getTradeDetail = (id: number) =>
  api.get<Order>(`/trades/${id}`).then((r) => r.data)

export const getTradeSummary = (period = '7d') =>
  api.get<TradeSummary>(`/trades/summary?period=${period}`).then((r) => r.data)

// ── Strategies ───────────────────────────────────────────────
export const getStrategies = () =>
  api.get<Strategy[]>('/strategies').then((r) => r.data)

export const getStrategyPerformance = (name: string, period = '30d') =>
  api.get<StrategyPerformance>(`/strategies/${name}/performance?period=${period}`).then((r) => r.data)

export const compareStrategies = (period = '30d') =>
  api.get<StrategyPerformance[]>(`/strategies/comparison?period=${period}`).then((r) => r.data)

export const updateStrategyParams = (name: string, params: Record<string, number>) =>
  api.put(`/strategies/${name}/params`, { params }).then((r) => r.data)

export const updateStrategyWeight = (name: string, weight: number) =>
  api.put(`/strategies/${name}/weight`, { weight }).then((r) => r.data)

export const getStrategyLogs = (params?: {
  symbol?: string
  strategy?: string
  page?: number
  size?: number
}) => api.get<StrategyLog[]>('/strategies/logs', { params }).then((r) => r.data)

// ── Engine ───────────────────────────────────────────────────
export const getEngineStatus = () =>
  api.get<EngineStatus>('/engine/status').then((r) => r.data)

export const startEngine = () =>
  api.post('/engine/start').then((r) => r.data)

export const stopEngine = () =>
  api.post('/engine/stop').then((r) => r.data)

// ── Agents ───────────────────────────────────────────────────
export const getMarketAnalysis = () =>
  api.get<MarketAnalysis>('/agents/market-analysis/latest').then((r) => r.data)

export const getMarketAnalysisHistory = (limit = 50) =>
  api.get<AgentLog[]>(`/agents/market-analysis/history?limit=${limit}`).then((r) => r.data)

export const getRiskAlerts = () =>
  api.get<RiskAlert[]>('/agents/risk/alerts').then((r) => r.data)

export const getRiskHistory = (limit = 50) =>
  api.get<AgentLog[]>(`/agents/risk/history?limit=${limit}`).then((r) => r.data)

// ── Trade Review Agent ────────────────────────────────────────
export const getTradeReview = () =>
  api.get('/agents/trade-review/latest').then((r) => r.data)

export const triggerTradeReview = () =>
  api.post('/agents/trade-review/run').then((r) => r.data)

export const getTradeReviewHistory = (limit = 50) =>
  api.get<AgentLog[]>(`/agents/trade-review/history?limit=${limit}`).then((r) => r.data)
