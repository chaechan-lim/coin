import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  getDonchianFuturesTradeGroupDetail,
  getDonchianFuturesTradeGroups,
  getFuturesRndStatus,
  getRndOverview,
  getPairsTradeGroupDetail,
  getPairsTradeGroups,
  getResearchAutoReviewStatus,
  getResearchOverview,
  getResearchStageHistory,
  updateResearchStage,
} from '../api/client'
import { formatTs } from '../utils/date'
import { fmtSignedPrice } from '../utils/format'
import type {
  AutoReview,
  DonchianFuturesTradeGroup,
  DonchianFuturesTradeGroupDetail,
  ExchangeName,
  FuturesRndStatus,
  PairsTradeGroup,
  PairsTradeGroupDetail,
  ResearchCandidate,
  ResearchStageHistoryEntry,
} from '../types'

const STAGE_STYLES: Record<string, string> = {
  live_rnd: 'bg-green-900/50 text-green-300 border-green-700/60',
  production: 'bg-emerald-900/50 text-emerald-300 border-emerald-700/60',
  shadow: 'bg-sky-900/50 text-sky-300 border-sky-700/60',
  candidate: 'bg-amber-900/50 text-amber-300 border-amber-700/60',
  research: 'bg-gray-800 text-gray-300 border-gray-700',
  hold: 'bg-red-950/50 text-red-300 border-red-800/60',
}

const DECISION_STYLES: Record<string, string> = {
  promote: 'bg-green-900/50 text-green-300 border-green-700/60',
  keep: 'bg-gray-800 text-gray-300 border-gray-700',
  demote: 'bg-red-950/50 text-red-300 border-red-800/60',
  pending: 'bg-yellow-950/50 text-yellow-300 border-yellow-800/60',
  error: 'bg-red-950/50 text-red-300 border-red-800/60',
}

function SummaryCard({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-xl border border-gray-700 bg-gray-800/80 p-3">
      <div className="text-[11px] uppercase tracking-wide text-gray-500">{label}</div>
      <div className="mt-1 text-lg font-semibold text-white">{value}</div>
      {hint && <div className="mt-1 text-xs text-gray-400">{hint}</div>}
    </div>
  )
}

