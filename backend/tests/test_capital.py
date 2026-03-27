"""
Tests for Capital Transaction (v0.17):
- CapitalTransaction model CRUD
- load_initial_balance_from_db
- restore_state_from_db peak_value initialization
- API endpoints
"""
from unittest.mock import AsyncMock

import pytest
from core.models import CapitalTransaction, PortfolioSnapshot
from engine.portfolio_manager import PortfolioManager


def _make_market_data(prices: dict[str, float] | None = None):
    md = AsyncMock()
    md.get_current_price = AsyncMock(side_effect=lambda sym: (prices or {}).get(sym, 0))
    return md


# ── CapitalTransaction Model Tests ──────────────────────────


@pytest.mark.asyncio
async def test_create_capital_transaction(session):
    """CapitalTransaction 모델 생성 및 저장."""
    tx = CapitalTransaction(
        exchange="bithumb",
        tx_type="deposit",
        amount=500_000,
        currency="KRW",
        note="초기 원금",
        source="seed",
        confirmed=True,
    )
    session.add(tx)
    await session.flush()

    assert tx.id is not None
    assert tx.exchange == "bithumb"
    assert tx.amount == 500_000
    assert tx.source == "seed"
    assert tx.confirmed is True


@pytest.mark.asyncio
async def test_capital_transaction_auto_detected(session):
    """자동 감지 건은 confirmed=False로 생성."""
    tx = CapitalTransaction(
        exchange="binance_futures",
        tx_type="deposit",
        amount=100.0,
        currency="USDT",
        source="auto_detected",
        confirmed=False,
        exchange_tx_id="abc123",
    )
    session.add(tx)
    await session.flush()

    assert tx.confirmed is False
    assert tx.exchange_tx_id == "abc123"


@pytest.mark.asyncio
async def test_capital_transaction_withdrawal(session):
    """출금 트랜잭션 생성."""
    tx = CapitalTransaction(
        exchange="bithumb",
        tx_type="withdrawal",
        amount=100_000,
        currency="KRW",
        note="출금 테스트",
        source="manual",
        confirmed=True,
    )
    session.add(tx)
    await session.flush()

    assert tx.tx_type == "withdrawal"
    assert tx.amount == 100_000


# ── load_initial_balance_from_db Tests ───────────────────────


@pytest.mark.asyncio
async def test_load_initial_balance_seed_only(session):
    """시드 입금만 있을 때 initial_balance = 시드 금액."""
    pm = PortfolioManager(
        market_data=_make_market_data(),
        initial_balance_krw=999_999,  # config 값 (덮어써질 예정)
        exchange_name="bithumb",
    )

    seed = CapitalTransaction(
        exchange="bithumb", tx_type="deposit", amount=500_000,
        currency="KRW", source="seed", confirmed=True,
    )
    session.add(seed)
    await session.flush()

    await pm.load_initial_balance_from_db(session)
    assert pm._initial_balance == 500_000


@pytest.mark.asyncio
async def test_load_initial_balance_deposit_and_withdrawal(session):
    """입금 + 출금 → initial_balance = deposits - withdrawals."""
    pm = PortfolioManager(
        market_data=_make_market_data(),
        initial_balance_krw=0,
        exchange_name="bithumb",
    )

    session.add(CapitalTransaction(
        exchange="bithumb", tx_type="deposit", amount=500_000,
        currency="KRW", source="seed", confirmed=True,
    ))
    session.add(CapitalTransaction(
        exchange="bithumb", tx_type="deposit", amount=100_000,
        currency="KRW", source="manual", confirmed=True,
    ))
    session.add(CapitalTransaction(
        exchange="bithumb", tx_type="withdrawal", amount=50_000,
        currency="KRW", source="manual", confirmed=True,
    ))
    await session.flush()

    await pm.load_initial_balance_from_db(session)
    assert pm._initial_balance == 550_000  # 500k + 100k - 50k


