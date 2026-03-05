/**
 * 통화 포맷 유틸 — 거래소별 USDT/KRW 자동 분기.
 */

/** USDT 또는 KRW 금액 포맷 (소수점/단위 자동) */
export function fmtPrice(n: number, isUsdt: boolean): string {
  return isUsdt
    ? n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 4 }) + ' USDT'
    : n.toLocaleString('ko-KR') + ' ₩'
}

/** 부호 포함 금액 포맷 (+1,234 ₩ / -0.52 USDT) */
export function fmtSignedPrice(n: number, isUsdt: boolean): string {
  const prefix = n >= 0 ? '+' : ''
  return prefix + fmtPrice(n, isUsdt)
}
