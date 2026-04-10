import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  getDonchianFuturesTradeGroupDetail,
  getDonchianFuturesTradeGroups,
  getPairsTradeGroupDetail,
  getPairsTradeGroups,
  getTradeSummary,
} from '../api/client'
import { formatTs } from '../utils/date'
import { fmtSignedPrice } from '../utils/format'
import type {
  DonchianFuturesTradeGroup,
  DonchianFuturesTradeGroupDetail,
  ExchangeName,
  PairsTradeGroup,
  PairsTradeGroupDetail,
  TradeSummary,
} from '../types'

const PERIOD_OPTIONS = [
  { value: 'today', label: 'Today' },
  { value: '7d', label: '7D' },
  { value: '30d', label: '30D' },
] as const

type KpiPeriod = (typeof PERIOD_OPTIONS)[number]['value']

function GroupRow({
  title,
  direction,
  status,
  openedAt,
  closedAt,
  realizedPnl,
}: {
  title: string
  direction: string
  status: string
  openedAt: string
  closedAt: string | null
  realizedPnl: number
}) {
  return (
    <div className="grid grid-cols-[1.3fr,0.7fr,0.7fr,0.9fr] gap-2 border-b border-gray-800 py-2 text-xs last:border-b-0">
      <div className="min-w-0">
        <div className="truncate text-white">{title}</div>
        <div className="text-gray-500">{direction}</div>
      </div>
      <div className="text-gray-300">{status}</div>
      <div className={realizedPnl >= 0 ? 'text-green-400' : 'text-red-400'}>
        {realizedPnl >= 0 ? '+' : ''}
        {realizedPnl.toFixed(2)}
      </div>
      <div className="text-right text-gray-500">
        {closedAt ? formatTs(closedAt, 'MM/dd HH:mm') : formatTs(openedAt, 'MM/dd HH:mm')}
      </div>
    </div>
  )
}

function KpiCard({
  label,
  value,
  hint,
  tone = 'text-white',
}: {
  label: string
  value: string
  hint?: string
  tone?: string
}) {
  return (
    <div className="rounded-lg border border-gray-700 bg-gray-800/80 p-3">
      <div className="text-[11px] uppercase tracking-wide text-gray-500">{label}</div>
      <div className={`mt-1 text-lg font-semibold ${tone}`}>{value}</div>
      {hint && <div className="mt-1 text-xs text-gray-400">{hint}</div>}
    </div>
  )
}

