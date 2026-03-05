import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { getServerEvents } from '../api/client'
import type { ServerEvent } from '../types'
import { formatTs } from '../utils/date'

const LEVEL_STYLES: Record<string, { bg: string; text: string }> = {
  info: { bg: 'bg-blue-900/40', text: 'text-blue-400' },
  warning: { bg: 'bg-yellow-900/40', text: 'text-yellow-400' },
  error: { bg: 'bg-red-900/40', text: 'text-red-400' },
  critical: { bg: 'bg-red-900/60', text: 'text-red-300' },
}

const CATEGORY_STYLES: Record<string, string> = {
  engine: 'bg-purple-800/50 text-purple-300',
  trade: 'bg-green-800/50 text-green-300',
  futures_trade: 'bg-emerald-800/50 text-emerald-300',
  risk: 'bg-orange-800/50 text-orange-300',
  rotation: 'bg-cyan-800/50 text-cyan-300',
  strategy: 'bg-indigo-800/50 text-indigo-300',
  signal: 'bg-blue-800/50 text-blue-300',
  health: 'bg-teal-800/50 text-teal-300',
  recovery: 'bg-amber-800/50 text-amber-300',
  system: 'bg-gray-700/50 text-gray-300',
}

const LEVELS = ['all', 'info', 'warning', 'error', 'critical'] as const
const CATEGORIES = ['all', 'engine', 'trade', 'futures_trade', 'risk', 'rotation', 'strategy', 'signal', 'health', 'recovery', 'system'] as const

interface SystemLogProps {
  realtimeEvents?: ServerEvent[]
}

export function SystemLog({ realtimeEvents = [] }: SystemLogProps) {
  const [page, setPage] = useState(1)
  const [levelFilter, setLevelFilter] = useState<string>('all')
  const [categoryFilter, setCategoryFilter] = useState<string>('all')
  const [expandedId, setExpandedId] = useState<number | null>(null)
  const size = 30

  const { data: events = [], isLoading } = useQuery({
    queryKey: ['serverEvents', page, levelFilter, categoryFilter],
    queryFn: () =>
      getServerEvents({
        page,
        size,
        level: levelFilter === 'all' ? undefined : levelFilter,
        category: categoryFilter === 'all' ? undefined : categoryFilter,
      }),
    staleTime: 15_000,
    refetchInterval: 30_000,
  })

  // Merge realtime events (prepend, deduplicate by id)
  const existingIds = new Set(events.map((e) => e.id))
  const merged =
    page === 1
      ? [...realtimeEvents.filter((e) => !existingIds.has(e.id)), ...events]
      : events

  return (
    <div className="space-y-4">
      {/* Filters */}
      <div className="flex gap-3 flex-wrap">
        <select
          value={levelFilter}
          onChange={(e) => { setLevelFilter(e.target.value); setPage(1) }}
          className="bg-gray-800 border border-gray-600 rounded px-3 py-1.5 text-sm"
        >
          {LEVELS.map((l) => (
            <option key={l} value={l}>
              {l === 'all' ? '전체 레벨' : l.toUpperCase()}
            </option>
          ))}
        </select>

        <select
          value={categoryFilter}
          onChange={(e) => { setCategoryFilter(e.target.value); setPage(1) }}
          className="bg-gray-800 border border-gray-600 rounded px-3 py-1.5 text-sm"
        >
          {CATEGORIES.map((c) => (
            <option key={c} value={c}>
              {c === 'all' ? '전체 카테고리' : c}
            </option>
          ))}
        </select>
      </div>

      {/* Event List */}
      <div className="bg-gray-800 rounded-lg border border-gray-700 divide-y divide-gray-700">
        {isLoading ? (
          <div className="p-8 text-center text-gray-500">로딩 중...</div>
        ) : merged.length === 0 ? (
          <div className="p-8 text-center text-gray-500">이벤트가 없습니다</div>
        ) : (
          merged.map((ev) => (
            <EventRow
              key={ev.id}
              event={ev}
              expanded={expandedId === ev.id}
              onToggle={() => setExpandedId(expandedId === ev.id ? null : ev.id)}
            />
          ))
        )}
      </div>

      {/* Pagination */}
      <div className="flex justify-between items-center">
        <button
          onClick={() => setPage((p) => Math.max(1, p - 1))}
          disabled={page === 1}
          className="px-4 py-2 text-sm bg-gray-700 rounded disabled:opacity-40 hover:bg-gray-600 active:bg-gray-500"
        >
          이전
        </button>
        <span className="text-sm text-gray-400">페이지 {page}</span>
        <button
          onClick={() => setPage((p) => p + 1)}
          disabled={events.length < size}
          className="px-4 py-2 text-sm bg-gray-700 rounded disabled:opacity-40 hover:bg-gray-600 active:bg-gray-500"
        >
          다음
        </button>
      </div>
    </div>
  )
}

function EventRow({
  event: ev,
  expanded,
  onToggle,
}: {
  event: ServerEvent
  expanded: boolean
  onToggle: () => void
}) {
  const ls = LEVEL_STYLES[ev.level] ?? LEVEL_STYLES.info
  const cs = CATEGORY_STYLES[ev.category] ?? CATEGORY_STYLES.system
  const hasExtra = ev.detail || ev.metadata

  return (
    <div className="px-3 md:px-4 py-3">
      <div
        className={`${hasExtra ? 'cursor-pointer' : ''}`}
        onClick={hasExtra ? onToggle : undefined}
      >
        {/* Desktop layout */}
        <div className="hidden sm:flex items-center gap-3">
          <span className={`text-xs font-semibold px-2 py-0.5 rounded ${ls.bg} ${ls.text} uppercase min-w-[60px] text-center`}>
            {ev.level}
          </span>
          <span className={`text-xs px-2 py-0.5 rounded ${cs} min-w-[64px] text-center`}>
            {ev.category}
          </span>
          <span className="flex-1 text-sm">{ev.title}</span>
          <span className="text-xs text-gray-500 whitespace-nowrap">
            {formatTs(ev.created_at, 'MM/dd HH:mm:ss')}
          </span>
          {hasExtra && (
            <span className="text-gray-500 text-xs">{expanded ? '▼' : '▶'}</span>
          )}
        </div>
        {/* Mobile layout - stacked */}
        <div className="sm:hidden space-y-1">
          <div className="flex items-center gap-1.5">
            <span className={`text-xs font-semibold px-1.5 py-0.5 rounded ${ls.bg} ${ls.text} uppercase`}>
              {ev.level}
            </span>
            <span className={`text-xs px-1.5 py-0.5 rounded ${cs}`}>
              {ev.category}
            </span>
            <span className="ml-auto text-xs text-gray-500">
              {formatTs(ev.created_at, 'HH:mm:ss')}
            </span>
            {hasExtra && (
              <span className="text-gray-500 text-xs">{expanded ? '▼' : '▶'}</span>
            )}
          </div>
          <div className="text-sm text-gray-200">{ev.title}</div>
        </div>
      </div>

      {/* Expanded detail */}
      {expanded && (
        <div className="mt-2 sm:ml-[140px] space-y-1">
          {ev.detail && (
            <p className="text-xs text-gray-400 whitespace-pre-wrap">{ev.detail}</p>
          )}
          {ev.metadata && (
            <div className="flex flex-wrap gap-1.5">
              {Object.entries(ev.metadata).map(([k, v]) => (
                <span key={k} className="text-xs bg-gray-700 px-2 py-0.5 rounded">
                  {k}: {String(v)}
                </span>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