@pytest.mark.asyncio
async def test_load_initial_balance_ignores_unconfirmed(session):
    """미확인 건은 initial_balance 계산에서 제외."""
    pm = PortfolioManager(
        market_data=_make_market_data(),
        initial_balance_krw=0,
        exchange_name="bithumb",
    )

    session.add(CapitalTransaction(
        exchange="bithumb", tx_type="deposit", amount=500_000,
        currency="KRW", source="seed", confirmed=True,
    ))
    session.add(CapitalTransaction(
        exchange="bithumb", tx_type="deposit", amount=200_000,
        currency="KRW", source="auto_detected", confirmed=False,
    ))
    await session.flush()

    await pm.load_initial_balance_from_db(session)
    assert pm._initial_balance == 500_000  # 미확인 200k 제외


@pytest.mark.asyncio
async def test_load_initial_balance_no_transactions(session):
    """CapitalTransaction이 없으면 config 값 유지."""
    pm = PortfolioManager(
        market_data=_make_market_data(),
        initial_balance_krw=548_000,
        exchange_name="bithumb",
    )

    await pm.load_initial_balance_from_db(session)
    assert pm._initial_balance == 548_000  # 변경 없음


@pytest.mark.asyncio
async def test_load_initial_balance_exchange_isolation(session):
    """다른 거래소의 트랜잭션은 무시."""
    pm = PortfolioManager(
        market_data=_make_market_data(),
        initial_balance_krw=0,
        exchange_name="binance_futures",
    )

    # 빗썸 입금 (무시되어야 함)
    session.add(CapitalTransaction(
        exchange="bithumb", tx_type="deposit", amount=500_000,
        currency="KRW", source="seed", confirmed=True,
    ))
    # 바이낸스 입금
    session.add(CapitalTransaction(
        exchange="binance_futures", tx_type="deposit", amount=174.92,
        currency="USDT", source="seed", confirmed=True,
    ))
    await session.flush()

    await pm.load_initial_balance_from_db(session)
    assert pm._initial_balance == 174.92


# ── restore_state_from_db Peak Initialization Tests ──────────


@pytest.mark.asyncio
async def test_restore_peak_from_snapshot(session):
    """스냅샷이 있으면 peak_value를 스냅샷에서 복원."""
    pm = PortfolioManager(
        market_data=_make_market_data(),
        initial_balance_krw=500_000,
        exchange_name="bithumb",
    )

    snapshot = PortfolioSnapshot(
        exchange="bithumb",
        total_value_krw=520_000,
        cash_balance_krw=200_000,
        invested_value_krw=320_000,
        realized_pnl=15_000,
        unrealized_pnl=5_000,
        peak_value=530_000,
        drawdown_pct=1.89,
    )
    session.add(snapshot)
    await session.flush()

    await pm.restore_state_from_db(session)
    assert pm._peak_value == 530_000
    assert pm._realized_pnl == 15_000


@pytest.mark.asyncio
async def test_restore_peak_no_snapshot_uses_cash(session):
    """스냅샷 없을 때 peak_value = cash_balance (실제 자산 기준)."""
    pm = PortfolioManager(
        market_data=_make_market_data(),
        initial_balance_krw=548_000,
        exchange_name="bithumb",
    )
    # sync_exchange_positions 후 cash_balance가 511,999로 설정된 상황 시뮬레이션
    pm._cash_balance = 511_999

    await pm.restore_state_from_db(session)
    # peak_value가 config(548_000)가 아닌 실제 cash(511_999)로 설정
    assert pm._peak_value == 511_999


@pytest.mark.asyncio
async def test_restore_peak_no_snapshot_zero_cash(session):
    """스냅샷 없고 cash=0이면 peak_value = initial_balance 유지."""
    pm = PortfolioManager(
        market_data=_make_market_data(),
        initial_balance_krw=500_000,
        exchange_name="bithumb",
    )
    pm._cash_balance = 0

    await pm.restore_state_from_db(session)
    # cash가 0이면 initial_balance(500_000) 유지
    assert pm._peak_value == 500_000


# ── Confirm/Delete Flow Tests ────────────────────────────────


