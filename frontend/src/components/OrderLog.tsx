import { useState, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { getStrategyLogs, getStrategies, getEngineStatus } from '../api/client'
import { formatTs } from '../utils/date'
import type { StrategyLog, ExchangeName } from '../types'

const SIGNAL_STYLE: Record<string, string> = {
  BUY: 'text-buy bg-green-900/40 border-green-800',
  SELL: 'text-sell bg-red-900/40 border-red-800',
  HOLD: 'text-gray-400 bg-gray-800 border-gray-700',
}

const VERDICT_STYLE: Record<string, string> = {
  BUY: 'text-buy bg-green-900/60 border-green-700',
  SELL: 'text-sell bg-red-900/60 border-red-700',
  HOLD: 'text-gray-400 bg-gray-800/80 border-gray-600',
}

/** Minimum active weight for a combined signal — mirrors SignalCombiner.MIN_ACTIVE_WEIGHT */
const MIN_ACTIVE_WEIGHT = 0.12

/**
 * Compute combined/final signal for a set of strategy logs.
 * Mirrors the core logic of SignalCombiner.combine() (backend/strategies/combiner.py).
 *
 * HOLD = abstain (not counted in active weight).
 * BUY/SELL scores are normalised by active_weight then compared to minConfidence.
 */
export function computeCombinedSignal(
  logs: StrategyLog[],
  weights: Record<string, number>,
  minConfidence: number,
): { action: 'BUY' | 'SELL' | 'HOLD'; confidence: number } {
  let buyScore = 0, sellScore = 0
  let buyActive = 0, sellActive = 0

  for (const log of logs) {
    const w = weights[log.strategy_name] ?? 0.1
    const conf = log.confidence ?? 0
    if (log.signal_type === 'BUY') {
      buyScore += w * conf
      buyActive += w
    } else if (log.signal_type === 'SELL') {
      sellScore += w * conf
      sellActive += w
    }
    // HOLD → abstain, not counted
  }

  const activeWeight = buyActive + sellActive
  if (activeWeight < MIN_ACTIVE_WEIGHT) {
    return { action: 'HOLD', confidence: 0 }
  }

  const buyNorm = buyScore / activeWeight
  const sellNorm = sellScore / activeWeight
  const isLong = buyNorm >= sellNorm
  const winningScore = isLong ? buyNorm : sellNorm

  if (winningScore < minConfidence) {
    return { action: 'HOLD', confidence: winningScore }
  }

  return { action: isLong ? 'BUY' : 'SELL', confidence: winningScore }
}

export function OrderLog({ exchange = 'bithumb' }: { exchange?: ExchangeName }) {
  const [symbol, setSymbol] = useState('')
  const [strategy, setStrategy] = useState('')
  const [page, setPage] = useState(1)

  // Fetch active strategies for dynamic filter list
  const { data: strategies } = useQuery({
    queryKey: ['strategies', exchange],
    queryFn: () => getStrategies(exchange),
    staleTime: 60_000,
  })

  // Fetch engine status for min_confidence threshold
  const { data: engineStatus } = useQuery({
    queryKey: ['engine-status', exchange],
    queryFn: () => getEngineStatus(exchange),
    staleTime: 30_000,
  })

  const minConfidence = engineStatus?.min_confidence ?? 0.55

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

  // Build strategy weights map for combined signal computation
  const weightsMap = useMemo(() => {
    if (!strategies) return {} as Record<string, number>
    return Object.fromEntries(strategies.map((s) => [s.name, s.current_weight]))
  }, [strategies])

  // Group logs by symbol
  const grouped = useMemo(() => {
    if (!data) return []
    const map = new Map<string, StrategyLog[]>()
    for (const log of data) {
      const key = log.symbol
      if (!map.has(key)) map.set(key, [])
      map.get(key)!.push(log)
    }
    return Array.from(map.entries())
  }, [data])

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
              <option value="">전략 (전체)</option>
              {strategies && strategies.length > 0
                ? strategies.map((s) => (
                    <option key={s.name} value={s.name}>{s.display_name}</option>
                  ))
                : null}
            </select>
          </div>
          <div className="flex items-center gap-1.5 ml-auto">
            <span className="text-gray-500 text-xs">임계값</span>
            <span className="text-yellow-400 text-xs font-medium">{(minConfidence * 100).toFixed(0)}%</span>
          </div>
        </div>
      </div>

      {isLoading ? (
        <div className="p-8 text-center text-gray-500">로딩 중...</div>
      ) : !data || data.length === 0 ? (
        <div className="p-8 text-center text-gray-500">로그 없음</div>
      ) : (
        <>
          <div className="divide-y divide-gray-700">
            {grouped.map(([sym, logs]) => (
              <div key={sym} className="px-3 md:px-4 py-3">
                {/* Coin header */}
                <div className="flex items-center gap-2 mb-2 flex-wrap">
                  <span className="text-white font-semibold text-sm">
                    {sym.replace(/\/(KRW|USDT)/, '')}
                  </span>
                  <span className="text-gray-500 text-xs">{sym.match(/\/(KRW|USDT)/)?.[0]?.replace('/', '') ?? ''}</span>
                  <span className="text-gray-600 text-xs ml-1">
                    {formatTs(logs[0].logged_at, 'MM/dd HH:mm')}
                  </span>

                  {/* Final combined verdict */}
                  {(() => {
                    const verdict = computeCombinedSignal(logs, weightsMap, minConfidence)
                    const vs = VERDICT_STYLE[verdict.action] ?? VERDICT_STYLE.HOLD
                    return (
                      <div className="ml-auto flex items-center gap-1.5">
                        <span className="text-gray-500 text-xs">최종</span>
                        <span className={`text-xs font-bold px-2 py-0.5 rounded border ${vs}`}>
                          {verdict.action}
                          {verdict.confidence > 0 && (
                            <span className="ml-1 font-normal opacity-80">
                              {(verdict.confidence * 100).toFixed(0)}%
                            </span>
                          )}
                        </span>
                      </div>
                    )
                  })()}
                </div>

                {/* Strategy signals for this coin */}
                <div className="flex flex-col gap-1.5 pl-2 border-l border-gray-700">
                  {logs.map((log: StrategyLog) => {
                    const signalStyle = SIGNAL_STYLE[log.signal_type ?? 'HOLD'] ?? SIGNAL_STYLE.HOLD
                    return (
                      <div key={log.id} className="hover:bg-gray-700/20 rounded px-2 py-1.5">
                        <div className="flex items-center gap-2 flex-wrap mb-1">
                          <span className={`text-xs font-bold border px-1.5 py-0.5 rounded ${signalStyle}`}>
                            {log.signal_type ?? '?'}
                          </span>
                          <span className="text-gray-400 text-xs">{log.strategy_name.replace(/_/g, ' ')}</span>
                          {log.was_executed && (
                            <span className="text-green-400 text-xs font-medium">✓ 체결</span>
                          )}
                        </div>

                        {log.confidence != null && (
                          <div className="flex items-center gap-2 mb-1">
                            <span className="text-gray-500 text-xs">신뢰도</span>
                            <div className="relative w-24 bg-gray-700 rounded-full h-1.5">
                              <div
                                className={`h-1.5 rounded-full ${log.signal_type === 'BUY' ? 'bg-green-500' : log.signal_type === 'SELL' ? 'bg-red-500' : 'bg-gray-500'}`}
                                style={{ width: `${log.confidence * 100}%` }}
                              />
                              {/* Threshold marker */}
                              <div
                                className="absolute top-1/2 -translate-y-1/2 w-0.5 h-3 bg-yellow-400/80"
                                style={{ left: `${minConfidence * 100}%` }}
                                title={`임계값: ${(minConfidence * 100).toFixed(0)}%`}
                              />
                            </div>
                            <span className={`text-xs font-medium ${log.confidence >= minConfidence ? 'text-gray-300' : 'text-gray-500'}`}>
                              {(log.confidence * 100).toFixed(0)}%
                            </span>
                          </div>
                        )}

                        {log.reason && (
                          <div className="text-gray-400 text-xs leading-relaxed">{log.reason}</div>
                        )}

                        {log.indicators && Object.keys(log.indicators).length > 0 && (
                          <div className="flex flex-wrap gap-1.5 mt-1">
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
              </div>
            ))}
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
