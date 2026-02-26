import { useState, useCallback } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { PortfolioSummary } from './PortfolioSummary'
import { PortfolioChart } from './PortfolioChart'
import { TradeHistory } from './TradeHistory'
import { StrategyPerformance } from './StrategyPerformance'
import { AgentStatus } from './AgentStatus'
import { OrderLog } from './OrderLog'
import { EngineControl } from './EngineControl'
import { RotationMonitor } from './RotationMonitor'
import { SystemLog } from './SystemLog'
import { useWebSocket } from '../hooks/useWebSocket'
import { getExchanges } from '../api/client'
import type { WsEvent, ServerEvent, ExchangeName } from '../types'
import { format } from 'date-fns'

const TABS = [
  { id: 'overview', label: '개요' },
  { id: 'trades', label: '거래 이력' },
  { id: 'logs', label: '신호 로그' },
  { id: 'strategies', label: '전략 성과' },
  { id: 'agents', label: '에이전트' },
  { id: 'rotation', label: '로테이션' },
  { id: 'system', label: '시스템 로그' },
] as const

type TabId = (typeof TABS)[number]['id']

const EXCHANGE_LABELS: Record<ExchangeName, string> = {
  bithumb: '빗썸 현물',
  binance_futures: '바이낸스 선물',
}

export function Dashboard() {
  const [tab, setTab] = useState<TabId>('overview')
  const [exchange, setExchange] = useState<ExchangeName>('bithumb')
  const [liveEvents, setLiveEvents] = useState<string[]>([])
  const [realtimeServerEvents, setRealtimeServerEvents] = useState<ServerEvent[]>([])
  const qc = useQueryClient()

  // 사용 가능한 거래소 목록 조회
  const { data: exchangeInfo } = useQuery({
    queryKey: ['exchanges'],
    queryFn: getExchanges,
    staleTime: 60_000,
  })

  const exchanges = exchangeInfo?.exchanges ?? ['bithumb']

  const onMessage = useCallback(
    (event: WsEvent) => {
      const time = format(new Date(), 'HH:mm:ss')

      if (event.event === 'portfolio_update') {
        qc.invalidateQueries({ queryKey: ['portfolio'] })
        setLiveEvents((prev) => [`[${time}] 포트폴리오 업데이트`, ...prev.slice(0, 49)])
      } else if (event.event === 'trade_executed') {
        const d = event.data
        const side = d.side === 'buy' ? '▲매수' : '▼매도'
        setLiveEvents((prev) => [
          `[${time}] ${side} ${d.symbol} @ ${d.price.toLocaleString()} [${d.strategy}]`,
          ...prev.slice(0, 49),
        ])
        qc.invalidateQueries({ queryKey: ['trades'] })
        qc.invalidateQueries({ queryKey: ['portfolio'] })
      } else if (event.event === 'agent_alert') {
        const d = event.data
        setLiveEvents((prev) => [
          `[${time}] ⚠️ ${d.agent}: ${d.message}`,
          ...prev.slice(0, 49),
        ])
        qc.invalidateQueries({ queryKey: ['agents'] })
      } else if (event.event === 'strategy_signal') {
        const d = event.data
        if (d.signal !== 'HOLD') {
          setLiveEvents((prev) => [
            `[${time}] 신호: ${d.signal} ${d.symbol} [${d.strategy}] ${(d.confidence * 100).toFixed(0)}%`,
            ...prev.slice(0, 49),
          ])
        }
      } else if (event.event === 'server_event') {
        const d = event.data
        setRealtimeServerEvents((prev) => [d, ...prev.slice(0, 99)])
        qc.invalidateQueries({ queryKey: ['serverEvents'] })
      }
    },
    [qc]
  )

  const { connected } = useWebSocket(onMessage)

  // 통화 기호
  const currencySymbol = exchange === 'binance_futures' ? 'USDT' : '₩'

  return (
    <div className="min-h-screen bg-gray-900 text-white">
      {/* Header */}
      <header className="border-b border-gray-700 px-3 md:px-6 py-2.5 md:py-3 flex items-center justify-between">
        <div className="flex items-center gap-2 md:gap-3 min-w-0">
          <span className="text-base md:text-xl font-bold whitespace-nowrap">🪙 코인 자동 매매</span>
          <span className="text-xs text-gray-500 hidden md:block">듀얼 엔진 트레이딩</span>
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          <div className={`w-2 h-2 rounded-full ${connected ? 'bg-green-500 animate-pulse' : 'bg-red-500'}`} />
          <span className="text-xs text-gray-400">{connected ? '연결됨' : '연결 중...'}</span>
        </div>
      </header>

      {/* Exchange selector */}
      {exchanges.length > 1 && (
        <div className="border-b border-gray-700 px-3 md:px-6 py-1.5 flex gap-2">
          {exchanges.map((ex) => (
            <button
              key={ex}
              onClick={() => setExchange(ex)}
              className={`px-3 py-1 text-xs md:text-sm rounded-full font-medium transition-colors ${
                exchange === ex
                  ? 'bg-blue-600 text-white'
                  : 'bg-gray-800 text-gray-400 hover:bg-gray-700 hover:text-white'
              }`}
            >
              {EXCHANGE_LABELS[ex] ?? ex}
            </button>
          ))}
        </div>
      )}

      {/* Tabs - horizontal scroll on mobile */}
      <nav className="border-b border-gray-700 overflow-x-auto scrollbar-hide">
        <div className="flex min-w-max">
          {TABS.map((t) => {
            // 로테이션 탭은 빗썸에서만 표시
            if (t.id === 'rotation' && exchange !== 'bithumb') return null
            return (
              <button
                key={t.id}
                onClick={() => setTab(t.id)}
                className={`shrink-0 px-3 md:px-4 py-2.5 md:py-3 text-xs md:text-sm font-medium transition-colors border-b-2 -mb-px whitespace-nowrap ${
                  tab === t.id
                    ? 'border-blue-500 text-blue-400'
                    : 'border-transparent text-gray-400 hover:text-white active:text-white'
                }`}
              >
                {t.label}
              </button>
            )
          })}
        </div>
      </nav>

      {/* Content */}
      <main className="p-3 md:p-6 space-y-3 md:space-y-4 max-w-7xl mx-auto">
        {tab === 'overview' && (
          <>
            <EngineControl liveEvents={liveEvents} exchange={exchange} />
            <PortfolioSummary exchange={exchange} />
            <PortfolioChart exchange={exchange} />
          </>
        )}
        {tab === 'trades' && <TradeHistory exchange={exchange} />}
        {tab === 'logs' && <OrderLog exchange={exchange} />}
        {tab === 'strategies' && <StrategyPerformance exchange={exchange} />}
        {tab === 'agents' && <AgentStatus exchange={exchange} />}
        {tab === 'rotation' && exchange === 'bithumb' && <RotationMonitor />}
        {tab === 'system' && <SystemLog realtimeEvents={realtimeServerEvents} />}
      </main>
    </div>
  )
}
