import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  getCapitalTransactions,
  getCapitalSummary,
  createCapitalTransaction,
  confirmCapitalTransaction,
  deleteCapitalTransaction,
} from '../api/client'
import type { ExchangeName } from '../types'
import { format } from 'date-fns'

const TX_TYPE_LABEL = { deposit: '입금', withdrawal: '출금' } as const
const SOURCE_LABEL = {
  manual: '수동',
  auto_detected: '자동 감지',
  seed: '초기 원금',
} as const

export function CapitalManager({
  exchange,
  open,
  onClose,
}: {
  exchange: ExchangeName
  open: boolean
  onClose: () => void
}) {
  const isUsdt = exchange === 'binance_futures'
  const currency = isUsdt ? 'USDT' : 'KRW'
  const qc = useQueryClient()

  const [txType, setTxType] = useState<'deposit' | 'withdrawal'>('deposit')
  const [amount, setAmount] = useState('')
  const [note, setNote] = useState('')
  const [showForm, setShowForm] = useState(false)

  const { data: transactions } = useQuery({
    queryKey: ['capital', 'transactions', exchange],
    queryFn: () => getCapitalTransactions(exchange),
    enabled: open,
    refetchInterval: 30_000,
  })

  const { data: summary } = useQuery({
    queryKey: ['capital', 'summary', exchange],
    queryFn: () => getCapitalSummary(exchange),
    enabled: open,
    refetchInterval: 30_000,
  })

  const createMutation = useMutation({
    mutationFn: createCapitalTransaction,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['capital'] })
      qc.invalidateQueries({ queryKey: ['portfolio'] })
      setAmount('')
      setNote('')
      setShowForm(false)
    },
  })

  const confirmMutation = useMutation({
    mutationFn: confirmCapitalTransaction,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['capital'] })
      qc.invalidateQueries({ queryKey: ['portfolio'] })
    },
  })

  const deleteMutation = useMutation({
    mutationFn: deleteCapitalTransaction,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['capital'] })
      qc.invalidateQueries({ queryKey: ['portfolio'] })
    },
  })

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    const parsed = parseFloat(amount)
    if (!parsed || parsed <= 0) return
    createMutation.mutate({
      exchange,
      tx_type: txType,
      amount: parsed,
      note: note || undefined,
    })
  }

  const fmtAmt = (n: number) =>
    isUsdt
      ? n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' USDT'
      : n.toLocaleString('ko-KR') + ' ₩'

  if (!open) return null

  const unconfirmedCount = transactions?.filter((t) => !t.confirmed).length ?? 0

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div
        className="bg-gray-800 rounded-xl w-full max-w-lg max-h-[85vh] overflow-hidden flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="px-4 py-3 border-b border-gray-700 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span className="font-semibold text-white">입출금 관리</span>
            {unconfirmedCount > 0 && (
              <span className="bg-yellow-500/20 text-yellow-400 text-[10px] px-1.5 py-0.5 rounded-full font-medium">
                {unconfirmedCount}건 미확인
              </span>
            )}
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-white text-lg leading-none">&times;</button>
        </div>

        {/* Summary */}
        {summary && (
          <div className="px-4 py-3 bg-gray-750 border-b border-gray-700 grid grid-cols-3 gap-3 text-center text-sm">
            <div>
              <div className="text-gray-400 text-xs">총 입금</div>
              <div className="text-buy font-medium">{fmtAmt(summary.total_deposits)}</div>
            </div>
            <div>
              <div className="text-gray-400 text-xs">총 출금</div>
              <div className="text-sell font-medium">{fmtAmt(summary.total_withdrawals)}</div>
            </div>
            <div>
              <div className="text-gray-400 text-xs">순 원금</div>
              <div className="text-white font-bold">{fmtAmt(summary.net_capital)}</div>
            </div>
          </div>
        )}

        {/* Actions */}
        <div className="px-4 py-2 border-b border-gray-700 flex gap-2">
          <button
            onClick={() => { setTxType('deposit'); setShowForm(!showForm) }}
            className="px-3 py-1.5 text-xs rounded bg-buy/20 text-buy hover:bg-buy/30 font-medium"
          >
            + 입금 기록
          </button>
          <button
            onClick={() => { setTxType('withdrawal'); setShowForm(!showForm) }}
            className="px-3 py-1.5 text-xs rounded bg-sell/20 text-sell hover:bg-sell/30 font-medium"
          >
            - 출금 기록
          </button>
        </div>

        {/* Form */}
        {showForm && (
          <form onSubmit={handleSubmit} className="px-4 py-3 border-b border-gray-700 space-y-2 bg-gray-900/50">
            <div className="flex items-center gap-2 text-sm">
              <span className={txType === 'deposit' ? 'text-buy' : 'text-sell'}>
                {TX_TYPE_LABEL[txType]}
              </span>
            </div>
            <div className="flex gap-2">
              <input
                type="number"
                step={isUsdt ? '0.01' : '1'}
                min="0"
                placeholder={`금액 (${currency})`}
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
                className="flex-1 bg-gray-700 text-white rounded px-3 py-1.5 text-sm outline-none focus:ring-1 focus:ring-blue-500"
                autoFocus
              />
              <input
                type="text"
                placeholder="메모 (선택)"
                value={note}
                onChange={(e) => setNote(e.target.value)}
                className="flex-1 bg-gray-700 text-white rounded px-3 py-1.5 text-sm outline-none focus:ring-1 focus:ring-blue-500"
              />
            </div>
            <div className="flex gap-2 justify-end">
              <button
                type="button"
                onClick={() => setShowForm(false)}
                className="px-3 py-1.5 text-xs text-gray-400 hover:text-white"
              >
                취소
              </button>
              <button
                type="submit"
                disabled={createMutation.isPending}
                className="px-4 py-1.5 text-xs rounded bg-blue-600 hover:bg-blue-500 text-white font-medium disabled:opacity-50"
              >
                {createMutation.isPending ? '처리 중...' : '저장'}
              </button>
            </div>
          </form>
        )}

        {/* Transaction list */}
        <div className="flex-1 overflow-y-auto">
          {!transactions?.length ? (
            <div className="px-4 py-8 text-center text-gray-500 text-sm">입출금 기록이 없습니다</div>
          ) : (
            <div className="divide-y divide-gray-700/50">
              {transactions.map((tx) => (
                <div key={tx.id} className={`px-4 py-2.5 flex items-center gap-3 ${!tx.confirmed ? 'bg-yellow-500/5' : ''}`}>
                  {/* Icon */}
                  <div className={`w-8 h-8 rounded-full flex items-center justify-center text-sm shrink-0 ${
                    tx.tx_type === 'deposit' ? 'bg-buy/20 text-buy' : 'bg-sell/20 text-sell'
                  }`}>
                    {tx.tx_type === 'deposit' ? '+' : '-'}
                  </div>

                  {/* Details */}
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-1.5">
                      <span className={`text-sm font-medium ${tx.tx_type === 'deposit' ? 'text-buy' : 'text-sell'}`}>
                        {TX_TYPE_LABEL[tx.tx_type as keyof typeof TX_TYPE_LABEL]} {fmtAmt(tx.amount)}
                      </span>
                      <span className="text-[10px] px-1 py-0.5 rounded bg-gray-700 text-gray-400">
                        {SOURCE_LABEL[tx.source as keyof typeof SOURCE_LABEL] ?? tx.source}
                      </span>
                      {!tx.confirmed && (
                        <span className="text-[10px] px-1.5 py-0.5 rounded bg-yellow-500/20 text-yellow-400 font-medium">
                          미확인
                        </span>
                      )}
                    </div>
                    <div className="text-xs text-gray-500 truncate">
                      {format(new Date(tx.created_at), 'yyyy-MM-dd HH:mm')}
                      {tx.note && ` — ${tx.note}`}
                    </div>
                  </div>

                  {/* Actions */}
                  <div className="flex gap-1 shrink-0">
                    {!tx.confirmed && (
                      <button
                        onClick={() => confirmMutation.mutate(tx.id)}
                        disabled={confirmMutation.isPending}
                        className="px-2 py-1 text-[10px] rounded bg-blue-600/20 text-blue-400 hover:bg-blue-600/30 font-medium"
                      >
                        확인
                      </button>
                    )}
                    {tx.source !== 'seed' && (
                      <button
                        onClick={() => { if (confirm('삭제하시겠습니까?')) deleteMutation.mutate(tx.id) }}
                        disabled={deleteMutation.isPending}
                        className="px-2 py-1 text-[10px] rounded bg-red-600/20 text-red-400 hover:bg-red-600/30 font-medium"
                      >
                        삭제
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
