import { format } from 'date-fns'

/**
 * UTC 타임스탬프 문자열 → 로컬 Date 변환 (null-safe).
 * null/undefined/빈 문자열 → null 반환.
 */
export function utcToLocal(ts: string | null | undefined): Date | null {
  if (!ts) return null
  const d = new Date(ts.endsWith('Z') ? ts : ts + 'Z')
  return isNaN(d.getTime()) ? null : d
}

/**
 * UTC 타임스탬프 → 포맷 문자열 변환 (null-safe).
 * 실패 시 fallback 문자열 반환.
 */
export function formatTs(ts: string | null | undefined, fmt: string, fallback = '—'): string {
  const d = utcToLocal(ts)
  if (!d) return fallback
  try {
    return format(d, fmt)
  } catch {
    return fallback
  }
}
