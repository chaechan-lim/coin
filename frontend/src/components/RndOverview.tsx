import { useQuery } from '@tanstack/react-query'
import { getRndOverview } from '../api/client'

interface RndEngine {
  name: string
  exchange: string
  running: boolean
  paused: boolean
  capital: number
  cumulative_pnl: number
  daily_pnl: number
  positions: { symbol: string; side: string; entry: number; qty: number }[]
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

export function RndOverview() {
  const { data, isLoading } = useQuery<RndOverviewData>({
    queryKey: ['rnd', 'overview'],
    queryFn: getRndOverview,
    refetchInterval: 15_000,
  })

  if (isLoading || !data) {
    return <div className="rounded-xl border border-gray-700 bg-gray-800 p-4 text-sm text-gray-500">R&D 로딩 중...</div>
  }

  return (
    <div className="space-y-3">
      {/* 요약 바 */}
      <div className="flex flex-wrap gap-4 rounded-xl border border-gray-700 bg-gray-800 px-4 py-3">
        <div>
          <div className="text-[11px] uppercase text-gray-500">총 자본</div>
          <div className="text-lg font-bold text-white">{data.total_capital.toFixed(0)} USDT</div>
        </div>
        <div>
          <div className="text-[11px] uppercase text-gray-500">총 수익</div>
          <div className={`text-lg font-bold ${pnlColor(data.total_cumulative_pnl)}`}>
            {formatPnl(data.total_cumulative_pnl)} ({data.total_pnl_pct.toFixed(2)}%)
          </div>
        </div>
        <div>
          <div className="text-[11px] uppercase text-gray-500">보유 포지션</div>
          <div className="text-lg font-bold text-white">{data.total_positions}건</div>
        </div>
        <div>
          <div className="text-[11px] uppercase text-gray-500">엔진</div>
          <div className="text-lg font-bold text-white">
            {data.engines.filter((e) => e.running && !e.paused).length}/{data.engines.length} 활성
          </div>
        </div>
      </div>

      {/* 전략별 카드 */}
      <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
        {data.engines.map((eng) => {
          const pnlPct = eng.capital > 0 ? (eng.cumulative_pnl / eng.capital) * 100 : 0
          return (
            <div
              key={eng.exchange}
              className="rounded-lg border border-gray-700 bg-gray-900/60 p-3 hover:border-gray-600 transition-colors"
            >
              <div className="flex items-center justify-between mb-2">
                <div className="flex items-center gap-2">
                  <StatusDot running={eng.running} paused={eng.paused} />
                  <span className="text-sm font-semibold text-white">{eng.name}</span>
                </div>
                <span className="text-xs text-gray-500">
                  {eng.capital.toFixed(0)} USDT{eng.leverage > 1 ? ` · ${eng.leverage}x` : ''}
                </span>
              </div>

              <div className="flex items-baseline gap-3 mb-1">
                <span className={`text-base font-bold ${pnlColor(eng.cumulative_pnl)}`}>
                  {formatPnl(eng.cumulative_pnl)} USDT
                </span>
                <span className={`text-xs ${pnlColor(pnlPct)}`}>
                  ({pnlPct >= 0 ? '+' : ''}{pnlPct.toFixed(2)}%)
                </span>
              </div>

              {eng.positions.length > 0 ? (
                <div className="mt-1 space-y-1">
                  {eng.positions.map((p, i) => (
                    <div key={i} className="flex items-center gap-2 text-xs">
                      <span className={p.side === 'long' ? 'text-green-400' : 'text-red-400'}>
                        {p.side === 'long' ? '▲' : '▼'} {p.side.toUpperCase()}
                      </span>
                      <span className="text-gray-300">{p.symbol}</span>
                      <span className="text-gray-500">@ {p.entry.toFixed(2)}</span>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="text-xs text-gray-500 mt-1">
                  {eng.idle_reason || '시그널 대기 중'}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