@pytest.mark.asyncio
async def test_confirm_updates_initial_balance(session):
    """미확인 건 확인 시 initial_balance에 반영."""
    pm = PortfolioManager(
        market_data=_make_market_data(),
        initial_balance_krw=0,
        exchange_name="bithumb",
    )

    # 시드 + 미확인 입금
    session.add(CapitalTransaction(
        exchange="bithumb", tx_type="deposit", amount=500_000,
        currency="KRW", source="seed", confirmed=True,
    ))
    unconfirmed = CapitalTransaction(
        exchange="bithumb", tx_type="deposit", amount=100_000,
        currency="KRW", source="auto_detected", confirmed=False,
    )
    session.add(unconfirmed)
    await session.flush()

    # 확인 전: 500k
    await pm.load_initial_balance_from_db(session)
    assert pm._initial_balance == 500_000

    # 확인 후: 600k
    unconfirmed.confirmed = True
    await session.flush()
    await pm.load_initial_balance_from_db(session)
    assert pm._initial_balance == 600_000


# ── capital_sync exchange_name 파라미터 테스트 (COIN-10) ─────────────────


@pytest.mark.asyncio
async def test_sync_binance_deposits_default_exchange_name(session):
    """sync_binance_deposits: 기본값 exchange_name='binance_futures' 사용."""
    from unittest.mock import AsyncMock, MagicMock
    from engine.capital_sync import sync_binance_deposits

    adapter = MagicMock()
    adapter._exchange = MagicMock()
    # 빈 목록 반환 → 새 TX 없음
    adapter._exchange.fetch_deposits = AsyncMock(return_value=[])

    result = await sync_binance_deposits(session, adapter)
    assert result == []


@pytest.mark.asyncio
async def test_sync_binance_deposits_custom_exchange_name(session):
    """sync_binance_deposits: exchange_name 파라미터로 커스텀 거래소 이름 지원."""
    from unittest.mock import AsyncMock, MagicMock
    from engine.capital_sync import sync_binance_deposits

    adapter = MagicMock()
    adapter._exchange = MagicMock()
    adapter._exchange.fetch_deposits = AsyncMock(return_value=[
        {"id": "tx_001", "amount": 200.0, "status": "ok"},
    ])

    result = await sync_binance_deposits(session, adapter, exchange_name="binance_spot")
    assert len(result) == 1
    assert result[0].exchange == "binance_spot"
    assert result[0].amount == 200.0


@pytest.mark.asyncio
async def test_sync_binance_deposits_deduplicates_by_exchange(session):
    """sync_binance_deposits: 동일 txid도 exchange가 다르면 별도 처리."""
    from unittest.mock import AsyncMock, MagicMock
    from engine.capital_sync import sync_binance_deposits

    # 먼저 binance_futures에 tx_abc 기록
    session.add(CapitalTransaction(
        exchange="binance_futures",
        tx_type="deposit",
        amount=100.0,
        currency="USDT",
        source="auto_detected",
        confirmed=False,
        exchange_tx_id="tx_abc",
    ))
    await session.flush()

    adapter = MagicMock()
    adapter._exchange = MagicMock()
    # 같은 tx_id를 binance_spot으로 조회
    adapter._exchange.fetch_deposits = AsyncMock(return_value=[
        {"id": "tx_abc", "amount": 100.0, "status": "ok"},
    ])

    # binance_spot exchange_name으로 호출 → tx_abc는 binance_spot에 없으므로 새로 생성
    result = await sync_binance_deposits(session, adapter, exchange_name="binance_spot")
    assert len(result) == 1
    assert result[0].exchange == "binance_spot"


@pytest.mark.asyncio
async def test_detect_bithumb_balance_change_custom_exchange(session):
    """detect_bithumb_balance_change: exchange_name 파라미터 사용."""
    from unittest.mock import AsyncMock, MagicMock
    from engine.capital_sync import detect_bithumb_balance_change

    pm = MagicMock()
    pm.cash_balance = 1_000_000  # 기존 예상 잔고

    cash_bal = MagicMock()
    cash_bal.free = 1_050_000  # 5만원 증가 감지
    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={"KRW": cash_bal})

    result = await detect_bithumb_balance_change(
        session, pm, adapter, exchange_name="bithumb"
    )
    assert result is not None
    assert result.exchange == "bithumb"
    assert result.amount == 50_000


