import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { getRndOverview } from '../api/client'

const SPOT_ENGINES = new Set(['binance_donchian', 'binance_fgdca'])
const FUTURES_ENGINES = new Set([
  'binance_donchian_futures', 'binance_pairs', 'binance_momentum', 'binance_hmm',
  'binance_breakout_pb', 'binance_vol_mom', 'binance_btc_neutral',
])

interface RndEngine {
  name: string
  exchange: string
  running: boolean
  paused: boolean
  capital: number
  cumulative_pnl: number
  daily_pnl: number
  positions: { symbol: string; side: string; entry: number; qty: number; current_price?: number; unrealized_pnl?: number; pnl_pct?: number }[]
  leverage: number
  idle_reason?: string | null
  next_evaluation_at?: string | null
}

interface RndOverviewData {
  total_capital: number
  total_cumulative_pnl: number
  total_pnl_pct: number
  total_positions: number
  engines: RndEngine[]
}

function StatusDot({ running, paused }: { running: boolean; paused: boolean }) {
  if (paused) return <span className="inline-block h-2 w-2 rounded-full bg-yellow-400" title="paused" />
  if (running) return <span className="inline-block h-2 w-2 rounded-full bg-green-400" title="running" />
  return <span className="inline-block h-2 w-2 rounded-full bg-gray-500" title="stopped" />
}

function formatPnl(v: number) {
  const s = v >= 0 ? `+${v.toFixed(2)}` : v.toFixed(2)
  return s
}

function pnlColor(v: number) {
  if (v > 0) return 'text-green-400'
  if (v < 0) return 'text-red-400'
  return 'text-gray-400'
}

