import axios from 'axios'
import type {
  ExchangeName,
  ExchangeInfo,
  PortfolioSummary,
  PortfolioHistoryPoint,
  DailyPnL,
  Order,
  TradeSummary,
  Strategy,
  StrategyPerformance,
  StrategyLog,
  MarketAnalysis,
  RiskAlert,
  AgentLog,
  EngineStatus,
  DonchianSpotStatus,
  RndEngineStatus,
  RotationStatus,
  ServerEvent,
  CapitalTransaction,
  CapitalSummary,
  ResearchOverview,
  ResearchAutoReviewStatus,
  ResearchStageHistoryEntry,
  ResearchStageState,
  ResearchStageUpdateRequest,
  FuturesRndStatus,
  PairsTradeGroup,
  PairsTradeGroupDetail,
  DonchianFuturesTradeGroup,
  DonchianFuturesTradeGroupDetail,
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

export const getDailyPnl = (days = 30, exchange: ExchangeName = 'bithumb') =>
  api.get<DailyPnL[]>('/portfolio/daily-pnl', { params: { days, exchange } }).then((r) => r.data)

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

export const getPairsTradeGroups = (params?: {
  page?: number
  size?: number
  status?: 'open' | 'closed' | 'all'
  exchange?: Extract<ExchangeName, 'binance_futures' | 'binance_spot'> | 'binance_pairs'
}) => api.get<PairsTradeGroup[]>('/trades/pairs/groups', { params: { exchange: 'binance_pairs', ...params } }).then((r) => r.data)

export const getDonchianFuturesTradeGroups = (params?: {
  page?: number
  size?: number
  status?: 'open' | 'closed' | 'all'
  exchange?: 'binance_donchian_futures'
}) => api.get<DonchianFuturesTradeGroup[]>('/trades/donchian-futures/groups', { params: { exchange: 'binance_donchian_futures', ...params } }).then((r) => r.data)

export const getPairsTradeGroupDetail = (tradeId: string, exchange: 'binance_pairs' = 'binance_pairs') =>
  api.get<PairsTradeGroupDetail>(`/trades/pairs/groups/${tradeId}`, { params: { exchange } }).then((r) => r.data)

export const getDonchianFuturesTradeGroupDetail = (
  tradeId: string,
  exchange: 'binance_donchian_futures' = 'binance_donchian_futures'
) => api.get<DonchianFuturesTradeGroupDetail>(`/trades/donchian-futures/groups/${tradeId}`, { params: { exchange } }).then((r) => r.data)

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

export const getSurgeScanStatus = () =>
  api.get('/engine/surge-scan').then((r) => r.data)

export const getFuturesRndStatus = () =>
  api.get<FuturesRndStatus | { status: string }>('/engine/futures-rnd/status').then((r) => r.data)

export const getPairsEngineStatus = () =>
  api.get<RndEngineStatus>('/engine/pairs/status').then((r) => r.data)

export const getDonchianEngineStatus = () =>
  api.get<DonchianSpotStatus>('/engine/donchian/status').then((r) => r.data)

export const getDonchianFuturesEngineStatus = () =>
  api.get<RndEngineStatus>('/engine/donchian-futures/status').then((r) => r.data)

export const getMomentumEngineStatus = () =>
  api.get<RndEngineStatus>('/engine/status', { params: { exchange: 'binance_momentum' } }).then((r) => r.data)

export const getHMMEngineStatus = () =>
  api.get<RndEngineStatus>('/engine/status', { params: { exchange: 'binance_hmm' } }).then((r) => r.data)

export const getFGDCAEngineStatus = () =>
  api.get<RndEngineStatus>('/engine/status', { params: { exchange: 'binance_fgdca' } }).then((r) => r.data)

export const getRndOverview = () =>
  api.get<any>('/engine/rnd/overview').then((r) => r.data)

export const getResearchOverview = () =>
  api.get<ResearchOverview>('/research/overview', { params: { include_auto_review: true } }).then((r) => r.data)

export const getResearchAutoReviewStatus = () =>
  api.get<ResearchAutoReviewStatus>('/research/auto-review/status').then((r) => r.data)

export const getResearchStages = () =>
  api.get<ResearchStageState[]>('/research/stages').then((r) => r.data)

export const getResearchStageHistory = (params?: { candidate_key?: string; limit?: number }) =>
  api.get<ResearchStageHistoryEntry[]>('/research/stage-history', { params }).then((r) => r.data)

export const updateResearchStage = (candidateKey: string, payload: ResearchStageUpdateRequest) =>
  api.put<ResearchStageState>(`/research/candidates/${candidateKey}/stage`, payload).then((r) => r.data)

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

// ── Performance Analytics ────────────────────────────────────
export const getPerformanceAnalytics = (exchange: ExchangeName = 'bithumb') =>
  api.get('/agents/performance/latest', { params: { exchange } }).then((r) => r.data)

export const triggerPerformanceAnalysis = (exchange: ExchangeName = 'bithumb') =>
  api.post('/agents/performance/run', null, { params: { exchange } }).then((r) => r.data)

// ── Strategy Advisor ────────────────────────────────────────
export const getStrategyAdvice = (exchange: ExchangeName = 'bithumb') =>
  api.get('/agents/strategy-advice/latest', { params: { exchange } }).then((r) => r.data)

export const triggerStrategyAdvice = (exchange: ExchangeName = 'bithumb') =>
  api.post('/agents/strategy-advice/run', null, { params: { exchange } }).then((r) => r.data)

// ── Server Events ───────────────────────────────────────────
export const getServerEvents = (params?: {
  page?: number
  size?: number
  level?: string
  category?: string
}) => api.get<ServerEvent[]>('/events', { params }).then((r) => r.data)

export const getServerEventCounts = () =>
  api.get<Record<string, number>>('/events/counts').then((r) => r.data)

// ── Capital Transactions ───────────────────────────────────
export const getCapitalTransactions = (exchange: ExchangeName = 'bithumb') =>
  api.get<CapitalTransaction[]>('/capital/transactions', { params: { exchange } }).then((r) => r.data)

export const getCapitalSummary = (exchange: ExchangeName = 'bithumb') =>
  api.get<CapitalSummary>('/capital/summary', { params: { exchange } }).then((r) => r.data)

export const createCapitalTransaction = (data: {
  exchange: string
  tx_type: string
  amount: number
  note?: string
}) => api.post<CapitalTransaction>('/capital/transactions', data).then((r) => r.data)

export const confirmCapitalTransaction = (id: number) =>
  api.post<CapitalTransaction>(`/capital/confirm/${id}`).then((r) => r.data)

export const deleteCapitalTransaction = (id: number) =>
  api.delete(`/capital/transactions/${id}`).then((r) => r.data)