function CandidateCard({
  item,
  latestHistory,
  isSubmitting,
  onApprove,
}: {
  item: ResearchCandidate
  latestHistory?: ResearchStageHistoryEntry
  isSubmitting: boolean
  onApprove: (item: ResearchCandidate, stage: string, note: string) => void
}) {
  const [targetStage, setTargetStage] = useState(item.stage)
  const [approvalNote, setApprovalNote] = useState('')
  const stageStyle = STAGE_STYLES[item.stage] ?? STAGE_STYLES.research
  const decision = item.auto_review?.decision ?? 'pending'
  const decisionStyle = DECISION_STYLES[decision] ?? DECISION_STYLES.keep
  const liveMetric = item.auto_review?.metrics.find((metric) =>
    metric.source.endsWith('_live_execution')
  )

  useEffect(() => {
    setTargetStage(item.stage)
  }, [item.stage])

  return (
    <div className="rounded-xl border border-gray-700 bg-gray-800/80 p-4 space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h4 className="text-sm font-semibold text-white">{item.title}</h4>
        <span className={`rounded-full border px-2 py-0.5 text-[11px] font-medium ${stageStyle}`}>effective {item.stage}</span>
        {item.catalog_stage !== item.stage && (
          <span className="rounded-full border border-gray-700 px-2 py-0.5 text-[11px] font-medium text-gray-300">
            catalog {item.catalog_stage}
          </span>
        )}
        <span className="rounded-full border border-gray-700 px-2 py-0.5 text-[11px] font-medium text-gray-300">
          {item.stage_source}
        </span>
        <span
          className={`rounded-full border px-2 py-0.5 text-[11px] font-medium ${
            item.execution_allowed
              ? 'border-green-700/60 bg-green-900/40 text-green-300'
              : 'border-gray-700 bg-gray-900/40 text-gray-400'
          }`}
        >
          {item.execution_allowed ? 'execution on' : 'execution off'}
        </span>
        <span className={`rounded-full border px-2 py-0.5 text-[11px] font-medium ${decisionStyle}`}>
          {decision}
        </span>
        {item.is_live_engine_running && (
          <span className="rounded-full border border-green-700/60 bg-green-900/40 px-2 py-0.5 text-[11px] text-green-300">
            live
          </span>
        )}
      </div>
      <p className="text-sm text-gray-300">{item.auto_review?.summary ?? item.rationale}</p>
      <div className="grid grid-cols-2 gap-2 text-xs md:grid-cols-4">
        <div>
          <div className="text-gray-500">시장</div>
          <div className="text-white">{item.market}</div>
        </div>
        <div>
          <div className="text-gray-500">방향</div>
          <div className="text-white">{item.directionality}</div>
        </div>
        <div>
          <div className="text-gray-500">추천 단계</div>
          <div className="text-white">{item.auto_review?.recommended_stage ?? item.stage}</div>
        </div>
        <div>
          <div className="text-gray-500">다음 단계</div>
          <div className="text-white">{item.next_stages.join(', ')}</div>
        </div>
      </div>
      {liveMetric && (
        <div className="grid grid-cols-2 gap-2 rounded-lg bg-gray-900/60 p-3 text-xs md:grid-cols-4">
          <div>
            <div className="text-gray-500">Live 표본</div>
            <div className="text-white">{liveMetric.trade_count ?? 0}</div>
          </div>
          <div>
            <div className="text-gray-500">Live 손익</div>
            <div className={liveMetric.return_pct >= 0 ? 'text-green-400' : 'text-red-400'}>
              {liveMetric.return_pct >= 0 ? '+' : ''}
              {liveMetric.return_pct.toFixed(2)}%
            </div>
          </div>
          <div>
            <div className="text-gray-500">Live MDD</div>
            <div className="text-white">{liveMetric.max_drawdown.toFixed(2)}%</div>
          </div>
          <div>
            <div className="text-gray-500">Live Sharpe</div>
            <div className="text-white">{liveMetric.sharpe.toFixed(2)}</div>
          </div>
        </div>
      )}
      {item.auto_review?.blockers?.length ? (
        <div className="rounded-lg border border-gray-700 bg-gray-900/60 p-3">
          <div className="mb-1 text-xs font-medium text-gray-400">현재 blocker</div>
          <div className="space-y-1 text-xs text-gray-300">
            {item.auto_review.blockers.slice(0, 3).map((blocker) => (
              <div key={blocker}>• {blocker}</div>
            ))}
          </div>
        </div>
      ) : null}
      <div className="rounded-lg border border-gray-700 bg-gray-900/50 p-3">
        <div className="mb-2 flex items-center justify-between gap-2">
          <div className="text-xs font-medium text-gray-400">승격/강등 승인</div>
          {item.approved_at && (
            <div className="text-[11px] text-gray-500">
              {item.approved_by ?? 'system'} · {formatTs(item.approved_at, 'MM/dd HH:mm')}
            </div>
          )}
        </div>
        <div className="mb-2 text-[11px] text-gray-500">
          {item.stage_managed
            ? '전용 R&D 엔진은 effective stage가 live_rnd / production일 때만 실주문 허용됩니다.'
            : '이 후보는 현재 거버넌스 stage만 관리하며, 전용 stage gate 런타임은 없습니다.'}
        </div>
        {item.approval_note && (
          <div className="mb-2 rounded bg-gray-800/70 px-2 py-1.5 text-xs text-gray-300">{item.approval_note}</div>
        )}
        {latestHistory && (
          <div className="mb-2 text-[11px] text-gray-500">
            최근 변경: {latestHistory.from_stage ?? '-'} → {latestHistory.to_stage} · {latestHistory.approved_by ?? latestHistory.approval_source}
          </div>
        )}
        <div className="flex flex-col gap-2 md:flex-row">
          <select
            value={targetStage}
            onChange={(event) => setTargetStage(event.target.value)}
            className="rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-white"
          >
            <option value={item.stage}>{item.stage} (current)</option>
            {item.next_stages.map((stage) => (
              <option key={stage} value={stage}>
                {stage}
              </option>
            ))}
          </select>
          <input
            value={approvalNote}
            onChange={(event) => setApprovalNote(event.target.value)}
            placeholder="변경 메모"
            className="min-w-0 flex-1 rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-white placeholder:text-gray-500"
          />
          <button
            type="button"
            disabled={targetStage === item.stage || isSubmitting}
            onClick={() => onApprove(item, targetStage, approvalNote)}
            className="rounded-lg border border-blue-700/60 bg-blue-900/30 px-3 py-2 text-sm font-medium text-blue-200 transition-colors hover:bg-blue-900/50 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {isSubmitting ? '적용 중...' : '승인 적용'}
          </button>
        </div>
      </div>
    </div>
  )
}

