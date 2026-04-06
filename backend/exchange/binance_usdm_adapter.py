"""
바이낸스 USDM 선물 어댑터
========================
ccxt `binanceusdm` 사용. 선물 전용 메서드 (레버리지, 포지션, 펀딩비) 구현.
"""

import asyncio
import structlog
from datetime import datetime, timezone
from typing import Optional

import ccxt.async_support as ccxt
import ccxt.pro as ccxtpro

from exchange.base import ExchangeAdapter
from exchange.data_models import (
    Candle,
    Ticker,
    OrderResult,
    Balance,
    OrderBook,
    FuturesPosition,
    OpenInterest,
    MarkPriceInfo,
    LongShortRatio,
)
from core.exceptions import (
    ExchangeConnectionError,
    ExchangeRateLimitError,
    InsufficientBalanceError,
    ExchangeError,
    OrderNotFoundError,
)

logger = structlog.get_logger(__name__)


class BinanceUSDMAdapter(ExchangeAdapter):
    """Binance USDM perpetual futures adapter using ccxt."""

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        testnet: bool = True,
        rate_limit: int = 10,
    ):
        self._api_key = api_key
        self._api_secret = api_secret
        self._testnet = testnet
        self._rate_limit = rate_limit
        self._exchange: Optional[ccxt.binanceusdm] = None
        self._ws_exchange: Optional[ccxtpro.binanceusdm] = None
        self._semaphore = asyncio.Semaphore(rate_limit)

    async def initialize(self) -> None:
        config = {
            "enableRateLimit": True,
            "rateLimit": int(1000 / self._rate_limit),
            "options": {
                "defaultType": "future",
            },
        }
        if self._api_key and self._api_secret:
            config["apiKey"] = self._api_key
            config["secret"] = self._api_secret

        self._exchange = ccxt.binanceusdm(config)

        if self._testnet:
            self._exchange.set_sandbox_mode(True)

        try:
            await self._exchange.load_markets()
            logger.info(
                "binance_usdm_connected",
                markets=len(self._exchange.markets),
                testnet=self._testnet,
            )
        except Exception as e:
            raise ExchangeConnectionError(f"Failed to connect to Binance USDM: {e}")

    async def close(self) -> None:
        if self._exchange:
            await self._exchange.close()
            logger.info("binance_usdm_disconnected")

    # Circuit breaker settings
    _CB_THRESHOLD = 5  # consecutive failures to trip
    _CB_RESET_SEC = 60  # seconds before retry after trip
    _API_TIMEOUT = 30  # seconds per API call

    def __init_cb(self):
        """Lazy init circuit breaker state (called in _call)."""
        if not hasattr(self, "_cb_failures"):
            self._cb_failures = 0
            self._cb_open_until = 0.0

    async def _call(self, method, *args, **kwargs):
        """Rate-limited API call with timeout and circuit breaker."""
        self.__init_cb()
        import time as _time

        now = _time.monotonic()
        if self._cb_failures >= self._CB_THRESHOLD:
            if now < self._cb_open_until:
                raise ExchangeConnectionError(
                    f"Circuit breaker open ({self._cb_failures} consecutive failures)"
                )
            # Reset after cooldown
            self._cb_failures = 0

        async with self._semaphore:
            try:
                result = await asyncio.wait_for(
                    method(*args, **kwargs), timeout=self._API_TIMEOUT
                )
                self._cb_failures = 0
                return result
            except asyncio.TimeoutError:
                self._cb_failures += 1
                self._cb_open_until = _time.monotonic() + self._CB_RESET_SEC
                raise ExchangeConnectionError(
                    f"API call timed out after {self._API_TIMEOUT}s"
                )
            except ccxt.RateLimitExceeded as e:
                raise ExchangeRateLimitError(str(e))
            except ccxt.InsufficientFunds as e:
                raise InsufficientBalanceError(str(e))
            except ccxt.OrderNotFound as e:
                raise OrderNotFoundError(str(e))
            except ccxt.NetworkError as e:
                self._cb_failures += 1
                self._cb_open_until = _time.monotonic() + self._CB_RESET_SEC
                raise ExchangeConnectionError(str(e))
            except ccxt.ExchangeError as e:
                raise ExchangeError(str(e))

    # ── 시세 조회 (공개 API) ──────────────────────────────────────

    async def fetch_tickers(self) -> dict:
        """Fetch all tickers (for dynamic coin selection)."""
        return await self._call(self._exchange.fetch_tickers)

    async def fetch_ticker(self, symbol: str) -> Ticker:
        data = await self._call(self._exchange.fetch_ticker, symbol)
        return Ticker(
            symbol=symbol,
            last=float(data["last"] or 0),
            bid=float(data["bid"] or 0),
            ask=float(data["ask"] or 0),
            high=float(data["high"] or 0),
            low=float(data["low"] or 0),
            volume=float(data["baseVolume"] or 0),
            timestamp=datetime.fromtimestamp(data["timestamp"] / 1000, tz=timezone.utc),
        )

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1h",
        limit: int = 100,
        since: int | None = None,
    ) -> list[Candle]:
        data = await self._call(
            self._exchange.fetch_ohlcv,
            symbol,
            timeframe,
            since=since,
            limit=limit,
        )
        return [
            Candle(
                timestamp=datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
            for row in data
        ]

    async def fetch_orderbook(self, symbol: str, limit: int = 20) -> OrderBook:
        data = await self._call(self._exchange.fetch_order_book, symbol, limit)
        return OrderBook(
            symbol=symbol,
            bids=[(float(b[0]), float(b[1])) for b in data["bids"]],
            asks=[(float(a[0]), float(a[1])) for a in data["asks"]],
            timestamp=datetime.now(timezone.utc),
        )

    async def fetch_balance(self) -> dict[str, Balance]:
        data = await self._call(self._exchange.fetch_balance)
        balances = {}
        for currency, info in data.items():
            if isinstance(info, dict) and "free" in info:
                balances[currency] = Balance(
                    currency=currency,
                    free=float(info.get("free", 0) or 0),
                    used=float(info.get("used", 0) or 0),
                    total=float(info.get("total", 0) or 0),
                )
        return balances

    # ── 주문 ─────────────────────────────────────────────────────

    # Binance USDM taker fee (BNB 미사용 기준)
    _DEFAULT_FEE_RATE = 0.0004  # 0.04%

    def _parse_order(self, data: dict) -> OrderResult:
        cost = float(data["cost"] or 0)
        # CCXT futures create_order 응답에 fee 미포함 → cost 기반 추정
        raw_fee = float((data.get("fee") or {}).get("cost", 0) or 0)
        fee = raw_fee if raw_fee > 0 else cost * self._DEFAULT_FEE_RATE

        return OrderResult(
            order_id=str(data["id"]),
            symbol=data["symbol"],
            side=data["side"],
            order_type=data["type"],
            status=data["status"],
            price=float(data["price"] or 0),
            amount=float(data["amount"] or 0),
            filled=float(data["filled"] or 0),
            remaining=float(data["remaining"] or 0),
            cost=cost,
            fee=fee,
            fee_currency=(data.get("fee") or {}).get("currency", "USDT"),
            timestamp=datetime.fromtimestamp(data["timestamp"] / 1000, tz=timezone.utc),
            info=data.get("info", {}),
        )

    async def create_limit_buy(
        self, symbol: str, amount: float, price: float
    ) -> OrderResult:
        data = await self._call(
            self._exchange.create_limit_buy_order, symbol, amount, price
        )
        logger.info("futures_limit_buy", symbol=symbol, amount=amount, price=price)
        return self._parse_order(data)

    async def create_limit_sell(
        self, symbol: str, amount: float, price: float
    ) -> OrderResult:
        data = await self._call(
            self._exchange.create_limit_sell_order, symbol, amount, price
        )
        logger.info("futures_limit_sell", symbol=symbol, amount=amount, price=price)
        return self._parse_order(data)

    async def create_market_buy(self, symbol: str, amount: float) -> OrderResult:
        data = await self._call(self._exchange.create_market_buy_order, symbol, amount)
        logger.info("futures_market_buy", symbol=symbol, amount=amount)
        return self._parse_order(data)

    async def create_market_sell(self, symbol: str, amount: float) -> OrderResult:
        data = await self._call(self._exchange.create_market_sell_order, symbol, amount)
        logger.info("futures_market_sell", symbol=symbol, amount=amount)
        return self._parse_order(data)

    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        await self._call(self._exchange.cancel_order, order_id, symbol)
        logger.info("futures_order_cancelled", order_id=order_id, symbol=symbol)
        return True

    async def fetch_order(self, order_id: str, symbol: str) -> OrderResult:
        data = await self._call(self._exchange.fetch_order, order_id, symbol)
        return self._parse_order(data)

    # ── 선물 전용 ────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        """심볼의 레버리지를 설정한다."""
        result = await self._call(self._exchange.set_leverage, leverage, symbol)
        logger.info("leverage_set", symbol=symbol, leverage=leverage)
        return result

    async def fetch_futures_position(self, symbol: str) -> FuturesPosition | None:
        """현재 선물 포지션 조회."""
        positions = await self._call(self._exchange.fetch_positions, [symbol])
        for pos in positions:
            contracts = float(pos.get("contracts", 0) or 0)
            if contracts == 0:
                continue
            side = pos.get("side", "long")
            return FuturesPosition(
                symbol=symbol,
                side=side,
                amount=contracts,
                entry_price=float(pos.get("entryPrice", 0) or 0),
                leverage=int(pos.get("leverage", 1) or 1),
                liquidation_price=float(pos.get("liquidationPrice", 0) or 0),
                unrealized_pnl=float(pos.get("unrealizedPnl", 0) or 0),
                margin=float(pos.get("initialMargin", 0) or 0),
            )
        return None

    async def fetch_funding_rate(self, symbol: str) -> float:
        """현재 펀딩비율 조회."""
        data = await self._call(self._exchange.fetch_funding_rate, symbol)
        return float(data.get("fundingRate", 0) or 0)

    async def fetch_leverage_brackets(self, symbol: str) -> list[dict]:
        """심볼별 노셔널 브라켓 + maintMarginRatio 조회.

        Returns:
            List of bracket dicts: [{notionalFloor, notionalCap,
            maintMarginRatio, maxLeverage, cum}]
        """
        base_symbol = symbol.replace("/", "")
        data = await self._call(
            self._exchange.fapiPrivateGetLeverageBracket, {"symbol": base_symbol}
        )
        if not isinstance(data, list) or not data:
            return []
        # Response: [{"symbol": "BTCUSDT", "brackets": [...]}]
        for item in data:
            if isinstance(item, dict) and item.get("symbol", "") == base_symbol:
                return item.get("brackets", [])
        return []

    async def fetch_position_risk(self, symbol: str | None = None) -> list[dict]:
        """포지션 리스크 데이터 조회 (markPrice, marginRatio 등).

        Args:
            symbol: 심볼 필터 (None=전체).

        Returns:
            List of position risk dicts.
        """
        params: dict = {}
        if symbol:
            params["symbol"] = symbol.replace("/", "")
        data = await self._call(self._exchange.fapiPrivateV2GetPositionRisk, params)
        return data if isinstance(data, list) else []

    async def fetch_income(
        self,
        income_type: str | None = None,
        start_time: int | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        """Income API 조회 (펀딩비, 수수료, 실현손익 등).

        Args:
            income_type: "FUNDING_FEE", "COMMISSION" 등. None=전체.
            start_time: 시작 타임스탬프(ms).
            limit: 최대 레코드 수 (바이낸스 최대 1000).
        """
        params: dict = {"limit": limit}
        if income_type:
            params["incomeType"] = income_type
        if start_time:
            params["startTime"] = start_time

        data = await self._call(self._exchange.fapiPrivateGetIncome, params)
        return [
            {
                "income_type": r.get("incomeType", ""),
                "income": float(r.get("income", 0)),
                "asset": r.get("asset", "USDT"),
                "time": int(r.get("time", 0)),
                "symbol": r.get("symbol", ""),
            }
            for r in data
        ]

    # ── 선물 시장 데이터 (OI, Mark Price, Long/Short Ratio) ────────

    async def fetch_open_interest(self, symbol: str) -> OpenInterest:
        """현재 미결제약정 조회 (CCXT fetchOpenInterest)."""
        data = await self._call(self._exchange.fetch_open_interest, symbol)
        oi_value = float(data.get("openInterestValue", 0) or 0)
        ts = data.get("timestamp")
        return OpenInterest(
            symbol=symbol,
            open_interest_value=oi_value,
            timestamp=datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            if ts
            else datetime.now(timezone.utc),
        )

    async def fetch_open_interest_history(
        self,
        symbol: str,
        period: str = "1h",
        limit: int = 30,
    ) -> list[OpenInterest]:
        """미결제약정 히스토리 조회 (fapiPublicGetOpenInterestHist).

        Args:
            period: 5m/15m/30m/1h/2h/4h/6h/12h/1d.
            limit: 최대 500.
        """
        base_symbol = symbol.replace("/", "")
        data = await self._call(
            self._exchange.fapiPublicGetOpenInterestHist,
            {"symbol": base_symbol, "period": period, "limit": limit},
        )
        if not isinstance(data, list):
            return []
        result: list[OpenInterest] = []
        for r in data:
            ts = int(r.get("timestamp", 0) or 0)
            result.append(
                OpenInterest(
                    symbol=symbol,
                    open_interest_value=float(r.get("sumOpenInterestValue", 0) or 0),
                    timestamp=datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
                    if ts
                    else datetime.now(timezone.utc),
                )
            )
        return result

    async def fetch_mark_price(self, symbol: str) -> MarkPriceInfo:
        """마크 프라이스 + 프리미엄 인덱스 조회 (fapiPublicGetPremiumIndex)."""
        base_symbol = symbol.replace("/", "")
        data = await self._call(
            self._exchange.fapiPublicGetPremiumIndex,
            {"symbol": base_symbol},
        )
        # API returns a single dict when symbol is specified
        if isinstance(data, list):
            data = data[0] if data else {}

        mark = float(data.get("markPrice", 0) or 0)
        index = float(data.get("indexPrice", 0) or 0)
        funding = float(data.get("lastFundingRate", 0) or 0)
        next_funding_ts = int(data.get("nextFundingTime", 0) or 0)

        return MarkPriceInfo(
            symbol=symbol,
            mark_price=mark,
            index_price=index,
            last_funding_rate=funding,
            next_funding_time=datetime.fromtimestamp(
                next_funding_ts / 1000, tz=timezone.utc
            )
            if next_funding_ts
            else datetime.now(timezone.utc),
            timestamp=datetime.now(timezone.utc),
        )

    async def fetch_long_short_ratio(
        self,
        symbol: str,
        period: str = "1h",
    ) -> LongShortRatio:
        """Top-trader 롱숏 비율 조회 (account + position).

        Args:
            period: 5m/15m/30m/1h/2h/4h/6h/12h/1d.
        """
        base_symbol = symbol.replace("/", "")
        params = {"symbol": base_symbol, "period": period, "limit": 1}

        # Account ratio (ccxt 4.x: fapiPublic → fapiData)
        acct_data = await self._call(
            self._exchange.fapiDataGetTopLongShortAccountRatio,
            params,
        )
        acct = acct_data[0] if isinstance(acct_data, list) and acct_data else {}

        # Position ratio (ccxt 4.x: fapiPublic → fapiData)
        pos_data = await self._call(
            self._exchange.fapiDataGetTopLongShortPositionRatio,
            params,
        )
        pos = pos_data[0] if isinstance(pos_data, list) and pos_data else {}

        ts = int(acct.get("timestamp", 0) or pos.get("timestamp", 0) or 0)
        return LongShortRatio(
            symbol=symbol,
            long_account_ratio=float(acct.get("longAccount", 0) or 0),
            short_account_ratio=float(acct.get("shortAccount", 0) or 0),
            long_position_ratio=float(pos.get("longPosition", 0) or 0),
            short_position_ratio=float(pos.get("shortPosition", 0) or 0),
            timestamp=datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            if ts
            else datetime.now(timezone.utc),
        )

    # ── WebSocket (ccxt.pro) ──────────────────────────────────────

    async def create_ws_exchange(self) -> None:
        """WebSocket 전용 ccxt.pro 인스턴스 생성."""
        config: dict = {
            "enableRateLimit": True,
            "options": {"defaultType": "future"},
        }
        if self._api_key and self._api_secret:
            config["apiKey"] = self._api_key
            config["secret"] = self._api_secret

        self._ws_exchange = ccxtpro.binanceusdm(config)
        if self._testnet:
            self._ws_exchange.set_sandbox_mode(True)
        # 메모리 최적화: REST 인스턴스의 markets 공유 (~11MB 절감)
        if self._exchange and self._exchange.markets:
            self._ws_exchange.markets = self._exchange.markets
            self._ws_exchange.symbols = self._exchange.symbols
            self._ws_exchange.currencies = self._exchange.currencies
            self._ws_exchange.markets_by_id = getattr(
                self._exchange, "markets_by_id", {}
            )
        logger.info("binance_ws_exchange_created", testnet=self._testnet)

    _WS_TIMEOUT = 60  # WebSocket 수신 타임아웃 (초)

    async def watch_tickers(self, symbols: list[str]) -> dict:
        """실시간 틱커 수신 (blocking — 새 데이터 올 때마다 반환)."""
        if not self._ws_exchange:
            raise ExchangeConnectionError("WebSocket exchange not initialized")
        return await asyncio.wait_for(
            self._ws_exchange.watch_tickers(symbols), timeout=self._WS_TIMEOUT
        )

    async def watch_balance(self) -> dict:
        """실시간 잔고 수신 (blocking — 잔고 변동 시 반환)."""
        if not self._ws_exchange:
            raise ExchangeConnectionError("WebSocket exchange not initialized")
        return await asyncio.wait_for(
            self._ws_exchange.watch_balance(), timeout=self._WS_TIMEOUT
        )

    async def watch_positions(self) -> list[dict]:
        """실시간 포지션 수신 (blocking — 포지션 변동 시 반환)."""
        if not self._ws_exchange:
            raise ExchangeConnectionError("WebSocket exchange not initialized")
        return await asyncio.wait_for(
            self._ws_exchange.watch_positions(), timeout=self._WS_TIMEOUT
        )

    async def watch_mark_prices(self, symbols: list[str]) -> dict:
        """실시간 마크 프라이스 수신 (blocking — 새 데이터 올 때마다 반환).

        ccxt.pro binanceusdm의 watch_mark_prices()를 사용. 미지원 시
        watch_tickers()로 폴백하여 마크 프라이스 필드만 추출.

        Returns:
            dict: {symbol: {markPrice, indexPrice, fundingRate, nextFundingTime}}
        """
        if not self._ws_exchange:
            raise ExchangeConnectionError("WebSocket exchange not initialized")
        if not symbols:
            return {}

        ws_method = getattr(self._ws_exchange, "watch_mark_prices", None)
        if ws_method is not None:
            return await asyncio.wait_for(
                ws_method(symbols), timeout=self._WS_TIMEOUT
            )

        # Fallback: ccxt.pro version does not expose watch_mark_prices —
        # use watch_tickers and extract mark-price relevant fields.
        logger.warning("watch_mark_prices_fallback_to_tickers", symbols=symbols)
        raw = await asyncio.wait_for(
            self._ws_exchange.watch_tickers(symbols), timeout=self._WS_TIMEOUT
        )
        return {
            symbol: {
                # Prefer the raw exchange markPrice field from info; fall back
                # to last-trade only when absent (the two diverge in volatile
                # futures markets — mark price is index-derived, not trade-derived).
                "markPrice": (data.get("info") or {}).get("markPrice") or data.get("last"),
                # "index" is not a ccxt unified ticker field; Binance exposes
                # indexPrice inside the raw info dict.
                "indexPrice": (data.get("info") or {}).get("indexPrice"),
                "fundingRate": (data.get("info") or {}).get("fundingRate"),
                "nextFundingTime": (data.get("info") or {}).get("nextFundingTime"),
            }
            for symbol, data in raw.items()
        }

    async def close_ws(self) -> None:
        """WebSocket 연결 정리."""
        if self._ws_exchange:
            await self._ws_exchange.close()
            self._ws_exchange = None
            logger.info("binance_ws_closed")
