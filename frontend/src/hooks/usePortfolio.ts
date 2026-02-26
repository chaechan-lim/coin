import { useQuery } from '@tanstack/react-query'
import { getPortfolioSummary, getPortfolioHistory } from '../api/client'
import type { ExchangeName } from '../types'

export function usePortfolioSummary(exchange: ExchangeName = 'bithumb') {
  return useQuery({
    queryKey: ['portfolio', 'summary', exchange],
    queryFn: () => getPortfolioSummary(exchange),
    refetchInterval: 30_000,
    staleTime: 15_000,
  })
}

export function usePortfolioHistory(period: string = '7d', exchange: ExchangeName = 'bithumb') {
  return useQuery({
    queryKey: ['portfolio', 'history', period, exchange],
    queryFn: () => getPortfolioHistory(period, exchange),
    refetchInterval: 60_000,
    staleTime: 30_000,
  })
}
