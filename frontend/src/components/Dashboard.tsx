import { useState, useCallback } from 'react'
import { useQueryClient } from '@tanstack/react-query'
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
import type { WsEvent, ServerEvent } from '../types'
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

export function Dashboard() {
  const [tab, setTab] = useState<TabId>('overview')
  const [liveEvents, setLiveEvents] = useState<string[]>([])
  const [realtimeServerEvents, setRealtimeServerEvents] = useState<ServerEvent[]>([])
  const qc = useQueryClient()

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

  return (
    <div className="min-h-screen bg-gray-900 text-white">
      {/* Header */}
      <header className="border-b border-gray-700 px-4 md:px-6 py-3 flex items-center justify-between">
        <div className="flex items-center gap-2 md:gap-3 min-w-0">
          <span className="text-lg md:text-xl font-bold whitespace-nowrap">🪙 코인 자동 매매</span>
          <span className="text-xs text-gray-500 hidden md:block">빗썸 기반 24시간 트레이딩</span>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <div className={`w-2 h-2 rounded-full ${connected ? 'bg-green-500 animate-pulse' : 'bg-red-500'}`} />
          <span className="text-xs text-gray-400 hidden sm:inline">{connected ? '연결됨' : '연결 중...'}</span>
        </div>
      </header>

      {/* Tabs - horizontal scroll on mobile */}
      <nav className="border-b border-gray-700 px-2 md:px-6">
        <div className="flex gap-0.5 md:gap-1 overflow-x-auto scrollbar-hide">
          {TABS.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`px-3 md:px-4 py-3 text-xs md:text-sm font-medium transition-colors border-b-2 -mb-px whitespace-nowrap ${
                tab === t.id
                  ? 'border-blue-500 text-blue-400'
                  : 'border-transparent text-gray-400 hover:text-white'
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
      </nav>

      {/* Content */}
      <main className="p-3 md:p-6 space-y-3 md:space-y-4 max-w-7xl mx-auto">
        {tab === 'overview' && (
          <>
            <EngineControl liveEvents={liveEvents} />
            <PortfolioSummary />
            <PortfolioChart />
          </>
        )}
        {tab === 'trades' && <TradeHistory />}
        {tab === 'logs' && <OrderLog />}
        {tab === 'strategies' && <StrategyPerformance />}
        {tab === 'agents' && <AgentStatus />}
        {tab === 'rotation' && <RotationMonitor />}
        {tab === 'system' && <SystemLog realtimeEvents={realtimeServerEvents} />}
      </main>
    </div>
  )
}
