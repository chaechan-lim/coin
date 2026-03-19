import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { getTrades } from '../api/client'
import { formatTs } from '../utils/date'
import { fmtPrice } from '../utils/format'
import type { Order, ExchangeName } from '../types'

const STRATEGY_COLORS: Record<string, string> = {
  // 활성 전략 (현물 4전략 — V2 하이브리드 엔진 공용)
  bnf_deviation: 'bg-amber-800 text-amber-200',
  cis_momentum: 'bg-emerald-800 text-emerald-200',
  larry_williams: 'bg-rose-800 text-rose-200',
  donchian_channel: 'bg-sky-800 text-sky-200',
  // V2 SpotEvaluator / Tier2Scanner
  spot_eval: 'bg-lime-800 text-lime-200',
  tier2_surge: 'bg-fuchsia-800 text-fuchsia-200',
  // 운영 전략 (SL/TP/리스크)
  risk_management: 'bg-yellow-800 text-yellow-200',
  stop_loss: 'bg-red-900 text-red-200',
  take_profit: 'bg-green-900 text-green-200',
  trailing_stop: 'bg-orange-800 text-orange-200',
  forced_liquidation: 'bg-red-800 text-red-200',
  time_expiry: 'bg-gray-600 text-gray-200',
  position_sync: 'bg-gray-700 text-gray-300',
}

