import { useQuery } from '@tanstack/react-query'
import { getPortfolioSummary, getPortfolioHistory } from '../api/client'

export function usePortfolioSummary() {
  return useQuery({
    queryKey: ['portfolio', 'summary'],
    queryFn: getPortfolioSummary,
    refetchInterval: 30_000,
    staleTime: 15_000,
  })
}

export function usePortfolioHistory(period: string = '7d') {
  return useQuery({
    queryKey: ['portfolio', 'history', period],
    queryFn: () => getPortfolioHistory(period),
    refetchInterval: 60_000,
    staleTime: 30_000,
  })
}
