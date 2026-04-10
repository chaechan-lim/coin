import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  getCapitalSummary,
  getDonchianEngineStatus,
  getDonchianFuturesEngineStatus,
  getFuturesRndStatus,
  getPairsEngineStatus,
  getPortfolioSummary,
  getTrades,
  getTradeSummary,
} from '../api/client'
import type {
  CapitalSummary,
  DonchianSpotStatus,
  FuturesRndStatus,
  Order,
  PortfolioSummary,
  RndEngineStatus,
  TradeSummary,
} from '../types'
import { fmtPrice, fmtSignedPrice } from '../utils/format'

function Stat({ label, value, tone = 'text-white' }: { label: string; value: string; tone?: string }) {
  return (
    <div>
      <div className="text-[11px] uppercase tracking-wide text-gray-500">{label}</div>
      <div className={`mt-1 text-sm font-semibold ${tone}`}>{value}</div>
    </div>
  )
}

function StatusBadge({ running, paused }: { running: boolean; paused?: boolean }) {
  const cls = paused
    ? 'border-yellow-800/60 bg-yellow-950/40 text-yellow-300'
    : running
      ? 'border-green-700/60 bg-green-900/40 text-green-300'
      : 'border-gray-700 bg-gray-900/60 text-gray-400'
  const label = paused ? 'paused' : running ? 'started' : 'stopped'
  return <span className={`rounded-full border px-2 py-0.5 text-[11px] font-medium ${cls}`}>{label}</span>
}

function OverviewCard({
  title,
  subtitle,
  note,
  badge,
  action,
  children,
}: {
  title: string
  subtitle: string
  note?: React.ReactNode
  badge?: React.ReactNode
  action?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <div className="rounded-xl border border-gray-700 bg-gray-800/80 p-4">
      <div className="mb-3 flex items-start justify-between gap-3">
        <div>
          <h4 className="text-sm font-semibold text-white">{title}</h4>
          <p className="mt-1 text-xs text-gray-400">{subtitle}</p>
          {note && <div className="mt-2 text-[11px] leading-5 text-gray-500">{note}</div>}
        </div>
        <div className="flex items-center gap-2">
          {action}
          {badge}
        </div>
      </div>
      <div className="grid grid-cols-2 gap-3 text-sm md:grid-cols-3">{children}</div>
    </div>
  )
}

type SpotHistoryWindow = 'all' | '30d' | '7d'
type StrategyWindow = 'today' | '7d' | '30d'

function WindowToggle({
  value,
  onChange,
  items,
  labels,
}: {
  value: string
  onChange: (value: string) => void
  items?: readonly string[]
  labels?: Record<string, string>
}) {
  const toggleItems = items ?? ['all', '30d', '7d']
  const toggleLabels = labels ?? {
    all: '전체',
    '30d': '30d',
    '7d': '7d',
  }

  return (
    <div className="flex items-center gap-1 rounded-full border border-gray-700 bg-gray-900/70 p-1">
      {toggleItems.map((item) => (
        <button
          key={item}
          onClick={() => onChange(item)}
          className={`rounded-full px-2 py-0.5 text-[11px] font-medium transition-colors ${
            value === item
              ? 'bg-sky-600 text-white'
              : 'text-gray-400 hover:bg-gray-800 hover:text-white'
          }`}
        >
          {toggleLabels[item]}
        </button>
      ))}
    </div>
  )
}

function formatTrades(summary?: TradeSummary) {
  if (!summary) return 'n/a'
  return `${summary.total_trades}건`
}

function formatPositionsCount(value: number | undefined) {
  return `${value ?? 0}개`
}