function GroupPanel({
  title,
  emptyLabel,
  pairs,
  donchian,
  onOpenPair,
  onOpenDonchian,
}: {
  title: string
  emptyLabel: string
  pairs?: PairsTradeGroup[]
  donchian?: DonchianFuturesTradeGroup[]
  onOpenPair?: (tradeId: string) => void
  onOpenDonchian?: (tradeId: string) => void
}) {
  const rows = pairs ?? donchian ?? []

  return (
    <div className="rounded-xl border border-gray-700 bg-gray-800/80 p-4">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h4 className="text-sm font-semibold text-white">{title}</h4>
        <span className="text-xs text-gray-500">{rows.length} groups</span>
      </div>
      {rows.length === 0 ? (
        <div className="rounded-lg bg-gray-900/50 p-4 text-sm text-gray-500">{emptyLabel}</div>
      ) : (
        <div>
          {(pairs ?? []).map((group) => (
            <button
              key={group.trade_id}
              type="button"
              className="block w-full text-left transition-colors hover:bg-gray-900/40"
              onClick={() => onOpenPair?.(group.trade_id)}
            >
              <GroupRow
                title={group.symbols.join(' / ')}
                direction={group.pair_direction}
                status={group.status}
                openedAt={group.opened_at}
                closedAt={group.closed_at}
                realizedPnl={group.realized_pnl}
              />
            </button>
          ))}
          {(donchian ?? []).map((group) => (
            <button
              key={group.trade_id}
              type="button"
              className="block w-full text-left transition-colors hover:bg-gray-900/40"
              onClick={() => onOpenDonchian?.(group.trade_id)}
            >
              <GroupRow
                title={group.symbol}
                direction={group.direction}
                status={group.status}
                openedAt={group.opened_at}
                closedAt={group.closed_at}
                realizedPnl={group.realized_pnl}
              />
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

function TradeDetailModal({
  title,
  detail,
  onClose,
}: {
  title: string
  detail: PairsTradeGroupDetail | DonchianFuturesTradeGroupDetail
  onClose: () => void
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div
        className="max-h-[85vh] w-full max-w-3xl overflow-hidden rounded-xl bg-gray-800"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-gray-700 px-4 py-3">
          <div>
            <h3 className="text-lg font-semibold text-white">{title}</h3>
            <div className="text-xs text-gray-400">
              trade_id={detail.trade_id} · status={detail.status} · opened {formatTs(detail.opened_at, 'MM/dd HH:mm:ss')}
            </div>
          </div>
          <button onClick={onClose} className="px-2 text-xl text-gray-400 hover:text-white">
            &times;
          </button>
        </div>
        <div className="grid gap-4 overflow-y-auto p-4 md:grid-cols-[1.1fr,0.9fr]">
          <div className="space-y-3">
            <div className="rounded-lg bg-gray-900/50 p-3">
              <div className="mb-2 text-xs font-medium text-gray-400">주문 묶음</div>
              <div className="space-y-2">
                {detail.orders.map((order) => (
                  <div key={order.id} className="rounded border border-gray-700 bg-gray-800/60 p-2 text-xs">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="text-white">
                        {order.symbol} · {order.side.toUpperCase()} {order.direction ? `· ${order.direction}` : ''}
                      </div>
                      <div className="text-gray-500">{formatTs(order.created_at, 'MM/dd HH:mm:ss')}</div>
                    </div>
                    <div className="mt-1 grid grid-cols-2 gap-2 text-gray-300 md:grid-cols-4">
                      <div>qty {order.executed_quantity ?? order.requested_quantity}</div>
                      <div>px {(order.executed_price ?? order.requested_price ?? 0).toLocaleString('en-US')}</div>
                      <div>fee {fmtSignedPrice(order.fee, true)}</div>
                      <div className={(order.realized_pnl ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}>
                        pnl {order.realized_pnl != null ? fmtSignedPrice(order.realized_pnl, true) : '-'}
                      </div>
                    </div>
                    {order.signal_reason && (
                      <div className="mt-2 rounded bg-gray-900/70 p-2 text-gray-400">{order.signal_reason}</div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          </div>
          <div className="space-y-3">
            <div className="rounded-lg bg-gray-900/50 p-3">
              <div className="mb-2 text-xs font-medium text-gray-400">거래 요약</div>
              <div className="space-y-1 text-sm text-gray-300">
                <div>status: <span className="text-white">{detail.status}</span></div>
                <div>realized: <span className={detail.realized_pnl >= 0 ? 'text-green-400' : 'text-red-400'}>{fmtSignedPrice(detail.realized_pnl, true)}</span></div>
                <div>fees: <span className="text-white">{fmtSignedPrice(detail.total_fees, true)}</span></div>
                <div>closed: <span className="text-white">{formatTs(detail.closed_at, 'MM/dd HH:mm:ss')}</span></div>
              </div>
            </div>
            <div className="rounded-lg bg-gray-900/50 p-3">
              <div className="mb-2 text-xs font-medium text-gray-400">실행 저널</div>
              <div className="space-y-2">
                {detail.journal.length === 0 ? (
                  <div className="text-sm text-gray-500">저널이 없습니다.</div>
                ) : (
                  detail.journal.map((event) => (
                    <div key={event.id} className="rounded border border-gray-700 bg-gray-800/60 p-2 text-xs">
                      <div className="flex items-center justify-between gap-2">
                        <div className="text-white">{event.title}</div>
                        <div className="text-gray-500">{formatTs(event.created_at, 'MM/dd HH:mm:ss')}</div>
                      </div>
                      {event.detail && <div className="mt-1 text-gray-400">{event.detail}</div>}
                      {event.metadata && (
                        <div className="mt-2 flex flex-wrap gap-1">
                          {Object.entries(event.metadata).map(([key, value]) => (
                            <span key={key} className="rounded bg-gray-900 px-1.5 py-0.5 text-[11px] text-gray-400">
                              {key}: {String(value)}
                            </span>
                          ))}
                        </div>
                      )}
                    </div>
                  ))
                )}
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

export function LiveTradeGroups({ exchange }: { exchange?: ExchangeName }) {
  const enabled = exchange === 'binance_futures'
  const [period, setPeriod] = useState<KpiPeriod>('today')
  const [selectedPairTradeId, setSelectedPairTradeId] = useState<string | null>(null)
  const [selectedDonchianTradeId, setSelectedDonchianTradeId] = useState<string | null>(null)

  const { data: pairsGroups } = useQuery({
    queryKey: ['live', 'trades', 'pairs', 'groups'],
    queryFn: () => getPairsTradeGroups({ status: 'all', size: 5 }),
    staleTime: 10_000,
    refetchInterval: 15_000,
    enabled,
  })

  const { data: donchianGroups } = useQuery({
    queryKey: ['live', 'trades', 'donchian-futures', 'groups'],
    queryFn: () => getDonchianFuturesTradeGroups({ status: 'all', size: 5 }),
    staleTime: 10_000,
    refetchInterval: 15_000,
    enabled,
  })

  const { data: pairDetail } = useQuery({
    queryKey: ['live', 'trades', 'pairs', 'group-detail', selectedPairTradeId],
    queryFn: () => getPairsTradeGroupDetail(selectedPairTradeId!),
    enabled: enabled && !!selectedPairTradeId,
    staleTime: 10_000,
  })

  const { data: donchianDetail } = useQuery({
    queryKey: ['live', 'trades', 'donchian-futures', 'group-detail', selectedDonchianTradeId],
    queryFn: () => getDonchianFuturesTradeGroupDetail(selectedDonchianTradeId!),
    enabled: enabled && !!selectedDonchianTradeId,
    staleTime: 10_000,
  })

  const { data: pairsSummary } = useQuery({
    queryKey: ['live', 'trades', 'pairs', 'summary', period],
    queryFn: () => getTradeSummary(period, 'binance_pairs'),
    staleTime: 10_000,
    refetchInterval: 15_000,
    enabled,
  })

  const { data: donchianSummary } = useQuery({
    queryKey: ['live', 'trades', 'donchian-futures', 'summary', period],
    queryFn: () => getTradeSummary(period, 'binance_donchian_futures'),
    staleTime: 10_000,
    refetchInterval: 15_000,
    enabled,
  })

  if (!enabled) return null

  const pairsStats = summarizeGroups(pairsGroups ?? [])
  const donchianStats = summarizeGroups(donchianGroups ?? [])

  return (
    <section className="space-y-3 md:space-y-4">
      <div className="rounded-xl border border-gray-700 bg-gray-800 p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h3 className="text-base font-semibold text-white">Grouped Trades</h3>
            <p className="mt-1 text-sm text-gray-400">
              Donchian Futures와 Pairs의 실제 체결을 거래 묶음 단위로 바로 확인합니다.
            </p>
          </div>
          <div className="flex gap-2">
            {PERIOD_OPTIONS.map((option) => (
              <button
                key={option.value}
                type="button"
                onClick={() => setPeriod(option.value)}
                className={`rounded-full border px-3 py-1 text-xs font-medium transition-colors ${
                  period === option.value
                    ? 'border-blue-500 bg-blue-600/20 text-blue-300'
                    : 'border-gray-700 bg-gray-900/50 text-gray-400 hover:text-white'
                }`}
              >
                {option.label}
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <KpiCard
          label="Pairs Open"
          value={String(pairsStats.openCount)}
          hint={`${periodLabel(period)} trades ${pairsSummary?.total_trades ?? 0}`}
        />
        <KpiCard
          label="Pairs Realized"
          value={formatSignedCompact(pairsSummary?.total_pnl ?? 0)}
          hint={`win rate ${formatWinRate(pairsSummary)}`}
          tone={signedTone(pairsSummary?.total_pnl ?? 0)}
        />
        <KpiCard
          label="Donchian Open"
          value={String(donchianStats.openCount)}
          hint={`${periodLabel(period)} trades ${donchianSummary?.total_trades ?? 0}`}
        />
        <KpiCard
          label="Donchian Realized"
          value={formatSignedCompact(donchianSummary?.total_pnl ?? 0)}
          hint={`win rate ${formatWinRate(donchianSummary)}`}
          tone={signedTone(donchianSummary?.total_pnl ?? 0)}
        />
      </div>

      <div className="grid gap-3 md:grid-cols-2">
        <KpiCard label="Pairs Closed Groups" value={String(pairsStats.closedCount)} hint={`recent grouped win ${pairsStats.winRate.toFixed(1)}%`} />
        <KpiCard label="Donchian Closed Groups" value={String(donchianStats.closedCount)} hint={`recent grouped win ${donchianStats.winRate.toFixed(1)}%`} />
      </div>

      <div className="grid gap-3 xl:grid-cols-2">
        <GroupPanel
          title="Pairs Grouped Trades"
          emptyLabel="최근 Pairs grouped trade가 없습니다."
          pairs={pairsGroups}
          onOpenPair={setSelectedPairTradeId}
        />
        <GroupPanel
          title="Donchian Futures Grouped Trades"
          emptyLabel="최근 Donchian futures grouped trade가 없습니다."
          donchian={donchianGroups}
          onOpenDonchian={setSelectedDonchianTradeId}
        />
      </div>

      {pairDetail && (
        <TradeDetailModal
          title="Pairs Group Detail"
          detail={pairDetail}
          onClose={() => setSelectedPairTradeId(null)}
        />
      )}
      {donchianDetail && (
        <TradeDetailModal
          title="Donchian Futures Group Detail"
          detail={donchianDetail}
          onClose={() => setSelectedDonchianTradeId(null)}
        />
      )}
    </section>
  )
}

function summarizeGroups(groups: Array<PairsTradeGroup | DonchianFuturesTradeGroup>) {
  const openCount = groups.filter((group) => group.status !== 'closed').length
  const closed = groups.filter((group) => group.status === 'closed')
  const wins = closed.filter((group) => group.realized_pnl > 0).length
  const realizedPnl = groups.reduce((sum, group) => sum + group.realized_pnl, 0)
  return {
    openCount,
    closedCount: closed.length,
    winRate: closed.length > 0 ? (wins / closed.length) * 100 : 0,
    realizedPnl,
  }
}

function formatSignedCompact(value: number): string {
  const sign = value > 0 ? '+' : ''
  return `${sign}${value.toFixed(2)} USDT`
}

function signedTone(value: number): string {
  if (value > 0) return 'text-green-400'
  if (value < 0) return 'text-red-400'
  return 'text-white'
}

function formatWinRate(summary: TradeSummary | undefined): string {
  return `${(summary?.win_rate ?? 0).toFixed(1)}%`
}

function periodLabel(period: KpiPeriod): string {
  if (period === 'today') return 'today'
  return period
}