export function RndOverview({ market }: { market?: 'spot' | 'futures' | 'all' }) {
  const { data, isLoading } = useQuery<RndOverviewData>({
    queryKey: ['rnd', 'overview'],
    queryFn: getRndOverview,
    refetchInterval: 15_000,
  })

  const filtered = useMemo(() => {
    if (!data) return null
    const engines = data.engines.filter((e) => {
      if (market === 'spot') return SPOT_ENGINES.has(e.exchange)
      if (market === 'futures') return FUTURES_ENGINES.has(e.exchange)
      return true
    })
    const total_capital = engines.reduce((s, e) => s + e.capital, 0)
    const total_pnl = engines.reduce((s, e) => s + e.cumulative_pnl, 0)
    const total_positions = engines.reduce((s, e) => s + e.positions.length, 0)
    return {
      ...data,
      engines,
      total_capital,
      total_cumulative_pnl: total_pnl,
      total_pnl_pct: total_capital > 0 ? (total_pnl / total_capital) * 100 : 0,
      total_positions,
    }
  }, [data, market])

  if (isLoading || !filtered) {
    return <div className="rounded-xl bg-gray-800/50 p-6 text-center text-sm text-gray-500">R&D 로딩 중...</div>
  }

  if (filtered.engines.length === 0) return null

  const label = market === 'spot' ? '현물' : market === 'futures' ? '선물' : '전체'

  return (
    <div className="space-y-3">
      {/* 요약 */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <div className="rounded-xl bg-gray-800 p-3">
          <div className="text-[10px] font-medium uppercase tracking-widest text-gray-500">{label} 자본</div>
          <div className="mt-1 text-lg font-semibold text-white">{filtered.total_capital.toFixed(0)}</div>
          <div className="text-[10px] text-gray-600">USDT</div>
        </div>
        <div className="rounded-xl bg-gray-800 p-3">
          <div className="text-[10px] font-medium uppercase tracking-widest text-gray-500">수익</div>
          <div className={`mt-1 text-lg font-semibold ${pnlColor(filtered.total_cumulative_pnl)}`}>
            {formatPnl(filtered.total_cumulative_pnl)}
          </div>
          <div className={`text-[10px] ${pnlColor(filtered.total_pnl_pct)}`}>{filtered.total_pnl_pct.toFixed(2)}%</div>
        </div>
        <div className="rounded-xl bg-gray-800 p-3">
          <div className="text-[10px] font-medium uppercase tracking-widest text-gray-500">포지션</div>
          <div className="mt-1 text-lg font-semibold text-white">{filtered.total_positions}</div>
          <div className="text-[10px] text-gray-600">보유 중</div>
        </div>
        <div className="rounded-xl bg-gray-800 p-3">
          <div className="text-[10px] font-medium uppercase tracking-widest text-gray-500">엔진</div>
          <div className="mt-1 text-lg font-semibold text-white">
            {filtered.engines.filter((e) => e.running && !e.paused).length}
            <span className="text-sm font-normal text-gray-500">/{filtered.engines.length}</span>
          </div>
          <div className="text-[10px] text-gray-600">활성</div>
        </div>
      </div>

      {/* 전략별 */}
      <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
        {filtered.engines.map((eng) => {
          const pnlPct = eng.capital > 0 ? (eng.cumulative_pnl / eng.capital) * 100 : 0
          return (
            <div
              key={eng.exchange}
              className="rounded-xl bg-gray-800 p-3.5 transition-colors hover:bg-gray-800/80"
            >
              <div className="flex items-center justify-between mb-2">
                <div className="flex items-center gap-2">
                  <StatusDot running={eng.running} paused={eng.paused} />
                  <span className="text-sm font-medium text-gray-100">{eng.name}</span>
                </div>
                <span className="text-[11px] text-gray-500">
                  {eng.capital.toFixed(0)}{eng.leverage > 1 ? ` · ${eng.leverage}x` : ''}
                </span>
              </div>

              <div className="flex items-baseline gap-2">
                <span className={`text-base font-semibold ${pnlColor(eng.cumulative_pnl)}`}>
                  {formatPnl(eng.cumulative_pnl)}
                </span>
                <span className={`text-[11px] ${pnlColor(pnlPct)}`}>
                  {pnlPct >= 0 ? '+' : ''}{pnlPct.toFixed(2)}%
                </span>
              </div>

              {eng.positions.length > 0 ? (
                <div className="mt-2 space-y-1.5">
                  {eng.positions.map((p: any, i: number) => (
                    <div key={i} className="rounded-lg bg-gray-900/50 px-2.5 py-1.5">
                      <div className="flex items-center gap-2 text-xs">
                        <span className={`font-medium ${p.side === 'long' ? 'text-green-400' : 'text-red-400'}`}>
                          {p.side === 'long' ? 'LONG' : 'SHORT'}
                        </span>
                        <span className="text-gray-200">{p.symbol?.replace('/USDT', '')}</span>
                        {p.current_price > 0 && (
                          <span className={`ml-auto font-medium ${p.pnl_pct >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                            {p.pnl_pct >= 0 ? '+' : ''}{p.pnl_pct?.toFixed(2)}%
                            <span className="ml-1.5 font-normal text-gray-500">
                              {p.unrealized_pnl >= 0 ? '+' : ''}{p.unrealized_pnl?.toFixed(2)}
                            </span>
                          </span>
                        )}
                      </div>
                      <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-gray-500">
                        <span>qty {p.qty?.toFixed(6) ?? '—'}</span>
                        <span>entry {p.entry?.toFixed(2) ?? '—'}</span>
                        {p.current_price > 0 && <span>now {p.current_price?.toFixed(2)}</span>}
                        {p.sl_price > 0 && <span className="text-red-400/70">SL {p.sl_price?.toFixed(2)}</span>}
                        {p.tp_price > 0 && <span className="text-green-400/70">TP {p.tp_price?.toFixed(2)}</span>}
                        {p.entry_z != null && p.entry_z !== 0 && <span className="text-blue-400/70">z={p.entry_z?.toFixed(2)}</span>}
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="mt-2 text-[11px] text-gray-600">
                  {eng.idle_reason || '대기 중'}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
