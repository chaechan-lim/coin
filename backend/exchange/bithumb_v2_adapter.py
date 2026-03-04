"""
Bithumb V2 API adapter.

Public endpoints (OHLCV, ticker, orderbook) → ccxt (inherited from BithumbAdapter)
Private endpoints (balance, orders) → direct aiohttp + JWT auth
"""
import asyncio
import hashlib
import time
import uuid as uuid_mod
import structlog
from datetime import datetime, timezone
from urllib.parse import urlencode

import aiohttp
import jwt

from exchange.bithumb_adapter import BithumbAdapter
from exchange.data_models import Balance, OrderResult
from core.exceptions import (
    ExchangeConnectionError,
    ExchangeRateLimitError,
    InsufficientBalanceError,
    ExchangeError,
    OrderNotFoundError,
)

logger = structlog.get_logger(__name__)

_BASE = "https://api.bithumb.com"


class BithumbV2Adapter(BithumbAdapter):
    """Bithumb adapter: ccxt for public data, V2 JWT for private endpoints."""

    def __init__(self, api_key: str = "", api_secret: str = "", rate_limit: int = 8):
        # ccxt is used only for public API — no auth keys passed
        super().__init__(api_key="", api_secret="", rate_limit=rate_limit)
        self._v2_key = api_key
        self._v2_secret = api_secret
        self._http: aiohttp.ClientSession | None = None

    async def initialize(self) -> None:
        await super().initialize()  # ccxt load_markets
        self._http = aiohttp.ClientSession()
        logger.info("bithumb_v2_ready")

    async def close(self) -> None:
        if self._http and not self._http.closed:
            await self._http.close()
        await super().close()

    # ── JWT auth ───────────────────────────────────────────

    def _jwt_token(self, params: dict | None = None) -> str:
        """Create JWT Bearer token for V2 auth."""
        payload = {
            "access_key": self._v2_key,
            "nonce": str(uuid_mod.uuid4()),
            "timestamp": round(time.time() * 1000),
        }
        if params:
            m = hashlib.sha512()
            m.update(urlencode(params).encode())
            payload["query_hash"] = m.hexdigest()
            payload["query_hash_alg"] = "SHA512"
        return jwt.encode(payload, self._v2_secret, algorithm="HS256")

    def _auth_hdr(self, params: dict | None = None) -> dict:
        return {"Authorization": f"Bearer {self._jwt_token(params)}"}

    async def _v2(self, method: str, path: str, params: dict | None = None):
        """Authenticated V2 request with error handling."""
        headers = self._auth_hdr(params)
        url = f"{_BASE}{path}"

        async with self._semaphore:
            try:
                if method == "GET":
                    r = await self._http.get(url, params=params, headers=headers)
                elif method == "POST":
                    # Bithumb V2 (Upbit-style): POST body = JSON, query_hash = urlencode(params)
                    r = await self._http.post(url, json=params, headers=headers)
                elif method == "DELETE":
                    r = await self._http.delete(url, params=params, headers=headers)
                else:
                    raise ValueError(f"Unsupported method: {method}")

                body = await r.json()

                if r.status == 429:
                    raise ExchangeRateLimitError("V2 rate limit exceeded")
                if r.status >= 400:
                    err = body.get("error", {})
                    name = err.get("name", "")
                    msg = err.get("message", str(body))
                    if "insufficient_funds" in name:
                        raise InsufficientBalanceError(msg)
                    if "order_not_found" in name:
                        raise OrderNotFoundError(msg)
                    raise ExchangeError(f"V2 {r.status}: {name} — {msg}")

                return body
            except aiohttp.ClientError as e:
                raise ExchangeConnectionError(f"V2 network error: {e}")

    # ── Symbol helpers ─────────────────────────────────────

    @staticmethod
    def _to_market(symbol: str) -> str:
        """BTC/KRW → KRW-BTC"""
        base, quote = symbol.split("/")
        return f"{quote}-{base}"

    @staticmethod
    def _from_market(market: str) -> str:
        """KRW-BTC → BTC/KRW"""
        quote, base = market.split("-")
        return f"{base}/{quote}"

    # ── Balance ────────────────────────────────────────────

    async def fetch_balance(self) -> dict[str, Balance]:
        rows = await self._v2("GET", "/v1/accounts")
        out = {}
        for r in rows:
            cur = r["currency"]
            free = float(r["balance"])
            locked = float(r["locked"])
            out[cur] = Balance(
                currency=cur, free=free, used=locked, total=free + locked,
            )
        return out

    # ── Order parsing ──────────────────────────────────────

    def _parse_v2(self, d: dict, symbol: str = "") -> OrderResult:
        """Parse V2 order response into OrderResult."""
        sym = symbol or self._from_market(d.get("market", ""))
        side = {"bid": "buy", "ask": "sell"}.get(d.get("side", ""), d.get("side", ""))
        status = {
            "wait": "open", "watch": "open",
            "done": "closed", "cancel": "canceled",
        }.get(d.get("state", ""), d.get("state", ""))

        vol = float(d.get("volume") or 0)
        remaining = float(d.get("remaining_volume") or 0)
        filled = float(d.get("executed_volume") or 0)
        price = float(d.get("price") or 0)
        fee = float(d.get("paid_fee") or 0)

        # Compute cost from individual trades if available
        trades = d.get("trades", [])
        if trades:
            cost = sum(float(t.get("funds") or 0) for t in trades)
            if filled > 0:
                price = cost / filled
        else:
            cost = price * filled

        try:
            ts = datetime.fromisoformat(d["created_at"])
        except (KeyError, ValueError):
            ts = datetime.now(timezone.utc)

        return OrderResult(
            order_id=d.get("uuid") or d.get("order_id", ""),
            symbol=sym, side=side, order_type=d.get("ord_type", "limit"),
            status=status, price=price, amount=vol, filled=filled,
            remaining=remaining, cost=cost, fee=fee, fee_currency="KRW",
            timestamp=ts, info=d,
        )

    async def _poll_fill(
        self, oid: str, symbol: str, timeout: float = 10,
    ) -> OrderResult:
        """Poll order until filled or timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = await self.fetch_order(oid, symbol)
            if r.status in ("closed", "canceled"):
                return r
            await asyncio.sleep(0.3)
        return await self.fetch_order(oid, symbol)

    # ── Orders ─────────────────────────────────────────────

    async def create_limit_buy(
        self, symbol: str, amount: float, price: float,
    ) -> OrderResult:
        p = {
            "market": self._to_market(symbol), "side": "bid",
            "volume": str(amount), "price": str(int(price)),
            "order_type": "limit",
        }
        d = await self._v2("POST", "/v2/orders", p)
        oid = d.get("uuid") or d.get("order_id", "")
        logger.info("v2_limit_buy", symbol=symbol, amount=amount, price=price, oid=oid)
        return await self._poll_fill(oid, symbol)

    async def create_limit_sell(
        self, symbol: str, amount: float, price: float,
    ) -> OrderResult:
        p = {
            "market": self._to_market(symbol), "side": "ask",
            "volume": str(amount), "price": str(int(price)),
            "order_type": "limit",
        }
        d = await self._v2("POST", "/v2/orders", p)
        oid = d.get("uuid") or d.get("order_id", "")
        logger.info("v2_limit_sell", symbol=symbol, amount=amount, price=price, oid=oid)
        return await self._poll_fill(oid, symbol)

    async def create_market_buy(self, symbol: str, amount: float) -> OrderResult:
        """Market buy: ord_type=price, price = total KRW to spend."""
        ticker = await self.fetch_ticker(symbol)
        krw = int(amount * ticker.ask)
        # 빗썸 최소 주문 금액: 5000 KRW
        if krw < 5000:
            raise ExchangeError(f"주문 금액 부족: {krw} KRW < 5000 KRW")
        # 빗썸 시장가 매수: price는 1000원 단위로 절삭 (고가 코인 호가 단위)
        krw = (krw // 1000) * 1000
        if krw < 5000:
            raise ExchangeError(f"주문 금액 부족 (1000원 절삭 후): {krw} KRW < 5000 KRW")
        p = {
            "market": self._to_market(symbol), "side": "bid",
            "price": str(krw), "order_type": "price",
        }
        logger.info("v2_market_buy_attempt", symbol=symbol, amount=amount, krw=krw, params=p)
        d = await self._v2("POST", "/v2/orders", p)
        oid = d.get("uuid") or d.get("order_id", "")
        logger.info("v2_market_buy", symbol=symbol, amount=amount, krw=krw, oid=oid)
        return await self._poll_fill(oid, symbol)

    async def create_market_sell(self, symbol: str, amount: float) -> OrderResult:
        """Market sell: ord_type=market, volume = base currency amount."""
        p = {
            "market": self._to_market(symbol), "side": "ask",
            "volume": str(amount), "order_type": "market",
        }
        d = await self._v2("POST", "/v2/orders", p)
        oid = d.get("uuid") or d.get("order_id", "")
        logger.info("v2_market_sell", symbol=symbol, amount=amount, oid=oid)
        return await self._poll_fill(oid, symbol)

    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        await self._v2("DELETE", "/v2/order", {"order_id": order_id})
        logger.info("v2_cancelled", oid=order_id, symbol=symbol)
        return True

    async def fetch_order(self, order_id: str, symbol: str) -> OrderResult:
        d = await self._v2("GET", "/v1/order", {"uuid": order_id})
        return self._parse_v2(d, symbol)
