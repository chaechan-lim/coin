import { lazy, Suspense, useCallback, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { EngineControl } from './EngineControl'
import { TradingAccountOverview } from './TradingAccountOverview'
import { useWebSocket } from '../hooks/useWebSocket'
import { getExchanges } from '../api/client'
import type { ExchangeName, ServerEvent, WsEvent } from '../types'
import { format } from 'date-fns'

const PortfolioSummary = lazy(() => import('./PortfolioSummary').then((m) => ({ default: m.PortfolioSummary })))
const PortfolioChart = lazy(() => import('./PortfolioChart').then((m) => ({ default: m.PortfolioChart })))
const TradeHistory = lazy(() => import('./TradeHistory').then((m) => ({ default: m.TradeHistory })))
const ResearchMonitor = lazy(() => import('./ResearchMonitor').then((m) => ({ default: m.ResearchMonitor })))
const LiveTradeGroups = lazy(() => import('./LiveTradeGroups').then((m) => ({ default: m.LiveTradeGroups })))
const AgentStatus = lazy(() => import('./AgentStatus').then((m) => ({ default: m.AgentStatus })))
const OrderLog = lazy(() => import('./OrderLog').then((m) => ({ default: m.OrderLog })))
const StrategyPerformance = lazy(() => import('./StrategyPerformance').then((m) => ({ default: m.StrategyPerformance })))
const RotationMonitor = lazy(() => import('./RotationMonitor').then((m) => ({ default: m.RotationMonitor })))
const DailyPnLStats = lazy(() => import('./DailyPnLStats').then((m) => ({ default: m.DailyPnLStats })))
const SystemLog = lazy(() => import('./SystemLog').then((m) => ({ default: m.SystemLog })))

const TABS = [
  { id: 'overview', label: '개요', description: '현재 어떤 엔진이 돌고 있고, 어디에 얼마를 배정했는지 한 번에 봅니다.' },
  { id: 'live', label: '실거래', description: '실제 체결, 포트폴리오, grouped trade, 손익 흐름을 확인합니다.' },
  { id: 'rnd', label: 'R&D', description: '후보 전략, grouped trade, auto-review 상태를 봅니다.' },
  { id: 'ops', label: '운영 로그', description: '실시간 이벤트, 시스템 이벤트, 에이전트 판단을 점검합니다.' },
  { id: 'advanced', label: '고급', description: '신호 로그, 전략 성과, 로테이션 같은 보조 분석 도구입니다.' },
] as const

const ADVANCED_VIEWS = [
  { id: 'signals', label: '신호 로그' },
  { id: 'strategies', label: '전략 성과' },
  { id: 'rotation', label: '종목/로테이션' },
  { id: 'stats', label: '일일 통계' },
] as const

type TabId = (typeof TABS)[number]['id']
type AdvancedViewId = (typeof ADVANCED_VIEWS)[number]['id']

const EXCHANGE_LABELS: Partial<Record<ExchangeName, string>> = {
  binance_futures: 'Binance',
  binance_spot: 'Binance',
}

const SPOT_EXCHANGES: ExchangeName[] = ['binance_spot']
const FUTURES_EXCHANGES: ExchangeName[] = ['binance_futures']

export function Dashboard() {
  const [tab, setTab] = useState<TabId>('overview')
  const [advancedView, setAdvancedView] = useState<AdvancedViewId>('signals')
  const [exchange, setExchange] = useState<ExchangeName>('binance_spot')
  const [liveEvents, setLiveEvents] = useState<string[]>([])
  const [realtimeServerEvents, setRealtimeServerEvents] = useState<ServerEvent[]>([])
  const qc = useQueryClient()
  const serverEventInvalidateTimer = useRef<ReturnType<typeof setTimeout>>()

  const { data: exchangeInfo } = useQuery({
    queryKey: ['exchanges'],
    queryFn: getExchanges,
    staleTime: 60_000,
  })

  const exchanges = exchangeInfo?.exchanges?.filter(
    (e: ExchangeName) =>
      e !== 'bithumb' &&
      e !== 'binance_surge' &&
      e !== 'binance_donchian' &&
      e !== 'binance_donchian_futures' &&
      e !== 'binance_pairs'
  ) ?? ['binance_spot']

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
  const activeTab = TABS.find((item) => item.id === tab) ?? TABS[0]
  const handleNavigateFromResearch = useCallback((nextTab: 'live' | 'ops') => {
    setTab(nextTab)
  }, [])

  return (
    <div className="min-h-screen bg-gray-900 text-white">
      <header className="flex items-center justify-between border-b border-gray-700 px-3 py-2.5 md:px-6 md:py-3">
        <div className="flex min-w-0 items-center gap-2 md:gap-3">
          <span className="whitespace-nowrap text-base font-bold md:text-xl">🪙 코인 자동 매매</span>
          <span className="hidden text-xs text-gray-500 md:block">live trading + R&D pipeline</span>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <div className={`h-2 w-2 rounded-full ${connected ? 'bg-green-500 animate-pulse' : 'bg-red-500'}`} />
          <span className="text-xs text-gray-400">{connected ? '연결됨' : '연결 중...'}</span>
        </div>
      </header>

      {exchanges.length > 1 && (() => {
        const spotExs = exchanges.filter((e) => SPOT_EXCHANGES.includes(e))
        const futuresExs = exchanges.filter((e) => FUTURES_EXCHANGES.includes(e))
        const ExBtn = ({ ex }: { ex: ExchangeName }) => (
          <button
            onClick={() => setExchange(ex)}
            className={`rounded-full px-3 py-1 text-xs font-medium transition-colors md:text-sm ${
              exchange === ex
                ? 'bg-blue-600 text-white'
                : 'bg-gray-800 text-gray-400 hover:bg-gray-700 hover:text-white'
            }`}
          >
            {EXCHANGE_LABELS[ex] ?? ex}
          </button>
        )
        return (
          <div className="flex items-center gap-1.5 border-b border-gray-700 px-3 py-1.5 md:gap-2 md:px-6">
            {spotExs.length > 0 && (
              <>
                <span className="text-[10px] font-medium uppercase tracking-wider text-gray-500">현물</span>
                {spotExs.map((ex) => <ExBtn key={ex} ex={ex} />)}
              </>
            )}
            {spotExs.length > 0 && futuresExs.length > 0 && <div className="mx-1 h-5 w-px bg-gray-700" />}
            {futuresExs.length > 0 && (
              <>
                <span className="text-[10px] font-medium uppercase tracking-wider text-gray-500">선물</span>
                {futuresExs.map((ex) => <ExBtn key={ex} ex={ex} />)}
              </>
            )}
          </div>
        )
      })()}

      <nav className="border-b border-gray-700 px-1 md:px-0">
        <div className="flex flex-wrap md:flex-nowrap">
          {TABS.map((item) => (
            <button
              key={item.id}
              onClick={() => setTab(item.id)}
              className={`-mb-px border-b-2 px-3 py-2 text-xs font-medium whitespace-nowrap transition-colors md:px-4 md:py-3 md:text-sm ${
                tab === item.id
                  ? 'border-blue-500 text-blue-400'
                  : 'border-transparent text-gray-400 hover:text-white active:text-white'
              }`}
            >
              {item.label}
            </button>
          ))}
        </div>
      </nav>

      <main className="mx-auto max-w-7xl space-y-3 p-3 md:space-y-4 md:p-6">
        <SectionHeader title={activeTab.label} description={activeTab.description} />

        <Suspense fallback={<PanelFallback />}>
          {tab === 'overview' && (
            <>
              <EngineControl liveEvents={liveEvents} exchange={exchange} />
              <TradingAccountOverview />
            </>
          )}

          {tab === 'live' && (
            <>
              <LiveTradeGroups exchange={exchange} />
              <TradeHistory exchange={exchange} />
              <PortfolioSummary exchange={exchange} />
              <PortfolioChart exchange={exchange} />
              <DailyPnLStats exchange={exchange} />
            </>
          )}

          {tab === 'rnd' && <ResearchMonitor exchange={exchange} onNavigate={handleNavigateFromResearch} />}

          {tab === 'ops' && (
            <>
              <EngineControl liveEvents={liveEvents} exchange={exchange} />
              <AgentStatus exchange={exchange} />
              <SystemLog realtimeEvents={realtimeServerEvents} />
            </>
          )}

          {tab === 'advanced' && (
            <AdvancedPanel
              exchange={exchange}
              selected={advancedView}
              onSelect={setAdvancedView}
            />
          )}
        </Suspense>
      </main>
    </div>
  )
}

function SectionHeader({ title, description }: { title: string; description: string }) {
  return (
    <section className="rounded-xl border border-gray-700 bg-gray-800/70 px-4 py-3">
      <h2 className="text-base font-semibold text-white">{title}</h2>
      <p className="mt-1 text-sm text-gray-400">{description}</p>
    </section>
  )
}

function PanelFallback() {
  return (
    <section className="rounded-xl border border-gray-700 bg-gray-800/70 p-6 text-sm text-gray-400">
      패널 로딩 중...
    </section>
  )
}

function AdvancedPanel({
  exchange,
  selected,
  onSelect,
}: {
  exchange: ExchangeName
  selected: AdvancedViewId
  onSelect: (view: AdvancedViewId) => void
}) {
  return (
    <section className="space-y-3 md:space-y-4">
      <div className="flex flex-wrap gap-2">
        {ADVANCED_VIEWS.map((view) => (
          <button
            key={view.id}
            onClick={() => onSelect(view.id)}
            className={`rounded-full border px-3 py-1.5 text-xs font-medium transition-colors md:text-sm ${
              selected === view.id
                ? 'border-blue-500 bg-blue-600/20 text-blue-300'
                : 'border-gray-700 bg-gray-800 text-gray-400 hover:text-white'
            }`}
          >
            {view.label}
          </button>
        ))}
      </div>

      {selected === 'signals' && <OrderLog exchange={exchange} />}
      {selected === 'strategies' && <StrategyPerformance exchange={exchange} />}
      {selected === 'rotation' && <RotationMonitor exchange={exchange} />}
      {selected === 'stats' && <DailyPnLStats exchange={exchange} />}
    </section>
  )
}
