import { useState, useCallback, useRef } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { PortfolioSummary } from './PortfolioSummary'
import { PortfolioChart } from './PortfolioChart'
import { TradeHistory } from './TradeHistory'
import { StrategyPerformance } from './StrategyPerformance'
import { AgentStatus } from './AgentStatus'
import { OrderLog } from './OrderLog'
import { EngineControl } from './EngineControl'
import { RotationMonitor } from './RotationMonitor'
import { DailyPnLStats } from './DailyPnLStats'
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
  { id: 'stats', label: '일일 통계' },
  { id: 'agents', label: '에이전트' },
  { id: 'rotation', label: '종목/로테이션' },
  { id: 'system', label: '시스템 로그' },
] as const

type TabId = (typeof TABS)[number]['id']

const EXCHANGE_LABELS: Partial<Record<ExchangeName, string>> = {
  binance_futures: 'Binance',
  binance_spot: 'Binance',
}

const SPOT_EXCHANGES: ExchangeName[] = ['binance_spot']
const FUTURES_EXCHANGES: ExchangeName[] = ['binance_futures']

export function Dashboard() {
  const [tab, setTab] = useState<TabId>('overview')
  const [exchange, setExchange] = useState<ExchangeName>('binance_spot')
  const [liveEvents, setLiveEvents] = useState<string[]>([])
  const [realtimeServerEvents, setRealtimeServerEvents] = useState<ServerEvent[]>([])
  const qc = useQueryClient()
  const serverEventInvalidateTimer = useRef<ReturnType<typeof setTimeout>>()

  // 사용 가능한 거래소 목록 조회
  const { data: exchangeInfo } = useQuery({
    queryKey: ['exchanges'],
    queryFn: getExchanges,
    staleTime: 60_000,
  })

  const exchanges = exchangeInfo?.exchanges?.filter((e: ExchangeName) => e !== 'bithumb') ?? ['binance_spot']

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
          `[${time}] ${side} ${d.symbol} @ ${d.price?.toLocaleString()} [${d.strategy}]`,
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
        clearTimeout(serverEventInvalidateTimer.current)
        serverEventInvalidateTimer.current = setTimeout(() => {
          qc.invalidateQueries({ queryKey: ['serverEvents'] })
        }, 2_000)
      }
    },
    [qc]
  )

  const { connected } = useWebSocket(onMessage)

  // 통화 기호
  const currencySymbol = exchange.startsWith('binance') ? 'USDT' : '₩'

  return (
    <div className="min-h-screen bg-gray-900 text-white">
      {/* Header */}
      <header className="border-b border-gray-700 px-3 md:px-6 py-2.5 md:py-3 flex items-center justify-between">
        <div className="flex items-center gap-2 md:gap-3 min-w-0">
          <span className="text-base md:text-xl font-bold whitespace-nowrap">🪙 코인 자동 매매</span>
          <span className="text-xs text-gray-500 hidden md:block">트리플 엔진 트레이딩</span>
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          <div className={`w-2 h-2 rounded-full ${connected ? 'bg-green-500 animate-pulse' : 'bg-red-500'}`} />
          <span className="text-xs text-gray-400">{connected ? '연결됨' : '연결 중...'}</span>
        </div>
      </header>

      {/* Exchange selector — grouped by spot / futures */}
      {exchanges.length > 1 && (() => {
        const spotExs = exchanges.filter((e) => SPOT_EXCHANGES.includes(e))
        const futuresExs = exchanges.filter((e) => FUTURES_EXCHANGES.includes(e))
        const ExBtn = ({ ex }: { ex: ExchangeName }) => (
          <button
            onClick={() => setExchange(ex)}
            className={`px-3 py-1 text-xs md:text-sm rounded-full font-medium transition-colors ${
              exchange === ex
                ? 'bg-blue-600 text-white'
                : 'bg-gray-800 text-gray-400 hover:bg-gray-700 hover:text-white'
            }`}
          >
            {EXCHANGE_LABELS[ex] ?? ex}
          </button>
        )
        return (
          <div className="border-b border-gray-700 px-3 md:px-6 py-1.5 flex items-center gap-1.5 md:gap-2">
            {spotExs.length > 0 && (
              <>
                <span className="text-[10px] text-gray-500 font-medium uppercase tracking-wider">현물</span>
                {spotExs.map((ex) => <ExBtn key={ex} ex={ex} />)}
              </>
            )}
            {spotExs.length > 0 && futuresExs.length > 0 && (
              <div className="w-px h-5 bg-gray-700 mx-1" />
            )}
            {futuresExs.length > 0 && (
              <>
                <span className="text-[10px] text-gray-500 font-medium uppercase tracking-wider">선물</span>
                {futuresExs.map((ex) => <ExBtn key={ex} ex={ex} />)}
              </>
            )}
          </div>
        )
      })()}

      {/* Tabs - horizontal scroll on mobile */}
      <nav className="border-b border-gray-700 overflow-x-auto scrollbar-hide">
        <div className="flex min-w-max">
          {TABS.map((t) => {
            // 종목/로테이션 탭은 모든 거래소에서 표시
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
        {tab === 'stats' && <DailyPnLStats exchange={exchange} />}
        {tab === 'agents' && <AgentStatus exchange={exchange} />}
        {tab === 'rotation' && <RotationMonitor exchange={exchange} />}
        {tab === 'system' && <SystemLog realtimeEvents={realtimeServerEvents} />}
      </main>
    </div>
  )
}