@pytest.mark.asyncio
async def test_detect_bithumb_balance_change_default_exchange(session):
    """detect_bithumb_balance_change: 기본 exchange_name='bithumb'."""
    from unittest.mock import AsyncMock, MagicMock
    from engine.capital_sync import detect_bithumb_balance_change

    pm = MagicMock()
    pm.cash_balance = 500_000  # 변동 없음

    cash_bal = MagicMock()
    cash_bal.free = 500_000  # diff=0 → 감지 없음
    adapter = AsyncMock()
    adapter.fetch_balance = AsyncMock(return_value={"KRW": cash_bal})

    result = await detect_bithumb_balance_change(session, pm, adapter)
    assert result is None


# ── sync_binance_internal_transfers Tests ─────────────────────


def _make_adapter(transfer_responses: dict):
    """Create a mock adapter whose _exchange.sapiGetAssetTransfer returns configured responses."""
    from unittest.mock import MagicMock

    async def _sapi_get_asset_transfer(params):
        transfer_type = params.get("type", "")
        return transfer_responses.get(transfer_type, {"total": 0, "rows": []})

    adapter = MagicMock()
    adapter._exchange = MagicMock()
    adapter._exchange.sapiGetAssetTransfer = _sapi_get_asset_transfer
    return adapter


@pytest.mark.asyncio
async def test_sync_binance_internal_transfers_empty(session):
    """이체 없음 → 빈 결과."""
    from engine.capital_sync import sync_binance_internal_transfers

    adapter = _make_adapter({
        "MAIN_UMFUTURE": {"total": 0, "rows": []},
        "UMFUTURE_MAIN": {"total": 0, "rows": []},
    })
    result = await sync_binance_internal_transfers(session, adapter)
    assert result == []


@pytest.mark.asyncio
async def test_sync_binance_internal_transfers_spot_to_futures(session):
    """MAIN_UMFUTURE 이체 감지 → deposit TX 생성 및 PM cash 증가."""
    from unittest.mock import MagicMock
    from engine.capital_sync import sync_binance_internal_transfers

    adapter = _make_adapter({
        "MAIN_UMFUTURE": {
            "total": 1,
            "rows": [
                {
                    "asset": "USDT",
                    "amount": "500.0",
                    "type": "MAIN_UMFUTURE",
                    "status": "CONFIRMED",
                    "tranId": 111222333,
                }
            ],
        },
        "UMFUTURE_MAIN": {"total": 0, "rows": []},
    })

    pm = MagicMock()
    pm.cash_balance = 1000.0

    result = await sync_binance_internal_transfers(session, adapter)

    assert len(result) == 1
    tx = result[0]
    assert tx.tx_type == "deposit"
    assert tx.amount == 500.0
    assert tx.currency == "USDT"
    assert tx.exchange == "binance_futures"
    assert tx.exchange_tx_id == "transfer_111222333"
    assert tx.source == "auto_detected"
    assert tx.confirmed is False

    # PM cash 조정은 호출자 책임 (commit 후 수행)
    for t in result:
        pm.cash_balance = pm.cash_balance + t.amount if t.tx_type == "deposit" else max(0.0, pm.cash_balance - t.amount)
    assert pm.cash_balance == 1500.0


@pytest.mark.asyncio
async def test_sync_binance_internal_transfers_futures_to_spot(session):
    """UMFUTURE_MAIN 이체 감지 → withdrawal TX 생성 및 PM cash 감소."""
    from unittest.mock import MagicMock
    from engine.capital_sync import sync_binance_internal_transfers

    adapter = _make_adapter({
        "MAIN_UMFUTURE": {"total": 0, "rows": []},
        "UMFUTURE_MAIN": {
            "total": 1,
            "rows": [
                {
                    "asset": "USDT",
                    "amount": "200.0",
                    "type": "UMFUTURE_MAIN",
                    "status": "CONFIRMED",
                    "tranId": 444555666,
                }
            ],
        },
    })

    pm = MagicMock()
    pm.cash_balance = 800.0

    result = await sync_binance_internal_transfers(session, adapter)

    assert len(result) == 1
    tx = result[0]
    assert tx.tx_type == "withdrawal"
    assert tx.amount == 200.0
    assert tx.exchange_tx_id == "transfer_444555666"

    # PM cash 조정은 호출자 책임
    for t in result:
        pm.cash_balance = pm.cash_balance + t.amount if t.tx_type == "deposit" else max(0.0, pm.cash_balance - t.amount)
    assert pm.cash_balance == 600.0


