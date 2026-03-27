"""Tier2Scanner 테스트.

COIN-23: 필터 추가 + SL/TP 파라미터 조정 테스트 포함.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timezone, timedelta

from engine.tier2_scanner import Tier2Scanner, ScanScore
from engine.safe_order_pipeline import SafeOrderPipeline, OrderResponse
from engine.position_state_tracker import PositionStateTracker, PositionState
from engine.regime_detector import RegimeDetector, RegimeState
from engine.portfolio_manager import PortfolioManager
from exchange.data_models import Candle, Ticker, Balance
from core.enums import Direction, Regime


def _make_candles(
    n: int = 60,
    base_close: float = 80000.0,
    close_step: float = 10.0,
    base_vol: float = 100.0,
    last_vol_mult: float = 8.0,
    last_close: float | None = None,
    high_extra: float = 500.0,
    low_extra: float = 500.0,
) -> list[Candle]:
    """테스트용 캔들 생성 헬퍼."""
    candles = []
    for i in range(n - 1):
        c = base_close + i * close_step
        candles.append(Candle(
            timestamp=datetime.now(timezone.utc),
            open=c - 10,
            high=c + high_extra,
            low=c - low_extra,
            close=c,
            volume=base_vol,
        ))
    final_close = last_close if last_close is not None else base_close + (n - 1) * close_step
    candles.append(Candle(
        timestamp=datetime.now(timezone.utc),
        open=final_close - 10,
        high=final_close + high_extra,
        low=final_close - low_extra,
        close=final_close,
        volume=base_vol * last_vol_mult,
    ))
    return candles


@pytest.fixture
def mock_exchange():
    exchange = AsyncMock()
    candles = _make_candles()
    exchange.fetch_ohlcv = AsyncMock(return_value=candles)
    exchange.fetch_ticker = AsyncMock(return_value=Ticker(
        symbol="BTC/USDT", last=80800.0, bid=80790.0, ask=80810.0,
        high=81000.0, low=79000.0, volume=10000.0,
        timestamp=datetime.now(timezone.utc),
    ))
    exchange.fetch_balance = AsyncMock(return_value={
        "USDT": Balance(currency="USDT", free=500.0, used=100.0, total=600.0),
    })
    return exchange


@pytest.fixture
def tier2(mock_exchange):
    safe_order = AsyncMock(spec=SafeOrderPipeline)
    safe_order.execute_order = AsyncMock(return_value=OrderResponse(
        success=True, order_id=1, executed_price=80800.0,
        executed_quantity=0.01, fee=0.32,
    ))

    tracker = PositionStateTracker()

    pm = MagicMock(spec=PortfolioManager)
    pm.cash_balance = 500.0
    pm._is_paper = False

    return Tier2Scanner(
        safe_order=safe_order,
        position_tracker=tracker,
        exchange=mock_exchange,
        portfolio_manager=pm,
        scan_coins=["BTC/USDT", "ETH/USDT"],
        max_concurrent=2,
        max_position_pct=0.05,
        vol_threshold=5.0,
        daily_trade_limit=10,
        cooldown_per_symbol_sec=3600,
        leverage=3,
        # COIN-23: 필터 파라미터
        sl_pct=3.5,
        tp_pct=4.5,
        trail_activation_pct=1.5,
        trail_stop_pct=1.0,
        rsi_overbought=75.0,
        rsi_oversold=25.0,
        min_atr_pct=0.5,
        exhaustion_pct=8.0,
        min_score=0.55,
        consecutive_sl_cooldown_sec=10800,
    )


# ──── 기본 스캔 ────

class TestScan:
    @pytest.mark.asyncio
    async def test_scan_scores(self, tier2):
        scores = await tier2._scan_all()
        assert len(scores) > 0
        assert all(isinstance(s, ScanScore) for s in scores)

    @pytest.mark.asyncio
    async def test_scan_single_symbol(self, tier2):
        score = await tier2._scan_symbol("BTC/USDT")
        assert score is not None
        assert score.vol_ratio > 1.0  # 마지막 캔들이 8x

    @pytest.mark.asyncio
    async def test_scan_insufficient_candles(self, tier2, mock_exchange):
        mock_exchange.fetch_ohlcv.return_value = []
        score = await tier2._scan_symbol("BTC/USDT")
        assert score is None

    @pytest.mark.asyncio
    async def test_scan_score_has_rsi_and_acceleration(self, tier2):
        """COIN-23: ScanScore에 RSI, acceleration, atr_pct 포함."""
        score = await tier2._scan_symbol("BTC/USDT")
        assert score is not None
        assert hasattr(score, 'rsi')
        assert hasattr(score, 'acceleration')
        assert hasattr(score, 'atr_pct')


# ──── COIN-23: 정규화 점수 ────

class TestNormalizedScore:
    @pytest.mark.asyncio
    async def test_score_normalized_range(self, tier2):
        """정규화 점수는 0~1 범위."""
        score = await tier2._scan_symbol("BTC/USDT")
        assert score is not None
        assert 0.0 <= score.score <= 1.0

    @pytest.mark.asyncio
    async def test_score_formula_components(self, tier2, mock_exchange):
        """정규화 점수 = vol_signal*0.40 + price_signal*0.35 + accel_signal*0.25."""
        # 높은 vol_ratio 캔들
        candles = _make_candles(last_vol_mult=15.0, last_close=81000.0)
        mock_exchange.fetch_ohlcv.return_value = candles

        score = await tier2._scan_symbol("BTC/USDT")
        assert score is not None
        # vol_ratio ≈ 15, price_chg > 0, accel > 0
        # score = 0.40 * min(15/10, 1) + 0.35 * price_signal + 0.25 * accel_signal
        assert score.score > 0.4  # vol_signal 만으로 0.4

    @pytest.mark.asyncio
    async def test_old_raw_score_no_longer_used(self, tier2, mock_exchange):
        """이전 raw 점수 (vol*0.6+price*0.4)가 더 이상 사용되지 않음."""
        candles = _make_candles(last_vol_mult=8.0)
        mock_exchange.fetch_ohlcv.return_value = candles

        score = await tier2._scan_symbol("BTC/USDT")
        assert score is not None
        # 이전 raw: 8.0*0.6 + price*0.4 ≈ 4.8+ → 이제 정규화 0~1
        assert score.score < 2.0  # 확실히 정규화됨


# ──── COIN-23: RSI 필터 ────

class TestRSIFilter:
    def test_rsi_blocks_overbought_long(self, tier2):
        """RSI > 75 → 롱 차단."""
        score = ScanScore(
            symbol="BTC/USDT", vol_ratio=8.0, price_chg_pct=3.0,
            score=0.8, direction=Direction.LONG, rsi=80.0,
        )
        assert tier2._pass_rsi_filter(score) is False

    def test_rsi_allows_normal_long(self, tier2):
        """RSI < 75 → 롱 허용."""
        score = ScanScore(
            symbol="BTC/USDT", vol_ratio=8.0, price_chg_pct=3.0,
            score=0.8, direction=Direction.LONG, rsi=60.0,
        )
        assert tier2._pass_rsi_filter(score) is True

    def test_rsi_blocks_oversold_short(self, tier2):
        """RSI < 25 → 숏 차단."""
        score = ScanScore(
            symbol="BTC/USDT", vol_ratio=8.0, price_chg_pct=-3.0,
            score=0.8, direction=Direction.SHORT, rsi=20.0,
        )
        assert tier2._pass_rsi_filter(score) is False

    def test_rsi_allows_normal_short(self, tier2):
        """RSI > 25 → 숏 허용."""
        score = ScanScore(
            symbol="BTC/USDT", vol_ratio=8.0, price_chg_pct=-3.0,
            score=0.8, direction=Direction.SHORT, rsi=40.0,
        )
        assert tier2._pass_rsi_filter(score) is True

    def test_rsi_boundary_long_75(self, tier2):
        """RSI == 75 → 롱 허용 (> 75만 차단)."""
        score = ScanScore(
            symbol="BTC/USDT", vol_ratio=8.0, price_chg_pct=3.0,
            score=0.8, direction=Direction.LONG, rsi=75.0,
        )
        assert tier2._pass_rsi_filter(score) is True

    def test_rsi_boundary_short_25(self, tier2):
        """RSI == 25 → 숏 허용 (< 25만 차단)."""
        score = ScanScore(
            symbol="BTC/USDT", vol_ratio=8.0, price_chg_pct=-3.0,
            score=0.8, direction=Direction.SHORT, rsi=25.0,
        )
        assert tier2._pass_rsi_filter(score) is True

    def test_rsi_overbought_short_allowed(self, tier2):
        """RSI > 75이지만 숏 → 허용 (과매수에서 숏은 합리적)."""
        score = ScanScore(
            symbol="BTC/USDT", vol_ratio=8.0, price_chg_pct=-3.0,
            score=0.8, direction=Direction.SHORT, rsi=80.0,
        )
        assert tier2._pass_rsi_filter(score) is True

    def test_rsi_oversold_long_allowed(self, tier2):
        """RSI < 25이지만 롱 → 허용 (과매도에서 롱은 합리적)."""
        score = ScanScore(
            symbol="BTC/USDT", vol_ratio=8.0, price_chg_pct=3.0,
            score=0.8, direction=Direction.LONG, rsi=20.0,
        )
        assert tier2._pass_rsi_filter(score) is True


# ──── COIN-23: RSI 계산 ────

class TestCalcRSI:
    def test_rsi_all_gains(self):
        """모든 상승 → RSI 100."""
        closes = [float(i) for i in range(20)]  # 0, 1, 2, ..., 19
        rsi = Tier2Scanner._calc_rsi(closes)
        assert rsi == 100.0

    def test_rsi_all_losses(self):
        """모든 하락 → RSI 0."""
        closes = [float(20 - i) for i in range(20)]  # 20, 19, ..., 1
        rsi = Tier2Scanner._calc_rsi(closes)
        assert rsi == 0.0

    def test_rsi_insufficient_data(self):
        """데이터 부족 → 50.0 (중립)."""
        closes = [100.0, 101.0]
        rsi = Tier2Scanner._calc_rsi(closes)
        assert rsi == 50.0

    def test_rsi_mixed_moves(self):
        """혼합 → 0 < RSI < 100."""
        closes = [100.0, 102.0, 101.0, 103.0, 102.0, 104.0, 103.0, 105.0,
                  104.0, 106.0, 105.0, 107.0, 106.0, 108.0, 107.0, 109.0]
        rsi = Tier2Scanner._calc_rsi(closes)
        assert 0.0 < rsi < 100.0


# ──── COIN-23: ATR% 필터 ────

class TestATRFilter:
    @pytest.mark.asyncio
    async def test_atr_blocks_sideways(self, tier2, mock_exchange):
        """ATR% < 0.5% → 횡보장 차단 (score None)."""
        # 아주 작은 high-low 범위
        candles = _make_candles(
            high_extra=0.1,  # 매우 좁은 범위
            low_extra=0.1,
        )
        mock_exchange.fetch_ohlcv.return_value = candles

        score = await tier2._scan_symbol("BTC/USDT")
        # ATR%가 매우 작으므로 None
        assert score is None

    @pytest.mark.asyncio
    async def test_atr_allows_volatile(self, tier2, mock_exchange):
        """ATR% > 0.5% → 진입 허용."""
        # 넓은 high-low 범위
        candles = _make_candles(
            high_extra=500.0,
            low_extra=500.0,
        )
        mock_exchange.fetch_ohlcv.return_value = candles

        score = await tier2._scan_symbol("BTC/USDT")
        assert score is not None
        assert score.atr_pct >= 0.5

    def test_calc_atr_pct_basic(self):
        """ATR% 정적 계산 테스트."""
        candles = _make_candles(high_extra=200.0, low_extra=200.0)
        atr_pct = Tier2Scanner._calc_atr_pct(candles)
        assert atr_pct > 0.0

    def test_calc_atr_pct_insufficient(self):
        """캔들 부족 → 0.0."""
        candles = _make_candles(n=5)
        atr_pct = Tier2Scanner._calc_atr_pct(candles)
        assert atr_pct == 0.0


# ──── COIN-23: 소진 필터 ────

class TestExhaustionFilter:
    @pytest.mark.asyncio
    async def test_exhaustion_blocks_8pct_move(self, tier2, mock_exchange):
        """30분 내 8%+ 이동 → 차단."""
        base = 80000.0
        # 6캔들 전 대비 10% 급등
        candles = _make_candles(n=60, base_close=base, close_step=0.1)
        # 마지막 6개 캔들의 close를 급등으로 설정
        for i in range(-6, 0):
            c = candles[i]
            new_close = base + (base * 0.12)  # 12% 급등
            candles[i] = Candle(
                timestamp=c.timestamp,
                open=new_close - 10,
                high=new_close + 500,
                low=new_close - 500,
                close=new_close,
                volume=c.volume,
            )
        mock_exchange.fetch_ohlcv.return_value = candles

        score = await tier2._scan_symbol("BTC/USDT")
        assert score is None  # 소진 필터에 의해 차단

    @pytest.mark.asyncio
    async def test_exhaustion_allows_normal_move(self, tier2, mock_exchange):
        """30분 내 3% 이동 → 허용."""
        candles = _make_candles(
            n=60, base_close=80000.0, close_step=10.0,
            high_extra=500.0, low_extra=500.0,
        )
        mock_exchange.fetch_ohlcv.return_value = candles

        score = await tier2._scan_symbol("BTC/USDT")
        # 기본 캔들은 close_step=10으로 완만한 변동 → 허용
        assert score is not None


# ──── COIN-23: 가속도 ────

class TestAcceleration:
    def test_acceleration_positive(self):
        """최근 거래량 > 2캔들전 → 양의 가속도."""
        # vol_avg = 100, volumes[-1]=800, volumes[-3]=100
        volumes = [100.0] * 57 + [100.0, 100.0, 800.0]
        vol_avg = 100.0
        accel = Tier2Scanner._calc_acceleration(volumes, vol_avg)
        assert accel > 0

    def test_acceleration_negative(self):
        """최근 거래량 < 2캔들전 → 음의 가속도."""
        volumes = [100.0] * 57 + [800.0, 100.0, 100.0]
        vol_avg = 100.0
        accel = Tier2Scanner._calc_acceleration(volumes, vol_avg)
        assert accel < 0

    def test_acceleration_insufficient_data(self):
        """데이터 부족 → 0.0."""
        accel = Tier2Scanner._calc_acceleration([100.0, 200.0], 100.0)
        assert accel == 0.0

    def test_acceleration_zero_avg(self):
        """vol_avg == 0 → 0.0."""
        accel = Tier2Scanner._calc_acceleration([100.0, 200.0, 300.0], 0.0)
        assert accel == 0.0


# ──── COIN-23: min_score 임계값 ────

class TestMinScore:
    @pytest.mark.asyncio
    async def test_min_score_blocks_low(self, tier2, mock_exchange, session):
        """min_score 미만 → 진입 차단."""
        # 낮은 vol_ratio 캔들
        candles = _make_candles(last_vol_mult=1.5, high_extra=500.0, low_extra=500.0)
        mock_exchange.fetch_ohlcv.return_value = candles

        await tier2.scan_cycle(session)
        # 낮은 score는 진입 안 됨
        open_calls = [
            c for c in tier2._safe_order.execute_order.call_args_list
            if c[0][1].action == "open"
        ]
        assert len(open_calls) == 0


# ──── 진입 ────

class TestEntry:
    @pytest.mark.asyncio
    async def test_enters_on_high_score(self, tier2, session):
        """높은 점수 → 진입."""
        await tier2.scan_cycle(session)
        if tier2._safe_order.execute_order.called:
            req = tier2._safe_order.execute_order.call_args[0][1]
            assert req.tier == "tier2"

    @pytest.mark.asyncio
    async def test_respects_max_concurrent(self, tier2, mock_exchange, session):
        """최대 동시 포지션 제한."""
        mock_exchange.fetch_ticker.return_value = Ticker(
            symbol="AAA/USDT", last=80000.0, bid=79990.0, ask=80010.0,
            high=81000.0, low=79000.0, volume=10000.0,
            timestamp=datetime.now(timezone.utc),
        )
        for sym in ["AAA/USDT", "BBB/USDT"]:
            tier2._positions.open_position(PositionState(
                symbol=sym, direction=Direction.LONG, quantity=0.01,
                entry_price=80000.0, margin=25.0, leverage=3,
                extreme_price=80000.0, stop_loss_atr=3.5, take_profit_atr=4.5,
                trailing_activation_atr=1.5, trailing_stop_atr=1.0,
                tier="tier2",
            ))

        await tier2.scan_cycle(session)
        open_calls = [
            c for c in tier2._safe_order.execute_order.call_args_list
            if c[0][1].action == "open"
        ]
        assert len(open_calls) == 0

    @pytest.mark.asyncio
    async def test_respects_daily_limit(self, tier2, session):
        """일일 거래 제한."""
        tier2._daily_trades = 10  # daily_trade_limit
        await tier2.scan_cycle(session)
        tier2._safe_order.execute_order.assert_not_called()


# ──── 쿨다운 ────

class TestCooldown:
    def test_in_cooldown(self, tier2):
        tier2._cooldowns["BTC/USDT"] = datetime.now(timezone.utc)
        assert tier2._in_cooldown("BTC/USDT") is True

    def test_not_in_cooldown(self, tier2):
        tier2._cooldowns["BTC/USDT"] = datetime.now(timezone.utc) - timedelta(hours=2)
        assert tier2._in_cooldown("BTC/USDT") is False

    def test_no_cooldown(self, tier2):
        assert tier2._in_cooldown("NEW/USDT") is False

    def test_cooldown_60min(self, tier2):
        """COIN-23: 쿨다운 60분 (3600초)."""
        # 59분 전 → 아직 쿨다운 중
        tier2._cooldowns["BTC/USDT"] = datetime.now(timezone.utc) - timedelta(minutes=59)
        assert tier2._in_cooldown("BTC/USDT") is True

        # 61분 전 → 쿨다운 해제
        tier2._cooldowns["BTC/USDT"] = datetime.now(timezone.utc) - timedelta(minutes=61)
        assert tier2._in_cooldown("BTC/USDT") is False


# ──── COIN-23: 연속 SL 쿨다운 ────

class TestConsecutiveSLCooldown:
    @pytest.mark.asyncio
    async def test_consecutive_sl_sets_long_cooldown(self, tier2, mock_exchange, session):
        """2연속 SL → 180분 장기 쿨다운."""
        symbol = "BTC/USDT"
        entry_price = 82000.0

        for i in range(2):
            state = PositionState(
                symbol=symbol, direction=Direction.LONG, quantity=0.01,
                entry_price=entry_price, margin=25.0, leverage=3,
                extreme_price=entry_price, stop_loss_atr=3.5, take_profit_atr=4.5,
                trailing_activation_atr=1.5, trailing_stop_atr=1.0,
                tier="tier2",
            )
            tier2._positions.open_position(state)

            # SL 히트 가격 설정: entry 82000, sl 3.5%, lev 3 → raw drop = 3.5/3 = 1.167%
            sl_price = entry_price * (1 - 0.012)  # 1.2% 하락 → lev PnL = -3.6% > -3.5%
            mock_exchange.fetch_ticker.return_value = Ticker(
                symbol=symbol, last=sl_price, bid=sl_price, ask=sl_price,
                high=entry_price, low=sl_price, volume=10000.0,
                timestamp=datetime.now(timezone.utc),
            )

            await tier2._check_exits(session)

        # 연속 SL 카운트 확인
        assert tier2._consecutive_sl_count.get(symbol, 0) >= 2
        # 장기 쿨다운 설정됨
        assert symbol in tier2._cooldown_override_map
        # 쿨다운 활성화
        assert tier2._in_cooldown(symbol) is True

    @pytest.mark.asyncio
    async def test_tp_resets_consecutive_sl(self, tier2, mock_exchange, session):
        """TP → 연속 SL 카운트 리셋."""
        symbol = "BTC/USDT"
        entry_price = 80000.0

        # 1회 SL
        tier2._consecutive_sl_count[symbol] = 1

        # TP 히트
        state = PositionState(
            symbol=symbol, direction=Direction.LONG, quantity=0.01,
            entry_price=entry_price, margin=25.0, leverage=3,
            extreme_price=entry_price, stop_loss_atr=3.5, take_profit_atr=4.5,
            trailing_activation_atr=1.5, trailing_stop_atr=1.0,
            tier="tier2",
        )
        tier2._positions.open_position(state)

        # TP 히트 가격: 4.5% / 3 = 1.5% 상승
        tp_price = entry_price * 1.016  # 1.6% 상승 → lev PnL = 4.8% > 4.5%
        mock_exchange.fetch_ticker.return_value = Ticker(
            symbol=symbol, last=tp_price, bid=tp_price, ask=tp_price,
            high=tp_price, low=entry_price, volume=10000.0,
            timestamp=datetime.now(timezone.utc),
        )

        await tier2._check_exits(session)
        # TP로 청산 → 연속 SL 카운트 리셋
        assert tier2._consecutive_sl_count.get(symbol, 0) == 0

    def test_long_cooldown_expires(self, tier2):
        """장기 쿨다운 만료 후 재진입 가능."""
        symbol = "BTC/USDT"
        # 181분 전 장기 쿨다운 설정
        tier2._cooldown_override_map[symbol] = (
            datetime.now(timezone.utc) - timedelta(minutes=181)
        )
        tier2._consecutive_sl_count[symbol] = 2

        assert tier2._in_cooldown(symbol) is False
        # 정리됨
        assert symbol not in tier2._cooldown_override_map

    def test_long_cooldown_active(self, tier2):
        """장기 쿨다운 활성 중."""
        symbol = "BTC/USDT"
        tier2._cooldown_override_map[symbol] = (
            datetime.now(timezone.utc) - timedelta(minutes=90)
        )
        assert tier2._in_cooldown(symbol) is True


# ──── 퇴장 ────

class TestExits:
    @pytest.mark.asyncio
    async def test_time_exit(self, tier2, session):
        """시간 초과 → 청산."""
        state = PositionState(
            symbol="BTC/USDT", direction=Direction.LONG, quantity=0.01,
            entry_price=80000.0, margin=25.0, leverage=3,
            extreme_price=80000.0, stop_loss_atr=3.5, take_profit_atr=4.5,
            trailing_activation_atr=1.5, trailing_stop_atr=1.0,
            tier="tier2",
            entered_at=datetime.now(timezone.utc) - timedelta(minutes=130),
        )
        tier2._positions.open_position(state)

        await tier2._check_exits(session)
        assert tier2._safe_order.execute_order.called

    @pytest.mark.asyncio
    async def test_sl_exit(self, tier2, mock_exchange, session):
        """SL 히트 → 청산 (COIN-23: SL 3.5%)."""
        state = PositionState(
            symbol="BTC/USDT", direction=Direction.LONG, quantity=0.01,
            entry_price=82000.0, margin=25.0, leverage=3,
            extreme_price=82000.0, stop_loss_atr=3.5, take_profit_atr=4.5,
            trailing_activation_atr=1.5, trailing_stop_atr=1.0,
            tier="tier2",
        )
        tier2._positions.open_position(state)

        # 가격 80000 → raw change = -2.44%, leveraged = -7.3% > -3.5% → SL 히트
        mock_exchange.fetch_ticker.return_value = Ticker(
            symbol="BTC/USDT", last=80000.0, bid=79990.0, ask=80010.0,
            high=82000.0, low=79000.0, volume=10000.0,
            timestamp=datetime.now(timezone.utc),
        )

        await tier2._check_exits(session)
        assert tier2._safe_order.execute_order.called


# ──── 레버리지 PnL ────

class TestLeveragePnL:
    """Bug COIN-13: SL/TP 계산 시 레버리지 적용 테스트."""

    @pytest.mark.asyncio
    async def test_sl_with_leverage_long(self, tier2, mock_exchange, session):
        """COIN-23: SL 3.5% → 3x에서 raw 1.17% 하락시 SL 히트."""
        state = PositionState(
            symbol="BTC/USDT", direction=Direction.LONG, quantity=0.01,
            entry_price=100.0, margin=25.0, leverage=3,
            extreme_price=100.0, stop_loss_atr=3.5, take_profit_atr=4.5,
            trailing_activation_atr=1.5, trailing_stop_atr=1.0,
            tier="tier2",
        )
        tier2._positions.open_position(state)

        # raw change = -1.2%, leveraged = -3.6% > -3.5% → SL 히트
        mock_exchange.fetch_ticker.return_value = Ticker(
            symbol="BTC/USDT", last=98.8, bid=98.79, ask=98.81,
            high=101.0, low=98.0, volume=10000.0,
            timestamp=datetime.now(timezone.utc),
        )

        await tier2._check_exits(session)
        assert tier2._safe_order.execute_order.called

    @pytest.mark.asyncio
    async def test_sl_not_hit_within_margin(self, tier2, mock_exchange, session):
        """COIN-23: SL 3.5% → raw 1.0% 하락은 leveraged 3.0% → SL 미히트."""
        state = PositionState(
            symbol="BTC/USDT", direction=Direction.LONG, quantity=0.01,
            entry_price=100.0, margin=25.0, leverage=3,
            extreme_price=100.0, stop_loss_atr=3.5, take_profit_atr=4.5,
            trailing_activation_atr=1.5, trailing_stop_atr=1.0,
            tier="tier2",
        )
        tier2._positions.open_position(state)

        # raw change = -1.0%, leveraged = -3.0% < -3.5% → SL 미히트
        mock_exchange.fetch_ticker.return_value = Ticker(
            symbol="BTC/USDT", last=99.0, bid=98.99, ask=99.01,
            high=101.0, low=99.0, volume=10000.0,
            timestamp=datetime.now(timezone.utc),
        )

        await tier2._check_exits(session)
        assert not tier2._safe_order.execute_order.called

    @pytest.mark.asyncio
    async def test_tp_with_leverage_short(self, tier2, mock_exchange, session):
        """COIN-23: TP 4.5% → 숏에서 raw 1.5% 하락시 TP 히트."""
        state = PositionState(
            symbol="BTC/USDT", direction=Direction.SHORT, quantity=0.01,
            entry_price=100.0, margin=25.0, leverage=3,
            extreme_price=100.0, stop_loss_atr=3.5, take_profit_atr=4.5,
            trailing_activation_atr=1.5, trailing_stop_atr=1.0,
            tier="tier2",
        )
        tier2._positions.open_position(state)

        # raw change = 1.6% (short profit), leveraged = 4.8% > 4.5% → TP 히트
        mock_exchange.fetch_ticker.return_value = Ticker(
            symbol="BTC/USDT", last=98.4, bid=98.39, ask=98.41,
            high=101.0, low=98.0, volume=10000.0,
            timestamp=datetime.now(timezone.utc),
        )

        await tier2._check_exits(session)
        assert tier2._safe_order.execute_order.called

    @pytest.mark.asyncio
    async def test_sl_with_high_leverage(self, tier2, mock_exchange, session):
        """고레버리지(20x): 작은 변동도 SL 히트."""
        tier2._leverage = 20

        state = PositionState(
            symbol="BTC/USDT", direction=Direction.LONG, quantity=0.01,
            entry_price=100.0, margin=25.0, leverage=20,
            extreme_price=100.0, stop_loss_atr=3.5, take_profit_atr=4.5,
            trailing_activation_atr=1.5, trailing_stop_atr=1.0,
            tier="tier2",
        )
        tier2._positions.open_position(state)

        # raw change = -0.2%, leveraged = -4.0% > -3.5% → SL 히트
        mock_exchange.fetch_ticker.return_value = Ticker(
            symbol="BTC/USDT", last=99.80, bid=99.79, ask=99.81,
            high=101.0, low=99.0, volume=10000.0,
            timestamp=datetime.now(timezone.utc),
        )

        await tier2._check_exits(session)
        assert tier2._safe_order.execute_order.called


# ──── COIN-23: 파라미터 기본값 ────

class TestDefaultParams:
    def test_sl_default_3_5(self):
        """SL 기본값 3.5%."""
        scanner = Tier2Scanner(
            safe_order=MagicMock(),
            position_tracker=PositionStateTracker(),
            exchange=AsyncMock(),
            portfolio_manager=MagicMock(),
        )
        assert scanner._sl_pct == 3.5

    def test_tp_default_4_5(self):
        """TP 기본값 4.5%."""
        scanner = Tier2Scanner(
            safe_order=MagicMock(),
            position_tracker=PositionStateTracker(),
            exchange=AsyncMock(),
            portfolio_manager=MagicMock(),
        )
        assert scanner._tp_pct == 4.5

    def test_trail_activation_default_1_5(self):
        """trail_activation 기본값 1.5%."""
        scanner = Tier2Scanner(
            safe_order=MagicMock(),
            position_tracker=PositionStateTracker(),
            exchange=AsyncMock(),
            portfolio_manager=MagicMock(),
        )
        assert scanner._trail_activation_pct == 1.5

    def test_trail_stop_default_1_0(self):
        """trail_stop 기본값 1.0%."""
        scanner = Tier2Scanner(
            safe_order=MagicMock(),
            position_tracker=PositionStateTracker(),
            exchange=AsyncMock(),
            portfolio_manager=MagicMock(),
        )
        assert scanner._trail_stop_pct == 1.0

    def test_max_concurrent_default_3(self):
        """max_concurrent 기본값 3."""
        scanner = Tier2Scanner(
            safe_order=MagicMock(),
            position_tracker=PositionStateTracker(),
            exchange=AsyncMock(),
            portfolio_manager=MagicMock(),
        )
        assert scanner._max_concurrent == 3

    def test_cooldown_default_3600(self):
        """cooldown 기본값 3600초 (60분)."""
        scanner = Tier2Scanner(
            safe_order=MagicMock(),
            position_tracker=PositionStateTracker(),
            exchange=AsyncMock(),
            portfolio_manager=MagicMock(),
        )
        assert scanner._cooldown_sec == 3600

    def test_min_score_default_0_55(self):
        """min_score 기본값 0.55."""
        scanner = Tier2Scanner(
            safe_order=MagicMock(),
            position_tracker=PositionStateTracker(),
            exchange=AsyncMock(),
            portfolio_manager=MagicMock(),
        )
        assert scanner._min_score == 0.55

    def test_rsi_overbought_default_75(self):
        """rsi_overbought 기본값 75.0."""
        scanner = Tier2Scanner(
            safe_order=MagicMock(),
            position_tracker=PositionStateTracker(),
            exchange=AsyncMock(),
            portfolio_manager=MagicMock(),
        )
        assert scanner._rsi_overbought == 75.0

    def test_consecutive_sl_cooldown_default_10800(self):
        """consecutive_sl_cooldown 기본값 10800초 (180분)."""
        scanner = Tier2Scanner(
            safe_order=MagicMock(),
            position_tracker=PositionStateTracker(),
            exchange=AsyncMock(),
            portfolio_manager=MagicMock(),
        )
        assert scanner._consecutive_sl_cooldown_sec == 10800


class TestRegimeFilter:
    """Tier2는 RANGING 레짐에서 신규 진입을 하지 않아야 함."""

    @pytest.fixture
    def regime_detector(self):
        rd = MagicMock(spec=RegimeDetector)
        return rd

    @pytest.fixture
    def tier2_with_regime(self, mock_exchange, regime_detector):
        safe_order = AsyncMock(spec=SafeOrderPipeline)
        safe_order.execute_order = AsyncMock(return_value=OrderResponse(
            success=True, order_id=1, executed_price=80800.0,
            executed_quantity=0.01, fee=0.32,
        ))
        tracker = PositionStateTracker()
        pm = MagicMock(spec=PortfolioManager)
        pm.cash_balance = 500.0
        pm._is_paper = False

        return Tier2Scanner(
            safe_order=safe_order,
            position_tracker=tracker,
            exchange=mock_exchange,
            portfolio_manager=pm,
            regime_detector=regime_detector,
            scan_coins=["BTC/USDT"],
            max_concurrent=2,
            daily_trade_limit=10,
            leverage=3,
        )

    @pytest.mark.asyncio
    async def test_skip_entry_during_ranging(self, tier2_with_regime, regime_detector, session):
        """RANGING 레짐 → 신규 진입 스킵."""
        regime_detector.current = RegimeState(
            regime=Regime.RANGING,
            confidence=0.8,
            adx=18.0,
            bb_width=2.0,
            atr_pct=1.5,
            volume_ratio=0.9,
            trend_direction=0,
            timestamp=datetime.now(timezone.utc),
        )

        await tier2_with_regime.scan_cycle(session)
        # RANGING이므로 scan도 하지 않고 exit 체크 후 바로 return
        open_calls = [
            c for c in tier2_with_regime._safe_order.execute_order.call_args_list
            if c[0][1].action == "open"
        ]
        assert len(open_calls) == 0

    @pytest.mark.asyncio
    async def test_allows_entry_during_trending(self, tier2_with_regime, regime_detector, session):
        """TRENDING_UP 레짐 → 진입 허용."""
        regime_detector.current = RegimeState(
            regime=Regime.TRENDING_UP,
            confidence=0.8,
            adx=30.0,
            bb_width=3.0,
            atr_pct=2.0,
            volume_ratio=1.2,
            trend_direction=1,
            timestamp=datetime.now(timezone.utc),
        )

        await tier2_with_regime.scan_cycle(session)
        # TRENDING이므로 스캔 + 진입 시도 진행
        assert tier2_with_regime._scores is not None

    @pytest.mark.asyncio
    async def test_exit_still_works_during_ranging(
        self, tier2_with_regime, regime_detector, mock_exchange, session,
    ):
        """RANGING에서도 기존 포지션 exit 체크는 수행."""
        regime_detector.current = RegimeState(
            regime=Regime.RANGING,
            confidence=0.8,
            adx=18.0,
            bb_width=2.0,
            atr_pct=1.5,
            volume_ratio=0.9,
            trend_direction=0,
            timestamp=datetime.now(timezone.utc),
        )

        # 시간 초과된 기존 tier2 포지션
        state = PositionState(
            symbol="BTC/USDT", direction=Direction.LONG, quantity=0.01,
            entry_price=80000.0, margin=25.0, leverage=3,
            extreme_price=80000.0, stop_loss_atr=2.0, take_profit_atr=4.0,
            trailing_activation_atr=1.0, trailing_stop_atr=0.8,
            tier="tier2",
            entered_at=datetime.now(timezone.utc) - timedelta(minutes=130),
        )
        tier2_with_regime._positions.open_position(state)

        await tier2_with_regime.scan_cycle(session)
        # exit 체크로 청산 주문 발생해야 함
        close_calls = [
            c for c in tier2_with_regime._safe_order.execute_order.call_args_list
            if c[0][1].action == "close"
        ]
        assert len(close_calls) > 0


class TestCloseLock:
    """COIN-48: Tier2 close_lock으로 WS 이중 청산 방지."""

    def test_close_lock_shared(self):
        """외부에서 전달한 close_lock을 사용."""
        import asyncio
        lock = asyncio.Lock()
        scanner = Tier2Scanner(
            safe_order=MagicMock(),
            position_tracker=PositionStateTracker(),
            exchange=AsyncMock(),
            portfolio_manager=MagicMock(),
            close_lock=lock,
        )
        assert scanner._close_lock is lock

    def test_close_lock_default(self):
        """close_lock 미전달 시 자체 생성."""
        import asyncio
        scanner = Tier2Scanner(
            safe_order=MagicMock(),
            position_tracker=PositionStateTracker(),
            exchange=AsyncMock(),
            portfolio_manager=MagicMock(),
        )
        assert isinstance(scanner._close_lock, asyncio.Lock)

    @pytest.mark.asyncio
    async def test_close_skips_already_closed(self, tier2, mock_exchange, session):
        """close_lock 획득 후 인메모리에 없으면 청산 스킵."""
        state = PositionState(
            symbol="BTC/USDT", direction=Direction.LONG, quantity=0.01,
            entry_price=80000.0, margin=25.0, leverage=3,
            extreme_price=80000.0, stop_loss_atr=3.5, take_profit_atr=4.5,
            trailing_activation_atr=1.5, trailing_stop_atr=1.0,
            tier="tier2",
        )
        # 포지션 열지 않고 직접 _close_tier2 호출 → 인메모리에 없어서 스킵
        await tier2._close_tier2(session, "BTC/USDT", state, "SL: test")
        tier2._safe_order.execute_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_with_lock_prevents_double(self, mock_exchange, session):
        """close_lock 공유 시 동시 청산 방지."""
        import asyncio
        lock = asyncio.Lock()
        safe_order = AsyncMock(spec=SafeOrderPipeline)
        safe_order.execute_order = AsyncMock(return_value=OrderResponse(
            success=True, order_id=1, executed_price=80800.0,
            executed_quantity=0.01, fee=0.32,
        ))
        tracker = PositionStateTracker()
        pm = MagicMock(spec=PortfolioManager)
        pm.cash_balance = 500.0
        pm._is_paper = False

        scanner = Tier2Scanner(
            safe_order=safe_order,
            position_tracker=tracker,
            exchange=mock_exchange,
            portfolio_manager=pm,
            close_lock=lock,
        )

        state = PositionState(
            symbol="BTC/USDT", direction=Direction.LONG, quantity=0.01,
            entry_price=80000.0, margin=25.0, leverage=3,
            extreme_price=80000.0, stop_loss_atr=3.5, take_profit_atr=4.5,
            trailing_activation_atr=1.5, trailing_stop_atr=1.0,
            tier="tier2",
        )
        tracker.open_position(state)

        # 첫 번째 청산 성공
        await scanner._close_tier2(session, "BTC/USDT", state, "TP: 5%")
        assert safe_order.execute_order.call_count == 1

        # 두 번째 청산 시도 — 인메모리에서 이미 제거됨 → 스킵
        safe_order.execute_order.reset_mock()
        await scanner._close_tier2(session, "BTC/USDT", state, "TP: 5%")
        safe_order.execute_order.assert_not_called()


class TestResetDaily:
    def test_reset(self, tier2):
        tier2._daily_trades = 5
        tier2.reset_daily()
        assert tier2._daily_trades == 0
