"""
SQLite 마이그레이션: exchange 컬럼 추가 (듀얼 엔진 지원)
==========================================================
기존 SQLite DB에 exchange 컬럼을 추가하고, Position 테이블의
unique 제약을 (symbol, exchange) 복합 키로 변경.

사용법:
    cd /home/chans/coin/backend
    .venv/bin/python db/migrate.py
"""

import sqlite3
import sys
import os

# DB 경로: 환경변수 또는 기본값
DB_URL = os.environ.get("DB_URL", "")
if "sqlite" in DB_URL:
    DB_PATH = DB_URL.split("///")[-1]
else:
    DB_PATH = os.path.join(os.path.dirname(__file__), "..", "coin_trading.db")


TABLES_NEED_EXCHANGE = [
    "orders",
    "trades",
    "positions",
    "portfolio_snapshots",
    "strategy_logs",
    "agent_analysis_logs",
]

POSITION_FUTURES_COLUMNS = {
    "direction": "TEXT NOT NULL DEFAULT 'long'",
    "leverage": "INTEGER NOT NULL DEFAULT 1",
    "liquidation_price": "REAL",
    "margin_used": "REAL NOT NULL DEFAULT 0.0",
    "last_sell_direction": "TEXT",  # COIN-41: v2 방향별 쿨다운 영속화
    "lowest_price": "REAL",         # COIN-64: 숏 포지션 extreme_price (최저가)
}


def has_column(cursor, table: str, column: str) -> bool:
    cursor.execute(f"PRAGMA table_info({table})")
    columns = [row[1] for row in cursor.fetchall()]
    return column in columns


def migrate(db_path: str) -> None:
    if not os.path.exists(db_path):
        print(
            f"DB not found at {db_path} — skipping migration (will be created on first run)"
        )
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    print(f"Migrating {db_path} ...")

    # 1. Add exchange column to all tables
    for table in TABLES_NEED_EXCHANGE:
        if not has_column(cursor, table, "exchange"):
            print(f"  ADD COLUMN {table}.exchange")
            cursor.execute(
                f"ALTER TABLE {table} ADD COLUMN exchange TEXT NOT NULL DEFAULT 'bithumb'"
            )
        else:
            print(f"  {table}.exchange already exists — skip")

    # 2. Add futures columns to positions
    for col, typedef in POSITION_FUTURES_COLUMNS.items():
        if not has_column(cursor, "positions", col):
            print(f"  ADD COLUMN positions.{col}")
            cursor.execute(f"ALTER TABLE positions ADD COLUMN {col} {typedef}")
        else:
            print(f"  positions.{col} already exists — skip")

    # 3. Recreate positions table to change unique constraint
    #    SQLite doesn't support DROP CONSTRAINT, so we use batch mode (rename+recreate+copy)
    cursor.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='positions'"
    )
    row = cursor.fetchone()
    if row:
        create_sql = row[0]
        # Check if the old unique constraint on symbol alone exists
        if "uq_position_symbol_exchange" not in create_sql and "UNIQUE" in create_sql:
            print(
                "  Rebuilding positions table for composite unique (symbol, exchange)..."
            )
            cursor.execute("ALTER TABLE positions RENAME TO _positions_old")
            cursor.execute("""
                CREATE TABLE positions (
                    id INTEGER PRIMARY KEY,
                    exchange TEXT NOT NULL DEFAULT 'bithumb',
                    symbol VARCHAR(20) NOT NULL,
                    quantity REAL DEFAULT 0.0,
                    average_buy_price REAL DEFAULT 0.0,
                    total_invested REAL DEFAULT 0.0,
                    current_value REAL DEFAULT 0.0,
                    unrealized_pnl REAL DEFAULT 0.0,
                    unrealized_pnl_pct REAL DEFAULT 0.0,
                    is_paper BOOLEAN DEFAULT 1,
                    is_surge BOOLEAN DEFAULT 0,
                    direction TEXT DEFAULT 'long',
                    leverage INTEGER DEFAULT 1,
                    liquidation_price REAL,
                    margin_used REAL DEFAULT 0.0,
                    entered_at DATETIME,
                    updated_at DATETIME,
                    UNIQUE (symbol, exchange)
                )
            """)
            cursor.execute("""
                INSERT INTO positions (
                    id, exchange, symbol, quantity, average_buy_price,
                    total_invested, current_value, unrealized_pnl,
                    unrealized_pnl_pct, is_paper, is_surge,
                    direction, leverage, liquidation_price, margin_used,
                    entered_at, updated_at
                )
                SELECT
                    id, exchange, symbol, quantity, average_buy_price,
                    total_invested, current_value, unrealized_pnl,
                    unrealized_pnl_pct, is_paper, is_surge,
                    COALESCE(direction, 'long'),
                    COALESCE(leverage, 1),
                    liquidation_price,
                    COALESCE(margin_used, 0.0),
                    entered_at, updated_at
                FROM _positions_old
            """)
            cursor.execute("DROP TABLE _positions_old")
            print("  positions table rebuilt successfully")
        else:
            print("  positions unique constraint already correct — skip")

    # 4. capital_transactions (exchange, exchange_tx_id) 유니크 인덱스 추가
    #    SQLite는 CREATE UNIQUE INDEX IF NOT EXISTS 지원
    cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_capital_tx_exchange_txid
        ON capital_transactions (exchange, exchange_tx_id)
        WHERE exchange_tx_id IS NOT NULL
    """)
    print("  uq_capital_tx_exchange_txid index ensured")

    conn.commit()
    conn.close()
    print("Migration complete!")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else DB_PATH
    migrate(path)