function scoreCandidate(item: ResearchCandidate): number {
  const review = item.auto_review
  const liveMetric = review?.metrics.find((metric) => metric.source.endsWith('_live_execution'))
  let score = 0

  if (item.is_live_engine_running) score += 60
  if (item.stage === 'candidate') score += 40
  if (item.stage === 'live_rnd') score += 35
  if (item.stage === 'shadow') score += 30

  switch (review?.decision) {
    case 'demote':
      score += 120
      break
    case 'pending':
      score += 90
      break
    case 'promote':
      score += 70
      break
    case 'error':
      score += 110
      break
    default:
      score += 20
  }

  score += Math.min((review?.blockers?.length ?? 0) * 8, 32)

  if (liveMetric) {
    if (liveMetric.return_pct < 0) score += 35
    if (liveMetric.max_drawdown > 5) score += 15
    if ((liveMetric.trade_count ?? 0) === 0) score += 10
  }

  return score
}

function priorityLabel(item: ResearchCandidate): string {
  const review = item.auto_review
  if (review?.decision === 'demote') return '즉시 점검'
  if (review?.decision === 'pending') return '캐시 대기'
  if (review?.decision === 'error') return '판정 오류'
  if (review?.decision === 'promote') return '승격 후보'
  if (item.is_live_engine_running && (review?.blockers?.length ?? 0) > 0) return '라이브 감시'
  return '모니터링'
}

function priorityTone(item: ResearchCandidate): string {
  const review = item.auto_review
  if (review?.decision === 'demote' || review?.decision === 'error') return 'border-red-800/60 bg-red-950/40 text-red-300'
  if (review?.decision === 'pending') return 'border-yellow-800/60 bg-yellow-950/30 text-yellow-300'
  if (review?.decision === 'promote') return 'border-green-700/60 bg-green-900/30 text-green-300'
  return 'border-gray-700 bg-gray-900/40 text-gray-300'
}

function GroupRow({
  title,
  direction,
  status,
  openedAt,
  closedAt,
  realizedPnl,
}: {
  title: string
  direction: string
  status: string
  openedAt: string
  closedAt: string | null
  realizedPnl: number
}) {
  return (
    <div className="grid grid-cols-[1.3fr,0.7fr,0.7fr,0.9fr] gap-2 border-b border-gray-800 py-2 text-xs last:border-b-0">
      <div className="min-w-0">
        <div className="truncate text-white">{title}</div>
        <div className="text-gray-500">{direction}</div>
      </div>
      <div className="text-gray-300">{status}</div>
      <div className={realizedPnl >= 0 ? 'text-green-400' : 'text-red-400'}>
        {realizedPnl >= 0 ? '+' : ''}
        {realizedPnl.toFixed(2)}
      </div>
      <div className="text-right text-gray-500">
        {closedAt ? formatTs(closedAt, 'MM/dd HH:mm') : formatTs(openedAt, 'MM/dd HH:mm')}
      </div>
    </div>
  )
}

