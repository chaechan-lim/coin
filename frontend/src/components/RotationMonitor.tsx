import { useQuery } from '@tanstack/react-query'
import { getRotationStatus, getMarketAnalysis, getSurgeScanStatus } from '../api/client'
import type { RotationStatus, SurgeScanStatus, ExchangeName } from '../types'
import { formatTs } from '../utils/date'

const MARKET_STATE_LABELS: Record<string, { label: string; color: string }> = {
  // v1 MarketState
  strong_uptrend: { label: '강한 상승', color: 'text-green-400' },
  uptrend: { label: '상승', color: 'text-green-300' },
  sideways: { label: '횡보', color: 'text-yellow-400' },
  downtrend: { label: '하락', color: 'text-red-400' },
  crash: { label: '폭락', color: 'text-red-600' },
  // v2 Regime
  trending_up: { label: '상승 추세', color: 'text-green-400' },
  trending_down: { label: '하락 추세', color: 'text-red-400' },
  ranging: { label: '횡보 구간', color: 'text-yellow-400' },
  volatile: { label: '고변동성', color: 'text-orange-400' },
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

  // 선물 탭에서 서지 스캔 상태도 가져오기
  const { data: surgeData } = useQuery<SurgeScanStatus>({
    queryKey: ['surge-scan'],
    queryFn: getSurgeScanStatus,
    refetchInterval: 10_000,
    enabled: isFutures,
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

  // V2 레짐이 있으면 우선 표시, 없으면 에이전트 분석 → 엔진 상태 폴백
  const v2 = analysis?.v2_regime
  const marketState = v2?.regime ?? analysis?.state ?? data.market_state
  const marketInfo = MARKET_STATE_LABELS[marketState] ?? {
    label: marketState,
    color: 'text-gray-400',
  }
  // 에이전트 분석과 V2 레짐이 다른 경우 보조 표시
  const agentState = analysis?.state
  const showAgentSecondary = v2 && agentState && agentState !== marketState

  const stripQuote = (sym: string) => sym.replace('/KRW', '').replace('/USDT', '')

  const maxScore = data.surge_scores.length > 0
    ? Math.max(...data.surge_scores.map((s) => s.score), data.surge_threshold * 1.5)
    : data.surge_threshold * 2

  return (
    <div className="space-y-4">
      {/* Summary Cards */}
      <div className={`grid grid-cols-2 ${isFutures ? 'md:grid-cols-3' : 'md:grid-cols-4'} gap-3`}>
        <Card label={v2 ? 'V2 레짐' : '시장 상태'}>
          <span className={`text-lg font-bold ${marketInfo.color}`}>{marketInfo.label}</span>
          {showAgentSecondary && (
            <div className="text-[10px] text-gray-500 mt-0.5">
              에이전트: {MARKET_STATE_LABELS[agentState]?.label ?? agentState}
            </div>
          )}
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
        <Card label={isFutures ? '마지막 평가' : '마지막 스캔'}>
          <span className="text-sm font-medium text-gray-300">
            {data.last_scan_time
              ? formatTs(data.last_scan_time, 'HH:mm:ss', '대기 중')
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

      {/* Surge Scan Status (futures) */}
      {isFutures && surgeData && <SurgeScanPanel data={surgeData} />}

      {/* Rotation Enabled Status — 현물만 */}
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

      {/* Surge Score Bar Chart — 현물만 */}
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


function SurgeScanPanel({ data }: { data: SurgeScanStatus }) {
  const stripQuote = (sym: string) => sym.replace('/USDT', '')
  const topScores = data.scores.filter((s) => s.score > 0).slice(0, 15)
  const maxScore = topScores.length > 0 ? Math.max(...topScores.map((s) => s.score), 0.5) : 1

  return (
    <div className="bg-gray-800 rounded-lg p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-cyan-400">
          서지 스캔 ({data.scan_symbols_count}종)
        </h3>
        <div className="flex items-center gap-3 text-xs text-gray-500">
          <span>오늘 {data.daily_trades}/{data.daily_limit}거래</span>
          {data.paused && <span className="text-yellow-400 font-medium">일시정지</span>}
          <span>{data.leverage}x</span>
          <span>·</span>
          <span>{data.scan_interval_sec}초 주기</span>
          {data.last_scan_time && (
            <span className="text-gray-600">{formatTs(data.last_scan_time, 'HH:mm:ss')}</span>
          )}
        </div>
      </div>

      {/* Open positions */}
      {data.scores.some((s) => s.has_position) && (
        <div className="space-y-1">
          <div className="text-xs text-gray-500">보유 포지션</div>
          <div className="flex flex-wrap gap-2">
            {data.scores.filter((s) => s.has_position).map((s) => {
              const pnlColor = (s.pnl_pct ?? 0) >= 0 ? 'text-buy' : 'text-sell'
              return (
                <div key={s.symbol} className="flex items-center gap-1.5 px-2.5 py-1 bg-cyan-900/30 border border-cyan-700/50 rounded-lg">
                  <span className="text-sm text-cyan-300 font-medium">{stripQuote(s.symbol)}</span>
                  <span className={`text-xs font-medium ${s.direction === 'short' ? 'text-sell' : 'text-buy'}`}>
                    {s.direction === 'short' ? 'S' : 'L'}
                  </span>
                  <span className={`text-xs font-mono ${pnlColor}`}>
                    {(s.pnl_pct ?? 0) >= 0 ? '+' : ''}{(s.pnl_pct ?? 0).toFixed(1)}%
                  </span>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* Surge score chart */}
      {topScores.length > 0 ? (
        <div className="space-y-1">
          <div className="text-xs text-gray-500">서지 점수 상위</div>
          <div className="space-y-1">
            {topScores.map((item) => {
              const pct = Math.min((item.score / maxScore) * 100, 100)
              const isHot = item.score >= 0.40
              return (
                <div key={item.symbol} className="flex items-center gap-2">
                  <span className="w-20 text-xs text-gray-400 text-right shrink-0 font-mono">
                    {stripQuote(item.symbol)}
                  </span>
                  <div className="flex-1 h-4 bg-gray-700 rounded overflow-hidden relative">
                    <div
                      className={`h-full rounded transition-all duration-500 ${
                        item.has_position ? 'bg-cyan-500' : isHot ? 'bg-orange-500' : 'bg-gray-500'
                      }`}
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                  <span className={`w-10 text-xs text-right shrink-0 font-mono ${
                    isHot ? 'text-orange-400 font-bold' : 'text-gray-500'
                  }`}>
                    {item.score.toFixed(2)}
                  </span>
                  <span className="w-14 text-[10px] text-gray-500 text-right shrink-0">
                    {item.price_chg >= 0 ? '+' : ''}{item.price_chg.toFixed(1)}%
                  </span>
                  <span className="w-10 text-[10px] text-gray-500 text-right shrink-0">
                    R{item.rsi.toFixed(0)}
                  </span>
                </div>
              )
            })}
          </div>
          <div className="flex items-center gap-3 mt-2 text-[10px] text-gray-600 flex-wrap">
            <span className="flex items-center gap-1">
              <span className="w-2.5 h-2.5 bg-cyan-500 rounded-sm inline-block" /> 보유중
            </span>
            <span className="flex items-center gap-1">
              <span className="w-2.5 h-2.5 bg-orange-500 rounded-sm inline-block" /> 진입 가능
            </span>
            <span className="flex items-center gap-1">
              <span className="w-2.5 h-2.5 bg-gray-500 rounded-sm inline-block" /> 대기
            </span>
          </div>
        </div>
      ) : (
        <p className="text-gray-500 text-sm">스캔 데이터 수집 중...</p>
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
