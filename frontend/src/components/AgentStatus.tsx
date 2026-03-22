import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { getMarketAnalysis, getRiskAlerts, getTradeReview, triggerTradeReview, getPerformanceAnalytics, triggerPerformanceAnalysis, getStrategyAdvice, triggerStrategyAdvice } from '../api/client'
import type { RiskAlert, ExchangeName } from '../types'
import { fmtPrice } from '../utils/format'

interface StrategyStats {
  trades: number
  win_rate: number
  total_pnl: number
}

interface OpenPosition {
  symbol: string
  invested: number
  unrealized_pnl: number
  unrealized_pnl_pct?: number
}

/** ISO 8601 UTC 타임스탬프를 KST 로컬 표시로 변환 (MM/DD HH:mm) */
function fmtKst(isoStr?: string): string {
  if (!isoStr) return ''
  try {
    const d = new Date(isoStr.endsWith('Z') ? isoStr : isoStr + 'Z')
    if (Number.isNaN(d.getTime())) return ''
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone: 'Asia/Seoul', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
    }).formatToParts(d)
    const get = (t: string) => parts.find((p) => p.type === t)?.value ?? ''
    return `${get('month')}/${get('day')} ${get('hour')}:${get('minute')}`
  } catch {
    return ''
  }
}

/** 경과 시간을 한국어로 표시 (e.g. "2시간 전", "3일 전") */
function fmtElapsed(isoStr?: string): string {
  if (!isoStr) return ''
  try {
    const d = new Date(isoStr.endsWith('Z') ? isoStr : isoStr + 'Z')
    if (Number.isNaN(d.getTime())) return ''
    const diffMs = Date.now() - d.getTime()
    if (diffMs < 0) return '방금'
    const diffMin = Math.floor(diffMs / 60_000)
    if (diffMin < 1) return '방금'
    if (diffMin < 60) return `${diffMin}분 전`
    const diffHour = Math.floor(diffMin / 60)
    if (diffHour < 24) return `${diffHour}시간 전`
    const diffDay = Math.floor(diffHour / 24)
    return `${diffDay}일 전`
  } catch {
    return ''
  }
}