function StrategyBadge({ name }: { name: string }) {
  const cls = STRATEGY_COLORS[name] ?? 'bg-gray-700 text-gray-300'
  return (
    <span className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${cls}`}>
      {name.replace(/_/g, ' ')}
    </span>
  )
}

function PnlBadge({ pnl_pct }: { pnl_pct: number }) {
  const isProfit = pnl_pct >= 0
  const color = isProfit ? 'text-green-400' : 'text-red-400'
  const sign = isProfit ? '+' : ''
  return (
    <span className={`text-xs font-semibold ${color}`}>
      {sign}{pnl_pct.toFixed(2)}%
    </span>
  )
}

function OrderDetail({ order, isUsdt = false }: { order: Order; isUsdt?: boolean }) {
  const [expanded, setExpanded] = useState(false)
  const side = order.side === 'buy'
  const price = order.executed_price ?? order.requested_price ?? 0
  const isFutures = !!order.direction
  const dirLabel = order.direction === 'short' ? 'SHORT' : 'LONG'
  const dirColor = order.direction === 'short' ? 'text-sell' : 'text-buy'
  // 청산 주문: 롱→sell, 숏→buy(close). realized_pnl이 있으면 청산 주문
  const hasPnl = order.realized_pnl_pct != null

  return (
    <div className="border-b border-gray-700/50">
      <button
        className="w-full text-left px-4 py-3 hover:bg-gray-700/30 transition-colors"
        onClick={() => setExpanded((v) => !v)}
      >
        {/* Desktop layout */}
        <div className="hidden sm:flex items-center justify-between">
          <div className="flex items-center gap-3">
            {isFutures ? (
              <span className={`text-sm font-bold ${dirColor}`}>
                {order.direction === 'short' ? '▼ SHORT' : '▲ LONG'}
              </span>
            ) : (
              <span className={`text-sm font-bold ${side ? 'text-buy' : 'text-sell'}`}>
                {side ? '▲ 매수' : '▼ 매도'}
              </span>
            )}
            <span className="text-white font-medium">{order.symbol}</span>
            {isFutures && order.leverage && order.leverage > 1 && (
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-yellow-500/20 text-yellow-400 font-semibold">{order.leverage}x</span>
            )}
            <StrategyBadge name={order.strategy_name} />
            {hasPnl && <PnlBadge pnl_pct={order.realized_pnl_pct!} />}
            {order.is_paper && (
              <span className="text-xs text-gray-500 border border-gray-600 px-1 rounded">페이퍼</span>
            )}
          </div>
          <div className="flex items-center gap-4 text-sm">
            {isFutures && order.margin_used != null && (
              <span className="text-gray-400 text-xs">{order.margin_used.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 4 })} USDT</span>
            )}
            <span className="text-gray-300">{fmtPrice(price, isUsdt)}</span>
            <span className="text-gray-500 text-xs">
              {formatTs(order.created_at, 'MM/dd HH:mm')}
            </span>
            <span className="text-gray-600">{expanded ? '▲' : '▼'}</span>
          </div>
        </div>
        {/* Mobile layout - stacked */}
        <div className="sm:hidden space-y-1.5">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              {isFutures ? (
                <span className={`text-sm font-bold ${dirColor}`}>
                  {order.direction === 'short' ? '▼ SHORT' : '▲ LONG'}
                </span>
              ) : (
                <span className={`text-sm font-bold ${side ? 'text-buy' : 'text-sell'}`}>
                  {side ? '▲ 매수' : '▼ 매도'}
                </span>
              )}
              <span className="text-white font-medium text-sm">{order.symbol.replace(/\/(KRW|USDT)/, '')}</span>
              {isFutures && order.leverage && order.leverage > 1 && (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-yellow-500/20 text-yellow-400 font-semibold">{order.leverage}x</span>
              )}
            </div>
            <span className="text-gray-500 text-xs">
              {formatTs(order.created_at, 'MM/dd HH:mm')}
            </span>
          </div>
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <StrategyBadge name={order.strategy_name} />
              {hasPnl && <PnlBadge pnl_pct={order.realized_pnl_pct!} />}
              {order.is_paper && (
                <span className="text-xs text-gray-500 border border-gray-600 px-1 rounded">P</span>
              )}
            </div>
            <span className="text-gray-300 text-sm">{fmtPrice(price, isUsdt)}</span>
          </div>
        </div>
      </button>

      {expanded && (
        <div className="px-4 pb-4 bg-gray-800/50 space-y-2 text-sm">
          {/* 전략 사유 (회고 핵심 정보) */}
          {order.signal_reason && (
            <div className="bg-gray-900 rounded-lg p-3">
              <div className="text-gray-400 text-xs mb-1">전략 사유</div>
              <div className="text-gray-200 leading-relaxed">{order.signal_reason}</div>
            </div>
          )}

          {/* 손익 정보 (매도 주문) */}
          {hasPnl && (
            <div className={`rounded-lg p-3 ${order.realized_pnl_pct! >= 0 ? 'bg-green-900/30 border border-green-800/50' : 'bg-red-900/30 border border-red-800/50'}`}>
              <div className="flex items-center justify-between">
                <div>
                  <span className="text-gray-400 text-xs">진입가</span>
                  <div className="text-white font-medium">{fmtPrice(order.entry_price ?? 0, isUsdt)}</div>
                </div>
                <div>
                  <span className="text-gray-400 text-xs">청산가</span>
                  <div className="text-white font-medium">{fmtPrice(price, isUsdt)}</div>
                </div>
                <div className="text-right">
                  <span className="text-gray-400 text-xs">수익률</span>
                  <div className={`font-bold text-lg ${order.realized_pnl_pct! >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                    {order.realized_pnl_pct! >= 0 ? '+' : ''}{order.realized_pnl_pct!.toFixed(2)}%
                  </div>
                </div>
                {order.realized_pnl != null && (
                  <div className="text-right">
                    <span className="text-gray-400 text-xs">손익</span>
                    <div className={`font-semibold ${order.realized_pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                      {order.realized_pnl >= 0 ? '+' : ''}{fmtPrice(order.realized_pnl, isUsdt)}
                    </div>
                  </div>
                )}
              </div>
            </div>
          )}

          {/* 메타 정보 */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">
            {isFutures && (
              <>
                <div>
                  <span className="text-gray-500">방향</span>
                  <div className={`font-medium ${dirColor}`}>{dirLabel} {order.leverage}x</div>
                </div>
                <div>
                  <span className="text-gray-500">사용 마진</span>
                  <div className="text-white font-medium">
                    {order.margin_used != null ? `${order.margin_used.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 4 })} USDT` : '-'}
                  </div>
                </div>
              </>
            )}
            <div>
              <span className="text-gray-500">신뢰도</span>
              <div className="text-white font-medium">
                {order.signal_confidence != null ? `${(order.signal_confidence * 100).toFixed(0)}%` : '-'}
              </div>
            </div>
            <div>
              <span className="text-gray-500">결합 점수</span>
              <div className="text-white font-medium">
                {order.combined_score != null ? `${(order.combined_score * 100).toFixed(0)}%` : '-'}
              </div>
            </div>
            <div>
              <span className="text-gray-500">요청 수량</span>
              <div className="text-white font-medium">{order.requested_quantity.toFixed(6)}</div>
            </div>
            <div>
              <span className="text-gray-500">수수료</span>
              <div className="text-white font-medium">{fmtPrice(order.fee, isUsdt)}</div>
            </div>
          </div>

          {/* 기여 전략 목록 */}
          {order.contributing_strategies && order.contributing_strategies.length > 1 && (
            <div>
              <div className="text-gray-400 text-xs mb-1">기여 전략</div>
              <div className="space-y-1">
                {order.contributing_strategies.map((cs, i) => (
                  <div key={i} className="flex items-start gap-2 text-xs text-gray-300">
                    <StrategyBadge name={cs.name} />
                    <span className={cs.signal === 'BUY' ? 'text-buy' : cs.signal === 'SELL' ? 'text-sell' : 'text-gray-500'}>
                      {cs.signal}
                    </span>
                    <span className="text-gray-500">({(cs.confidence * 100).toFixed(0)}%)</span>
                    <span className="text-gray-400 flex-1">{cs.reason}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export function TradeHistory({ exchange = 'bithumb' }: { exchange?: ExchangeName }) {
  const isUsdt = exchange.startsWith('binance')
  const [page, setPage] = useState(1)
  const [symbol, setSymbol] = useState('')
  const [strategy, setStrategy] = useState('')
  const [side, setSide] = useState('')

  const { data, isLoading } = useQuery({
    queryKey: ['trades', page, symbol, strategy, side, exchange],
    queryFn: () =>
      getTrades({
        page,
        size: 20,
        symbol: symbol || undefined,
        strategy: strategy || undefined,
        side: side || undefined,
        exchange,
      }),
    staleTime: 15_000,
  })

  return (
    <div className="bg-gray-800 rounded-xl overflow-hidden">
      <div className="px-4 py-3 border-b border-gray-700">
        <div className="flex items-center gap-3 flex-wrap">
          <h3 className="text-white font-semibold mr-2">거래 이력</h3>
          <div className="flex items-center gap-2 flex-wrap flex-1 min-w-0">
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
              {/* 활성 전략 (V2 하이브리드 엔진) */}
              <option value="bnf_deviation">BNF 이격도</option>
              <option value="cis_momentum">CIS 모멘텀</option>
              <option value="larry_williams">래리 윌리엄스</option>
              <option value="donchian_channel">돈치안 채널</option>
              <option value="spot_eval">현물 시그널</option>
              <option value="tier2_surge">서지 스캐너</option>
              {/* 운영 전략 (SL/TP/리스크) */}
              <option value="stop_loss">손절</option>
              <option value="take_profit">익절</option>
              <option value="trailing_stop">트레일링 스탑</option>
              <option value="risk_management">리스크 관리</option>
            </select>
            <select
              className="bg-gray-700 text-white text-xs px-2 py-1.5 rounded border border-gray-600"
              value={side}
              onChange={(e) => { setSide(e.target.value); setPage(1) }}
            >
              <option value="">전체</option>
              <option value="buy">매수</option>
              <option value="sell">매도</option>
            </select>
          </div>
        </div>
      </div>

      {isLoading ? (
        <div className="p-8 text-center text-gray-500">로딩 중...</div>
      ) : !data || data.length === 0 ? (
        <div className="p-8 text-center text-gray-500">거래 내역이 없습니다</div>
      ) : (
        <>
          {data.map((order) => (
            <OrderDetail key={order.id} order={order} isUsdt={isUsdt} />
          ))}
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
              disabled={data.length < 20}
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
