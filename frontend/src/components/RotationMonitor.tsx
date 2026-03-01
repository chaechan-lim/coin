import { useQuery } from '@tanstack/react-query'
import { getRotationStatus, getMarketAnalysis } from '../api/client'
import { format } from 'date-fns'
import type { RotationStatus, ExchangeName } from '../types'

const utcToLocal = (ts: string) => new Date(ts.endsWith('Z') ? ts : ts + 'Z')

const MARKET_STATE_LABELS: Record<string, { label: string; color: string }> = {
  strong_uptrend: { label: '강한 상승', color: 'text-green-400' },
  uptrend: { label: '상승', color: 'text-green-300' },
  sideways: { label: '횡보', color: 'text-yellow-400' },
  downtrend: { label: '하락', color: 'text-red-400' },
  crash: { label: '폭락', color: 'text-red-600' },
}

export function RotationMonitor({ exchange = 'bithumb' }: { exchange?: ExchangeName }) {
  const isFutures = exchange === 'binance_futures'
  const { data, isLoading, error } = useQuery<RotationStatus>({
    queryKey: ['rotation-status', exchange],
    queryFn: () => getRotationStatus(exchange),
    refetchInterval: 30_000,
  })

  // 시장 상태는 에이전트 탭과 동일한 소스 사용 (동기화)
  const { data: analysis } = useQuery({
    queryKey: ['agents', 'market-analysis', exchange],
    queryFn: () => getMarketAnalysis(exchange),
    refetchInterval: 60_000,
    staleTime: 30_000,
  })

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64 text-gray-400">
        로테이션 데이터 로딩 중...
      </div>
    )
  }

  if (error || !data) {
    return (
      <div className="bg-red-900/30 border border-red-700 rounded-lg p-4 text-red-300">
        로테이션 상태를 불러올 수 없습니다. 엔진이 실행 중인지 확인하세요.
      </div>
    )
  }

  // 에이전트 분석 상태 우선, 없으면 엔진 상태 폴백
  const marketState = analysis?.state ?? data.market_state
  const marketInfo = MARKET_STATE_LABELS[marketState] ?? {
    label: marketState,
    color: 'text-gray-400',
  }

  const stripQuote = (sym: string) => sym.replace('/KRW', '').replace('/USDT', '')

  const maxScore = data.surge_scores.length > 0
    ? Math.max(...data.surge_scores.map((s) => s.score), data.surge_threshold * 1.5)
    : data.surge_threshold * 2

  return (
    <div className="space-y-4">
      {/* Summary Cards */}
      <div className={`grid grid-cols-2 ${isFutures ? 'md:grid-cols-3' : 'md:grid-cols-4'} gap-3`}>
        <Card label="시장 상태">
          <span className={`text-lg font-bold ${marketInfo.color}`}>{marketInfo.label}</span>
        </Card>
        {!isFutures && (
          <Card label="서지 임계값">
            <span className="text-lg font-bold text-white">{data.surge_threshold.toFixed(1)}x</span>
          </Card>
        )}
        {!isFutures && (
          <Card label="현재 서지 코인">
            <span className="text-lg font-bold text-orange-400">
              {data.current_surge_symbol ? stripQuote(data.current_surge_symbol) : '-'}
            </span>
          </Card>
        )}
        <Card label="마지막 스캔">
          <span className="text-sm font-medium text-gray-300">
            {data.last_scan_time
              ? format(utcToLocal(data.last_scan_time), 'HH:mm:ss')
              : '대기 중'}
          </span>
        </Card>
      </div>

      {/* Tracked Coins */}
      <div className="bg-gray-800 rounded-lg p-4">
        <h3 className="text-sm font-medium text-gray-400 mb-2">추적 코인 ({data.tracked_coins.length}종)</h3>
        <div className="flex flex-wrap gap-2">
          {data.tracked_coins.map((coin) => (
            <span
              key={coin}
              className="px-3 py-1 bg-blue-900/50 border border-blue-700 rounded-full text-sm text-blue-300 font-medium"
            >
              {stripQuote(coin)}
            </span>
          ))}
        </div>
      </div>

      {/* Dynamic Rotation Coins (futures) */}
      {isFutures && data.rotation_coins.length > 0 && (
        <div className="bg-gray-800 rounded-lg p-4">
          <h3 className="text-sm font-medium text-gray-400 mb-2">동적 종목 ({data.rotation_coins.length}종)</h3>
          <p className="text-xs text-gray-500 mb-2">24h 거래대금 상위 자동 선정 (6시간 갱신)</p>
          <div className="flex flex-wrap gap-2">
            {data.rotation_coins.map((coin) => (
              <span
                key={coin}
                className="px-3 py-1 bg-purple-900/50 border border-purple-700 rounded-full text-sm text-purple-300 font-medium"
              >
                {stripQuote(coin)}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Rotation Enabled Status — 빗썸만 */}
      {!isFutures && (
        <div className="bg-gray-800 rounded-lg p-3 md:p-4 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span className="text-sm text-gray-400">로테이션</span>
            <span className={`text-sm font-bold ${data.rotation_enabled ? 'text-green-400' : 'text-red-400'}`}>
              {data.rotation_enabled ? 'ON' : 'OFF'}
            </span>
          </div>
          <span className="text-xs text-gray-500">
            쿨다운: {Math.round(data.rotation_cooldown_sec / 60)}분
          </span>
        </div>
      )}

      {/* Surge Score Bar Chart — 빗썸만 */}
      {!isFutures && (
        <div className="bg-gray-800 rounded-lg p-4">
          <h3 className="text-sm font-medium text-gray-400 mb-3">
            로테이션 코인 서지 점수 ({data.surge_scores.length}종)
          </h3>
          {data.surge_scores.length === 0 ? (
            <p className="text-gray-500 text-sm">아직 스캔 데이터가 없습니다. 첫 사이클을 기다려 주세요.</p>
          ) : (
            <div className="space-y-1.5">
              {data.surge_scores.map((item) => {
                const pct = Math.min((item.score / maxScore) * 100, 100)
                const isAbove = item.above_threshold
                return (
                  <div key={item.symbol} className="flex items-center gap-2">
                    <span className="w-16 text-xs text-gray-400 text-right shrink-0">
                      {stripQuote(item.symbol)}
                    </span>
                    <div className="flex-1 h-5 bg-gray-700 rounded overflow-hidden relative">
                      {/* Threshold line */}
                      <div
                        className="absolute top-0 bottom-0 w-px bg-yellow-500 z-10"
                        style={{ left: `${Math.min((data.surge_threshold / maxScore) * 100, 100)}%` }}
                      />
                      {/* Bar */}
                      <div
                        className={`h-full rounded transition-all duration-500 ${
                          isAbove ? 'bg-red-500' : 'bg-blue-600'
                        }`}
                        style={{ width: `${pct}%` }}
                      />
                    </div>
                    <span className={`w-12 text-xs text-right shrink-0 font-mono ${
                      isAbove ? 'text-red-400 font-bold' : 'text-gray-400'
                    }`}>
                      {item.score.toFixed(2)}x
                    </span>
                  </div>
                )
              })}
              {/* Legend */}
              <div className="flex items-center gap-3 md:gap-4 mt-3 text-xs text-gray-500 flex-wrap">
                <span className="flex items-center gap-1">
                  <span className="w-3 h-3 bg-red-500 rounded-sm inline-block" /> 초과
                </span>
                <span className="flex items-center gap-1">
                  <span className="w-3 h-3 bg-blue-600 rounded-sm inline-block" /> 미만
                </span>
                <span className="flex items-center gap-1">
                  <span className="w-px h-3 bg-yellow-500 inline-block" /> 임계값 ({data.surge_threshold.toFixed(1)}x)
                </span>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function Card({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="bg-gray-800 rounded-lg p-3">
      <div className="text-xs text-gray-500 mb-1">{label}</div>
      {children}
    </div>
  )
}
