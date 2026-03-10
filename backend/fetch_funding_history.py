"""
바이낸스 선물 과거 펀딩비 데이터 가져오기.

캐시: data/funding/{SYMBOL}_funding.csv
8시간 간격, 최대 1000개/요청 (~333일)

실행:
  cd backend && .venv/bin/python fetch_funding_history.py --days 540
"""
import asyncio
import argparse
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

CACHE_DIR = Path(__file__).parent / "data" / "funding"
DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT"]


async def fetch_funding_rates(symbol: str, days: int = 540) -> pd.DataFrame:
    """바이낸스 과거 펀딩비 조회 (public API).

    Returns:
        DataFrame with columns: [timestamp, funding_rate], indexed by timestamp
    """
    import ccxt.async_support as ccxt

    exchange = ccxt.binanceusdm()

    try:
        all_records = []
        end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
        start_time = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)

        current_start = start_time
        while current_start < end_time:
            params = {
                "symbol": symbol,
                "startTime": current_start,
                "limit": 1000,
            }
            data = await exchange.fapiPublicGetFundingRate(params)

            if not data:
                break

            for item in data:
                ts = datetime.fromtimestamp(int(item["fundingTime"]) / 1000, tz=timezone.utc)
                rate = float(item["fundingRate"])
                all_records.append({"timestamp": ts, "funding_rate": rate})

            # 다음 페이지
            last_time = int(data[-1]["fundingTime"])
            if last_time <= current_start:
                break
            current_start = last_time + 1

        if not all_records:
            return pd.DataFrame()

        df = pd.DataFrame(all_records)
        df.set_index("timestamp", inplace=True)
        df.sort_index(inplace=True)
        return df

    finally:
        await exchange.close()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=540)
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    args = parser.parse_args()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    for symbol in args.symbols:
        cache_path = CACHE_DIR / f"{symbol}_funding.csv"

        # 캐시 확인 (24시간 이내면 스킵)
        if cache_path.exists():
            age_hours = (datetime.now().timestamp() - cache_path.stat().st_mtime) / 3600
            if age_hours < 24:
                existing = pd.read_csv(cache_path, index_col=0, parse_dates=True)
                print(f"  {symbol}: 캐시 사용 ({len(existing)}건, {age_hours:.1f}시간 전)")
                continue

        print(f"  {symbol}: 펀딩비 {args.days}일 로딩...", end="", flush=True)
        df = await fetch_funding_rates(symbol, args.days)
        if len(df) > 0:
            df.to_csv(cache_path)
            print(f" {len(df)}건 ({df.index[0].date()} ~ {df.index[-1].date()})")
        else:
            print(" 실패 (데이터 없음)")


if __name__ == "__main__":
    asyncio.run(main())