/** **bold** 및 `code` 마크다운을 React 엘리먼트로 변환 */
function renderMd(text: string) {
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`)/)
  return parts.map((part, i) => {
    if (part.startsWith('**') && part.endsWith('**'))
      return <strong key={i} className="text-white font-semibold">{part.slice(2, -2)}</strong>
    if (part.startsWith('`') && part.endsWith('`'))
      return <code key={i} className="text-pink-400 bg-gray-800 px-1 rounded text-[10px]">{part.slice(1, -1)}</code>
    return <span key={i}>{part}</span>
  })
}

const STATE_COLORS: Record<string, string> = {
  strong_uptrend: 'bg-green-500',
  uptrend: 'bg-emerald-400',
  sideways: 'bg-yellow-400',
  downtrend: 'bg-orange-400',
  crash: 'bg-red-500',
}

const STATE_KR: Record<string, string> = {
  strong_uptrend: '강한 상승장',
  uptrend: '상승장',
  sideways: '횡보장',
  downtrend: '하락장',
  crash: '폭락',
}

const VOLATILITY_KR: Record<string, string> = {
  low: '낮음',
  medium: '보통',
  high: '높음',
  extreme: '극심',
}

function AlertBadge({ alert }: { alert: RiskAlert }) {
  const cls =
    alert.level === 'critical'
      ? 'bg-red-900 border-red-700 text-red-200'
      : alert.level === 'warning'
      ? 'bg-yellow-900 border-yellow-700 text-yellow-200'
      : 'bg-gray-700 border-gray-600 text-gray-300'

  return (
    <div className={`border rounded-lg p-3 text-xs ${cls}`}>
      <div className="flex items-center gap-2 mb-1">
        <span className="font-bold uppercase">{alert.level}</span>
        {alert.affected_coins.length > 0 && (
          <span className="text-xs opacity-70">[{alert.affected_coins.join(', ')}]</span>
        )}
      </div>
      <div>{alert.message}</div>
    </div>
  )
}

export function AgentStatus({ exchange = 'bithumb' }: { exchange?: ExchangeName }) {
  const isUsdt = exchange.startsWith('binance')
  const fmt = (n: number) => fmtPrice(n, isUsdt)
  const qc = useQueryClient()

  const { data: analysis } = useQuery({
    queryKey: ['agents', 'market-analysis', exchange],
    queryFn: () => getMarketAnalysis(exchange),
    refetchInterval: 60_000,
    staleTime: 30_000,
  })

  const { data: alerts } = useQuery({
    queryKey: ['agents', 'risk-alerts', exchange],
    queryFn: () => getRiskAlerts(exchange),
    refetchInterval: 30_000,
    staleTime: 15_000,
  })

  const { data: review } = useQuery({
    queryKey: ['agents', 'trade-review', exchange],
    queryFn: () => getTradeReview(exchange),
    refetchInterval: 300_000,
    staleTime: 60_000,
  })

  const reviewMut = useMutation({
    mutationFn: () => triggerTradeReview(exchange),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['agents', 'trade-review'] }),
  })

  const { data: perfData } = useQuery({
    queryKey: ['agents', 'performance', exchange],
    queryFn: () => getPerformanceAnalytics(exchange),
    refetchInterval: 300_000,
    staleTime: 120_000,
  })

  const perfMut = useMutation({
    mutationFn: () => triggerPerformanceAnalysis(exchange),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['agents', 'performance'] }),
  })

  const { data: adviceData } = useQuery({
    queryKey: ['agents', 'strategy-advice', exchange],
    queryFn: () => getStrategyAdvice(exchange),
    refetchInterval: 600_000,
    staleTime: 300_000,
  })

  const adviceMut = useMutation({
    mutationFn: () => triggerStrategyAdvice(exchange),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['agents', 'strategy-advice'] }),
  })

  const dotColor = analysis ? STATE_COLORS[analysis.state] ?? 'bg-gray-500' : 'bg-gray-600'
  const criticalAlerts = (alerts ?? []).filter((a) => a.level === 'critical')
  const warningAlerts = (alerts ?? []).filter((a) => a.level === 'warning')

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {/* Market Analysis Agent */}
        <div className="bg-gray-800 rounded-xl p-4">
          <div className="flex items-center gap-2 mb-3">
            <div className={`w-2 h-2 rounded-full ${dotColor}`} />
            <h3 className="text-white font-semibold text-sm">시장 분석 에이전트</h3>
          </div>

          {analysis && analysis.confidence != null ? (
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <span className="text-gray-400 text-sm">시장 상태</span>
                <span className={`font-bold text-sm ${dotColor.replace('bg-', 'text-')}`}>
                  {STATE_KR[analysis.state] ?? analysis.state}
                </span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-gray-400 text-sm">신뢰도</span>
                <div className="flex items-center gap-2">
                  <div className="w-24 bg-gray-700 rounded-full h-1.5">
                    <div
                      className="bg-blue-500 h-1.5 rounded-full"
                      style={{ width: `${(analysis.confidence ?? 0) * 100}%` }}
                    />
                  </div>
                  <span className="text-white text-xs">{((analysis.confidence ?? 0) * 100).toFixed(0)}%</span>
                </div>
              </div>
              {analysis.volatility_level && (
                <div className="flex items-center justify-between">
                  <span className="text-gray-400 text-sm">변동성</span>
                  <span className="text-white text-sm">{VOLATILITY_KR[analysis.volatility_level] ?? analysis.volatility_level}</span>
                </div>
              )}

              {/* Strategy weights */}
              {analysis.recommended_weights && Object.keys(analysis.recommended_weights).length > 0 && (
                <div>
                  <div className="text-gray-400 text-xs mb-2">전략 가중치</div>
                  <div className="space-y-1">
                    {Object.entries(analysis.recommended_weights)
                      .sort(([, a], [, b]) => b - a)
                      .map(([name, weight]) => (
                        <div key={name} className="flex items-center gap-2">
                          <span className="text-gray-400 text-xs w-16 sm:w-24 truncate">{name.replace(/_/g, ' ')}</span>
                          <div className="flex-1 bg-gray-700 rounded-full h-1">
                            <div
                              className="bg-blue-500 h-1 rounded-full transition-all"
                              style={{ width: `${weight * 100}%` }}
                            />
                          </div>
                          <span className="text-gray-400 text-xs w-8 text-right">{(weight * 100).toFixed(0)}%</span>
                        </div>
                      ))}
                  </div>
                </div>
              )}

              {/* Reasoning */}
              {analysis.reasoning && (
                <div className="bg-gray-900 rounded-lg p-2">
                  <div className="text-gray-500 text-xs mb-1">분석 근거</div>
                  <div className="text-gray-300 text-xs leading-relaxed">{analysis.reasoning}</div>
                </div>
              )}
            </div>
          ) : (
            <div className="text-gray-500 text-sm">분석 데이터 없음</div>
          )}
        </div>

        {/* Risk Management Agent */}
        <div className="bg-gray-800 rounded-xl p-4">
          <div className="flex items-center gap-2 mb-3">
            <div className={`w-2 h-2 rounded-full ${criticalAlerts.length > 0 ? 'bg-red-500' : warningAlerts.length > 0 ? 'bg-yellow-400' : 'bg-green-500'}`} />
            <h3 className="text-white font-semibold text-sm">리스크 관리 에이전트</h3>
            {(alerts ?? []).length > 0 && (
              <span className="ml-auto bg-red-800 text-red-200 text-xs px-2 py-0.5 rounded-full">
                {alerts!.length}개 경고
              </span>
            )}
          </div>

          {!alerts || alerts.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-24 text-green-400">
              <span className="text-2xl">✓</span>
              <span className="text-sm mt-1">리스크 이상 없음</span>
            </div>
          ) : (
            <div className="space-y-2">
              {alerts.map((alert, i) => (
                <AlertBadge key={i} alert={alert} />
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Trade Review Agent */}
      <div className="bg-gray-800 rounded-xl p-4">
        <div className="flex items-center gap-2 mb-3 flex-wrap">
          <div className={`w-2 h-2 rounded-full shrink-0 ${review?.total_trades > 0 ? 'bg-blue-500' : 'bg-gray-500'}`} />
          <h3 className="text-white font-semibold text-sm">매매 회고</h3>
          {review?.analyzed_at && (
            <span className="text-gray-400 text-xs" title={review.analyzed_at}>
              {fmtKst(review.analyzed_at)} 기준 · {review.period_hours ?? 24}시간 · {fmtElapsed(review.analyzed_at)}
            </span>
          )}
          <button
            onClick={() => reviewMut.mutate()}
            disabled={reviewMut.isPending}
            className="ml-auto text-xs bg-gray-700 hover:bg-gray-600 text-gray-300 px-3 py-1.5 rounded transition-colors disabled:opacity-50 active:bg-gray-500"
          >
            {reviewMut.isPending ? '분석 중...' : '수동 실행'}
          </button>
        </div>

        {review && review.total_trades > 0 ? (
          <div className="space-y-3">
            {/* 핵심 지표 */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <div className="bg-gray-900 rounded-lg p-2 text-center">
                <div className="text-gray-500 text-xs">거래 수</div>
                <div className="text-white font-bold">{review.total_trades}</div>
                <div className="text-gray-500 text-xs">매수 {review.buy_count} / 매도 {review.sell_count}</div>
              </div>
              <div className="bg-gray-900 rounded-lg p-2 text-center">
                <div className="text-gray-500 text-xs">승률</div>
                <div className={`font-bold ${review.win_rate >= 0.5 ? 'text-green-400' : 'text-red-400'}`}>
                  {(review.win_rate * 100).toFixed(1)}%
                </div>
                <div className="text-gray-500 text-xs">{review.win_count}승 {review.loss_count}패</div>
              </div>
              <div className="bg-gray-900 rounded-lg p-2 text-center">
                <div className="text-gray-500 text-xs">실현 손익</div>
                <div className={`font-bold ${review.total_realized_pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                  {review.total_realized_pnl >= 0 ? '+' : ''}{fmt(review.total_realized_pnl)}
                </div>
                <div className="text-gray-500 text-xs">건당 {fmt(review.avg_pnl_per_trade)}</div>
              </div>
              <div className="bg-gray-900 rounded-lg p-2 text-center">
                <div className="text-gray-500 text-xs">Profit Factor</div>
                <div className={`font-bold ${review.profit_factor >= 1.5 ? 'text-green-400' : review.profit_factor >= 1.0 ? 'text-yellow-400' : 'text-red-400'}`}>
                  {review.profit_factor.toFixed(2)}x
                </div>
                <div className="text-gray-500 text-xs">최대 +{fmt(review.largest_win)} / {fmt(review.largest_loss)}</div>
              </div>
            </div>

            {/* 전략별 성과 */}
            {review.by_strategy && Object.keys(review.by_strategy).length > 0 && (
              <div>
                <div className="text-gray-400 text-xs mb-2">전략별 성과</div>
                <div className="space-y-1">
                  {Object.entries(review.by_strategy)
                    .sort(([, a], [, b]) => (b as StrategyStats).total_pnl - (a as StrategyStats).total_pnl)
                    .map(([name, _stats]) => {
                      const stats = _stats as StrategyStats
                      return (
                      <div key={name} className="flex items-center gap-2 text-xs flex-wrap">
                        <span className="text-gray-400 w-20 sm:w-28 truncate">{name.replace(/_/g, ' ')}</span>
                        <span className="text-gray-300">{stats.trades}건</span>
                        <span className={`${stats.win_rate >= 0.5 ? 'text-green-400' : 'text-red-400'}`}>
                          {(stats.win_rate * 100).toFixed(0)}%
                        </span>
                        <span className={`ml-auto ${stats.total_pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                          {stats.total_pnl >= 0 ? '+' : ''}{fmt(stats.total_pnl)}
                        </span>
                      </div>
                    )})}
                </div>
              </div>
            )}

            {/* 보유 포지션 */}
            {review.open_positions && review.open_positions.length > 0 && (
              <div>
                <div className="text-gray-400 text-xs mb-2">보유 포지션</div>
                <div className="space-y-1">
                  {review.open_positions.map((pos: OpenPosition) => (
                    <div key={pos.symbol} className="flex items-center gap-2 text-xs bg-gray-900 rounded p-2">
                      <span className="text-white font-medium w-20">{pos.symbol.replace(/\/(KRW|USDT)/, '')}</span>
                      <span className="text-gray-400">투자 {fmt(pos.invested)}</span>
                      <span className={`ml-auto ${pos.unrealized_pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                        {pos.unrealized_pnl >= 0 ? '+' : ''}{fmt(pos.unrealized_pnl)} ({pos.unrealized_pnl_pct?.toFixed(2) ?? 0}%)
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* 인사이트 + 추천 */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              <div className="bg-gray-900 rounded-lg p-2">
                <div className="text-blue-400 text-xs font-medium mb-1">인사이트</div>
                <ul className="space-y-1">
                  {review.insights?.map((insight: string, i: number) => (
                    <li key={i} className="text-gray-300 text-xs leading-relaxed">• {renderMd(insight)}</li>
                  ))}
                </ul>
              </div>
              <div className="bg-gray-900 rounded-lg p-2">
                <div className="text-yellow-400 text-xs font-medium mb-1">추천</div>
                <ul className="space-y-1">
                  {review.recommendations?.map((rec: string, i: number) => (
                    <li key={i} className="text-gray-300 text-xs leading-relaxed">• {renderMd(rec)}</li>
                  ))}
                </ul>
              </div>
            </div>
          </div>
        ) : (
          <div className="text-center py-6">
            <div className="text-gray-500 text-sm">
              {review?.insights?.[0] ?? '매매 회고 데이터 없음'}
            </div>
            <div className="text-gray-600 text-xs mt-1">매시간 자동 분석 실행 / 위 버튼으로 수동 실행 가능</div>
          </div>
        )}
      </div>

      {/* Performance Analytics */}
      <div className="bg-gray-800 rounded-xl p-4">
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <h3 className="text-white text-sm font-semibold">성과 분석</h3>
            {perfData?.generated_at && !perfData.status && (
              <span className="text-gray-400 text-xs" title={perfData.generated_at}>
                {fmtKst(perfData.generated_at)} 기준 · {fmtElapsed(perfData.generated_at)}
              </span>
            )}
          </div>
          <button
            onClick={() => perfMut.mutate()}
            disabled={perfMut.isPending}
            className="text-xs px-2 py-1 rounded bg-gray-700 text-gray-300 hover:bg-gray-600 disabled:opacity-50"
          >
            {perfMut.isPending ? '분석 중...' : '수동 실행'}
          </button>
        </div>

        {perfData && !perfData.status ? (
          <div className="space-y-3">
            {/* 롤링 윈도우 */}
            <div className="grid grid-cols-3 gap-2">
              {['7d', '14d', '30d'].map((key) => {
                const w = perfData.windows?.[key]
                if (!w || w.total_trades === 0) return (
                  <div key={key} className="bg-gray-900 rounded-lg p-2 text-center">
                    <div className="text-gray-500 text-xs">{key}</div>
                    <div className="text-gray-600 text-xs">데이터 없음</div>
                  </div>
                )
                const pfColor = w.profit_factor >= 1.5 ? 'text-green-400' : w.profit_factor >= 1.0 ? 'text-yellow-400' : 'text-red-400'
                return (
                  <div key={key} className="bg-gray-900 rounded-lg p-2">
                    <div className="text-gray-400 text-xs font-medium mb-1">{key}</div>
                    <div className="text-white text-sm font-bold">{w.total_trades}건</div>
                    <div className={`text-xs ${w.win_rate >= 0.5 ? 'text-green-400' : 'text-red-400'}`}>
                      승률 {(w.win_rate * 100).toFixed(0)}%
                    </div>
                    <div className={`text-xs ${pfColor}`}>PF {w.profit_factor.toFixed(2)}</div>
                    <div className={`text-xs ${w.total_pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                      {fmt(w.total_pnl)}
                    </div>
                  </div>
                )
              })}
            </div>

            {/* 성과 저하 경고 */}
            {perfData.degradation_alerts?.length > 0 && (
              <div className="space-y-1">
                {perfData.degradation_alerts.map((alert: string, i: number) => (
                  <div key={i} className="bg-red-900/30 border border-red-800 rounded px-2 py-1 text-red-300 text-xs">
                    {alert}
                  </div>
                ))}
              </div>
            )}

            {/* 인사이트 + 추천 */}
            {(perfData.insights?.length > 0 || perfData.recommendations?.length > 0) && (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <div className="bg-gray-900 rounded-lg p-2">
                  <div className="text-blue-400 text-xs font-medium mb-1">인사이트</div>
                  <ul className="space-y-1">
                    {perfData.insights?.map((s: string, i: number) => (
                      <li key={i} className="text-gray-300 text-xs leading-relaxed">• {renderMd(s)}</li>
                    ))}
                  </ul>
                </div>
                <div className="bg-gray-900 rounded-lg p-2">
                  <div className="text-yellow-400 text-xs font-medium mb-1">추천</div>
                  <ul className="space-y-1">
                    {perfData.recommendations?.map((s: string, i: number) => (
                      <li key={i} className="text-gray-300 text-xs leading-relaxed">• {renderMd(s)}</li>
                    ))}
                  </ul>
                </div>
              </div>
            )}
          </div>
        ) : (
          <div className="text-center py-4 text-gray-500 text-xs">
            매일 21:30 자동 분석 / 수동 실행 가능
          </div>
        )}
      </div>

      {/* Strategy Advisor */}
      <div className="bg-gray-800 rounded-xl p-4">
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <h3 className="text-white text-sm font-semibold">전략 어드바이저</h3>
            {adviceData?.generated_at && !adviceData.status && (
              <span className="text-gray-400 text-xs" title={adviceData.generated_at}>
                {fmtKst(adviceData.generated_at)} 기준 · {fmtElapsed(adviceData.generated_at)}
              </span>
            )}
          </div>
          <button
            onClick={() => adviceMut.mutate()}
            disabled={adviceMut.isPending}
            className="text-xs px-2 py-1 rounded bg-gray-700 text-gray-300 hover:bg-gray-600 disabled:opacity-50"
          >
            {adviceMut.isPending ? '분석 중...' : '수동 실행'}
          </button>
        </div>

        {adviceData && !adviceData.status ? (
          <div className="space-y-3">
            {/* 종합 분석 */}
            {adviceData.analysis_summary && (
              <div className="bg-gray-900 rounded-lg p-2">
                <div className="text-purple-400 text-xs font-medium mb-1">종합 분석</div>
                <p className="text-gray-300 text-xs leading-relaxed">{renderMd(adviceData.analysis_summary)}</p>
              </div>
            )}

            {/* 청산 사유 분석 */}
            {adviceData.exit_analysis && Object.keys(adviceData.exit_analysis).length > 0 && (
              <div className="bg-gray-900 rounded-lg p-2">
                <div className="text-cyan-400 text-xs font-medium mb-1">청산 사유 (90일)</div>
                <div className="grid grid-cols-2 sm:grid-cols-3 gap-1">
                  {Object.entries(adviceData.exit_analysis).map(([type, stats]: [string, any]) => {
                    const label = { stop_loss: '손절', take_profit: '익절', trailing: '트레일링', signal: '시그널', other: '기타' }[type] || type
                    return (
                      <div key={type} className="text-xs text-gray-400">
                        {label}: <span className="text-white">{stats.count}회</span>{' '}
                        <span className={stats.win_rate >= 0.5 ? 'text-green-400' : 'text-red-400'}>
                          ({(stats.win_rate * 100).toFixed(0)}%)
                        </span>
                      </div>
                    )
                  })}
                </div>
              </div>
            )}

            {/* 파라미터 민감도 */}
            {adviceData.param_sensitivities?.length > 0 && (
              <div className="bg-gray-900 rounded-lg p-2">
                <div className="text-orange-400 text-xs font-medium mb-1">파라미터 분석</div>
                {adviceData.param_sensitivities.map((ps: any, i: number) => (
                  <div key={i} className="text-gray-300 text-xs mb-1">• {ps.improvement}</div>
                ))}
              </div>
            )}

            {/* 제안 */}
            {adviceData.suggestions?.length > 0 && (
              <div className="bg-gray-900 rounded-lg p-2">
                <div className="text-yellow-400 text-xs font-medium mb-1">제안</div>
                <ul className="space-y-1">
                  {adviceData.suggestions.map((s: string, i: number) => (
                    <li key={i} className="text-gray-300 text-xs leading-relaxed">• {renderMd(s)}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        ) : (
          <div className="text-center py-4 text-gray-500 text-xs">
            매주 일요일 22:00 자동 분석 / 수동 실행 가능
          </div>
        )}
      </div>
    </div>
  )
}