function FuturesCoordinatorCard({ status }: { status?: FuturesRndStatus | null }) {
  const paused = status?.entry_paused ?? false
  return (
    <OverviewCard
      title="선물 R&D 계좌"
      subtitle="메인 선물 엔진과 별개로, 현재 실제 선물 운용 한도와 사용 가능 마진을 봅니다."
      badge={<StatusBadge running={!!status} paused={paused} />}
    >
      <Stat label="운용 한도" value={status ? fmtPrice(status.global_capital_usdt, true) : 'n/a'} />
      <Stat label="가용 마진" value={status ? fmtPrice(status.global_available_margin, true) : 'n/a'} />
      <Stat label="예약 마진" value={status ? fmtPrice(status.global_reserved_margin, true) : 'n/a'} />
      <Stat
        label="당일 손익"
        value={status ? fmtSignedPrice(status.global_daily_pnl, true) : 'n/a'}
        tone={(status?.global_daily_pnl ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}
      />
      <Stat
        label="누적 손익"
        value={status ? fmtSignedPrice(status.global_cumulative_pnl, true) : 'n/a'}
        tone={(status?.global_cumulative_pnl ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}
      />
      <Stat label="예약 심볼" value={status ? String(Object.keys(status.reserved_symbols).length) : '0'} />
    </OverviewCard>
  )
}

function SpotAccountHistoryCard({
  portfolio,
  capital,
  window,
  periodTrades,
  lastTrade,
  onWindowChange,
}: {
  portfolio?: PortfolioSummary
  capital?: CapitalSummary
  window: SpotHistoryWindow
  periodTrades?: TradeSummary
  lastTrade?: Order
  onWindowChange: (value: SpotHistoryWindow) => void
}) {
  const periodLabel = window === 'all' ? '전체 실현 손익' : `최근 ${window} 실현`
  const periodPnl = window === 'all' ? (portfolio?.realized_pnl ?? 0) : (periodTrades?.total_pnl ?? 0)
  const periodTradeCount = window === 'all' ? `${portfolio?.trade_count ?? 0}건` : formatTrades(periodTrades)

  return (
    <OverviewCard
      title="Main Spot History"
      subtitle="이 카드는 `binance_spot` 메인 ledger 기준 누적 잔고/손익입니다. 현재 `Donchian Spot` live 전략 성과와 분리해서 읽어야 합니다."
      badge={<span className="rounded-full border border-sky-800/60 bg-sky-950/40 px-2 py-0.5 text-[11px] font-medium text-sky-300">ledger</span>}
      action={<WindowToggle value={window} onChange={(value) => onWindowChange(value as SpotHistoryWindow)} />}
    >
      <Stat label="계좌 총자산" value={portfolio ? fmtPrice(portfolio.total_value_krw, true) : 'n/a'} />
      <Stat label="가용 현금" value={portfolio ? fmtPrice(portfolio.cash_balance_krw, true) : 'n/a'} />
      <Stat label="순입금 원금" value={capital ? fmtPrice(capital.net_capital, true) : 'n/a'} />
      <Stat
        label="ledger 누적 손익"
        value={portfolio ? fmtSignedPrice(portfolio.total_pnl, true) : 'n/a'}
        tone={(portfolio?.total_pnl ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}
      />
      <Stat
        label={periodLabel}
        value={portfolio ? fmtSignedPrice(periodPnl, true) : 'n/a'}
        tone={periodPnl >= 0 ? 'text-green-400' : 'text-red-400'}
      />
      <Stat label={`${window === 'all' ? '누적' : window} 거래수`} value={periodTradeCount} />
      <Stat label="마지막 main spot 체결" value={formatLastTrade(lastTrade)} />
    </OverviewCard>
  )
}

function SpotStrategyCard({
  donchian,
  window,
  trades,
}: {
  donchian?: DonchianSpotStatus
  window: StrategyWindow
  trades?: TradeSummary
}) {
  const periodLabel = window === 'today' ? '오늘 거래' : `${window} 거래`
  const pnlLabel = window === 'today' ? '오늘 실현' : `${window} 실현`
  const periodPnl = window === 'today' ? (donchian?.daily_pnl ?? 0) : (trades?.total_pnl ?? 0)

  return (
    <OverviewCard
      title="Donchian Spot"
      subtitle="현재 live R&D 현물 전략 상태입니다. 계정 누적 손익과 다를 수 있습니다."
      note={buildEngineActivityNote({
        isRunning: donchian?.is_running ?? false,
        lastEvaluatedAt: donchian?.last_evaluated_at,
        nextEvaluationAt: donchian?.next_evaluation_at,
        recentIdleReason: donchian?.recent_idle_reason,
      })}
      badge={<StatusBadge running={donchian?.is_running ?? false} paused={donchian?.paused_daily_loss || donchian?.paused_total_loss} />}
    >
      <Stat label="전략 자본" value={donchian ? fmtPrice(donchian.initial_capital, true) : 'n/a'} />
      <Stat label="감시 코인" value={`${donchian?.coins.length ?? 0}종`} />
      <Stat label="활성 포지션" value={formatPositionsCount(donchian?.active_positions)} />
      <Stat label={periodLabel} value={formatTrades(trades)} />
      <Stat
        label={pnlLabel}
        value={donchian ? fmtSignedPrice(periodPnl, true) : 'n/a'}
        tone={periodPnl >= 0 ? 'text-green-400' : 'text-red-400'}
      />
      <Stat
        label="누적 손익"
        value={donchian ? fmtSignedPrice(donchian.cumulative_pnl, true) : 'n/a'}
        tone={(donchian?.cumulative_pnl ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}
      />
      <Stat label="상태" value={buildSpotStrategyStatus(donchian)} />
    </OverviewCard>
  )
}

function StrategyCard({
  title,
  subtitle,
  status,
  window,
  trades,
  positionsLabel,
}: {
  title: string
  subtitle: string
  status?: RndEngineStatus
  window: StrategyWindow
  trades?: TradeSummary
  positionsLabel: string
}) {
  const openPositions = Array.isArray(status?.positions)
    ? status?.positions.length
    : status?.position
      ? 1
      : 0
  const periodLabel = window === 'today' ? '오늘 거래' : `${window} 거래`
  const pnlLabel = window === 'today' ? '오늘 실현' : `${window} 실현`
  const periodPnl = window === 'today' ? (status?.daily_realized_pnl ?? 0) : (trades?.total_pnl ?? 0)
  const cumulativePnl = status?.cumulative_pnl ?? 0

  return (
    <OverviewCard
      title={title}
      subtitle={subtitle}
      note={buildEngineActivityNote({
        isRunning: status?.is_running ?? false,
        lastEvaluatedAt: status?.last_evaluated_at,
        nextEvaluationAt: status?.next_evaluation_at,
        recentIdleReason: status?.recent_idle_reason,
      })}
      badge={<StatusBadge running={status?.is_running ?? false} paused={status?.paused || status?.daily_paused} />}
    >
      <Stat label="운용 자본" value={status?.capital_usdt != null ? fmtPrice(status.capital_usdt, true) : 'n/a'} />
      <Stat label="가용 마진" value={status?.available_margin != null ? fmtPrice(status.available_margin, true) : 'n/a'} />
      <Stat label={positionsLabel} value={formatPositionsCount(openPositions)} />
      <Stat label={periodLabel} value={formatTrades(trades)} />
      <Stat label={pnlLabel} value={fmtSignedPrice(periodPnl, true)} tone={periodPnl >= 0 ? 'text-green-400' : 'text-red-400'} />
      <Stat label="누적 손익" value={fmtSignedPrice(cumulativePnl, true)} tone={cumulativePnl >= 0 ? 'text-green-400' : 'text-red-400'} />
    </OverviewCard>
  )
}

export function TradingAccountOverview() {
  const [spotHistoryWindow, setSpotHistoryWindow] = useState<SpotHistoryWindow>('30d')
  const [strategyWindow, setStrategyWindow] = useState<StrategyWindow>('today')
  const queryOptions = {
    staleTime: 10_000,
    refetchInterval: 15_000,
  } as const

  const { data: spotPortfolio } = useQuery({
    queryKey: ['portfolio', 'summary', 'binance_spot'],
    queryFn: () => getPortfolioSummary('binance_spot'),
    ...queryOptions,
  })

  const { data: donchianSpotStatus } = useQuery({
    queryKey: ['engine', 'status', 'binance_donchian'],
    queryFn: () => getDonchianEngineStatus(),
    ...queryOptions,
  })

  const { data: spotTrades } = useQuery({
    queryKey: ['trades', 'summary', strategyWindow, 'binance_donchian'],
    queryFn: () => getTradeSummary(strategyWindow, 'binance_donchian'),
    ...queryOptions,
  })

  const { data: spotCapital } = useQuery({
    queryKey: ['capital', 'summary', 'binance_spot'],
    queryFn: () => getCapitalSummary('binance_spot'),
    ...queryOptions,
  })

  const { data: spotAccountPeriodTrades } = useQuery({
    queryKey: ['trades', 'summary', spotHistoryWindow, 'binance_spot'],
    queryFn: () => getTradeSummary(spotHistoryWindow === 'all' ? '30d' : spotHistoryWindow, 'binance_spot'),
    ...queryOptions,
    enabled: spotHistoryWindow !== 'all',
  })

  const { data: latestSpotTrades } = useQuery({
    queryKey: ['trades', 'latest', 'binance_spot'],
    queryFn: () => getTrades({ exchange: 'binance_spot', size: 1, page: 1 }),
    ...queryOptions,
  })

  const { data: futuresCoordinator } = useQuery({
    queryKey: ['engine', 'futures-rnd', 'status'],
    queryFn: () => getFuturesRndStatus(),
    ...queryOptions,
  })

  const { data: donchianFuturesStatus } = useQuery({
    queryKey: ['engine', 'status', 'binance_donchian_futures'],
    queryFn: () => getDonchianFuturesEngineStatus(),
    ...queryOptions,
  })

  const { data: donchianFuturesTrades } = useQuery({
    queryKey: ['trades', 'summary', strategyWindow, 'binance_donchian_futures'],
    queryFn: () => getTradeSummary(strategyWindow, 'binance_donchian_futures'),
    ...queryOptions,
  })

  const { data: pairsStatus } = useQuery({
    queryKey: ['engine', 'status', 'binance_pairs'],
    queryFn: () => getPairsEngineStatus(),
    ...queryOptions,
  })

  const { data: pairsTrades } = useQuery({
    queryKey: ['trades', 'summary', strategyWindow, 'binance_pairs'],
    queryFn: () => getTradeSummary(strategyWindow, 'binance_pairs'),
    ...queryOptions,
  })

  const rndStatus = futuresCoordinator && 'status' in futuresCoordinator ? null : (futuresCoordinator as FuturesRndStatus | undefined)

  return (
    <section className="space-y-3 md:space-y-4">
      <div className="rounded-xl border border-gray-700 bg-gray-800 p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h3 className="text-base font-semibold text-white">트레이딩 대상 잔고</h3>
            <p className="text-sm text-gray-400">현물/선물 계좌를 분리해서, 실제 운용 자본과 오늘 거래 상황을 같이 봅니다.</p>
          </div>
          <div className="rounded-full border border-gray-700 bg-gray-900/50 px-3 py-1 text-xs text-gray-400">
            메인 엔진 on/off와 별개로 현재 live R&D 자본 기준
          </div>
        </div>
      </div>

      <div className="grid gap-3 xl:grid-cols-3">
        <SpotAccountHistoryCard
          portfolio={spotPortfolio}
          capital={spotCapital}
          window={spotHistoryWindow}
          periodTrades={spotAccountPeriodTrades}
          lastTrade={latestSpotTrades?.[0]}
          onWindowChange={(value) => setSpotHistoryWindow(value as SpotHistoryWindow)}
        />
        <SpotStrategyCard donchian={donchianSpotStatus} window={strategyWindow} trades={spotTrades} />
        <FuturesCoordinatorCard status={rndStatus} />
      </div>

      <div className="rounded-xl border border-gray-700 bg-gray-800/80 px-4 py-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="text-sm font-semibold text-white">Live 전략 기간 비교</div>
            <div className="mt-1 text-xs text-gray-400">아래 `Donchian Spot`, `Donchian Futures`, `Pairs Trading` 카드가 같은 기간 기준으로 같이 바뀝니다.</div>
          </div>
          <WindowToggle
            value={strategyWindow}
            onChange={(value) => setStrategyWindow(value as StrategyWindow)}
            items={['today', '7d', '30d']}
            labels={{ today: '오늘', '7d': '7d', '30d': '30d' }}
          />
        </div>
      </div>

      <div className="grid gap-3 xl:grid-cols-2">
        <StrategyCard
          title="Donchian Futures"
          subtitle="양방향 Donchian 선물 R&D 엔진 상태와 당일 거래 현황."
          status={donchianFuturesStatus}
          window={strategyWindow}
          trades={donchianFuturesTrades}
          positionsLabel="오픈 포지션"
        />
        <StrategyCard
          title="Pairs Trading"
          subtitle="BTC/ETH 페어 실거래 엔진 상태와 당일 거래 현황."
          status={pairsStatus}
          window={strategyWindow}
          trades={pairsTrades}
          positionsLabel="오픈 그룹"
        />
      </div>
    </section>
  )
}

function formatLastTrade(trade?: Order): string {
  if (!trade?.created_at) return '없음'
  const date = new Date(trade.created_at)
  if (Number.isNaN(date.getTime())) return '기록 오류'
  return date.toLocaleString('ko-KR', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

function buildSpotStrategyStatus(status?: DonchianSpotStatus): string {
  if (!status) return '상태 수집 중'
  if (status.paused_total_loss) return '누적 손실 컷 pause'
  if (status.paused_daily_loss) return '일일 손실 컷 pause'
  return status.is_running ? 'running' : 'stopped'
}

function formatEngineTime(value?: string | null): string {
  if (!value) return '없음'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '없음'
  return date.toLocaleString('ko-KR', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

function buildEngineActivityNote({
  isRunning,
  lastEvaluatedAt,
  nextEvaluationAt,
  recentIdleReason,
}: {
  isRunning: boolean
  lastEvaluatedAt?: string | null
  nextEvaluationAt?: string | null
  recentIdleReason?: string | null
}): React.ReactNode {
  const statusText = isRunning ? (recentIdleReason ?? '없음') : '엔진 정지'
  return (
    <>
      <div>최근 평가: {formatEngineTime(lastEvaluatedAt)} · 다음 평가: {formatEngineTime(nextEvaluationAt)}</div>
      <div>최근 상태: {statusText}</div>
    </>
  )
}