function GroupPanel({
  title,
  emptyLabel,
  pairs,
  donchian,
  onOpenPair,
  onOpenDonchian,
}: {
  title: string
  emptyLabel: string
  pairs?: PairsTradeGroup[]
  donchian?: DonchianFuturesTradeGroup[]
  onOpenPair?: (tradeId: string) => void
  onOpenDonchian?: (tradeId: string) => void
}) {
  const rows = pairs ?? donchian ?? []

  return (
    <div className="rounded-xl border border-gray-700 bg-gray-800/80 p-4">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h4 className="text-sm font-semibold text-white">{title}</h4>
        <span className="text-xs text-gray-500">{rows.length} groups</span>
      </div>
      {rows.length === 0 ? (
        <div className="rounded-lg bg-gray-900/50 p-4 text-sm text-gray-500">{emptyLabel}</div>
      ) : (
        <div>
          {(pairs ?? []).map((group) => (
            <button
              key={group.trade_id}
              type="button"
              className="block w-full text-left transition-colors hover:bg-gray-900/40"
              onClick={() => onOpenPair?.(group.trade_id)}
            >
              <GroupRow
                title={group.symbols.join(' / ')}
                direction={group.pair_direction}
                status={group.status}
                openedAt={group.opened_at}
                closedAt={group.closed_at}
                realizedPnl={group.realized_pnl}
              />
            </button>
          ))}
          {(donchian ?? []).map((group) => (
            <button
              key={group.trade_id}
              type="button"
              className="block w-full text-left transition-colors hover:bg-gray-900/40"
              onClick={() => onOpenDonchian?.(group.trade_id)}
            >
              <GroupRow
                title={group.symbol}
                direction={group.direction}
                status={group.status}
                openedAt={group.opened_at}
                closedAt={group.closed_at}
                realizedPnl={group.realized_pnl}
              />
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

function PriorityQueue({ items }: { items: ResearchCandidate[] }) {
  const top = items.slice(0, 3)
  return (
    <div className="rounded-xl border border-gray-700 bg-gray-800 p-4">
      <div className="mb-3 flex items-center justify-between gap-2">
        <div>
          <h3 className="text-base font-semibold text-white">우선순위 큐</h3>
          <p className="text-sm text-gray-400">live 상태와 auto-review 결과를 합쳐 다음 액션 우선순위를 정렬합니다.</p>
        </div>
      </div>
      <div className="space-y-2">
        {top.map((item) => (
          <div key={item.key} className={`rounded-lg border p-3 ${priorityTone(item)}`}>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="font-medium">{item.title}</div>
              <div className="text-xs">{priorityLabel(item)}</div>
            </div>
            <div className="mt-1 text-xs opacity-90">
              {item.auto_review?.blockers?.[0] ?? item.auto_review?.summary ?? item.rationale}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

function PromotionBridge({
  items,
  onNavigate,
}: {
  items: ResearchCandidate[]
  onNavigate?: (tab: 'live' | 'ops') => void
}) {
  const hints = items
    .slice(0, 4)
    .map((item) => buildPromotionHint(item))
    .filter((hint): hint is PromotionHint => hint !== null)

  if (hints.length === 0) return null

  return (
    <div className="rounded-xl border border-gray-700 bg-gray-800 p-4">
      <div className="mb-3">
        <h3 className="text-base font-semibold text-white">승격 힌트</h3>
        <p className="text-sm text-gray-400">R&D 판단과 실거래 확인 위치를 바로 연결합니다.</p>
      </div>
      <div className="grid gap-3 xl:grid-cols-2">
        {hints.map((hint) => (
          <div key={hint.key} className={`rounded-lg border p-3 ${hint.tone}`}>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="font-medium">{hint.title}</div>
              <div className="text-xs">{hint.label}</div>
            </div>
            <div className="mt-2 text-sm opacity-90">{hint.summary}</div>
            <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
              <div className="text-xs text-gray-300">{hint.nextAction}</div>
              <button
                type="button"
                onClick={() => onNavigate?.(hint.targetTab)}
                className="rounded-full border border-gray-600 bg-gray-900/50 px-3 py-1 text-[11px] font-medium text-white transition-colors hover:border-blue-500 hover:text-blue-300"
              >
                {hint.actionLabel}
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

type PromotionHint = {
  key: string
  title: string
  label: string
  summary: string
  nextAction: string
  tone: string
  targetTab: 'live' | 'ops'
  actionLabel: string
}

type ApprovalDraft = {
  item: ResearchCandidate
  targetStage: string
  note: string
}

function buildPromotionHint(item: ResearchCandidate): PromotionHint | null {
  const review = item.auto_review
  const blocker = review?.blockers?.[0]

  if (item.is_live_engine_running) {
    return {
      key: `${item.key}-live`,
      title: item.title,
      label: 'live_rnd 추적',
      summary: blocker ?? review?.summary ?? item.rationale,
      nextAction: '실거래 탭에서 grouped trade와 손익을 확인하고, 이상 징후는 운영 로그 탭에서 점검하세요.',
      tone: 'border-green-700/60 bg-green-950/20 text-green-200',
      targetTab: 'live',
      actionLabel: '실거래 보기',
    }
  }

  if (review?.decision === 'promote') {
    return {
      key: `${item.key}-promote`,
      title: item.title,
      label: '승격 후보',
      summary: review.summary,
      nextAction: '다음 단계는 shadow 또는 소액 live_rnd 검토입니다. 실거래 탭 KPI 구조를 기준으로 모니터링 계획을 맞추세요.',
      tone: 'border-blue-700/60 bg-blue-950/20 text-blue-200',
      targetTab: 'live',
      actionLabel: '실거래 기준 보기',
    }
  }

  if (review?.decision === 'demote' || review?.decision === 'error') {
    return {
      key: `${item.key}-risk`,
      title: item.title,
      label: '점검 필요',
      summary: blocker ?? review.summary,
      nextAction: '운영 로그 탭에서 관련 이벤트를 보고, blocker 해소 전에는 승격하지 않는 편이 맞습니다.',
      tone: 'border-red-800/60 bg-red-950/30 text-red-200',
      targetTab: 'ops',
      actionLabel: '운영 로그 보기',
    }
  }

  if ((review?.blockers?.length ?? 0) > 0 && item.stage === 'candidate') {
    return {
      key: `${item.key}-candidate`,
      title: item.title,
      label: 'candidate 보완',
      summary: blocker ?? item.rationale,
      nextAction: 'R&D 탭에서 blocker를 확인하고, 실거래 탭과 동일한 KPI로 shadow 모니터링 기준을 먼저 맞추세요.',
      tone: 'border-amber-800/60 bg-amber-950/20 text-amber-200',
      targetTab: 'live',
      actionLabel: '실거래 KPI 보기',
    }
  }

  return null
}

function ApprovalConfirmModal({
  draft,
  isSubmitting,
  onConfirm,
  onClose,
}: {
  draft: ApprovalDraft
  isSubmitting: boolean
  onConfirm: () => void
  onClose: () => void
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div
        className="w-full max-w-lg rounded-xl bg-gray-800 p-4"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="mb-3">
          <h3 className="text-lg font-semibold text-white">승격/강등 승인 확인</h3>
          <div className="mt-1 text-sm text-gray-400">
            {draft.item.title}의 effective stage를 변경합니다.
          </div>
        </div>
        <div className="rounded-lg border border-gray-700 bg-gray-900/50 p-3 text-sm text-gray-300">
          <div>현재: <span className="text-white">{draft.item.stage}</span></div>
          <div>변경: <span className="text-white">{draft.targetStage}</span></div>
          <div>실행 권한: <span className="text-white">{['live_rnd', 'production'].includes(draft.targetStage) ? '허용' : '중지/금지'}</span></div>
          {draft.note && <div className="mt-2 text-gray-400">메모: {draft.note}</div>}
        </div>
        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            disabled={isSubmitting}
            className="rounded-lg border border-gray-700 px-3 py-2 text-sm text-gray-300 transition-colors hover:bg-gray-700 disabled:opacity-40"
          >
            취소
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={isSubmitting}
            className="rounded-lg border border-blue-700/60 bg-blue-900/30 px-3 py-2 text-sm font-medium text-blue-200 transition-colors hover:bg-blue-900/50 disabled:opacity-40"
          >
            {isSubmitting ? '적용 중...' : '변경 승인'}
          </button>
        </div>
      </div>
    </div>
  )
}

function InlineNotice({
  kind,
  message,
  onClose,
}: {
  kind: 'success' | 'error'
  message: string
  onClose: () => void
}) {
  return (
    <div
      className={`rounded-xl border px-4 py-3 ${
        kind === 'success'
          ? 'border-green-700/60 bg-green-950/20 text-green-200'
          : 'border-red-800/60 bg-red-950/30 text-red-200'
      }`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="text-sm">{message}</div>
        <button type="button" onClick={onClose} className="text-xs opacity-80 hover:opacity-100">
          닫기
        </button>
      </div>
    </div>
  )
}

function StageHistoryPanel({ entries }: { entries: ResearchStageHistoryEntry[] }) {
  return (
    <div>
      {entries.length === 0 ? (
        <div className="rounded-lg bg-gray-900/50 p-4 text-sm text-gray-500">최근 stage 변경 이력이 없습니다.</div>
      ) : (
        <div className="space-y-2">
          {entries.map((entry) => (
            <div key={entry.id} className="rounded-lg border border-gray-700 bg-gray-900/50 p-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="text-sm font-medium text-white">{entry.title}</div>
                <div className="text-[11px] text-gray-500">{formatTs(entry.created_at, 'MM/dd HH:mm:ss')}</div>
              </div>
              <div className="mt-1 text-xs text-gray-300">
                {entry.from_stage ?? '-'} → {entry.to_stage} · {entry.approved_by ?? entry.approval_source}
              </div>
              {entry.approval_note && <div className="mt-1 text-xs text-gray-400">{entry.approval_note}</div>}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function TradeDetailModal({
  title,
  detail,
  isUsdt,
  onClose,
}: {
  title: string
  detail: PairsTradeGroupDetail | DonchianFuturesTradeGroupDetail
  isUsdt: boolean
  onClose: () => void
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div
        className="max-h-[85vh] w-full max-w-3xl overflow-hidden rounded-xl bg-gray-800"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-gray-700 px-4 py-3">
          <div>
            <h3 className="text-lg font-semibold text-white">{title}</h3>
            <div className="text-xs text-gray-400">
              trade_id={detail.trade_id} · status={detail.status} · opened {formatTs(detail.opened_at, 'MM/dd HH:mm:ss')}
            </div>
          </div>
          <button onClick={onClose} className="px-2 text-xl text-gray-400 hover:text-white">
            &times;
          </button>
        </div>
        <div className="grid gap-4 overflow-y-auto p-4 md:grid-cols-[1.1fr,0.9fr]">
          <div className="space-y-3">
            <div className="rounded-lg bg-gray-900/50 p-3">
              <div className="mb-2 text-xs font-medium text-gray-400">주문 묶음</div>
              <div className="space-y-2">
                {detail.orders.map((order) => (
                  <div key={order.id} className="rounded border border-gray-700 bg-gray-800/60 p-2 text-xs">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="text-white">
                        {order.symbol} · {order.side.toUpperCase()} {order.direction ? `· ${order.direction}` : ''}
                      </div>
                      <div className="text-gray-500">{formatTs(order.created_at, 'MM/dd HH:mm:ss')}</div>
                    </div>
                    <div className="mt-1 grid grid-cols-2 gap-2 text-gray-300 md:grid-cols-4">
                      <div>qty {order.executed_quantity ?? order.requested_quantity}</div>
                      <div>px {(order.executed_price ?? order.requested_price ?? 0).toLocaleString('en-US')}</div>
                      <div>fee {fmtSignedPrice(order.fee, isUsdt)}</div>
                      <div className={(order.realized_pnl ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}>
                        pnl {order.realized_pnl != null ? fmtSignedPrice(order.realized_pnl, isUsdt) : '-'}
                      </div>
                    </div>
                    {order.signal_reason && (
                      <div className="mt-2 rounded bg-gray-900/70 p-2 text-gray-400">{order.signal_reason}</div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          </div>
          <div className="space-y-3">
            <div className="rounded-lg bg-gray-900/50 p-3">
              <div className="mb-2 text-xs font-medium text-gray-400">거래 요약</div>
              <div className="space-y-1 text-sm text-gray-300">
                <div>status: <span className="text-white">{detail.status}</span></div>
                <div>realized: <span className={detail.realized_pnl >= 0 ? 'text-green-400' : 'text-red-400'}>{fmtSignedPrice(detail.realized_pnl, isUsdt)}</span></div>
                <div>fees: <span className="text-white">{fmtSignedPrice(detail.total_fees, isUsdt)}</span></div>
                <div>closed: <span className="text-white">{formatTs(detail.closed_at, 'MM/dd HH:mm:ss')}</span></div>
              </div>
            </div>
            <div className="rounded-lg bg-gray-900/50 p-3">
              <div className="mb-2 text-xs font-medium text-gray-400">실행 저널</div>
              <div className="space-y-2">
                {detail.journal.length === 0 ? (
                  <div className="text-sm text-gray-500">저널이 없습니다.</div>
                ) : (
                  detail.journal.map((event) => (
                    <div key={event.id} className="rounded border border-gray-700 bg-gray-800/60 p-2 text-xs">
                      <div className="flex items-center justify-between gap-2">
                        <div className="text-white">{event.title}</div>
                        <div className="text-gray-500">{formatTs(event.created_at, 'MM/dd HH:mm:ss')}</div>
                      </div>
                      {event.detail && <div className="mt-1 text-gray-400">{event.detail}</div>}
                      {event.metadata && (
                        <div className="mt-2 flex flex-wrap gap-1">
                          {Object.entries(event.metadata).map(([key, value]) => (
                            <span key={key} className="rounded bg-gray-900 px-1.5 py-0.5 text-[11px] text-gray-400">
                              {key}: {String(value)}
                            </span>
                          ))}
                        </div>
                      )}
                    </div>
                  ))
                )}
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

export function ResearchMonitor({
  exchange,
  onNavigate,
}: {
  exchange?: ExchangeName
  onNavigate?: (tab: 'live' | 'ops') => void
}) {
  const qc = useQueryClient()
  const enabled = exchange === 'binance_futures' || exchange === 'binance_spot'
  const [selectedPairTradeId, setSelectedPairTradeId] = useState<string | null>(null)
  const [selectedDonchianTradeId, setSelectedDonchianTradeId] = useState<string | null>(null)
  const [historyFilter, setHistoryFilter] = useState<string>('all')
  const [pendingApproval, setPendingApproval] = useState<ApprovalDraft | null>(null)
  const [notice, setNotice] = useState<{ kind: 'success' | 'error'; message: string } | null>(null)

  const { data: overview, isLoading: overviewLoading } = useQuery({
    queryKey: ['research', 'overview'],
    queryFn: () => getResearchOverview(),
    staleTime: 10_000,
    refetchInterval: 15_000,
    enabled,
  })

  const { data: reviewStatus } = useQuery({
    queryKey: ['research', 'auto-review', 'status'],
    queryFn: () => getResearchAutoReviewStatus(),
    staleTime: 10_000,
    refetchInterval: 15_000,
    enabled,
  })

  const { data: stageHistory } = useQuery({
    queryKey: ['research', 'stage-history', historyFilter],
    queryFn: () =>
      getResearchStageHistory({
        limit: 20,
        candidate_key: historyFilter === 'all' ? undefined : historyFilter,
      }),
    staleTime: 10_000,
    refetchInterval: 15_000,
    enabled,
  })

  const { data: coordinator } = useQuery({
    queryKey: ['engine', 'futures-rnd', 'status'],
    queryFn: () => getFuturesRndStatus(),
    staleTime: 10_000,
    refetchInterval: 15_000,
    enabled,
  })

  const { data: rndEngines } = useQuery({
    queryKey: ['engine', 'rnd', 'overview'],
    queryFn: () => getRndOverview(),
    staleTime: 10_000,
    refetchInterval: 15_000,
    enabled,
  })

  const { data: pairsGroups } = useQuery({
    queryKey: ['trades', 'pairs', 'groups', 'closed'],
    queryFn: () => getPairsTradeGroups({ status: 'all', size: 5 }),
    staleTime: 10_000,
    refetchInterval: 15_000,
    enabled,
  })

  const { data: donchianGroups } = useQuery({
    queryKey: ['trades', 'donchian-futures', 'groups', 'closed'],
    queryFn: () => getDonchianFuturesTradeGroups({ status: 'all', size: 5 }),
    staleTime: 10_000,
    refetchInterval: 15_000,
    enabled,
  })

  const { data: pairDetail } = useQuery({
    queryKey: ['trades', 'pairs', 'group-detail', selectedPairTradeId],
    queryFn: () => getPairsTradeGroupDetail(selectedPairTradeId!),
    enabled: enabled && !!selectedPairTradeId,
    staleTime: 10_000,
  })

  const { data: donchianDetail } = useQuery({
    queryKey: ['trades', 'donchian-futures', 'group-detail', selectedDonchianTradeId],
    queryFn: () => getDonchianFuturesTradeGroupDetail(selectedDonchianTradeId!),
    enabled: enabled && !!selectedDonchianTradeId,
    staleTime: 10_000,
  })

  if (!enabled) {
    return null
  }

  const rndStatus = coordinator && 'status' in coordinator ? null : (coordinator as FuturesRndStatus | undefined)
  const sortedItems = useMemo(
    () =>
      (overview?.items ?? [])
        .slice()
        .sort((a, b) => {
          const scoreDiff = scoreCandidate(b) - scoreCandidate(a)
          if (scoreDiff !== 0) return scoreDiff
          return a.title.localeCompare(b.title)
        }),
    [overview?.items]
  )
  const latestHistoryByCandidate = useMemo(
    () =>
      new Map((stageHistory ?? []).map((entry) => [entry.candidate_key, entry])),
    [stageHistory]
  )
  const approveMut = useMutation({
    mutationFn: ({ candidateKey, stage, note }: { candidateKey: string; stage: string; note: string }) =>
      updateResearchStage(candidateKey, {
        stage,
        approved_by: 'dashboard_operator',
        note: note || undefined,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['research'] })
      setNotice({ kind: 'success', message: 'stage 변경이 반영되었습니다.' })
      setPendingApproval(null)
    },
    onError: (error: unknown) => {
      const message =
        error instanceof Error
          ? error.message
          : 'stage 변경 중 오류가 발생했습니다.'
      setNotice({ kind: 'error', message })
    },
  })

  return (
    <section className="space-y-3 md:space-y-4">
      {notice && <InlineNotice kind={notice.kind} message={notice.message} onClose={() => setNotice(null)} />}
      <PriorityQueue items={sortedItems} />
      <PromotionBridge items={sortedItems} onNavigate={onNavigate} />

      <div className="rounded-xl border border-gray-700 bg-gray-800 p-4">
        <div className="mb-3 flex items-center justify-between gap-2">
          <div>
            <h3 className="text-base font-semibold text-white">R&D 파이프라인</h3>
            <p className="text-sm text-gray-400">승격 후보, 캐시 상태, live grouped trade를 한 화면에서 봅니다.</p>
          </div>
          {reviewStatus?.ready ? (
            <span className="rounded-full border border-green-700/60 bg-green-900/40 px-2 py-1 text-xs text-green-300">
              auto-review ready
            </span>
          ) : (
            <span className="rounded-full border border-yellow-700/60 bg-yellow-900/30 px-2 py-1 text-xs text-yellow-300">
              auto-review pending
            </span>
          )}
        </div>

        {overviewLoading ? (
          <div className="rounded-lg bg-gray-900/50 p-6 text-center text-sm text-gray-500">R&D 상태 로딩 중...</div>
        ) : (
          <>
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-5">
              <SummaryCard label="Live R&D" value={String(overview?.live_candidates ?? 0)} hint="현재 소액 실거래" />
              <SummaryCard label="Research" value={String(overview?.research_candidates ?? 0)} hint={overview?.recommended_focus} />
              <SummaryCard label="Planned" value={String(overview?.planned_candidates ?? 0)} hint="미연결 후보" />
              <SummaryCard
                label="Auto Review"
                value={reviewStatus?.ready ? `${reviewStatus.candidate_count}/${reviewStatus.total_candidates}` : 'warming'}
                hint={reviewStatus?.last_refresh_at ? `last ${formatTs(reviewStatus.last_refresh_at, 'MM/dd HH:mm:ss')}` : '첫 갱신 대기'}
              />
              <SummaryCard
                label="Futures R&D"
                value={rndStatus ? `${rndStatus.global_available_margin.toFixed(1)} USDT` : 'n/a'}
                hint={rndStatus ? `reserved ${rndStatus.global_reserved_margin.toFixed(1)} / pause ${rndStatus.entry_paused ? 'on' : 'off'}` : 'coordinator 없음'}
              />
            </div>
            {rndEngines?.engines?.length > 0 && (
              <div className="mt-3 grid gap-3 md:grid-cols-3 xl:grid-cols-4">
                {rndEngines.engines.map((eng: any) => (
                  <SummaryCard
                    key={eng.exchange}
                    label={eng.name}
                    value={eng.running ? (eng.paused ? '⏸ paused' : '🟢 running') : '⏹ stopped'}
                    hint={`PnL ${eng.cumulative_pnl?.toFixed(2) ?? '0.00'} / pos ${eng.positions?.length ?? 0} / ${eng.capital ?? 0} USDT`}
                  />
                ))}
              </div>
            )}
          </>
        )}
      </div>

      <div className="grid gap-3 xl:grid-cols-2">
        {sortedItems.map((item) => (
          <CandidateCard
            key={item.key}
            item={item}
            latestHistory={latestHistoryByCandidate.get(item.key)}
            isSubmitting={approveMut.isPending && approveMut.variables?.candidateKey === item.key}
            onApprove={(candidate, stage, note) =>
              setPendingApproval({ item: candidate, targetStage: stage, note })
            }
          />
        ))}
      </div>

      <div className="rounded-xl border border-gray-700 bg-gray-800 p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <div>
            <h3 className="text-base font-semibold text-white">승격/강등 이력</h3>
            <p className="text-sm text-gray-400">최근 승인 변경 로그입니다.</p>
          </div>
          <select
            value={historyFilter}
            onChange={(event) => setHistoryFilter(event.target.value)}
            className="rounded-lg border border-gray-700 bg-gray-900 px-3 py-2 text-sm text-white"
          >
            <option value="all">전체 후보</option>
            {sortedItems.map((item) => (
              <option key={item.key} value={item.key}>
                {item.title}
              </option>
            ))}
          </select>
        </div>
        <StageHistoryPanel entries={stageHistory ?? []} />
      </div>

      <div className="grid gap-3 xl:grid-cols-2">
        <GroupPanel
          title="Pairs Grouped Trades"
          emptyLabel="최근 Pairs grouped trade가 없습니다."
          pairs={pairsGroups}
          onOpenPair={setSelectedPairTradeId}
        />
        <GroupPanel
          title="Donchian Futures Grouped Trades"
          emptyLabel="최근 Donchian futures grouped trade가 없습니다."
          donchian={donchianGroups}
          onOpenDonchian={setSelectedDonchianTradeId}
        />
      </div>

      {pairDetail && (
        <TradeDetailModal
          title="Pairs Group Detail"
          detail={pairDetail}
          isUsdt={true}
          onClose={() => setSelectedPairTradeId(null)}
        />
      )}
      {donchianDetail && (
        <TradeDetailModal
          title="Donchian Futures Group Detail"
          detail={donchianDetail}
          isUsdt={true}
          onClose={() => setSelectedDonchianTradeId(null)}
        />
      )}
      {pendingApproval && (
        <ApprovalConfirmModal
          draft={pendingApproval}
          isSubmitting={approveMut.isPending}
          onConfirm={() =>
            approveMut.mutate({
              candidateKey: pendingApproval.item.key,
              stage: pendingApproval.targetStage,
              note: pendingApproval.note,
            })
          }
          onClose={() => setPendingApproval(null)}
        />
      )}
    </section>
  )
}
