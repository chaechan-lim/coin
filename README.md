# Coin Auto-Trading System

Bithumb (spot) + Binance Spot + Binance USDM (futures) **triple-engine** auto-trading bot.
Spot 4 strategies + Futures 6 strategies, weighted voting, dynamic SL/TP, volume surge rotation, AI agents, React dashboard (8 tabs). 338 tests.

---

## Architecture

```
                    ┌──────────────┐
                    │  React UI    │ :3000
                    │  Dashboard   │ (8 tabs)
                    └──────┬───────┘
                           │ REST + WebSocket
                    ┌──────┴───────┐
                    │   FastAPI    │ :8000
                    │   Backend    │
                    └──────┬───────┘
                           │
          ┌────────────────┼────────────────┐
          │                │                │
   ┌──────┴──────┐ ┌──────┴──────┐ ┌───────┴──────┐
   │  Strategies  │ │  Engines×3  │ │  AI Agents   │
   │  Spot 4 +   │ │ Bithumb     │ │  Market/Risk │
   │  Futures 6  │ │ + BN Spot   │ │  TradeReview │
   └──────┬──────┘ │ + BN Future │ └──────────────┘
          │        └──────┬──────┘
   ┌──────┴──────┐        │
   │  Combiner   │ ┌──────┴──────┐
   │  (weighted) │ │  PostgreSQL │
   └─────────────┘ └─────────────┘
```

### Triple Engine

| Engine | Exchange | Market | Features |
|--------|----------|--------|----------|
| TradingEngine | Bithumb V2 | Spot (KRW) | SL/TP/trailing, rotation, dynamic SL, asymmetric mode |
| TradingEngine | Binance Spot | Spot (USDT) | Same as Bithumb, USDT base currency |
| BinanceFuturesEngine | Binance USDM | Futures (USDT) | Long/short, 3x leverage, liquidation guard, WebSocket monitor |

### Strategies

**Spot (4 strategies — v0.23)**

| Strategy | Weight | Description |
|---|---|---|
| CIS Momentum | 0.32 | Pure momentum (ADX+RSI trend-follow) |
| Larry Williams | 0.32 | Volatility breakout + Williams %R |
| Donchian Channel | 0.26 | Turtle trading (20/10 period channel) |
| BNF Deviation | 0.10 | Mean reversion (Bollinger deviation) |

**Futures (6 strategies)**

| Strategy | Weight | Description |
|---|---|---|
| Bollinger + RSI | 0.31 | Bollinger band + RSI composite |
| RSI | 0.25 | RSI oversold/overbought reversal |
| Stochastic RSI | 0.15 | Stochastic RSI momentum |
| OBV Divergence | 0.13 | On-balance volume divergence |
| MACD Crossover | 0.08 | MACD/Signal crossover |
| MA Crossover | 0.08 | Moving average crossover |

### Safety Features

| Feature | Description |
|---------|-------------|
| Cross-exchange conflict | Blocks spot buy if futures short exists (and vice versa) |
| Post-sell washout | 4h cooldown before re-buying same coin after sell |
| PositionTracker DB | SL/TP/trailing survives server restart |
| Snapshot reconcile | Prevents fake balance spikes from async interleaving |
| Asymmetric mode | Blocks spot buys in downtrend/crash markets |

---

## Prerequisites

- Python 3.12+
- Node.js 18+ (via NVM)
- Docker Engine (for PostgreSQL)

---

## Quick Start

### 1. PostgreSQL Setup

```bash
# Docker Engine 설치 (WSL2 — sudo 필요)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# 쉘 재시작 후:

# PostgreSQL만 기동 (backend는 직접 실행)
docker compose up -d postgres

# 연결 확인
docker compose exec postgres pg_isready -U coin -d coin_trading
```

### 2. Backend Setup

```bash
cd backend

# venv 생성 (최초 1회)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# .env 확인 (기본값: PostgreSQL)
# DB_URL=postgresql+asyncpg://coin:coin@localhost:5432/coin_trading

# 서버 실행
.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
```

### 3. Frontend Setup

```bash
cd frontend
npm install
npm run dev   # http://localhost:3000 (vite --host 0.0.0.0)
```

### 4. Engine Start

```bash
# 빗썸 현물 엔진 시작
curl -X POST http://localhost:8000/api/v1/engine/start

# 바이낸스 선물 엔진 시작
curl -X POST "http://localhost:8000/api/v1/engine/start?exchange=binance_futures"

# 바이낸스 현물 엔진 시작
curl -X POST "http://localhost:8000/api/v1/engine/start?exchange=binance_spot"

# 선물 엔진 중지 (포지션 있으면 경고)
curl -X POST "http://localhost:8000/api/v1/engine/stop?exchange=binance_futures"
# 강제 중지
curl -X POST "http://localhost:8000/api/v1/engine/stop?exchange=binance_futures&force=true"
```

- Dashboard: http://localhost:3000
- API Docs: http://localhost:8000/docs
- Health: http://localhost:8000/health

---

## Key Configuration (.env)

| Variable | Description | Default |
|----------|-------------|---------|
| `TRADING_MODE` | `paper` / `live` (Bithumb) | `paper` |
| `BINANCE_ENABLED` | Enable Binance futures | `false` |
| `BINANCE_TRADING_MODE` | `paper` / `live` (futures, independent) | `paper` |
| `BINANCE_SPOT_ENABLED` | Enable Binance spot | `false` |
| `BINANCE_SPOT_TRADING_MODE` | `paper` / `live` (spot, independent) | `paper` |
| `DB_URL` | Database connection string | PostgreSQL |
| `BINANCE_DEFAULT_LEVERAGE` | Futures leverage | `3` |

---

## Testing

```bash
cd backend
.venv/bin/python -m pytest tests/ -v   # 338 tests
# Tests use in-memory SQLite (aiosqlite)
```

---

## Backtest

```bash
cd backend

# 기본 (BTC, 540일, 4시간봉)
.venv/bin/python backtest.py

# 선물 모드 (롱/숏 + 레버리지)
.venv/bin/python backtest.py --futures --leverage 3

# 로테이션 모드 (20코인 서지 감지)
.venv/bin/python backtest.py --rotation --dynamic-rotation
```

---

## Docker (Full Stack)

```bash
docker compose up -d --build
docker compose logs -f backend
```

---

## Raspberry Pi (ARM64)

```bash
# PostgreSQL만 Docker로 실행 (postgres:16-alpine ARM64 지원)
docker compose up -d postgres

# Backend는 직접 실행
cd backend && .venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
```

---

## License

Private project. All rights reserved.
