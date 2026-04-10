from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import ResearchCandidateState, ResearchCandidateStageHistory
from db.session import get_session_factory
from research.registry import (
    RESEARCH_STAGES,
    ResearchCandidate,
    get_candidate,
    get_candidate_by_venue,
    get_stage_rule,
    is_execution_allowed_stage,
)


BOOTSTRAP_APPROVED_STAGES: dict[str, tuple[str, str]] = {
    "donchian_daily_spot": ("live_rnd", "기존 현물 Donchian 라이브 운영 유지"),
    "pairs_trading_futures": ("live_rnd", "기존 Pairs live R&D 운영 유지"),
    "donchian_futures_bi": ("live_rnd", "기존 Donchian Futures live R&D 운영 유지"),
}


@dataclass(frozen=True)
class ResearchStageSnapshot:
    candidate_key: str
    title: str
    venue: str
    catalog_stage: str
    effective_stage: str
    approved_stage: str | None
    stage_source: str
    execution_allowed: bool
    approved_by: str | None
    approval_note: str | None
    approved_at: datetime | None


@dataclass(frozen=True)
class ResearchStageHistoryEntry:
    id: int
    candidate_key: str
    title: str
    from_stage: str | None
    to_stage: str
    approval_source: str
    approved_by: str | None
    approval_note: str | None
    created_at: datetime


