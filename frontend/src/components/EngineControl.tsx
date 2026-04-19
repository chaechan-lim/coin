import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  getDonchianEngineStatus,
  getDonchianFuturesEngineStatus,
  getEngineStatus,
  getFuturesRndStatus,
  getPairsEngineStatus,
  getPortfolioSummary,
  getRndOverview,
  getTrades,
  getTradeSummary,
  startEngine,
  stopEngine,
} from '../api/client'
import type { DonchianSpotStatus, ExchangeName, FuturesRndStatus } from '../types'

export function EngineControl({ liveEvents, exchange = 'bithumb' }: { liveEvents: string[]; exchange?: ExchangeName }) {
  const qc = useQueryClient()
  const isFutures = exchange === 'binance_futures'
  const isSpot = exchange === 'binance_spot'

  const { data: status } = useQuery({
    queryKey: ['engine', 'status', exchange],
    queryFn: () => getEngineStatus(exchange),
    refetchInterval: 10_000,
  })

  const { data: surgeStatus } = useQuery({
    queryKey: ['engine', 'status', 'binance_surge'],
    queryFn: () => getEngineStatus('binance_surge' as ExchangeName),
    refetchInterval: 10_000,
    enabled: isFutures,
  })

  const { data: donchianSpotStatus } = useQuery({
    queryKey: ['engine', 'status', 'binance_donchian'],
    queryFn: () => getDonchianEngineStatus(),
    refetchInterval: 10_000,
    enabled: isSpot,
  })

  const { data: spotPortfolio } = useQuery({
    queryKey: ['portfolio', 'summary', 'binance_spot'],
    queryFn: () => getPortfolioSummary('binance_spot'),
    refetchInterval: 10_000,
    enabled: isSpot,
  })

  const { data: donchianSpotTradeSummary } = useQuery({
    queryKey: ['trades', 'summary', 'binance_donchian', 'today'],
    queryFn: () => getTradeSummary('today', 'binance_donchian'),
    refetchInterval: 10_000,
    enabled: isSpot,
  })

  const { data: donchianFuturesStatus } = useQuery({
    queryKey: ['engine', 'status', 'binance_donchian_futures'],
    queryFn: () => getDonchianFuturesEngineStatus(),
    refetchInterval: 10_000,
    enabled: isFutures,
  })

  const { data: pairsStatus } = useQuery({
    queryKey: ['engine', 'status', 'binance_pairs'],
    queryFn: () => getPairsEngineStatus(),
    refetchInterval: 10_000,
    enabled: isFutures,
  })

  const { data: futuresRndRaw } = useQuery({
    queryKey: ['engine', 'status', 'futures-rnd'],
    queryFn: () => getFuturesRndStatus(),
    refetchInterval: 10_000,
    enabled: isFutures,
  })

  const { data: donchianFuturesTradeSummary } = useQuery({
    queryKey: ['trades', 'summary', 'binance_donchian_futures', 'today'],
    queryFn: () => getTradeSummary('today', 'binance_donchian_futures'),
    refetchInterval: 10_000,
    enabled: isFutures,
  })

  const { data: pairsTradeSummary } = useQuery({
    queryKey: ['trades', 'summary', 'binance_pairs', 'today'],
    queryFn: () => getTradeSummary('today', 'binance_pairs'),
    refetchInterval: 10_000,
    enabled: isFutures,
  })

  const { data: rndOverviewData } = useQuery({
    queryKey: ['rnd', 'overview'],
    queryFn: () => getRndOverview(),
    refetchInterval: 15_000,
    enabled: isFutures || isSpot,
  })

  const { data: fgdcaStatus } = useQuery({
    queryKey: ['engine', 'status', 'binance_fgdca'],
    queryFn: () => getEngineStatus('binance_fgdca' as ExchangeName),
    refetchInterval: 10_000,
    enabled: isSpot,
  })

  const startMut = useMutation({
    mutationFn: () => startEngine(exchange),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['engine'] }),
  })

  const stopMut = useMutation({
    mutationFn: () => stopEngine(exchange),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['engine'] }),
  })

  const isRunning = status?.is_running ?? false
  const isPaper = status?.mode === 'paper'
  const effectiveRunning = isSpot
    ? resolveDonchianSpotRunning(donchianSpotStatus) || isRunning
    : isFutures
      ? Boolean(donchianFuturesStatus?.is_running || pairsStatus?.is_running || isRunning)
      : isRunning
  const modeBadgeLabel = isSpot || isFutures
    ? '실전(R&D)'
    : isPaper
      ? '페이퍼'
      : '실전'
  const modeBadgeTone = isSpot || isFutures
    ? 'bg-cyan-900 text-cyan-300'
    : isPaper
      ? 'bg-blue-900 text-blue-300'
      : 'bg-orange-900 text-orange-300'
  const cardTitle = isFutures || isSpot ? '실운영 상태' : '엔진 상태'
  const cardNote = isFutures
    ? '실운영은 아래 선물 R&D 엔진 카드가 기준입니다. 메인 엔진 제어는 하단으로 내렸습니다.'
    : isSpot
      ? '실운영은 아래 Donchian Spot 카드가 기준입니다. 메인 엔진 제어는 하단으로 내렸습니다.'
      : null
  const futuresRndStatus =
    futuresRndRaw && !('status' in futuresRndRaw)
      ? (futuresRndRaw as FuturesRndStatus)
      : null

  return (
    <div className="bg-gray-800 rounded-xl p-3 md:p-4">
      <div className="mb-3 flex items-center justify-between gap-2 md:mb-4">
        <div className="flex min-w-0 flex-wrap items-center gap-2 md:gap-3">
          <div className="flex items-center gap-2">
            <div className={`h-2.5 w-2.5 shrink-0 rounded-full ${effectiveRunning ? 'bg-green-500 animate-pulse' : 'bg-gray-500'}`} />
            <h3 className="text-sm font-semibold text-white md:text-base">{cardTitle}</h3>
          </div>
          <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${modeBadgeTone}`}>
            {modeBadgeLabel}
          </span>
          {cardNote && <span className="text-[11px] text-gray-500">{cardNote}</span>}
        </div>
      </div>

      {!isSpot && !isFutures && status && (
        <div className="mb-3 grid grid-cols-2 gap-3 text-sm md:grid-cols-4">
          <div>
            <div className="text-xs text-gray-500">오늘 거래</div>
            <div className="font-medium text-white">{status.daily_trade_count}건</div>
          </div>
          <div>
            <div className="text-xs text-gray-500">평가 주기</div>
            <div className="font-medium text-white">{status.evaluation_interval_sec}초</div>
          </div>
          <div>
            <div className="text-xs text-gray-500">활성 전략</div>
            <div className="font-medium text-white">{status.strategies_active.length}개</div>
          </div>
          <div>
            <div className="text-xs text-gray-500">추적 코인</div>
            <div className="font-medium text-white">{status.tracked_coins.length}종</div>
          </div>
        </div>
      )}

      {isSpot && (
        <div className="mb-3 rounded-lg border border-gray-700 bg-gray-900/50 p-3">
          <div className="mb-2">
            <div className="text-xs font-medium text-gray-400">현물 계좌/운용 현황</div>
            <div className="text-[11px] text-gray-500">
              현물 실계좌 잔고와 현재 live R&D 대상인 `Donchian Spot` 상태를 같이 봅니다.
            </div>
          </div>
          <div className="grid gap-2 md:grid-cols-3">
            <StatusCard
              title="Main Spot History"
              badge="actual"
              badgeTone="sky"
              subtitle={formatAmount(spotPortfolio?.total_value_krw)}
              meta={`cash ${formatAmount(spotPortfolio?.cash_balance_krw)} · binance_spot ledger 기준`}
              metrics={[
                { label: '보유 포지션', value: `${spotPortfolio?.positions.length ?? 0}` },
                { label: '누적 손익', value: formatSignedAmount(spotPortfolio?.total_pnl), tone: signedTone(spotPortfolio?.total_pnl ?? 0) },
                { label: '누적 수익률', value: formatSignedPct(spotPortfolio?.total_pnl_pct ?? 0), tone: signedTone(spotPortfolio?.total_pnl_pct ?? 0) },
                { label: '누적 거래수', value: `${spotPortfolio?.trade_count ?? 0}건` },
              ]}
            />
            <StatusCard
              title="Main Spot"
              badge={isRunning ? 'started' : 'stopped'}
              badgeTone={isRunning ? 'green' : 'gray'}
              subtitle={`${status?.tracked_coins.length ?? 0}종 추적`}
              meta={`${status?.strategies_active.length ?? 0}개 전략`}
              emphasis={!isRunning ? 'muted' : 'normal'}
              metrics={[
                { label: '오늘 거래', value: `${status?.daily_trade_count ?? 0}건` },
                { label: '평가 주기', value: `${status?.evaluation_interval_sec ?? 0}초` },
                { label: '모드', value: isPaper ? 'paper' : 'live' },
                { label: '최소 신뢰도', value: `${((status?.min_confidence ?? 0) * 100).toFixed(0)}%` },
              ]}
            />
            <StatusCard
              title="Donchian Spot"
              badge={resolveDonchianSpotRunning(donchianSpotStatus) ? 'started' : 'stopped'}
              badgeTone={resolveDonchianSpotRunning(donchianSpotStatus) ? 'green' : 'gray'}
              subtitle={`${(donchianSpotStatus?.initial_capital ?? 0).toFixed(0)} USDT`}
              meta={buildDonchianSpotMeta(donchianSpotStatus)}
              emphasis="primary"
              metrics={[
                { label: '감시 코인', value: `${donchianSpotStatus?.coins.length ?? 0}종` },
                { label: '오늘 거래', value: `${donchianSpotTradeSummary?.total_trades ?? 0}건` },
                { label: '활성 포지션', value: `${donchianSpotStatus?.active_positions ?? 0}` },
                { label: '누적 손익', value: formatSignedAmount(donchianSpotStatus?.cumulative_pnl), tone: signedTone(donchianSpotStatus?.cumulative_pnl ?? 0) },
              ]}
            />
            <StatusCard
              title="Fear & Greed DCA"
              badge={fgdcaStatus?.is_running ? 'started' : 'stopped'}
              badgeTone={fgdcaStatus?.is_running ? 'green' : 'gray'}
              subtitle="200 USDT · BTC/ETH · 매주 월요일"
              emphasis="primary"
              metrics={[
                { label: '오늘 거래', value: `${fgdcaStatus?.daily_trade_count ?? 0}건` },
              ]}
            />
          </div>
        </div>
      )}

      {isFutures && (
        <div className="mb-3 rounded-lg bg-gray-900/50 p-3">
          <div className="mb-2 flex items-center justify-between gap-2">
            <div className="text-xs font-medium text-gray-400">선물 R&D 엔진 상태</div>
          </div>
          <div className="grid gap-2 md:grid-cols-3">
            {(rndOverviewData?.engines ?? [])
              .filter((e: any) => !['binance_donchian', 'binance_fgdca'].includes(e.exchange))
              .map((eng: any) => (
                <StatusCard
                  key={eng.exchange}
                  title={eng.name}
                  badge={eng.paused ? 'paused' : eng.running ? 'running' : 'stopped'}
                  badgeTone={eng.paused ? 'amber' : eng.running ? 'green' : 'gray'}
                  subtitle={`${eng.capital ?? 0} USDT${eng.leverage > 1 ? ` · ${eng.leverage}x` : ''}`}
                  emphasis="primary"
                  metrics={[
                    { label: '포지션', value: `${eng.positions?.length ?? 0}` },
                    { label: '누적 PnL', value: formatSignedAmount(eng.cumulative_pnl), tone: signedTone(eng.cumulative_pnl ?? 0) },
                    { label: '일일 PnL', value: formatSignedAmount(eng.daily_pnl), tone: signedTone(eng.daily_pnl ?? 0) },
                  ]}
                />
              ))}
          </div>
        </div>
      )}

      {isFutures && futuresRndStatus && (
        <div className="mb-3 rounded-lg bg-gray-900/50 p-3">
          <div className="mb-2 text-xs font-medium text-gray-400">R&D 자본 풀</div>
          <div className="grid gap-2 md:grid-cols-4">
            <StatusCard
              title="R&D Pool"
              badge={futuresRndStatus.entry_paused ? 'paused' : 'open'}
              badgeTone={futuresRndStatus.entry_paused ? 'amber' : 'sky'}
              subtitle={formatUsdtValue(futuresRndStatus.global_capital_usdt)}
              metrics={[
                { label: '가용 마진', value: formatUsdtValue(futuresRndStatus.global_available_margin) },
                { label: '예약 마진', value: formatUsdtValue(futuresRndStatus.global_reserved_margin) },
                { label: '누적 손익', value: formatSignedAmount(futuresRndStatus.global_cumulative_pnl), tone: signedTone(futuresRndStatus.global_cumulative_pnl ?? 0) },
                { label: '예약 심볼', value: `${countReservedSymbols(futuresRndStatus)}` },
              ]}
            />
          </div>
        </div>
      )}

      {isFutures && surgeStatus && (
        <div className="mb-3 flex items-center gap-2 rounded-lg bg-gray-900/50 px-2 py-1.5">
          <div className={`h-2 w-2 shrink-0 rounded-full ${surgeStatus.is_running ? 'bg-cyan-500 animate-pulse' : 'bg-gray-600'}`} />
          <span className="text-xs text-gray-400">서지 엔진</span>
          <span className={`text-xs font-medium ${surgeStatus.is_running ? 'text-cyan-400' : 'text-gray-500'}`}>
            {surgeStatus.is_running ? '활성' : '비활성'}
          </span>
          {surgeStatus.is_running && (
            <span className="text-xs text-gray-500">· {surgeStatus.tracked_coins?.length ?? 0}종 스캔</span>
          )}
        </div>
      )}

      {(isSpot || isFutures) && status && (
        <div className="mb-3 rounded-lg border border-yellow-800/60 bg-yellow-950/20 p-3">
          <div className="mb-2 flex items-start justify-between gap-3">
            <div>
              <div className="text-xs font-medium text-yellow-300">레거시 메인 엔진 제어</div>
              <div className="text-[11px] text-yellow-200/80">
                이 버튼은 현재 실운영 R&D 엔진이 아니라 메인 전략 엔진만 시작/중지합니다.
              </div>
              <div className="mt-1 text-[11px] text-yellow-200/70">
                {isFutures
                  ? '현재 운영 기준에선 binance_futures 메인 엔진은 비권장입니다.'
                  : '현재 운영 기준에선 binance_spot 메인 엔진보다 Donchian Spot 모니터링이 우선입니다.'}
              </div>
            </div>
            <span className="rounded-full bg-yellow-900/60 px-2 py-0.5 text-[11px] font-medium text-yellow-200">
              legacy
            </span>
          </div>
          <div className="mb-3 grid grid-cols-2 gap-3 text-sm md:grid-cols-4">
            <div>
              <div className="text-xs text-gray-500">상태</div>
              <div className="font-medium text-white">{isRunning ? 'started' : 'stopped'}</div>
            </div>
            <div>
              <div className="text-xs text-gray-500">오늘 거래</div>
              <div className="font-medium text-white">{status.daily_trade_count}건</div>
            </div>
            <div>
              <div className="text-xs text-gray-500">평가 주기</div>
              <div className="font-medium text-white">{status.evaluation_interval_sec}초</div>
            </div>
            <div>
              <div className="text-xs text-gray-500">활성 전략</div>
              <div className="font-medium text-white">{status.strategies_active.length}개</div>
            </div>
          </div>
          <div className="flex shrink-0 gap-2">
            <button
              onClick={() => startMut.mutate()}
              disabled={isRunning || startMut.isPending}
              className="rounded-lg bg-green-700 px-3 py-1.5 text-sm font-medium text-white transition-colors hover:bg-green-600 active:bg-green-500 disabled:opacity-40"
            >
              메인 엔진 시작
            </button>
            <button
              onClick={() => stopMut.mutate()}
              disabled={!isRunning || stopMut.isPending}
              className="rounded-lg bg-red-800 px-3 py-1.5 text-sm font-medium text-white transition-colors hover:bg-red-700 active:bg-red-600 disabled:opacity-40"
            >
              메인 엔진 중지
            </button>
          </div>
        </div>
      )}

      <RecentRndTrades />
    </div>
  )
}

function StatusCard({
  title,
  badge,
  badgeTone = 'gray',
  subtitle,
  meta,
  emphasis = 'normal',
  metrics = [],
}: {
  title: string
  badge: string
  subtitle: string
  meta?: string
  emphasis?: 'normal' | 'primary' | 'muted'
  badgeTone?: 'green' | 'gray' | 'amber' | 'sky'
  metrics?: Array<{ label: string; value: string; tone?: string }>
}) {
  const tone =
    emphasis === 'primary'
      ? 'border-cyan-700/60 bg-cyan-950/20'
      : emphasis === 'muted'
        ? 'border-gray-800 bg-gray-900/30'
        : 'border-gray-700 bg-gray-800/70'
  const badgeStyle =
    badgeTone === 'green'
      ? 'bg-green-900/50 text-green-300'
      : badgeTone === 'amber'
        ? 'bg-yellow-900/40 text-yellow-300'
        : badgeTone === 'sky'
          ? 'bg-sky-900/40 text-sky-300'
          : 'bg-gray-700 text-gray-400'

  return (
    <div className={`rounded-lg border p-3 ${tone}`}>
      <div className="flex items-center justify-between gap-2">
        <div className="text-sm font-medium text-white">{title}</div>
        <div className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${badgeStyle}`}>{badge}</div>
      </div>
      <div className="mt-2 text-xs text-gray-400">{subtitle}</div>
      {meta && <div className="mt-1 text-[11px] text-gray-500">{meta}</div>}
      {metrics.length > 0 && (
        <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
          {metrics.map((metric) => (
            <div key={`${title}-${metric.label}`} className="rounded bg-gray-900/40 px-2 py-1.5">
              <div className="text-[11px] text-gray-500">{metric.label}</div>
              <div className={`mt-0.5 font-medium ${metric.tone ?? 'text-white'}`}>{metric.value}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function formatAmount(value: number | undefined): string {
  return `${(value ?? 0).toLocaleString('en-US', { minimumFractionDigits: 1, maximumFractionDigits: 2 })} USDT`
}

function formatUsdtValue(value: number | undefined): string {
  return `${(value ?? 0).toLocaleString('en-US', { minimumFractionDigits: 1, maximumFractionDigits: 1 })} USDT`
}

function formatSignedAmount(value: number | undefined): string {
  const amount = value ?? 0
  const sign = amount > 0 ? '+' : ''
  return `${sign}${amount.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })} USDT`
}

function formatSignedPct(value: number): string {
  const sign = value > 0 ? '+' : ''
  return `${sign}${value.toFixed(2)}%`
}

function formatWinRate(value: number | undefined): string {
  return `${(value ?? 0).toFixed(1)}%`
}

function signedTone(value: number): string {
  if (value > 0) return 'text-green-400'
  if (value < 0) return 'text-red-400'
  return 'text-white'
}

function resolveDonchianSpotRunning(status: DonchianSpotStatus | undefined): boolean {
  if (!status) return false
  return status.is_running ?? true
}

function buildDonchianSpotMeta(status: DonchianSpotStatus | undefined): string {
  if (!status) return '상태 수집 중'
  if (status.paused_total_loss) return '누적 손실 컷으로 일시 정지'
  if (status.paused_daily_loss) return '일일 손실 컷으로 일시 정지'
  return `${status.coins.length}종 감시 중`
}

function buildFuturesMeta(paused?: boolean, dailyPaused?: boolean, engineConflict?: boolean): string {
  if (engineConflict) return '외부 포지션 충돌 감지'
  if (paused) return '누적 손실 컷으로 일시 정지'
  if (dailyPaused) return '일일 손실 컷으로 일시 정지'
  return 'running'
}

function buildPairsMeta(status: {
  coordinator_enabled?: boolean
  paused?: boolean
  daily_paused?: boolean
  engine_conflict?: boolean
} | undefined): string {
  if (!status) return '상태 수집 중'
  if (status.engine_conflict) return '외부 포지션 충돌 감지'
  if (status.paused) return '누적 손실 컷으로 일시 정지'
  if (status.daily_paused) return '일일 손실 컷으로 일시 정지'
  return status.coordinator_enabled ? 'coordinator on' : 'coordinator off'
}

function formatPairLabel(coinA?: string, coinB?: string): string {
  if (!coinA || !coinB) return 'pair'
  return `${coinA.replace('/USDT', '')}/${coinB.replace('/USDT', '')}`
}

function countReservedSymbols(status: FuturesRndStatus | null): number {
  if (!status) return 0
  return Object.values(status.reserved_symbols).reduce((sum, items) => sum + items.length, 0)
}

const ENGINE_LABELS: Record<string, string> = {
  binance_hmm: 'HMM',
  binance_momentum: 'Momentum',
  binance_donchian: 'Donchian',
  binance_donchian_futures: 'DonchianF',
  binance_pairs: 'Pairs',
  binance_fgdca: 'DCA',
  binance_breakout_pb: 'BP',
  binance_vol_mom: 'VolMom',
  binance_btc_neutral: 'BTCneutral',
}

function RecentRndTrades() {
  // R&D 선물 + 현물 최근 거래
  const { data: futuresTrades } = useQuery({
    queryKey: ['trades', 'rnd', 'futures', 'recent'],
    queryFn: () => getTrades({ page: 1, size: 10, exchange: 'binance_futures' as ExchangeName }),
    refetchInterval: 15_000,
  })
  const { data: spotTrades } = useQuery({
    queryKey: ['trades', 'rnd', 'spot', 'recent'],
    queryFn: () => getTrades({ page: 1, size: 5, exchange: 'binance_spot' as ExchangeName }),
    refetchInterval: 15_000,
  })

  const allTrades = [...(futuresTrades ?? []), ...(spotTrades ?? [])]
    .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
    .slice(0, 10)

  return (
    <div>
      <div className="mb-1 text-xs text-gray-500">최근 R&D 거래</div>
      <div className="h-28 overflow-y-auto rounded-lg bg-gray-900 p-2 font-mono text-xs space-y-0.5">
        {allTrades.length === 0 ? (
          <div className="text-gray-600">거래 없음 — 시그널 대기 중</div>
        ) : (
          allTrades.map((t: any, i: number) => {
            const time = new Date(t.created_at).toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' })
            const date = new Date(t.created_at).toLocaleDateString('ko-KR', { month: '2-digit', day: '2-digit' })
            const side = t.side === 'buy' ? '▲매수' : '▼매도'
            const sideColor = t.side === 'buy' ? 'text-green-400' : 'text-red-400'
            const engine = ENGINE_LABELS[t.exchange] || t.exchange
            const pnl = t.realized_pnl && t.realized_pnl !== 0
              ? ` PnL ${t.realized_pnl > 0 ? '+' : ''}${t.realized_pnl.toFixed(2)}`
              : ''
            const pnlColor = t.realized_pnl > 0 ? 'text-green-400' : t.realized_pnl < 0 ? 'text-red-400' : ''
            return (
              <div key={i} className="text-gray-400 flex gap-1">
                <span className="text-gray-600">{date} {time}</span>
                <span className={sideColor}>{side}</span>
                <span>{t.symbol?.replace('/USDT', '')}</span>
                <span className="text-gray-600">@{t.executed_price?.toFixed(1)}</span>
                <span className="text-gray-600">[{engine}]</span>
                {pnl && <span className={pnlColor}>{pnl}</span>}
              </div>
            )
          })
        )}
      </div>
    </div>
  )
}
