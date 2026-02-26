import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { getEngineStatus, startEngine, stopEngine } from '../api/client'
import type { ExchangeName } from '../types'

export function EngineControl({ liveEvents, exchange = 'bithumb' }: { liveEvents: string[]; exchange?: ExchangeName }) {
  const qc = useQueryClient()

  const { data: status } = useQuery({
    queryKey: ['engine', 'status', exchange],
    queryFn: () => getEngineStatus(exchange),
    refetchInterval: 10_000,
  })

  const startMut = useMutation({
    mutationFn: () => startEngine(exchange),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['engine'] }),
  })

  const stopMut = useMutation({
    mutationFn: () => stopEngine(exchange),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['engine'] }),
  })

  const isRunning = status?.is_running ?? false
  const isPaper = status?.mode === 'paper'

  return (
    <div className="bg-gray-800 rounded-xl p-3 md:p-4">
      <div className="flex items-center justify-between mb-3 md:mb-4 gap-2">
        <div className="flex items-center gap-2 md:gap-3 min-w-0 flex-wrap">
          <div className="flex items-center gap-2">
            <div className={`w-2.5 h-2.5 rounded-full shrink-0 ${isRunning ? 'bg-green-500 animate-pulse' : 'bg-gray-500'}`} />
            <h3 className="text-white font-semibold text-sm md:text-base">엔진 상태</h3>
          </div>
          <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${isPaper ? 'bg-blue-900 text-blue-300' : 'bg-orange-900 text-orange-300'}`}>
            {isPaper ? '페이퍼' : '실전'}
          </span>
        </div>
        <div className="flex gap-2 shrink-0">
          <button
            onClick={() => startMut.mutate()}
            disabled={isRunning || startMut.isPending}
            className="px-3 py-1.5 bg-green-700 hover:bg-green-600 disabled:opacity-40 text-white text-sm rounded-lg font-medium transition-colors active:bg-green-500"
          >
            시작
          </button>
          <button
            onClick={() => stopMut.mutate()}
            disabled={!isRunning || stopMut.isPending}
            className="px-3 py-1.5 bg-red-800 hover:bg-red-700 disabled:opacity-40 text-white text-sm rounded-lg font-medium transition-colors active:bg-red-600"
          >
            중지
          </button>
        </div>
      </div>

      {status && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm mb-3">
          <div>
            <div className="text-gray-500 text-xs">오늘 거래</div>
            <div className="text-white font-medium">{status.daily_trade_count}건</div>
          </div>
          <div>
            <div className="text-gray-500 text-xs">평가 주기</div>
            <div className="text-white font-medium">{status.evaluation_interval_sec}초</div>
          </div>
          <div>
            <div className="text-gray-500 text-xs">활성 전략</div>
            <div className="text-white font-medium">{status.strategies_active.length}개</div>
          </div>
          <div>
            <div className="text-gray-500 text-xs">추적 코인</div>
            <div className="text-white font-medium">{status.tracked_coins.length}종</div>
          </div>
        </div>
      )}

      {/* Live event feed */}
      <div>
        <div className="text-gray-500 text-xs mb-1">실시간 이벤트</div>
        <div className="bg-gray-900 rounded-lg p-2 h-24 overflow-y-auto font-mono text-xs space-y-0.5">
          {liveEvents.length === 0 ? (
            <div className="text-gray-600">대기 중...</div>
          ) : (
            [...liveEvents].reverse().map((e, i) => (
              <div key={i} className="text-gray-400">{e}</div>
            ))
          )}
        </div>
      </div>
    </div>
  )
}
