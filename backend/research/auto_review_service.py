from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog

from research.evaluator import get_auto_review, serialize_auto_review
from research.registry import RESEARCH_CANDIDATES

logger = structlog.get_logger(__name__)


class ResearchAutoReviewService:
    def __init__(self, engine_registry, *, refresh_interval_sec: int = 900):
        self._engine_registry = engine_registry
        self._refresh_interval_sec = refresh_interval_sec
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._last_refresh_at: datetime | None = None

    def _build_live_context(self, candidate_key: str, venue: str) -> dict[str, Any]:
        eng = self._engine_registry.get_engine(venue) if venue else None
        if candidate_key in {"pairs_trading_futures", "donchian_futures_bi"}:
            return {"live_capital_usdt": getattr(eng, "_initial_capital", None) if eng is not None else None}
        return {}

    async def refresh_all(self) -> None:
        snapshots: dict[str, dict[str, Any]] = {}
        for candidate in RESEARCH_CANDIDATES:
            try:
                review = await get_auto_review(
                    candidate.key,
                    live_context=self._build_live_context(candidate.key, candidate.venue),
                )
                snapshots[candidate.key] = serialize_auto_review(review)
            except Exception as exc:
                logger.warning("research_auto_review_refresh_failed", candidate_key=candidate.key, exc_info=True)
                snapshots[candidate.key] = {
                    "candidate_key": candidate.key,
                    "decision": "error",
                    "recommended_stage": candidate.stage,
                    "summary": f"auto_review_failed: {type(exc).__name__}",
                    "blockers": ["자동 판정 중 예외 발생"],
                    "metrics": [],
                }
        self._snapshots = snapshots
        self._last_refresh_at = datetime.now(timezone.utc)
        logger.info("research_auto_review_refreshed", candidates=len(snapshots))

    def get_snapshot(self, candidate_key: str) -> dict[str, Any] | None:
        return self._snapshots.get(candidate_key)

    def get_status(self) -> dict[str, Any]:
        total_candidates = len(RESEARCH_CANDIDATES)
        ready_candidates = len(self._snapshots)
        pending_candidates = max(total_candidates - ready_candidates, 0)
        age_sec = None
        if self._last_refresh_at is not None:
            age_sec = int((datetime.now(timezone.utc) - self._last_refresh_at).total_seconds())
        return {
            "ready": bool(self._snapshots),
            "candidate_count": ready_candidates,
            "total_candidates": total_candidates,
            "pending_candidates": pending_candidates,
            "last_refresh_at": self._last_refresh_at.isoformat() if self._last_refresh_at else None,
            "refresh_interval_sec": self._refresh_interval_sec,
            "snapshot_age_sec": age_sec,
        }
