import { useState, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell,
  AreaChart, Area,
} from 'recharts'
import { getDailyPnl } from '../api/client'
import type { ExchangeName } from '../types'
import { fmtSignedPrice } from '../utils/format'

const PERIODS = [
  { label: '7일', days: 7 },
  { label: '30일', days: 30 },
  { label: '90일', days: 90 },
  { label: '전체', days: 365 },
] as const

export function DailyPnLStats({ exchange = 'bithumb' }: { exchange?: ExchangeName }) {
  const [days, setDays] = useState(30)
  const isUsdt = exchange.startsWith('binance')
  const currency = isUsdt ? 'USDT' : '원'

  const { data, isLoading } = useQuery({
    queryKey: ['dailyPnl', days, exchange],
    queryFn: () => getDailyPnl(days, exchange),
    staleTime: 60_000,
  })

  const records = data ?? []

  // Summary stats
  const stats = useMemo(() => {
    if (records.length === 0) return null
    const totalPnl = records.reduce((s, r) => s + r.daily_pnl, 0)
    const avgPct = records.reduce((s, r) => s + r.daily_pnl_pct, 0) / records.length
    const positiveDays = records.filter((r) => r.daily_pnl > 0).length
    const winRate = (positiveDays / records.length) * 100
    const maxLoss = Math.min(...records.map((r) => r.daily_pnl))
    const maxGain = Math.max(...records.map((r) => r.daily_pnl))
    const totalTrades = records.reduce((s, r) => s + r.trade_count, 0)
    return { totalPnl, avgPct, winRate, maxLoss, maxGain, totalTrades, positiveDays, totalDays: records.length }
  }, [records])

  // Chart data
  const chartData = useMemo(() => {
    let cumulative = 0
    return records.map((r) => {
      cumulative += r.daily_pnl
      return {
        date: r.date.slice(5), // MM-DD
        fullDate: r.date,
        pnl: isUsdt ? parseFloat(r.daily_pnl.toFixed(2)) : Math.round(r.daily_pnl),
        pnlPct: r.daily_pnl_pct,
        cumulative: isUsdt ? parseFloat(cumulative.toFixed(2)) : Math.round(cumulative),
        trades: r.trade_count,
      }
    })
  }, [records, isUsdt])

  const formatValue = (v: number) => fmtSignedPrice(v, isUsdt)

  return (
    <div className="space-y-3">
      {/* Header + Period Selector */}
      <div className="bg-gray-800 rounded-xl p-4">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-white font-semibold">일일 손익 통계</h3>
          <div className="flex gap-1">
            {PERIODS.map((p) => (
              <button
                key={p.days}
                onClick={() => setDays(p.days)}
                className={`px-2 py-1 rounded text-xs font-medium transition-colors ${
                  days === p.days ? 'bg-blue-600 text-white' : 'text-gray-400 hover:text-white hover:bg-gray-700'
                }`}
              >
                {p.label}
              </button>
            ))}
          </div>
        </div>

        {isLoading ? (
          <div className="h-32 flex items-center justify-center text-gray-500">로딩 중...</div>
        ) : !stats ? (
          <div className="h-32 flex items-center justify-center text-gray-500">데이터 없음</div>
        ) : (
          <>
            {/* Summary Cards */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-2 mb-4">
              <SummaryCard
                label="누적 손익"
                value={formatValue(stats.totalPnl)}
                positive={stats.totalPnl >= 0}
              />
              <SummaryCard
                label="평균 일 수익률"
                value={`${stats.avgPct >= 0 ? '+' : ''}${stats.avgPct.toFixed(2)}%`}
                positive={stats.avgPct >= 0}
              />
              <SummaryCard
                label="승률 (양수일)"
                value={`${stats.winRate.toFixed(1)}% (${stats.positiveDays}/${stats.totalDays})`}
                positive={stats.winRate >= 50}
              />
              <SummaryCard
                label="최대 일 손실"
                value={stats.maxLoss >= 0 ? '없음' : formatValue(stats.maxLoss)}
                positive={stats.maxLoss >= 0}
              />
            </div>
          </>
        )}
      </div>

      {/* Bar Chart: Daily PnL */}
      {chartData.length > 0 && (
        <div className="bg-gray-800 rounded-xl p-4">
          <h4 className="text-gray-300 text-sm font-medium mb-2">일별 손익</h4>
          <ResponsiveContainer width="100%" height={200}>
            <BarChart data={chartData} barSize={chartData.length > 60 ? 4 : chartData.length > 30 ? 6 : 12}>
              <XAxis
                dataKey="date"
                tick={{ fill: '#9ca3af', fontSize: 10 }}
                tickLine={false}
                interval={Math.max(0, Math.floor(chartData.length / 8) - 1)}
              />
              <YAxis tick={{ fill: '#9ca3af', fontSize: 10 }} tickLine={false} />
              <Tooltip
                contentStyle={{ backgroundColor: '#1f2937', border: 'none', borderRadius: '8px' }}
                labelStyle={{ color: '#9ca3af' }}
                labelFormatter={(label, payload) => {
                  const item = payload?.[0]?.payload
                  return item?.fullDate ?? label
                }}
                formatter={(v: number) => [formatValue(v), '일일 손익']}
              />
              <Bar dataKey="pnl" radius={[2, 2, 0, 0]}>
                {chartData.map((entry, i) => (
                  <Cell key={i} fill={entry.pnl >= 0 ? '#10b981' : '#ef4444'} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}

      {/* Area Chart: Cumulative PnL */}
      {chartData.length > 0 && (
        <div className="bg-gray-800 rounded-xl p-4">
          <h4 className="text-gray-300 text-sm font-medium mb-2">누적 손익</h4>
          <ResponsiveContainer width="100%" height={180}>
            <AreaChart data={chartData}>
              <XAxis
                dataKey="date"
                tick={{ fill: '#9ca3af', fontSize: 10 }}
                tickLine={false}
                interval={Math.max(0, Math.floor(chartData.length / 8) - 1)}
              />
              <YAxis tick={{ fill: '#9ca3af', fontSize: 10 }} tickLine={false} />
              <Tooltip
                contentStyle={{ backgroundColor: '#1f2937', border: 'none', borderRadius: '8px' }}
                labelStyle={{ color: '#9ca3af' }}
                labelFormatter={(label, payload) => {
                  const item = payload?.[0]?.payload
                  return item?.fullDate ?? label
                }}
                formatter={(v: number) => [formatValue(v), '누적 손익']}
              />
              <defs>
                <linearGradient id="cumGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="#3b82f6" stopOpacity={0.3} />
                  <stop offset="95%" stopColor="#3b82f6" stopOpacity={0} />
                </linearGradient>
              </defs>
              <Area
                type="monotone"
                dataKey="cumulative"
                stroke="#3b82f6"
                fill="url(#cumGrad)"
                strokeWidth={2}
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}

      {/* Table (desktop) + Cards (mobile) */}
      {records.length > 0 && (
        <div className="bg-gray-800 rounded-xl p-4">
          <h4 className="text-gray-300 text-sm font-medium mb-2">일별 상세</h4>

          {/* Desktop Table */}
          <table className="w-full text-xs hidden sm:table">
            <thead>
              <tr className="text-gray-500 border-b border-gray-700">
                <th className="py-1 text-left">날짜</th>
                <th className="py-1 text-right">시작 자산</th>
                <th className="py-1 text-right">마감 자산</th>
                <th className="py-1 text-right">일일 손익</th>
                <th className="py-1 text-right">수익률</th>
                <th className="py-1 text-right">매매</th>
                <th className="py-1 text-right">수수료</th>
              </tr>
            </thead>
            <tbody>
              {[...records].reverse().map((r) => {
                const pnlColor = r.daily_pnl >= 0 ? 'text-buy' : 'text-sell'
                return (
                  <tr key={r.date} className="border-b border-gray-700/30">
                    <td className="py-1 text-gray-300">{r.date}</td>
                    <td className="py-1 text-right text-gray-400">
                      {isUsdt ? r.open_value.toFixed(2) : r.open_value.toLocaleString()}
                    </td>
                    <td className="py-1 text-right text-gray-400">
                      {isUsdt ? r.close_value.toFixed(2) : r.close_value.toLocaleString()}
                    </td>
                    <td className={`py-1 text-right font-medium ${pnlColor}`}>
                      {formatValue(r.daily_pnl)}
                    </td>
                    <td className={`py-1 text-right ${pnlColor}`}>
                      {r.daily_pnl_pct >= 0 ? '+' : ''}{r.daily_pnl_pct.toFixed(2)}%
                    </td>
                    <td className="py-1 text-right text-gray-400">
                      {r.trade_count} ({r.win_count}W/{r.loss_count}L)
                    </td>
                    <td className="py-1 text-right text-gray-500">
                      {isUsdt ? r.total_fees.toFixed(2) : r.total_fees.toLocaleString()}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>

          {/* Mobile Cards */}
          <div className="sm:hidden space-y-2">
            {[...records].reverse().map((r) => {
              const pnlColor = r.daily_pnl >= 0 ? 'text-buy' : 'text-sell'
              return (
                <div key={r.date} className="bg-gray-900 rounded-lg p-2.5">
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-gray-200 text-xs font-medium">{r.date}</span>
                    <span className={`text-xs font-bold ${pnlColor}`}>
                      {formatValue(r.daily_pnl)}
                    </span>
                  </div>
                  <div className="flex items-center justify-between text-xs">
                    <span className={pnlColor}>
                      {r.daily_pnl_pct >= 0 ? '+' : ''}{r.daily_pnl_pct.toFixed(2)}%
                    </span>
                    <span className="text-gray-500">{r.trade_count}건</span>
                    <span className="text-gray-500">{r.win_count}W/{r.loss_count}L</span>
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}

function SummaryCard({
  label,
  value,
  positive,
}: {
  label: string
  value: string
  positive: boolean
}) {
  return (
    <div className="bg-gray-900 rounded-lg p-2.5">
      <div className="text-gray-500 text-[10px] mb-0.5">{label}</div>
      <div className={`text-sm font-bold ${positive ? 'text-buy' : 'text-sell'}`}>{value}</div>
    </div>
  )
}
