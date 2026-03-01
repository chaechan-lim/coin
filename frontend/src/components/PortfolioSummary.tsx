import { useState } from 'react'
import { usePortfolioSummary } from '../hooks/usePortfolio'
import { CapitalManager } from './CapitalManager'
import type { ExchangeName } from '../types'

function StatCard({
  label,
  value,
  sub,
  color,
  action,
}: {
  label: string
  value: string
  sub?: string
  color?: string
  action?: React.ReactNode
}) {
  return (
    <div className="bg-gray-800 rounded-xl p-4 flex flex-col gap-1">
      <span className="text-gray-400 text-xs">{label}</span>
      <span className={`text-xl font-bold ${color ?? 'text-white'}`}>{value}</span>
      {(sub || action) && (
        <div className="flex items-center gap-1.5">
          {sub && <span className="text-gray-500 text-xs">{sub}</span>}
          {action}
        </div>
      )}
    </div>
  )
}

function fmtKrw(n: number) {
  return n.toLocaleString('ko-KR') + ' ₩'
}

function fmtUsdt(n: number) {
  return n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' USDT'
}

function fmtPct(n: number) {
  const sign = n >= 0 ? '+' : ''
  return `${sign}${n.toFixed(2)}%`
}

export function PortfolioSummary({ exchange = 'bithumb' }: { exchange?: ExchangeName }) {
  const isUsdt = exchange === 'binance_futures'
  const fmt = isUsdt ? fmtUsdt : fmtKrw
  const { data, isLoading } = usePortfolioSummary(exchange)
  const [capitalOpen, setCapitalOpen] = useState(false)

  if (isLoading || !data) {
    return (
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 animate-pulse">
        {Array.from({ length: 8 }).map((_, i) => (
          <div key={i} className="bg-gray-800 rounded-xl p-4 h-20" />
        ))}
      </div>
    )
  }

  const pnlColor = data.total_pnl >= 0 ? 'text-buy' : 'text-sell'
  const drawdownColor = data.drawdown_pct > 5 ? 'text-sell' : data.drawdown_pct > 2 ? 'text-yellow-400' : 'text-gray-300'
  const returnFromInitial = data.initial_balance_krw > 0
    ? ((data.total_value_krw - data.initial_balance_krw) / data.initial_balance_krw) * 100
    : 0
  const returnColor = returnFromInitial >= 0 ? 'text-buy' : 'text-sell'

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <StatCard
          label="총 자산"
          value={fmt(data.total_value_krw)}
          sub={data.initial_balance_krw > 0 ? `원금 ${fmt(data.initial_balance_krw)}` : undefined}
          action={
            <button
              onClick={() => setCapitalOpen(true)}
              className="text-[10px] px-1.5 py-0.5 rounded bg-blue-600/20 text-blue-400 hover:bg-blue-600/30 font-medium"
            >
              입출금
            </button>
          }
        />
        <StatCard
          label="원금 대비 수익"
          value={fmtPct(returnFromInitial)}
          sub={`${returnFromInitial >= 0 ? '+' : ''}${isUsdt
            ? (data.total_value_krw - data.initial_balance_krw).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' USDT'
            : (data.total_value_krw - data.initial_balance_krw).toLocaleString('ko-KR') + ' ₩'}`}
          color={returnColor}
        />
        <StatCard label="현금 잔액" value={fmt(data.cash_balance_krw)} />
        <StatCard
          label="고점 대비"
          value={data.drawdown_pct > 0 ? `-${data.drawdown_pct.toFixed(2)}%` : '0.00%'}
          sub={`고점: ${fmt(data.peak_value)}`}
          color={drawdownColor}
        />
        <StatCard label="실현 손익" value={fmt(data.realized_pnl)} color={data.realized_pnl >= 0 ? 'text-buy' : 'text-sell'} />
        <StatCard label="미실현 손익" value={fmt(data.unrealized_pnl)} color={data.unrealized_pnl >= 0 ? 'text-buy' : 'text-sell'} />
        <StatCard label={isUsdt ? '사용 마진' : '투자 금액'} value={fmt(data.invested_value_krw)} />
        <StatCard
          label="수수료 지출"
          value={fmt(data.total_fees)}
          sub={`${data.trade_count}건 거래`}
          color="text-orange-400"
        />
      </div>

      {data.positions.length > 0 && (
        <div className="bg-gray-800 rounded-xl overflow-hidden">
          <div className="px-4 py-3 border-b border-gray-700 text-sm font-semibold text-gray-300">
            보유 포지션
          </div>
          {/* Desktop table */}
          <table className="w-full text-sm hidden md:table">
            <thead>
              <tr className="text-gray-500 text-xs border-b border-gray-700">
                <th className="px-4 py-2 text-left">코인</th>
                {isUsdt && <th className="px-4 py-2 text-center">방향</th>}
                <th className="px-4 py-2 text-right">수량</th>
                <th className="px-4 py-2 text-right">진입가</th>
                <th className="px-4 py-2 text-right">현재가</th>
                <th className="px-4 py-2 text-right">{isUsdt ? '마진' : '평가금액'}</th>
                {isUsdt && <th className="px-4 py-2 text-right">청산가</th>}
                <th className="px-4 py-2 text-right">손절가</th>
                <th className="px-4 py-2 text-right">익절가</th>
                <th className="px-4 py-2 text-right">미실현 손익</th>
              </tr>
            </thead>
            <tbody>
              {data.positions.map((pos) => {
                const dirLabel = pos.direction === 'short' ? 'SHORT' : 'LONG'
                const dirColor = pos.direction === 'short' ? 'text-sell' : 'text-buy'
                return (
                  <tr key={pos.symbol} className="border-b border-gray-700/50 hover:bg-gray-700/30">
                    <td className="px-4 py-2 font-medium text-white">
                      {pos.symbol}
                      {pos.leverage && pos.leverage > 1 && (
                        <span className="ml-1.5 text-[10px] px-1.5 py-0.5 rounded bg-yellow-500/20 text-yellow-400 font-semibold">{pos.leverage}x</span>
                      )}
                      {pos.trailing_active && (
                        <span className="ml-1.5 text-[10px] px-1.5 py-0.5 rounded bg-purple-500/20 text-purple-400 font-semibold">TRAIL</span>
                      )}
                      {pos.is_surge && (
                        <span className="ml-1.5 text-[10px] px-1.5 py-0.5 rounded bg-cyan-500/20 text-cyan-400 font-semibold">SURGE</span>
                      )}
                    </td>
                    {isUsdt && (
                      <td className={`px-4 py-2 text-center font-semibold text-xs ${dirColor}`}>{dirLabel}</td>
                    )}
                    <td className="px-4 py-2 text-right text-gray-300">{pos.quantity.toFixed(6)}</td>
                    <td className="px-4 py-2 text-right text-gray-300">{isUsdt ? pos.average_buy_price.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : pos.average_buy_price.toLocaleString()}</td>
                    <td className="px-4 py-2 text-right text-gray-300">{isUsdt ? pos.current_price.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : pos.current_price.toLocaleString()}</td>
                    <td className="px-4 py-2 text-right text-gray-300">{isUsdt ? pos.current_value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : pos.current_value.toLocaleString()}</td>
                    {isUsdt && (
                      <td className="px-4 py-2 text-right text-red-400/80 text-xs">
                        {pos.liquidation_price ? pos.liquidation_price.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '-'}
                      </td>
                    )}
                    <td className="px-4 py-2 text-right text-red-400/80 text-xs">
                      {pos.stop_loss_price
                        ? isUsdt
                          ? pos.stop_loss_price.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 4 })
                          : pos.stop_loss_price.toLocaleString()
                        : '-'}
                    </td>
                    <td className="px-4 py-2 text-right text-buy/80 text-xs">
                      {pos.take_profit_price
                        ? isUsdt
                          ? pos.take_profit_price.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 4 })
                          : pos.take_profit_price.toLocaleString()
                        : '-'}
                    </td>
                    <td className={`px-4 py-2 text-right font-semibold ${pos.unrealized_pnl >= 0 ? 'text-buy' : 'text-sell'}`}>
                      {pos.unrealized_pnl >= 0 ? '+' : ''}{isUsdt ? pos.unrealized_pnl.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : pos.unrealized_pnl.toLocaleString()}
                      <span className="text-xs ml-1">({fmtPct(pos.unrealized_pnl_pct)})</span>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          {/* Mobile card layout */}
          <div className="md:hidden divide-y divide-gray-700/50">
            {data.positions.map((pos) => {
              const dirLabel = pos.direction === 'short' ? 'SHORT' : 'LONG'
              const dirColor = pos.direction === 'short' ? 'text-sell' : 'text-buy'
              return (
                <div key={pos.symbol} className="px-4 py-3 space-y-1.5">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-medium text-white">{pos.symbol.replace(/\/(KRW|USDT)/, '')}</span>
                      {isUsdt && (
                        <>
                          <span className={`text-[10px] px-1.5 py-0.5 rounded font-semibold ${dirColor} ${pos.direction === 'short' ? 'bg-sell/20' : 'bg-buy/20'}`}>{dirLabel}</span>
                          {pos.leverage && pos.leverage > 1 && (
                            <span className="text-[10px] px-1.5 py-0.5 rounded bg-yellow-500/20 text-yellow-400 font-semibold">{pos.leverage}x</span>
                          )}
                        </>
                      )}
                      {pos.trailing_active && (
                        <span className="text-[10px] px-1.5 py-0.5 rounded bg-purple-500/20 text-purple-400 font-semibold">TRAIL</span>
                      )}
                      {pos.is_surge && (
                        <span className="text-[10px] px-1.5 py-0.5 rounded bg-cyan-500/20 text-cyan-400 font-semibold">SURGE</span>
                      )}
                    </div>
                    <span className={`font-semibold ${pos.unrealized_pnl >= 0 ? 'text-buy' : 'text-sell'}`}>
                      {fmtPct(pos.unrealized_pnl_pct)}
                    </span>
                  </div>
                  <div className="grid grid-cols-2 gap-x-4 text-xs">
                    <div className="flex justify-between">
                      <span className="text-gray-500">진입가</span>
                      <span className="text-gray-300">{isUsdt ? pos.average_buy_price.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : pos.average_buy_price.toLocaleString()}</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-gray-500">현재가</span>
                      <span className="text-gray-300">{isUsdt ? pos.current_price.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : pos.current_price.toLocaleString()}</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-gray-500">{isUsdt ? '마진' : '평가금액'}</span>
                      <span className="text-gray-300">{isUsdt ? pos.current_value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : pos.current_value.toLocaleString()}</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-gray-500">손익</span>
                      <span className={pos.unrealized_pnl >= 0 ? 'text-buy' : 'text-sell'}>
                        {pos.unrealized_pnl >= 0 ? '+' : ''}{isUsdt ? pos.unrealized_pnl.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : pos.unrealized_pnl.toLocaleString()}
                      </span>
                    </div>
                    {isUsdt && pos.liquidation_price && (
                      <div className="flex justify-between col-span-2 mt-0.5 pt-1 border-t border-gray-700/30">
                        <span className="text-gray-500">청산가</span>
                        <span className="text-red-400/80">{pos.liquidation_price.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
                      </div>
                    )}
                    {(pos.stop_loss_price || pos.take_profit_price) && (
                      <>
                        <div className="flex justify-between">
                          <span className="text-gray-500">손절가</span>
                          <span className="text-red-400/80">
                            {pos.stop_loss_price
                              ? isUsdt
                                ? pos.stop_loss_price.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 4 })
                                : pos.stop_loss_price.toLocaleString()
                              : '-'}
                          </span>
                        </div>
                        <div className="flex justify-between">
                          <span className="text-gray-500">익절가</span>
                          <span className="text-buy/80">
                            {pos.take_profit_price
                              ? isUsdt
                                ? pos.take_profit_price.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 4 })
                                : pos.take_profit_price.toLocaleString()
                              : '-'}
                          </span>
                        </div>
                      </>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}

      <CapitalManager exchange={exchange} open={capitalOpen} onClose={() => setCapitalOpen(false)} />
    </div>
  )
}
