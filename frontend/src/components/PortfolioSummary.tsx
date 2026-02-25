import { usePortfolioSummary } from '../hooks/usePortfolio'

function StatCard({
  label,
  value,
  sub,
  color,
}: {
  label: string
  value: string
  sub?: string
  color?: string
}) {
  return (
    <div className="bg-gray-800 rounded-xl p-4 flex flex-col gap-1">
      <span className="text-gray-400 text-xs">{label}</span>
      <span className={`text-xl font-bold ${color ?? 'text-white'}`}>{value}</span>
      {sub && <span className="text-gray-500 text-xs">{sub}</span>}
    </div>
  )
}

function fmt(n: number) {
  return n.toLocaleString('ko-KR') + ' ₩'
}

function fmtPct(n: number) {
  const sign = n >= 0 ? '+' : ''
  return `${sign}${n.toFixed(2)}%`
}

export function PortfolioSummary() {
  const { data, isLoading } = usePortfolioSummary()

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
  const drawdownColor = data.drawdown_pct > 5 ? 'text-sell' : 'text-gray-300'

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <StatCard label="총 자산" value={fmt(data.total_value_krw)} />
        <StatCard
          label="총 수익"
          value={fmt(data.total_pnl)}
          sub={fmtPct(data.total_pnl_pct)}
          color={pnlColor}
        />
        <StatCard label="현금 잔액" value={fmt(data.cash_balance_krw)} />
        <StatCard
          label="최대 낙폭"
          value={fmtPct(data.drawdown_pct)}
          sub={`고점: ${fmt(data.peak_value)}`}
          color={drawdownColor}
        />
        <StatCard label="실현 손익" value={fmt(data.realized_pnl)} color={data.realized_pnl >= 0 ? 'text-buy' : 'text-sell'} />
        <StatCard label="미실현 손익" value={fmt(data.unrealized_pnl)} color={data.unrealized_pnl >= 0 ? 'text-buy' : 'text-sell'} />
        <StatCard label="투자 금액" value={fmt(data.invested_value_krw)} />
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
          <table className="w-full text-sm">
            <thead>
              <tr className="text-gray-500 text-xs border-b border-gray-700">
                <th className="px-4 py-2 text-left">코인</th>
                <th className="px-4 py-2 text-right">수량</th>
                <th className="px-4 py-2 text-right">평균 매수가</th>
                <th className="px-4 py-2 text-right">현재가</th>
                <th className="px-4 py-2 text-right">평가금액</th>
                <th className="px-4 py-2 text-right">미실현 손익</th>
              </tr>
            </thead>
            <tbody>
              {data.positions.map((pos) => (
                <tr key={pos.symbol} className="border-b border-gray-700/50 hover:bg-gray-700/30">
                  <td className="px-4 py-2 font-medium text-white">{pos.symbol}</td>
                  <td className="px-4 py-2 text-right text-gray-300">{pos.quantity.toFixed(6)}</td>
                  <td className="px-4 py-2 text-right text-gray-300">{pos.average_buy_price.toLocaleString()}</td>
                  <td className="px-4 py-2 text-right text-gray-300">{pos.current_price.toLocaleString()}</td>
                  <td className="px-4 py-2 text-right text-gray-300">{pos.current_value.toLocaleString()}</td>
                  <td className={`px-4 py-2 text-right font-semibold ${pos.unrealized_pnl >= 0 ? 'text-buy' : 'text-sell'}`}>
                    {pos.unrealized_pnl >= 0 ? '+' : ''}{pos.unrealized_pnl.toLocaleString()}
                    <span className="text-xs ml-1">({fmtPct(pos.unrealized_pnl_pct)})</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
