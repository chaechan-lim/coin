import type { Position, ExchangeName } from '../types'
import { fmtPrice } from '../utils/format'

interface Props {
  position: Position
  exchange: ExchangeName
  onClose: () => void
}

function fmtNum(n: number, isUsdt: boolean): string {
  return isUsdt
    ? n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 4 })
    : n.toLocaleString('ko-KR')
}

function Row({ label, value, color }: { label: string; value: React.ReactNode; color?: string }) {
  return (
    <div className="flex justify-between py-1.5">
      <span className="text-gray-400 text-sm">{label}</span>
      <span className={`text-sm font-medium ${color ?? 'text-gray-200'}`}>{value}</span>
    </div>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1">
      <h4 className="text-xs font-semibold text-gray-500 uppercase tracking-wider">{title}</h4>
      <div className="divide-y divide-gray-700/30">{children}</div>
    </div>
  )
}

export function PositionDetailModal({ position: pos, exchange, onClose }: Props) {
  const isUsdt = exchange.startsWith('binance')
  const isFutures = exchange === 'binance_futures' || exchange === 'binance_surge'
  const fmt = (n: number) => fmtPrice(n, isUsdt)
  const fmtN = (n: number) => fmtNum(n, isUsdt)
  const pnlColor = pos.unrealized_pnl >= 0 ? 'text-buy' : 'text-sell'
  const direction = pos.direction || 'long'
  const dirLabel = direction === 'short' ? 'SHORT' : 'LONG'
  const dirColor = direction === 'short' ? 'text-sell' : 'text-buy'

  const priceChange = pos.average_buy_price > 0
    ? ((pos.current_price - pos.average_buy_price) / pos.average_buy_price) * 100
    : 0

  const holdHours = pos.entered_at
    ? Math.round((Date.now() - new Date(pos.entered_at).getTime()) / 3600000 * 10) / 10
    : null

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={onClose}
    >
      <div
        className="bg-gray-800 rounded-xl w-full max-w-md max-h-[85vh] overflow-hidden flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="px-4 py-3 border-b border-gray-700 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span className="font-semibold text-white text-lg">
              {pos.symbol.replace(/\/(KRW|USDT)/, '')}
            </span>
            {isFutures && (
              <span className={`text-xs px-2 py-0.5 rounded font-semibold ${dirColor} ${direction === 'short' ? 'bg-sell/20' : 'bg-buy/20'}`}>
                {dirLabel}
              </span>
            )}
            {pos.leverage && pos.leverage > 1 && (
              <span className="text-xs px-2 py-0.5 rounded bg-yellow-500/20 text-yellow-400 font-semibold">
                {pos.leverage}x
              </span>
            )}
            {pos.trailing_active && (
              <span className="text-xs px-2 py-0.5 rounded bg-purple-500/20 text-purple-400 font-semibold">
                TRAIL
              </span>
            )}
            {pos.is_surge && (
              <span className="text-xs px-2 py-0.5 rounded bg-cyan-500/20 text-cyan-400 font-semibold">
                SURGE
              </span>
            )}
          </div>
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-white text-lg leading-none px-1"
          >
            &times;
          </button>
        </div>

        {/* P&L Banner */}
        <div className={`px-4 py-3 ${pos.unrealized_pnl >= 0 ? 'bg-buy/10' : 'bg-sell/10'}`}>
          <div className="flex justify-between items-end">
            <div>
              <div className="text-xs text-gray-400">미실현 손익</div>
              <div className={`text-2xl font-bold ${pnlColor}`}>
                {pos.unrealized_pnl >= 0 ? '+' : ''}{fmtN(pos.unrealized_pnl)}
              </div>
            </div>
            <div className={`text-xl font-semibold ${pnlColor}`}>
              {pos.unrealized_pnl_pct >= 0 ? '+' : ''}{pos.unrealized_pnl_pct.toFixed(2)}%
            </div>
          </div>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto px-4 py-3 space-y-4">
          <Section title="가격 정보">
            <Row label="진입가" value={fmtN(pos.average_buy_price)} />
            <Row
              label="현재가"
              value={
                <span>
                  {fmtN(pos.current_price)}
                  <span className={`ml-1.5 text-xs ${priceChange >= 0 ? 'text-buy' : 'text-sell'}`}>
                    ({priceChange >= 0 ? '+' : ''}{priceChange.toFixed(2)}%)
                  </span>
                </span>
              }
            />
            {pos.highest_price != null && (
              <Row
                label={direction === 'short' ? '최저가 (트레일링 기준)' : '최고가 (트레일링 기준)'}
                value={fmtN(pos.highest_price)}
                color="text-purple-400"
              />
            )}
          </Section>

          <Section title="포지션 상세">
            <Row label="수량" value={pos.quantity.toFixed(6)} />
            <Row label={isUsdt ? '투입 마진' : '투자금'} value={fmt(pos.total_invested ?? pos.current_value)} />
            <Row label={isUsdt ? '마진 사용' : '평가금액'} value={fmt(pos.current_value)} />
            {isFutures && pos.margin_used != null && pos.margin_used > 0 && (
              <Row label="마진" value={fmt(pos.margin_used)} />
            )}
            {holdHours != null && (
              <Row label="보유 시간" value={`${holdHours}시간`} />
            )}
            {pos.max_hold_hours != null && (
              <Row label="최대 보유 제한" value={`${pos.max_hold_hours}시간`} color="text-yellow-400" />
            )}
          </Section>

          {/* SL/TP Section */}
          {(pos.stop_loss_price || pos.take_profit_price || pos.stop_loss_pct || pos.take_profit_pct) && (
            <Section title="손절 / 익절">
              {pos.stop_loss_price != null && (
                <Row
                  label={`손절가 (${pos.stop_loss_pct?.toFixed(1) ?? '-'}%)`}
                  value={fmtN(pos.stop_loss_price)}
                  color="text-sell"
                />
              )}
              {pos.take_profit_price != null && (
                <Row
                  label={`익절가 (${pos.take_profit_pct?.toFixed(1) ?? '-'}%)`}
                  value={fmtN(pos.take_profit_price)}
                  color="text-buy"
                />
              )}
              {pos.trailing_active && (
                <>
                  {pos.trailing_activation_pct != null && (
                    <Row
                      label="트레일링 활성화"
                      value={`+${pos.trailing_activation_pct.toFixed(1)}%`}
                      color="text-purple-400"
                    />
                  )}
                  {pos.trailing_stop_pct != null && (
                    <Row
                      label="트레일링 폭"
                      value={`${pos.trailing_stop_pct.toFixed(1)}%`}
                      color="text-purple-400"
                    />
                  )}
                </>
              )}
            </Section>
          )}

          {/* Futures-specific */}
          {isFutures && pos.liquidation_price != null && (
            <Section title="선물 상세">
              <Row label="레버리지" value={`${pos.leverage ?? 1}x`} />
              <Row
                label="청산가"
                value={fmtN(pos.liquidation_price)}
                color="text-red-500"
              />
              {pos.average_buy_price > 0 && pos.liquidation_price > 0 && (
                <Row
                  label="청산 거리"
                  value={`${Math.abs((pos.current_price - pos.liquidation_price) / pos.current_price * 100).toFixed(2)}%`}
                  color={
                    Math.abs((pos.current_price - pos.liquidation_price) / pos.current_price * 100) < 10
                      ? 'text-red-500'
                      : 'text-gray-300'
                  }
                />
              )}
            </Section>
          )}

          {/* Price Bar Visualization */}
          <PriceBar position={pos} isUsdt={isUsdt} isFutures={isFutures} />
        </div>
      </div>
    </div>
  )
}