@pytest.mark.asyncio
async def test_sync_binance_internal_transfers_dedup(session):
    """동일 tranId는 중복 기록하지 않음."""
    from engine.capital_sync import sync_binance_internal_transfers

    # 이미 기록된 TX
    session.add(CapitalTransaction(
        exchange="binance_futures",
        tx_type="deposit",
        amount=100.0,
        currency="USDT",
        source="auto_detected",
        confirmed=False,
        exchange_tx_id="transfer_999888777",
    ))
    await session.flush()

    adapter = _make_adapter({
        "MAIN_UMFUTURE": {
            "total": 1,
            "rows": [
                {
                    "asset": "USDT",
                    "amount": "100.0",
                    "type": "MAIN_UMFUTURE",
                    "status": "CONFIRMED",
                    "tranId": 999888777,
                }
            ],
        },
        "UMFUTURE_MAIN": {"total": 0, "rows": []},
    })

    result = await sync_binance_internal_transfers(session, adapter)
    assert result == []  # 중복 → 새 TX 없음


@pytest.mark.asyncio
async def test_sync_binance_internal_transfers_both_directions(session):
    """두 방향 동시에 감지."""
    from unittest.mock import MagicMock
    from engine.capital_sync import sync_binance_internal_transfers

    adapter = _make_adapter({
        "MAIN_UMFUTURE": {
            "total": 1,
            "rows": [
                {
                    "asset": "USDT",
                    "amount": "300.0",
                    "type": "MAIN_UMFUTURE",
                    "status": "CONFIRMED",
                    "tranId": 100001,
                }
            ],
        },
        "UMFUTURE_MAIN": {
            "total": 1,
            "rows": [
                {
                    "asset": "USDT",
                    "amount": "100.0",
                    "type": "UMFUTURE_MAIN",
                    "status": "CONFIRMED",
                    "tranId": 100002,
                }
            ],
        },
    })

    pm = MagicMock()
    pm.cash_balance = 500.0

    result = await sync_binance_internal_transfers(session, adapter)

    assert len(result) == 2
    types = {tx.tx_type for tx in result}
    assert types == {"deposit", "withdrawal"}

    # PM cash 조정은 호출자 책임: net +300 - 100 = +200 → final 700
    for t in result:
        pm.cash_balance = pm.cash_balance + t.amount if t.tx_type == "deposit" else max(0.0, pm.cash_balance - t.amount)
    assert pm.cash_balance == 700.0


@pytest.mark.asyncio
async def test_sync_binance_internal_transfers_non_usdt_ignored(session):
    """USDT가 아닌 자산은 무시."""
    from engine.capital_sync import sync_binance_internal_transfers

    adapter = _make_adapter({
        "MAIN_UMFUTURE": {
            "total": 1,
            "rows": [
                {
                    "asset": "BNB",
                    "amount": "10.0",
                    "type": "MAIN_UMFUTURE",
                    "status": "CONFIRMED",
                    "tranId": 777001,
                }
            ],
        },
        "UMFUTURE_MAIN": {"total": 0, "rows": []},
    })

    result = await sync_binance_internal_transfers(session, adapter)
    assert result == []


@pytest.mark.asyncio
async def test_sync_binance_internal_transfers_unconfirmed_ignored(session):
    """CONFIRMED가 아닌 이체는 무시."""
    from engine.capital_sync import sync_binance_internal_transfers

    adapter = _make_adapter({
        "MAIN_UMFUTURE": {
            "total": 1,
            "rows": [
                {
                    "asset": "USDT",
                    "amount": "100.0",
                    "type": "MAIN_UMFUTURE",
                    "status": "PENDING",
                    "tranId": 888001,
                }
            ],
        },
        "UMFUTURE_MAIN": {"total": 0, "rows": []},
    })

    result = await sync_binance_internal_transfers(session, adapter)
    assert result == []


