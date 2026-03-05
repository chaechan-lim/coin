import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { getStrategyLogs } from '../api/client'
import { formatTs } from '../utils/date'
import type { StrategyLog, ExchangeName } from '../types'

const SIGNAL_STYLE: Record<string, string> = {
  BUY: 'text-buy bg-green-900/40 border-green-800',
  SELL: 'text-sell bg-red-900/40 border-red-800',
  HOLD: 'text-gray-400 bg-gray-800 border-gray-700',
}

export function OrderLog({ exchange = 'bithumb' }: { exchange?: ExchangeName }) {
  const [symbol, setSymbol] = useState('')
  const [strategy, setStrategy] = useState('')
  const [page, setPage] = useState(1)

  const { data, isLoading } = useQuery({
    queryKey: ['strategy-logs', symbol, strategy, page, exchange],
    queryFn: () =>
      getStrategyLogs({
        symbol: symbol || undefined,
        strategy: strategy || undefined,
        page,
        size: 30,
        exchange,
      }),
    staleTime: 20_000,
  })

  return (
    <div className="bg-gray-800 rounded-xl overflow-hidden">
      <div className="px-4 py-3 border-b border-gray-700">
        <div className="flex items-center gap-3 flex-wrap">
          <h3 className="text-white font-semibold mr-2 text-sm md:text-base">전략 신호 로그</h3>
          <div className="flex items-center gap-2 flex-wrap">
            <input
              className="bg-gray-700 text-white text-xs px-2 py-1.5 rounded border border-gray-600 w-24 sm:w-28"
              placeholder="코인"
              value={symbol}
              onChange={(e) => { setSymbol(e.target.value); setPage(1) }}
            />
            <select
              className="bg-gray-700 text-white text-xs px-2 py-1.5 rounded border border-gray-600"
              value={strategy}
              onChange={(e) => { setStrategy(e.target.value); setPage(1) }}
            >
              <option value="">전략</option>
              <option value="ma_crossover">MA 크로스</option>
              <option value="rsi">RSI</option>
              <option value="macd_crossover">MACD</option>
              <option value="bollinger_rsi">볼린저+RSI</option>
              <option value="stochastic_rsi">스토캐스틱RSI</option>
              <option value="obv_divergence">OBV 다이버전스</option>
              <option value="bnf_deviation">BNF 이격도</option>
              <option value="cis_momentum">CIS 모멘텀</option>
              <option value="larry_williams">래리 윌리엄스</option>
              <option value="donchian_channel">돈치안 채널</option>
              <option value="risk_management">리스크 관리</option>
            </select>
          </div>
          <span className="text-gray-500 text-xs hidden sm:inline">* 체결 여부와 무관하게 모든 신호</span>
        </div>
      </div>

      {isLoading ? (
        <div className="p-8 text-center text-gray-500">로딩 중...</div>
      ) : !data || data.length === 0 ? (
        <div className="p-8 text-center text-gray-500">로그 없음</div>
      ) : (
        <>
          <div className="divide-y divide-gray-700/50">
            {data.map((log: StrategyLog) => {
              const signalStyle = SIGNAL_STYLE[log.signal_type ?? 'HOLD'] ?? SIGNAL_STYLE.HOLD
              return (
                <div key={log.id} className="px-3 md:px-4 py-3 hover:bg-gray-700/20">
                  <div className="flex items-center gap-2 md:gap-3 mb-1 flex-wrap">
                    <span className={`text-xs font-bold border px-1.5 py-0.5 rounded ${signalStyle}`}>
                      {log.signal_type ?? '?'}
                    </span>
                    <span className="text-white text-sm font-medium">{log.symbol.replace(/\/(KRW|USDT)/, '')}</span>
                    <span className="text-gray-500 text-xs hidden sm:inline">{log.strategy_name.replace(/_/g, ' ')}</span>
                    {log.was_executed && (
                      <span className="text-green-400 text-xs">✓</span>
                    )}
                    <span className="ml-auto text-gray-500 text-xs">
                      {formatTs(log.logged_at, 'MM/dd HH:mm')}
                    </span>
                  </div>

                  {log.confidence != null && (
                    <div className="flex items-center gap-2 mb-1">
                      <span className="text-gray-500 text-xs">신뢰도</span>
                      <div className="w-20 bg-gray-700 rounded-full h-1">
                        <div
                          className={`h-1 rounded-full ${log.signal_type === 'BUY' ? 'bg-green-500' : log.signal_type === 'SELL' ? 'bg-red-500' : 'bg-gray-500'}`}
                          style={{ width: `${log.confidence * 100}%` }}
                        />
                      </div>
                      <span className="text-gray-400 text-xs">{(log.confidence * 100).toFixed(0)}%</span>
                    </div>
                  )}

                  {log.reason && (
                    <div className="text-gray-300 text-xs leading-relaxed">{log.reason}</div>
                  )}

                  {log.indicators && Object.keys(log.indicators).length > 0 && (
                    <div className="flex flex-wrap gap-2 mt-1">
                      {Object.entries(log.indicators).map(([k, v]) => (
                        <span key={k} className="text-xs bg-gray-700 px-1.5 py-0.5 rounded text-gray-400">
                          {k}: {typeof v === 'number' ? v.toLocaleString() : String(v)}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
          <div className="flex justify-center gap-2 p-3">
            <button
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              disabled={page === 1}
              className="px-4 py-2 bg-gray-700 text-gray-300 rounded text-sm disabled:opacity-40 active:bg-gray-600"
            >
              이전
            </button>
            <span className="px-3 py-2 text-gray-400 text-sm">{page}페이지</span>
            <button
              onClick={() => setPage((p) => p + 1)}
              disabled={!data || data.length < 30}
              className="px-4 py-2 bg-gray-700 text-gray-300 rounded text-sm disabled:opacity-40 active:bg-gray-600"
            >
              다음
            </button>
          </div>
        </>
      )}
    </div>
  )
}
