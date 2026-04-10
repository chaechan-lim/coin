import { useQuery } from '@tanstack/react-query'
import {
  getDonchianEngineStatus,
  getDonchianFuturesEngineStatus,
  getFuturesRndStatus,
  getPairsEngineStatus,
  getPortfolioSummary,
  getTradeSummary,
} from '../api/client'
import type {
  DonchianSpotStatus,
  FuturesRndStatus,
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
  badge,
  children,
}: {
  title: string
  subtitle: string
  badge?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <div className="rounded-xl border border-gray-700 bg-gray-800/80 p-4">
      <div className="mb-3 flex items-start justify-between gap-3">
        <div>
          <h4 className="text-sm font-semibold text-white">{title}</h4>
          <p className="mt-1 text-xs text-gray-400">{subtitle}</p>
        </div>
        {badge}
      </div>
      <div className="grid grid-cols-2 gap-3 text-sm md:grid-cols-3">{children}</div>
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

function SpotAccountCard({
  portfolio,
  donchian,
  trades,
}: {
  portfolio?: PortfolioSummary
  donchian?: DonchianSpotStatus
  trades?: TradeSummary
}) {
  return (
    <OverviewCard
      title="현물 계좌"
      subtitle="실제 현물 계좌 잔고와 Donchian Daily 현물 운용 상태를 같이 봅니다."
      badge={<StatusBadge running={donchian?.is_running ?? false} paused={donchian?.paused_daily_loss || donchian?.paused_total_loss} />}
    >
      <Stat label="계좌 총자산" value={portfolio ? fmtPrice(portfolio.total_value_krw, true) : 'n/a'} />
      <Stat label="가용 현금" value={portfolio ? fmtPrice(portfolio.cash_balance_krw, true) : 'n/a'} />
      <Stat label="보유 포지션" value={formatPositionsCount(portfolio?.positions.length)} />
      <Stat label="Donchian 자본" value={donchian ? fmtPrice(donchian.initial_capital, true) : 'n/a'} />
      <Stat label="오늘 거래" value={formatTrades(trades)} />
      <Stat
        label="누적 손익"
        value={donchian ? fmtSignedPrice(donchian.cumulative_pnl, true) : 'n/a'}
        tone={(donchian?.cumulative_pnl ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}
      />
    </OverviewCard>
  )
}

function StrategyCard({
  title,
  subtitle,
  status,
  trades,
  positionsLabel,
}: {
  title: string
  subtitle: string
  status?: RndEngineStatus
  trades?: TradeSummary
  positionsLabel: string
}) {
  const openPositions = Array.isArray(status?.positions)
    ? status?.positions.length
    : status?.position
      ? 1
      : 0
  const dailyPnl = status?.daily_realized_pnl ?? 0
  const cumulativePnl = status?.cumulative_pnl ?? 0

  return (
    <OverviewCard
      title={title}
      subtitle={subtitle}
      badge={<StatusBadge running={status?.is_running ?? false} paused={status?.paused || status?.daily_paused} />}
    >
      <Stat label="운용 자본" value={status?.capital_usdt != null ? fmtPrice(status.capital_usdt, true) : 'n/a'} />
      <Stat label="가용 마진" value={status?.available_margin != null ? fmtPrice(status.available_margin, true) : 'n/a'} />
      <Stat label={positionsLabel} value={formatPositionsCount(openPositions)} />
      <Stat label="오늘 거래" value={formatTrades(trades)} />
      <Stat label="당일 손익" value={fmtSignedPrice(dailyPnl, true)} tone={dailyPnl >= 0 ? 'text-green-400' : 'text-red-400'} />
      <Stat label="누적 손익" value={fmtSignedPrice(cumulativePnl, true)} tone={cumulativePnl >= 0 ? 'text-green-400' : 'text-red-400'} />
    </OverviewCard>
  )
}

export function TradingAccountOverview() {
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
    queryKey: ['trades', 'summary', 'today', 'binance_donchian'],
    queryFn: () => getTradeSummary('today', 'binance_donchian'),
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
    queryKey: ['trades', 'summary', 'today', 'binance_donchian_futures'],
    queryFn: () => getTradeSummary('today', 'binance_donchian_futures'),
    ...queryOptions,
  })

  const { data: pairsStatus } = useQuery({
    queryKey: ['engine', 'status', 'binance_pairs'],
    queryFn: () => getPairsEngineStatus(),
    ...queryOptions,
  })

  const { data: pairsTrades } = useQuery({
    queryKey: ['trades', 'summary', 'today', 'binance_pairs'],
    queryFn: () => getTradeSummary('today', 'binance_pairs'),
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

      <div className="grid gap-3 xl:grid-cols-2">
        <SpotAccountCard portfolio={spotPortfolio} donchian={donchianSpotStatus} trades={spotTrades} />
        <FuturesCoordinatorCard status={rndStatus} />
      </div>

      <div className="grid gap-3 xl:grid-cols-2">
        <StrategyCard
          title="Donchian Futures"
          subtitle="양방향 Donchian 선물 R&D 엔진 상태와 당일 거래 현황."
          status={donchianFuturesStatus}
          trades={donchianFuturesTrades}
          positionsLabel="오픈 포지션"
        />
        <StrategyCard
          title="Pairs Trading"
          subtitle="BTC/ETH 페어 실거래 엔진 상태와 당일 거래 현황."
          status={pairsStatus}
          trades={pairsTrades}
          positionsLabel="오픈 그룹"
        />
      </div>
    </section>
  )
}