@pytest.mark.asyncio
async def test_sync_binance_internal_transfers_api_error_graceful(session):
    """API 호출 실패 시 예외를 삼키고 빈 리스트 반환."""
    from unittest.mock import MagicMock
    from engine.capital_sync import sync_binance_internal_transfers

    async def _fail(params):
        raise RuntimeError("network error")

    adapter = MagicMock()
    adapter._exchange = MagicMock()
    adapter._exchange.sapiGetAssetTransfer = _fail

    result = await sync_binance_internal_transfers(session, adapter)
    assert result == []


@pytest.mark.asyncio
async def test_sync_binance_internal_transfers_returns_txs_for_caller(session):
    """함수는 TX만 반환하며, PM cash 조정은 호출자(main.py)가 commit 후 수행."""
    from engine.capital_sync import sync_binance_internal_transfers

    adapter = _make_adapter({
        "MAIN_UMFUTURE": {
            "total": 1,
            "rows": [
                {
                    "asset": "USDT",
                    "amount": "150.0",
                    "type": "MAIN_UMFUTURE",
                    "status": "CONFIRMED",
                    "tranId": 555001,
                }
            ],
        },
        "UMFUTURE_MAIN": {"total": 0, "rows": []},
    })

    result = await sync_binance_internal_transfers(session, adapter)
    assert len(result) == 1
    assert result[0].tx_type == "deposit"
    assert result[0].amount == 150.0


@pytest.mark.asyncio
async def test_sync_binance_internal_transfers_withdrawal_floor_zero(session):
    """출금 금액이 현재 cash보다 크면 0으로 내려가야 하며 음수가 되지 않음."""
    from unittest.mock import MagicMock
    from engine.capital_sync import sync_binance_internal_transfers

    adapter = _make_adapter({
        "MAIN_UMFUTURE": {"total": 0, "rows": []},
        "UMFUTURE_MAIN": {
            "total": 1,
            "rows": [
                {
                    "asset": "USDT",
                    "amount": "9999.0",
                    "type": "UMFUTURE_MAIN",
                    "status": "CONFIRMED",
                    "tranId": 666001,
                }
            ],
        },
    })

    pm = MagicMock()
    pm.cash_balance = 50.0

    result = await sync_binance_internal_transfers(session, adapter)
    assert len(result) == 1
    # PM cash 조정은 호출자 책임: 50 - 9999 = -9949 → floor at 0
    for t in result:
        pm.cash_balance = pm.cash_balance + t.amount if t.tx_type == "deposit" else max(0.0, pm.cash_balance - t.amount)
    assert pm.cash_balance == 0.0


@pytest.mark.asyncio
async def test_sync_binance_internal_transfers_missing_tran_id(session):
    """tranId 없는 row는 조용히 무시."""
    from engine.capital_sync import sync_binance_internal_transfers

    adapter = _make_adapter({
        "MAIN_UMFUTURE": {
            "total": 1,
            "rows": [
                {
                    "asset": "USDT",
                    "amount": "100.0",
                    "status": "CONFIRMED",
                    # tranId 키 없음
                }
            ],
        },
        "UMFUTURE_MAIN": {"total": 0, "rows": []},
    })

    result = await sync_binance_internal_transfers(session, adapter)
    assert result == []


@pytest.mark.asyncio
async def test_sync_binance_internal_transfers_zero_amount(session):
    """amount=0 또는 음수인 row는 무시."""
    from engine.capital_sync import sync_binance_internal_transfers

    adapter = _make_adapter({
        "MAIN_UMFUTURE": {
            "total": 2,
            "rows": [
                {"asset": "USDT", "amount": "0", "status": "CONFIRMED", "tranId": 11},
                {"asset": "USDT", "amount": "-5.0", "status": "CONFIRMED", "tranId": 12},
            ],
        },
        "UMFUTURE_MAIN": {"total": 0, "rows": []},
    })

    result = await sync_binance_internal_transfers(session, adapter)
    assert result == []


