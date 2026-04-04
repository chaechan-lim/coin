"""
DerivativesDataService — 선물 파생 데이터 인메모리 캐시.

OI, Mark Price, Premium, Top Trader Long/Short Ratio를 주기적으로 수집하고
인메모리에 캐싱한다. RegimeDetector 및 전략에 주입하여 과열 감지,
squeeze 전략, 청산 버퍼에 활용.
"""

import asyncio
import time
import structlog
from collections import deque
from dataclasses import dataclass

from exchange.base import ExchangeAdapter
from exchange.data_models import OpenInterest, MarkPriceInfo, LongShortRatio

logger = structlog.get_logger(__name__)

# 기본 설정
_DEFAULT_TTL_SEC = 300  # 5분 캐시 TTL
_DEFAULT_HISTORY_HOURS = 24  # 시계열 보관 시간
_DEFAULT_COLLECT_INTERVAL = 300  # 수집 주기 (초)


@dataclass
class DerivativesSnapshot:
    """심볼별 파생 데이터 스냅샷."""

    open_interest: OpenInterest | None = None
    mark_price: MarkPriceInfo | None = None
    long_short_ratio: LongShortRatio | None = None
    updated_at: float = 0.0  # monotonic timestamp


class DerivativesDataService:
    """선물 파생 데이터 인메모리 캐시 + 주기적 수집.

    특징:
    - 심볼별 최신 값 캐시 (dict)
    - OI/MarkPrice 시계열 보관 (deque, 최근 N시간)
    - TTL 기반 staleness 감지
    - 개별 메트릭 실패 시 다른 메트릭에 영향 없음 (graceful degradation)
    """

    def __init__(
        self,
        exchange: ExchangeAdapter,
        ttl_sec: int = _DEFAULT_TTL_SEC,
        history_hours: int = _DEFAULT_HISTORY_HOURS,
        collect_interval: int = _DEFAULT_COLLECT_INTERVAL,
    ):
        self._exchange = exchange
        self._ttl_sec = ttl_sec
        self._history_hours = history_hours
        self._collect_interval = collect_interval

        # 최대 시계열 항목 수 (5분 간격 기준)
        self._max_history = max(1, (history_hours * 3600) // collect_interval)

        # 최신 값 캐시
        self._snapshots: dict[str, DerivativesSnapshot] = {}

        # 시계열 (deque with maxlen)
        self._oi_history: dict[str, deque[OpenInterest]] = {}
        self._mark_history: dict[str, deque[MarkPriceInfo]] = {}

        # 수집 태스크
        self._task: asyncio.Task | None = None
        self._is_running = False
        self._symbols: list[str] = []

    # ── 조회 API (public) ──────────────────────────────

    def get_open_interest(self, symbol: str) -> OpenInterest | None:
        """심볼의 최신 OI 조회."""
        snap = self._snapshots.get(symbol)
        return snap.open_interest if snap else None

    def get_open_interest_history(
        self,
        symbol: str,
        hours: int | None = None,
    ) -> list[OpenInterest]:
        """심볼의 OI 시계열 조회."""
        history = self._oi_history.get(symbol)
        if not history:
            return []
        if hours is None:
            return list(history)
        # 시간 필터링
        cutoff = time.time() - hours * 3600
        return [oi for oi in history if oi.timestamp.timestamp() >= cutoff]

    def get_mark_price(self, symbol: str) -> MarkPriceInfo | None:
        """심볼의 최신 마크 프라이스 + 프리미엄 조회."""
        snap = self._snapshots.get(symbol)
        return snap.mark_price if snap else None

    def get_long_short_ratio(self, symbol: str) -> LongShortRatio | None:
        """심볼의 최신 롱/숏 비율 조회."""
        snap = self._snapshots.get(symbol)
        return snap.long_short_ratio if snap else None

    def get_snapshot(self, symbol: str) -> DerivativesSnapshot | None:
        """심볼의 전체 파생 데이터 스냅샷 조회."""
        return self._snapshots.get(symbol)

    def is_stale(self, symbol: str) -> bool:
        """데이터가 TTL을 초과했는지 확인."""
        snap = self._snapshots.get(symbol)
        if not snap or snap.updated_at == 0.0:
            return True
        return (time.monotonic() - snap.updated_at) > self._ttl_sec

    def status(self) -> dict:
        """서비스 상태 정보 반환 (API/엔진 상태용)."""
        return {
            "is_running": self._is_running,
            "symbols": len(self._symbols),
            "cached_symbols": len(self._snapshots),
        }

    # ── 수집 루프 (async) ──────────────────────────────

    async def start(self, symbols: list[str]) -> None:
        """주기적 수집 시작."""
        if self._is_running:
            return
        self._symbols = symbols
        self._is_running = True

        # 초기 deque 생성
        for s in symbols:
            if s not in self._oi_history:
                self._oi_history[s] = deque(maxlen=self._max_history)
            if s not in self._mark_history:
                self._mark_history[s] = deque(maxlen=self._max_history)

        self._task = asyncio.create_task(
            self._collect_loop(), name="derivatives_collect"
        )
        logger.info(
            "derivatives_service_started",
            symbols=len(symbols),
            interval=self._collect_interval,
            ttl=self._ttl_sec,
        )

    async def stop(self) -> None:
        """수집 중지."""
        self._is_running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("derivatives_service_stopped")

    async def _collect_loop(self) -> None:
        """주기적 수집 루프."""
        # 시작 직후 즉시 첫 수집
        await self._collect_all()
        while self._is_running:
            try:
                await asyncio.sleep(self._collect_interval)
                if not self._is_running:
                    break
                await self._collect_all()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("derivatives_collect_loop_error", error=str(e))
                await asyncio.sleep(30)  # 에러 시 30초 대기 후 재시도

    async def _collect_all(self) -> None:
        """모든 심볼의 파생 데이터 수집. 심볼 간 병렬 처리."""
        t0 = time.monotonic()
        results = await asyncio.gather(
            *(self._collect_symbol(s) for s in self._symbols),
            return_exceptions=True,
        )
        collected = sum(r[0] for r in results if isinstance(r, tuple))
        errors = sum(r[1] for r in results if isinstance(r, tuple))
        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.info(
            "derivatives_collected",
            symbols=len(self._symbols),
            collected=collected,
            errors=errors,
            elapsed_ms=round(elapsed_ms, 1),
        )

    async def _collect_symbol(self, symbol: str) -> tuple[int, int]:
        """단일 심볼의 파생 데이터 수집. Returns (collected, errors)."""
        snap = self._snapshots.get(symbol) or DerivativesSnapshot()
        collected = 0
        errors = 0
        symbol_ok = False

        # OI 수집
        try:
            oi = await self._exchange.fetch_open_interest(symbol)
            snap.open_interest = oi
            self._oi_history[symbol].append(oi)
            collected += 1
            symbol_ok = True
        except Exception as e:
            errors += 1
            logger.debug("derivatives_oi_error", symbol=symbol, error=str(e))

        # Mark Price 수집
        try:
            mp = await self._exchange.fetch_mark_price(symbol)
            snap.mark_price = mp
            self._mark_history[symbol].append(mp)
            collected += 1
            symbol_ok = True
        except Exception as e:
            errors += 1
            logger.debug("derivatives_mark_error", symbol=symbol, error=str(e))

        # Long/Short Ratio 수집
        try:
            ls = await self._exchange.fetch_long_short_ratio(symbol)
            snap.long_short_ratio = ls
            collected += 1
            symbol_ok = True
        except Exception as e:
            errors += 1
            logger.debug("derivatives_ls_ratio_error", symbol=symbol, error=str(e))

        if symbol_ok:
            snap.updated_at = time.monotonic()
        self._snapshots[symbol] = snap
        return collected, errors
