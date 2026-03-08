import asyncio
import structlog
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, case
from core.utils import utcnow

from core.models import Position, PortfolioSnapshot, Trade, Order
from services.market_data import MarketDataService

logger = structlog.get_logger(__name__)


class PortfolioManager:
    """Manages portfolio state, positions, and P&L calculation."""

    def __init__(
        self,
        market_data: MarketDataService,
        initial_balance_krw: float = 500_000,
        is_paper: bool = True,
        exchange_name: str = "bithumb",
    ):
        self._market_data = market_data
        self._initial_balance = initial_balance_krw
        self._cash_balance = initial_balance_krw
        self._is_paper = is_paper
        self._exchange_name = exchange_name
        self._peak_value = initial_balance_krw
        self._realized_pnl = 0.0
        self._peak_already_adjusted = False
        self._sync_lock = asyncio.Lock()  # eval 중 sync 차단
        self._last_total_value: float | None = None  # 스파이크 감지용
        self._snapshot_skip_count = 0  # 연속 스킵 → 실제 변화 강제 기록
        self._last_income_time_ms: int = 0  # Income API 페이지네이션 마커

    @property
    def cash_balance(self) -> float:
        return self._cash_balance

    @cash_balance.setter
    def cash_balance(self, value: float) -> None:
        self._cash_balance = value

    async def update_position_on_buy(
        self, session: AsyncSession, symbol: str, quantity: float, price: float, cost: float, fee: float,
        is_surge: bool = False,
    ) -> None:
        """Update position after a buy trade."""
        from datetime import datetime, timezone
        result = await session.execute(
            select(Position).where(
                Position.symbol == symbol,
                Position.exchange == self._exchange_name,
            )
        )
        position = result.scalar_one_or_none()

        now = datetime.now(timezone.utc)
        if position:
            # Update average buy price
            total_cost = position.average_buy_price * position.quantity + price * quantity
            position.quantity += quantity
            position.average_buy_price = total_cost / position.quantity if position.quantity > 0 else 0
            position.total_invested += cost + fee
            if is_surge:
                position.is_surge = True
            if not position.entered_at:
                position.entered_at = now
            position.last_trade_at = now
        else:
            position = Position(
                exchange=self._exchange_name,
                symbol=symbol,
                quantity=quantity,
                average_buy_price=price,
                total_invested=cost + fee,
                is_paper=self._is_paper,
                is_surge=is_surge,
                entered_at=now,
                last_trade_at=now,
            )
            session.add(position)

        self._cash_balance -= (cost + fee)
        await session.flush()

        logger.info(
            "position_updated_buy",
            symbol=symbol,
            quantity=position.quantity,
            avg_price=position.average_buy_price,
            cash_remaining=self._cash_balance,
        )

    async def update_position_on_sell(
        self, session: AsyncSession, symbol: str, quantity: float, price: float, cost: float, fee: float
    ) -> None:
        """Update position after a sell trade."""
        result = await session.execute(
            select(Position).where(
                Position.symbol == symbol,
                Position.exchange == self._exchange_name,
            )
        )
        position = result.scalar_one_or_none()

        if not position or position.quantity < quantity:
            logger.warning("sell_exceeds_position", symbol=symbol, quantity=quantity)
            return

        old_quantity = position.quantity
        sell_ratio = quantity / old_quantity if old_quantity > 0 else 1.0
        is_futures = "futures" in self._exchange_name

        if is_futures and position.leverage and position.leverage > 1:
            # 선물: margin 기반 정산 — notional이 아닌 마진+레버리지 PnL 반환
            margin_returned = position.total_invested * sell_ratio
            direction = position.direction or "long"
            entry = position.average_buy_price
            if entry and entry > 0:
                if direction == "long":
                    pnl_pct = (price - entry) / entry
                else:
                    pnl_pct = (entry - price) / entry
                leveraged_pnl = margin_returned * position.leverage * pnl_pct
            else:
                leveraged_pnl = 0.0
            realized = leveraged_pnl - fee
            cash_returned = margin_returned + leveraged_pnl - fee
        else:
            # 현물: 기존 방식 (notional 기반)
            sell_proceeds = cost - fee
            buy_cost = position.average_buy_price * quantity
            realized = sell_proceeds - buy_cost
            cash_returned = sell_proceeds

        self._realized_pnl += realized

        position.quantity -= quantity
        now = utcnow()
        if position.quantity <= 0.0001:  # Effectively zero
            position.quantity = 0
            position.average_buy_price = 0
            position.total_invested = 0
            position.is_surge = False
            position.entered_at = None
        else:
            # 부분 매도: total_invested를 남은 비율만큼 축소
            position.total_invested *= (position.quantity / old_quantity)

        # 매매 타이밍 기록 (재시작 시 쿨다운/washout 복원)
        position.last_trade_at = now
        position.last_sell_at = now

        self._cash_balance += cash_returned
        await session.flush()

        logger.info(
            "position_updated_sell",
            symbol=symbol,
            quantity_sold=quantity,
            realized_pnl=round(realized, 4),
            cash_returned=round(cash_returned, 4),
            remaining_quantity=position.quantity,
            is_futures=is_futures,
        )

    async def get_portfolio_summary(self, session: AsyncSession) -> dict:
        """Get current portfolio summary."""
        result = await session.execute(
            select(Position).where(
                Position.quantity > 0,
                Position.exchange == self._exchange_name,
            )
        )
        positions = list(result.scalars().all())

        total_invested = 0.0
        total_current_value = 0.0
        position_details = []

        is_futures = "futures" in self._exchange_name

        for pos in positions:
            try:
                current_price = await self._market_data.get_current_price(pos.symbol)
                notional = pos.quantity * current_price
                entry_notional = pos.average_buy_price * pos.quantity

                # 선물: 숏은 PnL 방향 반전, 현물: 항상 롱
                is_short = is_futures and getattr(pos, "direction", "long") == "short"
                if is_short:
                    unrealized_pnl = entry_notional - notional
                else:
                    unrealized_pnl = notional - entry_notional

                # 선물: 에쿼티 = 마진(total_invested) + 미실현PnL
                # 현물: 에쿼티 = qty × current_price
                if is_futures:
                    current_value = pos.total_invested + unrealized_pnl
                    pnl_base = pos.total_invested  # 마진 대비 수익률
                else:
                    current_value = notional
                    pnl_base = entry_notional

                unrealized_pnl_pct = (
                    (unrealized_pnl / pnl_base * 100) if pnl_base > 0 else 0
                )

                # Update position with current values
                pos.current_value = current_value
                pos.unrealized_pnl = unrealized_pnl
                pos.unrealized_pnl_pct = unrealized_pnl_pct
                pos.updated_at = utcnow()

                total_invested += pos.total_invested
                total_current_value += current_value

                # SL/TP 가격 계산
                sl_pct = getattr(pos, "stop_loss_pct", None)
                tp_pct = getattr(pos, "take_profit_pct", None)
                entry = pos.average_buy_price
                _is_short = is_futures and getattr(pos, "direction", "long") == "short"

                sl_price = None
                tp_price = None
                if sl_pct and entry:
                    sl_price = entry * (1 + sl_pct / 100) if _is_short else entry * (1 - sl_pct / 100)
                if tp_pct and entry:
                    tp_price = entry * (1 - tp_pct / 100) if _is_short else entry * (1 + tp_pct / 100)

                position_details.append({
                    "symbol": pos.symbol,
                    "quantity": pos.quantity,
                    "average_buy_price": pos.average_buy_price,
                    "current_price": current_price,
                    "current_value": current_value,
                    "unrealized_pnl": unrealized_pnl,
                    "unrealized_pnl_pct": round(unrealized_pnl_pct, 2),
                    "direction": getattr(pos, "direction", None),
                    "leverage": getattr(pos, "leverage", None),
                    "liquidation_price": getattr(pos, "liquidation_price", None),
                    "stop_loss_price": round(sl_price, 4) if sl_price else None,
                    "take_profit_price": round(tp_price, 4) if tp_price else None,
                    "trailing_active": getattr(pos, "trailing_active", None),
                    "is_surge": getattr(pos, "is_surge", None),
                })
            except Exception as e:
                logger.warning("price_fetch_failed", symbol=pos.symbol, error=str(e))

        # 총 수수료 / 거래 횟수 집계 (orders + trades 양쪽에서)
        fee_result = await session.execute(
            select(
                func.coalesce(func.sum(Order.fee), 0),
                func.count(Order.id),
            ).where(Order.exchange == self._exchange_name)
        )
        fee_row = fee_result.one()
        total_fees = float(fee_row[0])
        trade_count = int(fee_row[1])
        # orders.fee=0인 경우 trades 테이블에서 보충
        if total_fees == 0 and trade_count > 0:
            trade_fee_result = await session.execute(
                select(func.coalesce(func.sum(Trade.fee), 0))
                .where(Trade.exchange == self._exchange_name)
            )
            total_fees = float(trade_fee_result.scalar())

        total_value = self._cash_balance + total_current_value
        total_unrealized_pnl = total_current_value - total_invested

        # Track peak for drawdown — 스파이크 방어 포함
        if self._last_total_value is None:
            # 첫 호출: 초기화
            self._last_total_value = total_value
        else:
            change_pct = abs(total_value - self._last_total_value) / self._last_total_value * 100 if self._last_total_value > 0 else 0
            if change_pct > 15:
                # 스파이크 감지: peak 업데이트 건너뜀
                logger.warning(
                    "spike_detected_peak_not_updated",
                    exchange=self._exchange_name,
                    last_total=round(self._last_total_value, 2),
                    current_total=round(total_value, 2),
                    change_pct=round(change_pct, 2),
                )
            else:
                # 정상 변동: peak 업데이트 + last_total 갱신
                if total_value > self._peak_value:
                    self._peak_value = total_value
                self._last_total_value = total_value
        drawdown_pct = (
            (self._peak_value - total_value) / self._peak_value * 100
            if self._peak_value > 0 else 0
        )

        # USDT(바이낸스)는 소수점 2자리, KRW(빗썸)는 정수
        dp = 2 if "binance" in self._exchange_name else 0

        return {
            "exchange": self._exchange_name,
            "total_value_krw": round(total_value, dp),
            "cash_balance_krw": round(self._cash_balance, dp),
            "invested_value_krw": round(total_current_value, dp),
            "initial_balance_krw": round(self._initial_balance, dp),
            "realized_pnl": round(self._realized_pnl, dp),
            "unrealized_pnl": round(total_unrealized_pnl, dp),
            "total_pnl": round(self._realized_pnl + total_unrealized_pnl, dp),
            "total_pnl_pct": round(
                (self._realized_pnl + total_unrealized_pnl) / self._initial_balance * 100, 2
            ) if self._initial_balance > 0 else 0,
            "total_fees": round(total_fees, dp),
            "trade_count": trade_count,
            "peak_value": round(self._peak_value, dp),
            "drawdown_pct": round(drawdown_pct, 2),
            "positions": position_details,
        }

    async def take_snapshot(self, session: AsyncSession) -> PortfolioSnapshot | None:
        """Take a portfolio snapshot for historical tracking.

        스파이크 방어 (2중):
        1. 직전 스냅샷 대비 cash_balance 20%+ 급변 → 건너뜀 (sync 오염)
        2. 직전 3개 스냅샷 중앙값 대비 total_value 15%+ 급변 → 건너뜀 (이중계산)
        """
        summary = await self.get_portfolio_summary(session)
        new_total = summary["total_value_krw"]
        new_cash = summary["cash_balance_krw"]

        # 직전 스냅샷 3개 조회 (cash + total + invested 스파이크 감지용, 단일 쿼리)
        prev_result = await session.execute(
            select(
                PortfolioSnapshot.total_value_krw,
                PortfolioSnapshot.cash_balance_krw,
                PortfolioSnapshot.invested_value_krw,
            )
            .where(PortfolioSnapshot.exchange == self._exchange_name)
            .order_by(PortfolioSnapshot.snapshot_at.desc())
            .limit(3)
        )
        prev_rows = prev_result.all()

        if prev_rows:
            is_spike = False

            # 1) Cash spike check (직전 1개)
            #    선물: 내부 장부 기반이므로 cash는 매매로만 변동 → 스킵
            #    현물: 거래소 sync 기반이므로 여전히 필요
            if "futures" not in self._exchange_name:
                prev_cash = prev_rows[0][1]
                if prev_cash is not None and prev_cash > 0:
                    cash_change_pct = abs(new_cash - prev_cash) / prev_cash * 100
                    if cash_change_pct > 20:
                        is_spike = True
                        logger.warning(
                            "snapshot_skipped_cash_spike",
                            exchange=self._exchange_name,
                            prev_cash=round(prev_cash, 2),
                            new_cash=round(new_cash, 2),
                            cash_change_pct=round(cash_change_pct, 1),
                            skip_count=self._snapshot_skip_count + 1,
                        )

            # 2) Total value spike check (중앙값 기준 10% 이상 변동)
            #    시장 변동: cash 불변 + invested/total 변동 → 허용
            #    스파이크: cash 변동(매매) + total 비정상 급등 → 차단
            if not is_spike:
                prev_totals = [r[0] for r in prev_rows if r[0] and r[0] > 0]
                if prev_totals:
                    baseline = sorted(prev_totals)[len(prev_totals) // 2]
                    if baseline > 0:
                        total_change_pct = abs(new_total - baseline) / baseline * 100
                        if total_change_pct > 10:
                            prev_cash_val = prev_rows[0][1] or 0
                            cash_delta_pct = abs(new_cash - prev_cash_val) / baseline * 100 if baseline > 0 else 0
                            if cash_delta_pct > 3:
                                is_spike = True
                                logger.warning(
                                    "snapshot_skipped_total_spike",
                                    exchange=self._exchange_name,
                                    baseline=round(baseline, 2),
                                    new_total=round(new_total, 2),
                                    total_change_pct=round(total_change_pct, 1),
                                    cash_delta_pct=round(cash_delta_pct, 1),
                                    skip_count=self._snapshot_skip_count + 1,
                                )

            # 3) Invested → 0 스파이크 (sync 실패로 포지션이 순간 사라짐)
            #    이전에 invested > 0이었는데 갑자기 0이 되면 sync 오류
            if not is_spike:
                new_invested = summary["invested_value_krw"]
                prev_invested_vals = [r[2] for r in prev_rows if r[2] is not None]
                if prev_invested_vals:
                    prev_invested = prev_invested_vals[0]
                    if prev_invested > 10 and new_invested < 1:
                        is_spike = True
                        logger.warning(
                            "snapshot_skipped_invested_zero",
                            exchange=self._exchange_name,
                            prev_invested=round(prev_invested, 2),
                            new_invested=round(new_invested, 2),
                            skip_count=self._snapshot_skip_count + 1,
                        )

            if is_spike:
                self._snapshot_skip_count += 1
                if self._snapshot_skip_count < 3:
                    return None
                # 3회 연속 스킵 → 일시적 스파이크가 아닌 실제 변화 (포지션 청산 등)
                logger.info(
                    "snapshot_forced_after_consecutive_skips",
                    exchange=self._exchange_name,
                    skip_count=self._snapshot_skip_count,
                    total=round(new_total, 2),
                    cash=round(new_cash, 2),
                )
                self._snapshot_skip_count = 0
            else:
                self._snapshot_skip_count = 0

        snapshot = PortfolioSnapshot(
            exchange=self._exchange_name,
            total_value_krw=new_total,
            cash_balance_krw=new_cash,
            invested_value_krw=summary["invested_value_krw"],
            realized_pnl=summary["realized_pnl"],
            unrealized_pnl=summary["unrealized_pnl"],
            peak_value=summary["peak_value"],
            drawdown_pct=summary["drawdown_pct"],
        )
        session.add(snapshot)
        await session.flush()
        return snapshot

    @staticmethod
    async def cleanup_spike_snapshots(session: AsyncSession, exchange_name: str) -> int:
        """기존 스파이크 스냅샷 자동 보정 (서버 시작 시 실행).

        고립된 이상값만 보정: 좌측 3개 + 우측 3개 이웃이 비슷한 수준인데
        해당 포인트만 10% 이상 벗어나는 경우에만 보정.
        출금/입금으로 인한 레벨 시프트는 건드리지 않음.
        """
        result = await session.execute(
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.exchange == exchange_name)
            .order_by(PortfolioSnapshot.snapshot_at.asc())
        )
        snapshots = list(result.scalars().all())

        if len(snapshots) < 7:
            return 0

        fixed_count = 0
        values = [s.total_value_krw for s in snapshots]

        # Pass 1: 고립 스파이크 감지 및 보정
        for i in range(3, len(snapshots) - 3):
            left = [values[j] for j in range(i - 3, i) if values[j] > 0]
            right = [values[j] for j in range(i + 1, i + 4) if values[j] > 0]

            if len(left) < 2 or len(right) < 2:
                continue

            left_med = sorted(left)[len(left) // 2]
            right_med = sorted(right)[len(right) // 2]

            if left_med <= 0 or right_med <= 0:
                continue

            # 좌우 이웃이 비슷한 수준인지 확인 (10% 이내)
            sides_gap = abs(left_med - right_med) / max(left_med, right_med) * 100
            if sides_gap > 10:
                continue  # 레벨 시프트 — 건너뜀

            # 좌우 모두에서 10% 이상 벗어나면 스파이크
            dev_left = abs(values[i] - left_med) / left_med * 100
            dev_right = abs(values[i] - right_med) / right_med * 100

            if dev_left > 10 and dev_right > 10:
                old_val = values[i]
                corrected = round((left_med + right_med) / 2, 2)
                snapshots[i].total_value_krw = corrected
                values[i] = corrected
                fixed_count += 1
                logger.info(
                    "spike_snapshot_corrected",
                    exchange=exchange_name,
                    snapshot_id=snapshots[i].id,
                    old_total=round(old_val, 2),
                    new_total=corrected,
                    dev_left=round(dev_left, 1),
                    dev_right=round(dev_right, 1),
                )

        if fixed_count > 0:
            await session.flush()
            logger.info("spike_cleanup_complete", exchange=exchange_name, fixed=fixed_count)

        return fixed_count

    async def reconcile_cash_from_db(self, session: AsyncSession) -> None:
        """DB 포지션 기준으로 현금 잔고를 재계산 (인메모리 누수 방지).

        공식: cash = initial_balance - total_invested + realized_pnl - total_fees

        선물/실거래 현물은 건너뜀: sync_exchange_positions(5분)에서 거래소 API 기준으로
        정확한 잔고가 설정됨. 공식 계산은 수수료/슬리피지 누적 오차로 실제 잔고와 괴리.
        paper 모드만 공식 기반 reconcile 적용.
        """
        if "futures" in self._exchange_name:
            return
        if not self._is_paper:
            return

        result = await session.execute(
            select(Position).where(
                Position.quantity > 0,
                Position.exchange == self._exchange_name,
            )
        )
        positions = list(result.scalars().all())
        total_invested = sum(p.total_invested for p in positions)

        # 수수료 합산
        fee_result = await session.execute(
            select(func.coalesce(func.sum(Order.fee), 0))
            .where(Order.exchange == self._exchange_name)
        )
        total_fees = float(fee_result.scalar())
        if total_fees == 0:
            trade_fee_result = await session.execute(
                select(func.coalesce(func.sum(Trade.fee), 0))
                .where(Trade.exchange == self._exchange_name)
            )
            total_fees = float(trade_fee_result.scalar())

        old_cash = self._cash_balance
        self._cash_balance = (
            self._initial_balance - total_invested + self._realized_pnl - total_fees
        )
        if abs(old_cash - self._cash_balance) > 1.0:
            logger.warning(
                "cash_balance_reconciled",
                old=round(old_cash, 2),
                new=round(self._cash_balance, 2),
                diff=round(old_cash - self._cash_balance, 2),
            )

    async def apply_income(self, exchange_adapter) -> float:
        """Income API에서 펀딩비를 가져와 _cash_balance에 반영 (선물 전용).

        COMMISSION은 _parse_order에서 이미 추정 차감되므로 FUNDING_FEE만 반영.
        """
        if "futures" not in self._exchange_name:
            return 0.0

        try:
            from datetime import timedelta
            start_time = self._last_income_time_ms
            if start_time == 0:
                start_time = int(
                    (datetime.now(timezone.utc) - timedelta(hours=8)).timestamp() * 1000
                )

            records = await exchange_adapter.fetch_income(
                income_type="FUNDING_FEE",
                start_time=start_time + 1,
                limit=1000,
            )
            if not records:
                return 0.0

            total_applied = 0.0
            for rec in records:
                amount = rec["income"]
                rec_time = rec["time"]
                total_applied += amount
                if rec_time > self._last_income_time_ms:
                    self._last_income_time_ms = rec_time

            if abs(total_applied) > 0.001:
                self._cash_balance += total_applied
                logger.info(
                    "income_applied",
                    exchange=self._exchange_name,
                    total=round(total_applied, 4),
                    records=len(records),
                    cash_after=round(self._cash_balance, 2),
                )
            return total_applied
        except Exception as e:
            logger.warning("income_fetch_failed", exchange=self._exchange_name, error=str(e))
            return 0.0

    async def initialize_cash_from_exchange(self, exchange_adapter) -> None:
        """서버 시작 시 거래소 실잔고에서 cash 초기화 (선물, 1회성)."""
        if "futures" not in self._exchange_name:
            return
        try:
            balances = await exchange_adapter.fetch_balance()
            cash_bal = balances.get("USDT")
            if not cash_bal:
                return
            raw_positions = await exchange_adapter._exchange.fetch_positions()
            total_margin = 0.0
            total_unrealized = 0.0
            for fp in raw_positions:
                if float(fp.get("contracts", 0) or 0) > 0:
                    total_margin += float(fp.get("initialMargin", 0) or 0)
                    total_unrealized += float(fp.get("unrealizedPnl", 0) or 0)
            wallet = cash_bal.total - total_unrealized
            self._cash_balance = wallet - total_margin
            logger.info(
                "futures_cash_initialized",
                cash=round(self._cash_balance, 2),
                wallet=round(wallet, 2),
                margin=round(total_margin, 2),
            )
        except Exception as e:
            logger.warning("futures_cash_init_failed", error=str(e))

    async def sync_exchange_positions(
        self, session: AsyncSession, exchange_adapter, tracked_coins: list[str]
    ) -> None:
        """거래소 실제 잔고를 DB 포지션과 동기화.

        - 거래소에 보유 중이지만 DB에 없는 코인 → 포지션 생성
        - DB 포지션 수량 vs 거래소 실제 수량 불일치 → 거래소 기준으로 보정
        - 실제 현금 잔고로 cash_balance 갱신
        """
        if self._sync_lock.locked():
            logger.info("sync_skipped_during_eval", exchange=self._exchange_name)
            return

        try:
            balances = await exchange_adapter.fetch_balance()
        except Exception as e:
            logger.warning("sync_exchange_balances_failed", error=str(e))
            return

        # 현금 통화 결정
        if "binance" in self._exchange_name:
            cash_symbol = "USDT"
        else:
            cash_symbol = "KRW"

        # 실제 현금 잔고
        cash_bal = balances.get(cash_symbol)
        actual_cash = cash_bal.free if cash_bal else 0

        # 기존 DB 포지션 조회
        result = await session.execute(
            select(Position).where(Position.exchange == self._exchange_name)
        )
        db_positions = {p.symbol: p for p in result.scalars().all()}

        # tracked_coins에 포함된 심볼 + 실제 잔고가 있는 코인 처리
        synced_count = 0
        total_invested = 0.0

        # 선물: fetch_positions로 실제 마진/방향/레버리지 조회
        is_futures = "futures" in self._exchange_name
        futures_positions = {}
        if is_futures:
            try:
                raw_positions = await exchange_adapter._exchange.fetch_positions()
                for fp in raw_positions:
                    contracts = float(fp.get("contracts", 0) or 0)
                    if contracts > 0:
                        sym = fp.get("symbol", "")
                        futures_positions[sym] = fp
            except Exception as e:
                logger.warning("fetch_futures_positions_failed", error=str(e))

        # 선물: 최근 거래된 포지션의 margin 오버라이트 보호 (grace period 10분)
        # sync가 거래 직후 exchange API에서 일시적으로 틀린 initialMargin을 읽어서
        # total_invested를 오염시키는 것을 방지
        from datetime import datetime, timezone, timedelta
        _margin_grace = timedelta(minutes=10)
        _now_utc = datetime.now(timezone.utc)
        _protected_syms: set[str] = set()
        if is_futures:
            for db_sym, db_pos in db_positions.items():
                if db_pos.last_trade_at and (_now_utc - db_pos.last_trade_at) < _margin_grace:
                    _protected_syms.add(db_sym)

        for symbol, bal in balances.items():
            if symbol == cash_symbol or bal.total <= 0:
                continue

            # 심볼 형식 변환: "ADA" → "ADA/KRW" 또는 "ADA/USDT"
            pair = f"{symbol}/{cash_symbol}"

            # 너무 작은 잔고 무시 (dust)
            try:
                current_price = await self._market_data.get_current_price(pair)
                coin_value = bal.total * current_price
                # 바이낸스: 1 USDT 미만, 빗썸: 1000원 미만 무시
                min_value = 1.0 if "binance" in self._exchange_name else 1000
                if coin_value < min_value:
                    continue
            except Exception:
                continue

            # 선물: 실제 마진/방향/레버리지 가져오기
            fp_data = futures_positions.get(f"{pair}:USDT") if is_futures else None
            if is_futures and fp_data:
                margin = float(fp_data.get("initialMargin", 0) or 0)
                direction = fp_data.get("side", "long")
                raw_lev = fp_data.get("leverage")
                if raw_lev:
                    leverage = int(raw_lev)
                elif margin > 0:
                    # ccxt가 leverage=None 반환 시 notional/margin으로 계산
                    notional = abs(float(fp_data.get("notional", 0) or 0))
                    leverage = max(1, round(notional / margin)) if notional > 0 else 1
                else:
                    leverage = 1
                entry_price = float(fp_data.get("entryPrice", 0) or current_price)
                liq_price = float(fp_data.get("liquidationPrice", 0) or 0) or None
                invested = margin  # 선물: 마진이 실제 투자금
            else:
                margin = 0
                direction = "long"
                leverage = 1
                entry_price = current_price
                liq_price = None
                invested = bal.total * current_price  # 현물: 노셔널

            db_pos = db_positions.get(pair)

            if db_pos is None:
                # DB에 없는 포지션 → 신규 생성 (기존 보유 코인)
                from datetime import datetime, timezone
                new_pos = Position(
                    exchange=self._exchange_name,
                    symbol=pair,
                    quantity=bal.total,
                    average_buy_price=entry_price,
                    total_invested=invested,
                    is_paper=self._is_paper,
                    entered_at=datetime.now(timezone.utc),
                    direction=direction,
                    leverage=leverage,
                    liquidation_price=liq_price,
                    margin_used=margin,
                )
                session.add(new_pos)
                total_invested += new_pos.total_invested
                synced_count += 1
                logger.info(
                    "position_synced_from_exchange",
                    symbol=pair, quantity=bal.total,
                    price=entry_price, invested=round(invested, 2),
                    direction=direction, leverage=leverage,
                )
            elif abs(db_pos.quantity - bal.total) / max(db_pos.quantity, 0.0001) > 0.01:
                # DB 수량과 거래소 수량이 1% 이상 차이 → 거래소 기준으로 보정
                old_qty = db_pos.quantity
                db_pos.quantity = bal.total
                if is_futures and fp_data:
                    if pair not in _protected_syms:
                        db_pos.total_invested = margin
                        db_pos.margin_used = margin
                    db_pos.direction = direction
                    db_pos.leverage = leverage
                    db_pos.liquidation_price = liq_price
                elif old_qty > 0:
                    ratio = bal.total / old_qty
                    db_pos.total_invested *= ratio
                total_invested += db_pos.total_invested
                logger.info(
                    "position_quantity_adjusted",
                    symbol=pair, old=old_qty, new=bal.total,
                )
            else:
                # 수량 일치 — 선물 메타데이터(레버리지/방향/마진) 보정
                if is_futures and fp_data:
                    changed = False
                    if leverage > 1 and getattr(db_pos, "leverage", 1) != leverage:
                        db_pos.leverage = leverage
                        changed = True
                    if direction and getattr(db_pos, "direction", None) != direction:
                        db_pos.direction = direction
                        changed = True
                    if margin > 0 and abs(getattr(db_pos, "margin_used", 0) - margin) > 0.01:
                        if pair not in _protected_syms:
                            db_pos.total_invested = margin
                            db_pos.margin_used = margin
                            changed = True
                    if liq_price and getattr(db_pos, "liquidation_price", None) != liq_price:
                        db_pos.liquidation_price = liq_price
                        changed = True
                    if changed:
                        logger.info("position_metadata_corrected",
                                    symbol=pair, leverage=leverage, direction=direction)
                total_invested += db_pos.total_invested

        # 선물: fetch_positions에만 있고 balances에 없는 포지션 동기화
        if is_futures and futures_positions:
            from datetime import datetime, timezone
            for fp_sym, fp_data in futures_positions.items():
                # fp_sym 형식: "SOL/USDT:USDT" → pair: "SOL/USDT"
                pair = fp_sym.replace(":USDT", "")

                # balances 루프에서 이미 처리된 심볼은 스킵
                base_sym = pair.split("/")[0]
                if base_sym in balances and balances[base_sym].total > 0:
                    continue

                # DB에 이미 있는 포지션 → 메타데이터만 보정
                if pair in db_positions:
                    db_pos = db_positions[pair]
                    fp_margin = float(fp_data.get("initialMargin", 0) or 0)
                    fp_direction = fp_data.get("side", "long")
                    fp_raw_lev = fp_data.get("leverage")
                    if fp_raw_lev:
                        fp_leverage = int(fp_raw_lev)
                    elif fp_margin > 0:
                        fp_notional = abs(float(fp_data.get("notional", 0) or 0))
                        fp_leverage = max(1, round(fp_notional / fp_margin)) if fp_notional > 0 else 1
                    else:
                        fp_leverage = 1
                    fp_liq = float(fp_data.get("liquidationPrice", 0) or 0) or None
                    fp_entry = float(fp_data.get("entryPrice", 0) or 0)
                    fp_contracts = float(fp_data.get("contracts", 0) or 0)

                    changed = False
                    if fp_leverage > 1 and getattr(db_pos, "leverage", 1) != fp_leverage:
                        db_pos.leverage = fp_leverage
                        changed = True
                    if fp_direction and getattr(db_pos, "direction", None) != fp_direction:
                        db_pos.direction = fp_direction
                        changed = True
                    if fp_margin > 0 and abs(getattr(db_pos, "margin_used", 0) - fp_margin) > 0.01:
                        if pair not in _protected_syms:
                            db_pos.total_invested = fp_margin
                            db_pos.margin_used = fp_margin
                            changed = True
                    if fp_liq and getattr(db_pos, "liquidation_price", None) != fp_liq:
                        db_pos.liquidation_price = fp_liq
                        changed = True
                    if fp_entry > 0 and abs(db_pos.average_buy_price - fp_entry) > 0.0001:
                        db_pos.average_buy_price = fp_entry
                        changed = True
                    if fp_contracts > 0 and abs(db_pos.quantity - fp_contracts) / max(db_pos.quantity, 0.0001) > 0.01:
                        db_pos.quantity = fp_contracts
                        changed = True
                    if changed:
                        logger.info("futures_metadata_corrected",
                                    symbol=pair, leverage=fp_leverage,
                                    direction=fp_direction, margin=round(fp_margin, 2))
                    total_invested += db_pos.total_invested
                    continue

                contracts = float(fp_data.get("contracts", 0) or 0)
                if contracts <= 0:
                    continue
                margin = float(fp_data.get("initialMargin", 0) or 0)
                if margin < 1.0:
                    continue
                direction = fp_data.get("side", "long")
                raw_lev = fp_data.get("leverage")
                if raw_lev:
                    leverage = int(raw_lev)
                elif margin > 0:
                    notional = abs(float(fp_data.get("notional", 0) or 0))
                    leverage = max(1, round(notional / margin)) if notional > 0 else 1
                else:
                    leverage = 1
                entry_price = float(fp_data.get("entryPrice", 0) or 0)
                liq_price = float(fp_data.get("liquidationPrice", 0) or 0) or None

                new_pos = Position(
                    exchange=self._exchange_name,
                    symbol=pair,
                    quantity=contracts,
                    average_buy_price=entry_price,
                    total_invested=margin,
                    is_paper=self._is_paper,
                    entered_at=datetime.now(timezone.utc),
                    direction=direction,
                    leverage=leverage,
                    liquidation_price=liq_price,
                    margin_used=margin,
                )
                session.add(new_pos)
                total_invested += margin
                synced_count += 1
                logger.info(
                    "futures_position_synced",
                    symbol=pair, contracts=contracts, direction=direction,
                    margin=round(margin, 2), leverage=leverage,
                    entry_price=entry_price,
                )

        # DB에 있지만 거래소에 없는 포지션 → 수동 매도 등으로 사라진 경우 quantity=0 처리
        exchange_symbols = set()
        for symbol, bal in balances.items():
            if symbol == cash_symbol or bal.total <= 0:
                continue
            exchange_symbols.add(f"{symbol}/{cash_symbol}")
        if is_futures:
            for fp_sym in futures_positions:
                exchange_symbols.add(fp_sym.replace(":USDT", ""))
        for db_sym, db_pos in db_positions.items():
            if db_pos.quantity > 0 and db_sym not in exchange_symbols:
                logger.info(
                    "position_cleared_not_on_exchange",
                    symbol=db_sym, old_qty=db_pos.quantity,
                )
                db_pos.quantity = 0
                synced_count += 1

        await session.flush()

        # 현금 잔고 처리
        old_cash = self._cash_balance

        if is_futures:
            # 선물: 내부 장부 + Income API가 권위적 → cash 덮어쓰기 안 함
            # 감사 로그만 출력 (불일치 모니터링)
            total_unrealized_exchange = sum(
                float(fp.get("unrealizedPnl", 0) or 0)
                for fp in futures_positions.values()
            )
            cash_total = cash_bal.total if cash_bal else 0
            wallet_balance = cash_total - total_unrealized_exchange
            exchange_cash = wallet_balance - total_invested
            diff = abs(exchange_cash - self._cash_balance)
            if diff > 1.0:
                logger.info(
                    "futures_cash_audit",
                    exchange=self._exchange_name,
                    internal_cash=round(self._cash_balance, 2),
                    exchange_cash=round(exchange_cash, 2),
                    diff=round(diff, 2),
                )
        else:
            self._cash_balance = actual_cash

        if synced_count > 0 or abs(old_cash - self._cash_balance) > 1.0:
            logger.info(
                "exchange_positions_synced",
                exchange=self._exchange_name,
                synced=synced_count,
                cash_balance=round(self._cash_balance, 2),
                initial_balance=round(self._initial_balance, 2),
            )

    async def restore_state_from_db(self, session: AsyncSession) -> None:
        """서버 재시작 시 최신 스냅샷에서 peak_value, realized_pnl 복원."""
        result = await session.execute(
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.exchange == self._exchange_name)
            .order_by(PortfolioSnapshot.snapshot_at.desc())
            .limit(1)
        )
        snapshot = result.scalar_one_or_none()
        if snapshot:
            self._peak_value = snapshot.peak_value or self._peak_value
            self._realized_pnl = snapshot.realized_pnl or 0.0
            # 스냅샷의 total_value로 _last_total_value 초기화 (재시작 첫 평가 스파이크 방지)
            self._last_total_value = snapshot.total_value_krw
            # 스냅샷의 peak는 이미 출금 조정이 반영된 값 → 이중 조정 방지
            self._peak_already_adjusted = True
            logger.info(
                "portfolio_state_restored",
                exchange=self._exchange_name,
                peak_value=round(self._peak_value, 2),
                realized_pnl=round(self._realized_pnl, 2),
                last_total_value=round(self._last_total_value, 2),
            )
        else:
            # 첫 실행: peak를 현재 실제 자산으로 설정 (config값보다 낮을 수 있음)
            actual_total = self._cash_balance
            if actual_total > 0:
                self._peak_value = actual_total
            logger.info(
                "no_snapshot_peak_from_actual",
                exchange=self._exchange_name,
                peak_value=round(self._peak_value, 2),
            )

    async def load_initial_balance_from_db(self, session: AsyncSession) -> None:
        """DB CapitalTransaction에서 확정된 입출금 합계로 initial_balance 재계산."""
        from core.models import CapitalTransaction
        result = await session.execute(
            select(
                func.coalesce(func.sum(
                    case((CapitalTransaction.tx_type == "deposit", CapitalTransaction.amount), else_=0)
                ), 0),
                func.coalesce(func.sum(
                    case((CapitalTransaction.tx_type == "withdrawal", CapitalTransaction.amount), else_=0)
                ), 0),
            ).where(
                CapitalTransaction.exchange == self._exchange_name,
                CapitalTransaction.confirmed == True,  # noqa: E712
            )
        )
        deposits, withdrawals = result.one()
        if deposits > 0 or withdrawals > 0:
            old_initial = self._initial_balance
            self._initial_balance = deposits - withdrawals

            # 출금 시 peak_value 비례 조정 (가짜 드로다운 방지)
            # 단, restore_state_from_db()에서 이미 조정된 peak를 복원한 경우 이중 적용 방지
            if old_initial > 0 and withdrawals > 0 and not self._peak_already_adjusted:
                ratio = self._initial_balance / old_initial
                if 0 < ratio < 1:
                    self._peak_value = self._peak_value * ratio
                    logger.info(
                        "peak_adjusted_for_withdrawal",
                        exchange=self._exchange_name,
                        old_peak=round(self._peak_value / ratio, 2),
                        new_peak=round(self._peak_value, 2),
                        ratio=round(ratio, 4),
                    )

            logger.info(
                "initial_balance_from_capital",
                exchange=self._exchange_name,
                deposits=round(deposits, 2),
                withdrawals=round(withdrawals, 2),
                initial_balance=round(self._initial_balance, 2),
            )

    @staticmethod
    async def record_daily_pnl(
        session: AsyncSession, exchange_name: str, target_date=None
    ) -> "DailyPnL | None":
        """해당 일자의 일일 손익을 PortfolioSnapshot + Orders에서 계산하여 DailyPnL에 upsert.

        target_date: date 객체. None이면 어제(UTC) 기준.
        """
        from datetime import date, timedelta, time
        from core.models import DailyPnL

        if target_date is None:
            target_date = (utcnow() - timedelta(days=1)).date()

        day_start = datetime.combine(target_date, time.min, tzinfo=timezone.utc)
        day_end = datetime.combine(target_date + timedelta(days=1), time.min, tzinfo=timezone.utc)

        # 1) 해당 일자의 첫/마지막 스냅샷 → open_value / close_value
        first_snap = await session.execute(
            select(PortfolioSnapshot.total_value_krw)
            .where(
                PortfolioSnapshot.exchange == exchange_name,
                PortfolioSnapshot.snapshot_at >= day_start,
                PortfolioSnapshot.snapshot_at < day_end,
            )
            .order_by(PortfolioSnapshot.snapshot_at.asc())
            .limit(1)
        )
        open_value = first_snap.scalar_one_or_none()

        last_snap = await session.execute(
            select(PortfolioSnapshot.total_value_krw)
            .where(
                PortfolioSnapshot.exchange == exchange_name,
                PortfolioSnapshot.snapshot_at >= day_start,
                PortfolioSnapshot.snapshot_at < day_end,
            )
            .order_by(PortfolioSnapshot.snapshot_at.desc())
            .limit(1)
        )
        close_value = last_snap.scalar_one_or_none()

        if open_value is None or close_value is None:
            logger.info("daily_pnl_no_snapshots", exchange=exchange_name, date=str(target_date))
            return None

        # 입출금 보정: 당일 입출금은 손익이 아님
        from core.models import CapitalTransaction
        cap_result = await session.execute(
            select(
                func.coalesce(func.sum(case(
                    (CapitalTransaction.tx_type == "deposit", CapitalTransaction.amount),
                    else_=0,
                )), 0),
                func.coalesce(func.sum(case(
                    (CapitalTransaction.tx_type == "withdrawal", CapitalTransaction.amount),
                    else_=0,
                )), 0),
            ).where(
                CapitalTransaction.exchange == exchange_name,
                CapitalTransaction.confirmed == True,  # noqa: E712
                CapitalTransaction.source != "seed",   # 시드 입금은 초기 잔고 → 이미 스냅샷에 반영
                CapitalTransaction.created_at >= day_start,
                CapitalTransaction.created_at < day_end,
            )
        )
        deposits, withdrawals = cap_result.one()
        net_inflow = float(deposits) - float(withdrawals)

        daily_pnl_val = close_value - open_value - net_inflow
        daily_pnl_pct = (daily_pnl_val / open_value * 100) if open_value > 0 else 0.0

        # 2) 해당 일자 매매 집계
        order_stats = await session.execute(
            select(
                func.count(Order.id),
                func.coalesce(func.sum(case((Order.side == "buy", 1), else_=0)), 0),
                func.coalesce(func.sum(case((Order.side == "sell", 1), else_=0)), 0),
                func.coalesce(func.sum(Order.fee), 0),
            ).where(
                Order.exchange == exchange_name,
                Order.status == "filled",
                Order.created_at >= day_start,
                Order.created_at < day_end,
            )
        )
        trade_count, buy_count, sell_count, total_fees = order_stats.one()

        # 3) 실현 손익 + 승/패 카운트 (매도 주문의 realized_pnl 기준)
        sell_orders_result = await session.execute(
            select(Order).where(
                Order.exchange == exchange_name,
                Order.side == "sell",
                Order.status == "filled",
                Order.created_at >= day_start,
                Order.created_at < day_end,
            )
        )
        sell_orders = list(sell_orders_result.scalars().all())

        realized_pnl = 0.0
        win_count = 0
        loss_count = 0
        for sell_order in sell_orders:
            if sell_order.realized_pnl is not None:
                realized_pnl += sell_order.realized_pnl
                if sell_order.realized_pnl >= 0:
                    win_count += 1
                else:
                    loss_count += 1
            elif sell_order.executed_price and sell_order.executed_quantity:
                # fallback: realized_pnl 없으면 Position에서 계산
                pos_result = await session.execute(
                    select(Position.average_buy_price).where(
                        Position.symbol == sell_order.symbol,
                        Position.exchange == exchange_name,
                    )
                )
                avg_buy = pos_result.scalar_one_or_none()
                if avg_buy and avg_buy > 0:
                    pnl = (sell_order.executed_price - avg_buy) * sell_order.executed_quantity
                    if sell_order.direction == "short":
                        pnl = -pnl
                    pnl -= sell_order.fee or 0
                    realized_pnl += pnl
                    if pnl >= 0:
                        win_count += 1
                    else:
                        loss_count += 1

        # 4) Upsert
        existing = await session.execute(
            select(DailyPnL).where(
                DailyPnL.exchange == exchange_name,
                DailyPnL.date == target_date,
            )
        )
        record = existing.scalar_one_or_none()

        if record:
            record.open_value = open_value
            record.close_value = close_value
            record.daily_pnl = round(daily_pnl_val, 4)
            record.daily_pnl_pct = round(daily_pnl_pct, 4)
            record.realized_pnl = round(realized_pnl, 4)
            record.total_fees = round(float(total_fees), 4)
            record.trade_count = int(trade_count)
            record.buy_count = int(buy_count)
            record.sell_count = int(sell_count)
            record.win_count = win_count
            record.loss_count = loss_count
        else:
            record = DailyPnL(
                exchange=exchange_name,
                date=target_date,
                open_value=open_value,
                close_value=close_value,
                daily_pnl=round(daily_pnl_val, 4),
                daily_pnl_pct=round(daily_pnl_pct, 4),
                realized_pnl=round(realized_pnl, 4),
                total_fees=round(float(total_fees), 4),
                trade_count=int(trade_count),
                buy_count=int(buy_count),
                sell_count=int(sell_count),
                win_count=win_count,
                loss_count=loss_count,
            )
            session.add(record)

        await session.flush()
        logger.info(
            "daily_pnl_recorded",
            exchange=exchange_name,
            date=str(target_date),
            pnl=round(daily_pnl_val, 2),
            pnl_pct=round(daily_pnl_pct, 2),
            trades=int(trade_count),
        )
        return record

    @property
    def realized_pnl(self) -> float:
        return self._realized_pnl