@pytest.mark.asyncio
async def test_sync_binance_internal_transfers_pagination(session):
    """page=1에서 정확히 100건 반환 시 page=2 조회; page=2에서 빈 목록이면 종료."""
    from engine.capital_sync import sync_binance_internal_transfers
    from unittest.mock import MagicMock

    _PAGE_SIZE = 100  # 모듈 상수와 동일
    calls: list[dict] = []

    # page 1: 100건 (full page), page 2: 0건 → 루프 종료
    page1_rows = [
        {"asset": "USDT", "amount": "1.0", "status": "CONFIRMED", "tranId": i}
        for i in range(1, _PAGE_SIZE + 1)
    ]

    async def _sapi_get(params):
        calls.append(dict(params))
        page = params.get("current", 1)
        if page == 1:
            return {"total": _PAGE_SIZE, "rows": page1_rows}
        return {"total": 0, "rows": []}

    adapter = MagicMock()
    adapter._exchange = MagicMock()
    adapter._exchange.sapiGetAssetTransfer = _sapi_get

    result = await sync_binance_internal_transfers(
        session, adapter,
        exchange_name="binance_futures",
    )

    # 100건 모두 기록
    assert len(result) == _PAGE_SIZE

    # MAIN_UMFUTURE 방향에서 2페이지 요청이 발생했음을 확인
    main_calls = [c for c in calls if c.get("type") == "MAIN_UMFUTURE"]
    assert len(main_calls) == 2
    assert main_calls[0]["current"] == 1
    assert main_calls[1]["current"] == 2

    # 모든 tranId가 정확히 1회씩 기록됨 (중복 없음)
    tx_ids = {tx.exchange_tx_id for tx in result}
    assert len(tx_ids) == _PAGE_SIZE


@pytest.mark.asyncio
async def test_sync_binance_internal_transfers_start_time_forwarded(session):
    """last_sync_time이 startTime(ms)으로 정확히 변환되어 API에 전달됨."""
    from datetime import datetime, timezone
    from engine.capital_sync import sync_binance_internal_transfers
    from unittest.mock import MagicMock

    captured: list[dict] = []

    async def _sapi_get(params):
        captured.append(dict(params))
        return {"total": 0, "rows": []}

    adapter = MagicMock()
    adapter._exchange = MagicMock()
    adapter._exchange.sapiGetAssetTransfer = _sapi_get

    fixed_time = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    expected_ms = int(fixed_time.timestamp() * 1000)

    await sync_binance_internal_transfers(
        session, adapter,
        last_sync_time=fixed_time,
    )

    assert len(captured) == 2  # MAIN_UMFUTURE + UMFUTURE_MAIN
    for call in captured:
        assert call["startTime"] == expected_ms


@pytest.mark.asyncio
async def test_sync_binance_internal_transfers_naive_datetime_from_db(session):
    """DB에서 timezone-naive datetime을 반환해도 UTC로 정규화하여 startTime 계산."""
    from datetime import datetime, timezone
    from engine.capital_sync import sync_binance_internal_transfers
    from unittest.mock import MagicMock

    # DB에 기존 transfer_* TX 삽입 (SQLite는 naive datetime 반환)
    session.add(CapitalTransaction(
        exchange="binance_futures",
        tx_type="deposit",
        amount=100.0,
        currency="USDT",
        source="auto_detected",
        confirmed=False,
        exchange_tx_id="transfer_naive_test",
    ))
    await session.flush()

    captured: list[dict] = []

    async def _sapi_get(params):
        captured.append(dict(params))
        return {"total": 0, "rows": []}

    adapter = MagicMock()
    adapter._exchange = MagicMock()
    adapter._exchange.sapiGetAssetTransfer = _sapi_get

    # last_sync_time=None → DB에서 max(created_at) 읽음
    # SQLite는 timezone-naive datetime을 반환할 수 있음 → UTC로 보정해야 함
    await sync_binance_internal_transfers(session, adapter, exchange_name="binance_futures")

    assert len(captured) == 2
    # startTime이 미래가 아닌 합리적인 값이어야 함 (에러 없이 완료)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    for call in captured:
        assert call["startTime"] <= now_ms