function PriceBar({ position: pos, isUsdt, isFutures }: { position: Position; isUsdt: boolean; isFutures: boolean }) {
  const direction = pos.direction || 'long'
  const prices: { label: string; price: number; color: string }[] = []

  if (pos.stop_loss_price) prices.push({ label: 'SL', price: pos.stop_loss_price, color: 'bg-sell' })
  prices.push({ label: '진입', price: pos.average_buy_price, color: 'bg-gray-400' })
  prices.push({ label: '현재', price: pos.current_price, color: pos.unrealized_pnl >= 0 ? 'bg-buy' : 'bg-sell' })
  if (pos.take_profit_price) prices.push({ label: 'TP', price: pos.take_profit_price, color: 'bg-buy' })
  if (isFutures && pos.liquidation_price) prices.push({ label: '청산', price: pos.liquidation_price, color: 'bg-red-600' })

  prices.sort((a, b) => a.price - b.price)

  const minP = prices[0]?.price ?? 0
  const maxP = prices[prices.length - 1]?.price ?? 1
  const range = maxP - minP || 1

  if (prices.length < 2) return null

  return (
    <div className="space-y-1">
      <h4 className="text-xs font-semibold text-gray-500 uppercase tracking-wider">가격 분포</h4>
      <div className="relative h-8 bg-gray-700/50 rounded-lg overflow-hidden">
        {/* Entry to current price fill */}
        {(() => {
          const entryPct = ((pos.average_buy_price - minP) / range) * 100
          const currentPct = ((pos.current_price - minP) / range) * 100
          const left = Math.min(entryPct, currentPct)
          const width = Math.abs(currentPct - entryPct)
          const isProfit = (direction === 'long' && pos.current_price >= pos.average_buy_price) ||
            (direction === 'short' && pos.current_price <= pos.average_buy_price)
          return (
            <div
              className={`absolute top-0 bottom-0 ${isProfit ? 'bg-buy/20' : 'bg-sell/20'}`}
              style={{ left: `${left}%`, width: `${width}%` }}
            />
          )
        })()}
        {prices.map((p, i) => {
          const pct = ((p.price - minP) / range) * 100
          return (
            <div key={i} className="absolute top-0 bottom-0 flex flex-col items-center" style={{ left: `${pct}%`, transform: 'translateX(-50%)' }}>
              <div className={`w-0.5 h-full ${p.color}`} />
              <span className="absolute -bottom-4 text-[9px] text-gray-400 whitespace-nowrap">{p.label}</span>
            </div>
          )
        })}
      </div>
      <div className="h-4" /> {/* spacer for labels */}
    </div>
  )
}
