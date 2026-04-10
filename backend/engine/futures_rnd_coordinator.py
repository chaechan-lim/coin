from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class PendingReservation:
    token: str
    symbols: set[str]
    margin: float
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class EngineRiskState:
    capital_limit: float
    confirmed_symbols: set[str] = field(default_factory=set)
    confirmed_margin: float = 0.0
    cumulative_pnl: float = 0.0
    daily_pnl: float = 0.0
    pending: dict[str, PendingReservation] = field(default_factory=dict)


class FuturesRndCoordinator:
    """공용 선물 R&D 리스크 레이어.

    - 심볼 예약 충돌 방지
    - 전역/엔진별 마진 버짓 제한
    - 전역 일일/누적 손실 한도 초과 시 신규 진입 차단
    """

    def __init__(
        self,
        global_capital_usdt: float,
        daily_loss_limit_pct: float = 0.05,
        total_loss_limit_pct: float = 0.10,
    ):
        self._global_capital = float(global_capital_usdt)
        self._daily_loss_limit_pct = float(daily_loss_limit_pct)
        self._total_loss_limit_pct = float(total_loss_limit_pct)
        self._lock = asyncio.Lock()
        self._states: dict[str, EngineRiskState] = {}
        self._last_day = datetime.now(timezone.utc).date()
        self._paused = False

    def _roll_day(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today == self._last_day:
            return
        self._last_day = today
        for state in self._states.values():
            state.daily_pnl = 0.0
        self._paused = False

    def _get_state(self, engine_name: str, capital_limit: float) -> EngineRiskState:
        state = self._states.get(engine_name)
        if state is None:
            state = EngineRiskState(capital_limit=float(capital_limit))
            self._states[engine_name] = state
        else:
            state.capital_limit = float(capital_limit)
        return state

    def _state_total_margin(self, state: EngineRiskState) -> float:
        return state.confirmed_margin + sum(item.margin for item in state.pending.values())

    def _global_margin(self) -> float:
        return sum(self._state_total_margin(state) for state in self._states.values())

    def _global_cumulative_pnl(self) -> float:
        return sum(state.cumulative_pnl for state in self._states.values())

    def _global_daily_pnl(self) -> float:
        return sum(state.daily_pnl for state in self._states.values())

    def _reserved_symbols(self) -> dict[str, str]:
        owners: dict[str, str] = {}
        for engine_name, state in self._states.items():
            for symbol in state.confirmed_symbols:
                owners[symbol] = engine_name
            for pending in state.pending.values():
                for symbol in pending.symbols:
                    owners.setdefault(symbol, engine_name)
        return owners

    def _refresh_pause_state(self) -> None:
        total_loss = -self._global_capital * self._total_loss_limit_pct
        daily_loss = -self._global_capital * self._daily_loss_limit_pct
        self._paused = (
            self._global_cumulative_pnl() <= total_loss
            or self._global_daily_pnl() <= daily_loss
        )

    async def register_engine(self, engine_name: str, capital_limit: float) -> None:
        async with self._lock:
            self._roll_day()
            self._get_state(engine_name, capital_limit)
            self._refresh_pause_state()

    async def request_reservation(
        self,
        engine_name: str,
        capital_limit: float,
        symbols: list[str],
        margin_required: float,
    ) -> tuple[bool, str, str | None]:
        async with self._lock:
            self._roll_day()
            state = self._get_state(engine_name, capital_limit)
            self._refresh_pause_state()
            if self._paused:
                return False, "global_loss_limit_reached", None

            requested_symbols = set(symbols)
            owners = self._reserved_symbols()
            conflicts = sorted(symbol for symbol in requested_symbols if symbol in owners and owners[symbol] != engine_name)
            if conflicts:
                return False, f"symbol_reserved:{','.join(conflicts)}", None

            next_engine_margin = self._state_total_margin(state) + float(margin_required)
            if next_engine_margin > state.capital_limit + 1e-9:
                return False, "engine_cap_exceeded", None

            next_global_margin = self._global_margin() + float(margin_required)
            if next_global_margin > self._global_capital + 1e-9:
                return False, "global_cap_exceeded", None

            token = uuid.uuid4().hex[:12]
            state.pending[token] = PendingReservation(
                token=token,
                symbols=requested_symbols,
                margin=float(margin_required),
            )
            return True, "reserved", token

    async def try_reserve_entry(
        self,
        engine_name: str,
        symbols: list[str],
        margin_required: float,
    ) -> tuple[bool, str]:
        capital_limit = self._states.get(engine_name).capital_limit if engine_name in self._states else self._global_capital
        ok, reason, _token = await self.request_reservation(engine_name, capital_limit, symbols, margin_required)
        return ok, reason

    async def release_reservation(self, engine_name: str, token: str | None) -> None:
        if token is None:
            return
        async with self._lock:
            state = self._states.get(engine_name)
            if state is None:
                return
            state.pending.pop(token, None)
            self._refresh_pause_state()

    async def sync_engine_state(
        self,
        engine_name: str,
        symbols: list[str],
        reserved_margin: float,
        cumulative_pnl: float,
        daily_pnl: float,
        capital_limit: float | None = None,
        reservation_token: str | None = None,
    ) -> None:
        async with self._lock:
            self._roll_day()
            effective_capital_limit = capital_limit
            if effective_capital_limit is None:
                effective_capital_limit = self._states.get(engine_name).capital_limit if engine_name in self._states else self._global_capital
            state = self._get_state(engine_name, effective_capital_limit)
            if reservation_token is not None:
                state.pending.pop(reservation_token, None)
            state.confirmed_symbols = set(symbols)
            state.confirmed_margin = float(reserved_margin)
            state.cumulative_pnl = float(cumulative_pnl)
            state.daily_pnl = float(daily_pnl)
            self._refresh_pause_state()

    async def note_pnl(self, engine_name: str, pnl_delta: float) -> None:
        async with self._lock:
            self._roll_day()
            state = self._states.get(engine_name)
            if state is None:
                state = self._get_state(engine_name, self._global_capital)
            state.cumulative_pnl += float(pnl_delta)
            state.daily_pnl += float(pnl_delta)
            self._refresh_pause_state()

    async def get_status(self) -> dict:
        async with self._lock:
            self._roll_day()
            self._refresh_pause_state()
            return {
                "global_capital_usdt": round(self._global_capital, 2),
                "global_reserved_margin": round(self._global_margin(), 2),
                "global_available_margin": round(max(0.0, self._global_capital - self._global_margin()), 2),
                "global_cumulative_pnl": round(self._global_cumulative_pnl(), 2),
                "global_daily_pnl": round(self._global_daily_pnl(), 2),
                "daily_loss_limit_pct": self._daily_loss_limit_pct,
                "total_loss_limit_pct": self._total_loss_limit_pct,
                "entry_paused": self._paused,
                "reserved_symbols": self._reserved_symbols(),
                "engines": {
                    engine_name: {
                        "capital_limit": round(state.capital_limit, 2),
                        "confirmed_symbols": sorted(state.confirmed_symbols),
                        "confirmed_margin": round(state.confirmed_margin, 2),
                        "pending_margin": round(sum(item.margin for item in state.pending.values()), 2),
                        "pending_symbols": sorted({symbol for item in state.pending.values() for symbol in item.symbols}),
                        "cumulative_pnl": round(state.cumulative_pnl, 2),
                        "daily_pnl": round(state.daily_pnl, 2),
                    }
                    for engine_name, state in sorted(self._states.items())
                },
            }
