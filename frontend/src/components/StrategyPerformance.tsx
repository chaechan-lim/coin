import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell } from 'recharts'
import { compareStrategies } from '../api/client'
import type { ExchangeName } from '../types'

const STRATEGY_KR: Record<string, string> = {
  volatility_breakout: '변동성 돌파',
  ma_crossover: 'MA 크로스',
  rsi: 'RSI',
  macd_crossover: 'MACD',
  bollinger_rsi: '볼린저+RSI',
  stochastic_rsi: '스토캐스틱RSI',
  obv_divergence: 'OBV 다이버전스',
  bnf_deviation: 'BNF 이격도',
  cis_momentum: 'CIS 모멘텀',
  larry_williams: '래리 윌리엄스',
  donchian_channel: '돈치안 채널',
  risk_management: '리스크 관리',
}

const PERIODS = ['7d', '30d', '90d'] as const

export function StrategyPerformance({ exchange = 'bithumb' }: { exchange?: ExchangeName }) {
  const [period, setPeriod] = useState<string>('30d')
  const isUsdt = exchange.startsWith('binance')
  const { data, isLoading } = useQuery({
    queryKey: ['strategies', 'comparison', period, exchange],
    queryFn: () => compareStrategies(period, exchange),
    staleTime: 60_000,
  })

  const chartData = (data ?? []).map((s) => ({
    name: STRATEGY_KR[s.strategy_name] ?? s.strategy_name,
    winRate: s.win_rate,
    pnl: isUsdt ? parseFloat(s.total_pnl.toFixed(2)) : Math.round(s.total_pnl / 1000),
    rawPnl: s.total_pnl,
    trades: s.total_trades,
    avgReturn: s.avg_return_pct,
  }))

  return (
    <div className="bg-gray-800 rounded-xl p-4">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-white font-semibold">전략별 성과</h3>
        <div className="flex gap-1">
          {PERIODS.map((p) => (
            <button
              key={p}
              onClick={() => setPeriod(p)}
              className={`px-2 py-1 rounded text-xs font-medium transition-colors ${
                period === p ? 'bg-blue-600 text-white' : 'text-gray-400 hover:text-white hover:bg-gray-700'
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
        <>
          <ResponsiveContainer width="100%" height={180}>
            <BarChart data={chartData} barSize={20}>
              <XAxis dataKey="name" tick={{ fill: '#9ca3af', fontSize: 10 }} tickLine={false} />
              <YAxis tick={{ fill: '#9ca3af', fontSize: 10 }} tickLine={false} unit="%" />
              <Tooltip
                contentStyle={{ backgroundColor: '#1f2937', border: 'none', borderRadius: '8px' }}
                labelStyle={{ color: '#9ca3af' }}
                formatter={(v: number) => [`${v.toFixed(1)}%`, '승률']}
              />
              <Bar dataKey="winRate" radius={[4, 4, 0, 0]}>
                {chartData.map((entry, i) => (
                  <Cell key={i} fill={entry.winRate >= 50 ? '#10b981' : '#ef4444'} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>

          {/* Desktop Table */}
          <table className="w-full text-xs mt-2 hidden sm:table">
            <thead>
              <tr className="text-gray-500 border-b border-gray-700">
                <th className="py-1 text-left">전략</th>
                <th className="py-1 text-right">거래수</th>
                <th className="py-1 text-right">승률</th>
                <th className="py-1 text-right">총 손익</th>
                <th className="py-1 text-right">평균수익</th>
              </tr>
            </thead>
            <tbody>
              {chartData.map((s) => (
                <tr key={s.name} className="border-b border-gray-700/30">
                  <td className="py-1 text-gray-300">{s.name}</td>
                  <td className="py-1 text-right text-gray-400">{s.trades}</td>
                  <td className={`py-1 text-right font-medium ${s.winRate >= 50 ? 'text-buy' : 'text-sell'}`}>
                    {s.winRate.toFixed(1)}%
                  </td>
                  <td className={`py-1 text-right font-medium ${s.pnl >= 0 ? 'text-buy' : 'text-sell'}`}>
                    {s.pnl >= 0 ? '+' : ''}{isUsdt ? `${s.pnl.toFixed(2)} USDT` : `${s.pnl}k ₩`}
                  </td>
                  <td className={`py-1 text-right ${s.avgReturn >= 0 ? 'text-buy' : 'text-sell'}`}>
                    {s.avgReturn >= 0 ? '+' : ''}{s.avgReturn.toFixed(2)}%
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {/* Mobile Cards */}
          <div className="sm:hidden mt-2 space-y-2">
            {chartData.map((s) => (
              <div key={s.name} className="bg-gray-900 rounded-lg p-2.5">
                <div className="flex items-center justify-between mb-1">
                  <span className="text-gray-200 text-xs font-medium">{s.name}</span>
                  <span className={`text-xs font-bold ${s.winRate >= 50 ? 'text-buy' : 'text-sell'}`}>
                    {s.winRate.toFixed(1)}%
                  </span>
                </div>
                <div className="flex items-center justify-between text-xs">
                  <span className="text-gray-500">{s.trades}건</span>
                  <span className={s.pnl >= 0 ? 'text-buy' : 'text-sell'}>
                    {s.pnl >= 0 ? '+' : ''}{isUsdt ? `${s.pnl.toFixed(2)} USDT` : `${s.pnl}k ₩`}
                  </span>
                  <span className={s.avgReturn >= 0 ? 'text-buy' : 'text-sell'}>
                    평균 {s.avgReturn >= 0 ? '+' : ''}{s.avgReturn.toFixed(2)}%
                  </span>
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  )
}
