import { useState } from 'react'
import { AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from 'recharts'
import { format } from 'date-fns'
import { usePortfolioHistory } from '../hooks/usePortfolio'

/** API 타임스탬프(UTC, timezone 미표기)를 로컬 Date로 변환 */
const utcToLocal = (ts: string) => new Date(ts.endsWith('Z') ? ts : ts + 'Z')

const PERIODS = ['1d', '7d', '30d', '90d', 'all'] as const

export function PortfolioChart() {
  const [period, setPeriod] = useState<string>('7d')
  const { data, isLoading } = usePortfolioHistory(period)

  const chartData = (data ?? []).map((p) => ({
    time: format(utcToLocal(p.timestamp), 'MM/dd HH:mm'),
    value: Math.round(p.total_value),
    pnl: Math.round(p.unrealized_pnl),
  }))

  return (
    <div className="bg-gray-800 rounded-xl p-4">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-white font-semibold">포트폴리오 추이</h3>
        <div className="flex gap-1">
          {PERIODS.map((p) => (
            <button
              key={p}
              onClick={() => setPeriod(p)}
              className={`px-2 py-1 rounded text-xs font-medium transition-colors ${
                period === p
                  ? 'bg-blue-600 text-white'
                  : 'text-gray-400 hover:text-white hover:bg-gray-700'
              }`}
            >
              {p}
            </button>
          ))}
        </div>
      </div>

      {isLoading ? (
        <div className="h-48 flex items-center justify-center text-gray-500">로딩 중...</div>
      ) : chartData.length === 0 ? (
        <div className="h-48 flex items-center justify-center text-gray-500">데이터 없음</div>
      ) : (
        <ResponsiveContainer width="100%" height={200}>
          <AreaChart data={chartData}>
            <defs>
              <linearGradient id="valueGrad" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor="#3b82f6" stopOpacity={0.3} />
                <stop offset="95%" stopColor="#3b82f6" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
            <XAxis dataKey="time" tick={{ fill: '#9ca3af', fontSize: 10 }} tickLine={false} />
            <YAxis
              tick={{ fill: '#9ca3af', fontSize: 10 }}
              tickLine={false}
              tickFormatter={(v) => `${(v / 1000).toFixed(0)}k`}
            />
            <Tooltip
              contentStyle={{ backgroundColor: '#1f2937', border: 'none', borderRadius: '8px' }}
              labelStyle={{ color: '#9ca3af' }}
              formatter={(v: number) => [`${v.toLocaleString()} ₩`, '총 자산']}
            />
            <Area
              type="monotone"
              dataKey="value"
              stroke="#3b82f6"
              strokeWidth={2}
              fill="url(#valueGrad)"
            />
          </AreaChart>
        </ResponsiveContainer>
      )}
    </div>
  )
}
