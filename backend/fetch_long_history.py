"""
바이낸스에서 장기 OHLCV 데이터를 가져와 KRW 변환 후 CSV 캐시에 저장.

빗썸 API는 1h 캔들 ~2,300개(≈97일)만 제공하므로,
바이낸스 USDT 쌍에서 데이터를 가져와 KRW 환산 후 빗썸 캐시와 병합.

사용법:
  python fetch_long_history.py --days 540
  python fetch_long_history.py --days 540 --timeframe 4h
  python fetch_long_history.py --days 540 --coins BTC ETH XRP SOL ADA
"""

import asyncio
import argparse
from datetime import datetime, timezone
from pathlib import Path

import ccxt.async_support as ccxt
import pandas as pd


# KRW 변환 (전략은 % 변동 기반이므로 근사값으로 충분)
# 바이낸스 USDT→KRW 환산 배수
USDT_KRW_RATE = 1_400

COIN_MAP = {
    "BTC": ("BTC/USDT", "BTC_KRW"),
    "ETH": ("ETH/USDT", "ETH_KRW"),
    "XRP": ("XRP/USDT", "XRP_KRW"),
    "SOL": ("SOL/USDT", "SOL_KRW"),
    "ADA": ("ADA/USDT", "ADA_KRW"),
}


def _tf_ms(tf: str) -> int:
    """타임프레임 → 밀리초."""
    m = {"1m": 60_000, "5m": 300_000, "15m": 900_000,
         "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
    return m[tf]


def _tf_hours(tf: str) -> float:
    m = {"1m": 1/60, "5m": 5/60, "15m": 15/60, "1h": 1, "4h": 4, "1d": 24}
    return m[tf]


async def fetch_binance_ohlcv(
    exchange: ccxt.binance,
    symbol: str,
    timeframe: str,
    days: int,
) -> pd.DataFrame:
    """바이낸스에서 페이지네이션으로 장기 데이터 fetch."""
    tf_ms = _tf_ms(timeframe)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    candles_needed = int(days * 24 / _tf_hours(timeframe)) + 200
    start_ms = now_ms - candles_needed * tf_ms

    all_data = []
    cursor = start_ms
    page_limit = 1000  # 바이낸스 최대 1000

    print(f"  {symbol}: fetching {candles_needed} candles from "
          f"{datetime.fromtimestamp(start_ms/1000, tz=timezone.utc).strftime('%Y-%m-%d')}...")

    page = 0
    while cursor < now_ms:
        try:
            raw = await exchange.fetch_ohlcv(
                symbol, timeframe, since=cursor, limit=page_limit,
            )
        except Exception as e:
            print(f"    Error at page {page}: {e}")
            break

        if not raw:
            break

        all_data.extend(raw)
        last_ts = raw[-1][0]
        if last_ts <= cursor:
            break
        cursor = last_ts + tf_ms
        page += 1

        if len(raw) < page_limit:
            break

        # 레이트 리밋 (public API: 1200/min)
        if page % 10 == 0:
            await asyncio.sleep(0.5)
            print(f"    ...{len(all_data)} candles fetched (page {page})")

    if not all_data:
        raise ValueError(f"No data for {symbol}")

    df = pd.DataFrame(all_data, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    df = df[~df.index.duplicated(keep="last")]
    df.sort_index(inplace=True)

    print(f"    {len(df)} candles: "
          f"{df.index[0].strftime('%Y-%m-%d')} ~ {df.index[-1].strftime('%Y-%m-%d')}")
    return df


async def main():
    parser = argparse.ArgumentParser(description="바이낸스 장기 데이터 → KRW CSV 캐시")
    parser.add_argument("--days", type=int, default=540, help="가져올 일수")
    parser.add_argument("--timeframe", type=str, default="1h", help="타임프레임")
    parser.add_argument("--coins", nargs="+", default=["BTC", "ETH", "XRP", "SOL", "ADA"])
    parser.add_argument("--rate", type=float, default=USDT_KRW_RATE,
                        help=f"USDT/KRW 환산율 (기본 {USDT_KRW_RATE})")
    args = parser.parse_args()

    cache_dir = Path(__file__).parent / ".cache"
    cache_dir.mkdir(exist_ok=True)

    # 바이낸스 공개 API (키 불필요)
    exchange = ccxt.binance({"enableRateLimit": True})

    try:
        print(f"바이낸스에서 {args.days}일 {args.timeframe} 데이터 수집 시작")
        print(f"USDT→KRW 환산율: {args.rate:,.0f}")
        print()

        for coin in args.coins:
            if coin not in COIN_MAP:
                print(f"  {coin}: 지원하지 않는 코인 (건너뜀)")
                continue

            usdt_symbol, krw_name = COIN_MAP[coin]
            cache_path = cache_dir / f"{krw_name}_{args.timeframe}.csv"

            # 기존 빗썸 캐시 백업
            backup_path = cache_dir / f"{krw_name}_{args.timeframe}.bithumb_backup.csv"
            if cache_path.exists() and not backup_path.exists():
                import shutil
                shutil.copy2(cache_path, backup_path)
                print(f"  기존 캐시 백업: {backup_path.name}")

            # 바이낸스에서 fetch
            df = await fetch_binance_ohlcv(exchange, usdt_symbol, args.timeframe, args.days)

            # USDT → KRW 변환
            for col in ["open", "high", "low", "close"]:
                df[col] = df[col] * args.rate
            # 볼륨은 코인 단위 그대로 (KRW 변환 불필요)

            # 기존 빗썸 최근 데이터 병합 (빗썸 데이터가 있으면 최근 부분은 빗썸 우선)
            if backup_path.exists():
                bithumb_df = pd.read_csv(
                    backup_path, parse_dates=["timestamp"], index_col="timestamp",
                )
                if bithumb_df.index.tz is None:
                    bithumb_df.index = bithumb_df.index.tz_localize("UTC")
                bithumb_df.sort_index(inplace=True)

                # 바이낸스 데이터 + 빗썸 최근 데이터 (빗썸이 있는 구간은 빗썸 우선)
                bithumb_start = bithumb_df.index[0]
                binance_old = df[df.index < bithumb_start]
                merged = pd.concat([binance_old, bithumb_df])
                merged = merged[~merged.index.duplicated(keep="last")]
                merged.sort_index(inplace=True)
                df = merged
                print(f"    빗썸 데이터 병합: {len(bithumb_df)}캔들 (최근 구간 빗썸 원본 유지)")

            # CSV 저장
            df.to_csv(cache_path)
            print(f"    저장: {cache_path.name} ({len(df)} rows)")
            print(f"    범위: {df.index[0].strftime('%Y-%m-%d %H:%M')} ~ {df.index[-1].strftime('%Y-%m-%d %H:%M')}")
            print()

    finally:
        await exchange.close()

    print("완료! 이제 --days 540 백테스트를 실행할 수 있습니다:")
    print(f"  python backtest.py --portfolio --days 540 --risk --trade-limits --asymmetric")


if __name__ == "__main__":
    asyncio.run(main())