class ResearchStageGateService:
    def __init__(self, session_factory=None, engine_registry=None):
        self._session_factory = session_factory or get_session_factory()
        self._engine_registry = engine_registry

    async def ensure_bootstrap_states(self, candidate_keys: set[str] | None = None) -> None:
        async with self._session_factory() as session:
            rows = (
                await session.execute(select(ResearchCandidateState))
            ).scalars().all()
            existing = {row.candidate_key for row in rows}
            for candidate_key, (stage, note) in BOOTSTRAP_APPROVED_STAGES.items():
                if candidate_keys is not None and candidate_key not in candidate_keys:
                    continue
                if candidate_key in existing:
                    continue
                session.add(
                    ResearchCandidateState(
                        candidate_key=candidate_key,
                        approved_stage=stage,
                        approval_source="bootstrap",
                        approved_by="system_bootstrap",
                        approval_note=note,
                    )
                )
            await session.commit()

    async def list_snapshots(self) -> list[ResearchStageSnapshot]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(select(ResearchCandidateState))
            ).scalars().all()
        state_map = {row.candidate_key: row for row in rows}
        return [self._build_snapshot(candidate, state_map.get(candidate.key)) for candidate in self._iter_candidates()]

    async def list_history(
        self,
        *,
        candidate_key: str | None = None,
        limit: int = 50,
    ) -> list[ResearchStageHistoryEntry]:
        async with self._session_factory() as session:
            stmt = select(ResearchCandidateStageHistory).order_by(ResearchCandidateStageHistory.created_at.desc())
            if candidate_key:
                stmt = stmt.where(ResearchCandidateStageHistory.candidate_key == candidate_key)
            rows = (await session.execute(stmt.limit(limit))).scalars().all()
        return [self._build_history_entry(row) for row in rows]

    async def get_snapshot(self, candidate_key: str) -> ResearchStageSnapshot:
        candidate = get_candidate(candidate_key)
        async with self._session_factory() as session:
            row = await self._get_state_row(session, candidate_key)
        return self._build_snapshot(candidate, row)

    async def get_snapshot_for_venue(self, venue: str) -> ResearchStageSnapshot | None:
        candidate = get_candidate_by_venue(venue)
        if candidate is None:
            return None
        return await self.get_snapshot(candidate.key)

    async def get_effective_stage(self, candidate_key: str) -> str:
        return (await self.get_snapshot(candidate_key)).effective_stage

    async def is_execution_allowed_for_venue(self, venue: str) -> bool:
        snapshot = await self.get_snapshot_for_venue(venue)
        if snapshot is None:
            return True
        return snapshot.execution_allowed

    async def approve_stage(
        self,
        candidate_key: str,
        target_stage: str,
        *,
        approved_by: str | None = None,
        approval_note: str | None = None,
        approval_source: str = "manual",
    ) -> ResearchStageSnapshot:
        if target_stage not in RESEARCH_STAGES:
            raise ValueError(f"Invalid stage: {target_stage}")

        candidate = get_candidate(candidate_key)
        async with self._session_factory() as session:
            row = await self._get_state_row(session, candidate_key)
            current_stage = row.approved_stage if row is not None else candidate.stage
            if target_stage != current_stage:
                allowed_targets = set(get_stage_rule(current_stage).next_stages)
                if target_stage not in allowed_targets:
                    raise ValueError(
                        f"Invalid stage transition: {current_stage} -> {target_stage}"
                    )
            await self._enforce_runtime_gate(candidate, target_stage)

            if row is None:
                row = ResearchCandidateState(candidate_key=candidate_key, approved_stage=target_stage)
                session.add(row)
            row.approved_stage = target_stage
            row.approved_by = approved_by
            row.approval_note = approval_note
            row.approval_source = approval_source
            session.add(
                ResearchCandidateStageHistory(
                    candidate_key=candidate_key,
                    from_stage=current_stage,
                    to_stage=target_stage,
                    approval_source=approval_source,
                    approved_by=approved_by,
                    approval_note=approval_note,
                )
            )
            await session.commit()
            await session.refresh(row)
            return self._build_snapshot(candidate, row)

    async def auto_demote(
        self,
        candidate_key: str,
        target_stage: str,
        *,
        reason: str,
        approved_by: str = "risk_guard",
    ) -> ResearchStageSnapshot:
        return await self.approve_stage(
            candidate_key,
            target_stage,
            approved_by=approved_by,
            approval_note=reason,
            approval_source="risk_auto",
        )

    async def _get_state_row(self, session, candidate_key: str) -> ResearchCandidateState | None:
        return (
            await session.execute(
                select(ResearchCandidateState).where(
                    ResearchCandidateState.candidate_key == candidate_key
                )
            )
        ).scalar_one_or_none()

    def _build_snapshot(
        self,
        candidate: ResearchCandidate,
        state: ResearchCandidateState | None,
    ) -> ResearchStageSnapshot:
        effective_stage = state.approved_stage if state is not None else candidate.stage
        return ResearchStageSnapshot(
            candidate_key=candidate.key,
            title=candidate.title,
            venue=candidate.venue,
            catalog_stage=candidate.stage,
            effective_stage=effective_stage,
            approved_stage=state.approved_stage if state is not None else None,
            stage_source=state.approval_source if state is not None else "catalog",
            execution_allowed=is_execution_allowed_stage(effective_stage),
            approved_by=state.approved_by if state is not None else None,
            approval_note=state.approval_note if state is not None else None,
            approved_at=state.updated_at if state is not None else None,
        )

    def _build_history_entry(self, row: ResearchCandidateStageHistory) -> ResearchStageHistoryEntry:
        candidate = get_candidate(row.candidate_key)
        return ResearchStageHistoryEntry(
            id=row.id,
            candidate_key=row.candidate_key,
            title=candidate.title,
            from_stage=row.from_stage,
            to_stage=row.to_stage,
            approval_source=row.approval_source,
            approved_by=row.approved_by,
            approval_note=row.approval_note,
            created_at=row.created_at,
        )

    async def _enforce_runtime_gate(self, candidate: ResearchCandidate, target_stage: str) -> None:
        if self._engine_registry is None:
            return
        if not candidate.stage_managed:
            return
        if is_execution_allowed_stage(target_stage):
            return
        eng = self._engine_registry.get_engine(candidate.venue)
        if eng is None or not getattr(eng, "is_running", False):
            return
        await eng.stop()

    @staticmethod
    def _iter_candidates():
        from research.registry import RESEARCH_CANDIDATES

        return RESEARCH_CANDIDATES
