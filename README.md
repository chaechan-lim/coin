# Coin Auto-Trading System

Bithumb (spot, inactive) + Binance Spot + Binance USDM (main futures engine disabled) + live R&D futures engines (`Donchian Futures Bi`, `Pairs Trading`) + Surge support.
Spot 4 strategies + Futures 7 strategies + ML signal filter, weighted voting, dynamic SL/TP, shared futures R&D risk coordinator, research promotion pipeline, Discord bot, React dashboard.

---

## Architecture

```
                    ┌──────────────┐
                    │  React UI    │ :3000
                    │  Dashboard   │
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
   │  Strategies  │ │   Engines   │ │  AI Agents   │
   │  Spot 4 +   │ │ Bithumb     │ │  Market/Risk │
   │  Futures 7  │ │ + BN Spot   │ │  TradeReview │
   │  + ML Filter │ │ + R&D + Surge│ │  + Analytics │
   └──────┬──────┘ │ + Surge     │ └──────────────┘
          │        └──────┬──────┘
   ┌──────┴──────┐        │
   │  Combiner   │ ┌──────┴──────┐
   │  (weighted) │ │  PostgreSQL │
   └─────────────┘ └─────────────┘
```

### Engine Layout

| Engine | Exchange | Market | Features |
|--------|----------|--------|----------|
| TradingEngine | Bithumb V2 | Spot KRW (inactive) | SL/TP/trailing, rotation, dynamic SL, asymmetric mode |
| TradingEngine | Binance Spot | Spot USDT (live) | Same as Bithumb, USDT base, paired exit |
| BinanceFuturesEngine / FuturesEngineV2 | Binance USDM | Futures USDT | Main engine available but can be disabled independently |
| DonchianFuturesBiEngine | Binance USDM | Futures USDT (live R&D) | Daily breakout long/short, grouped trade journal, shared R&D coordinator |
| PairsTradingLiveEngine | Binance USDM | Futures USDT (live R&D) | Delta-neutral pair trading, grouped trade journal, shared R&D coordinator |
| SurgeEngine | Binance Surge | Futures USDT | Volume spike detection, short-term trades, shares futures PM |

### Research Pipeline

- `GET /api/v1/research/overview` returns the R&D candidate board.
- `GET /api/v1/research/stages` returns the approved/effective stage state.
- `GET /api/v1/research/stage-history` returns recent promotion/demotion approvals.
- `PUT /api/v1/research/candidates/{candidate_key}/stage` is the approval path for promotion/demotion.
- `GET /api/v1/research/auto-review/status` returns auto-review cache readiness and refresh age.
- Auto review is refreshed in the background and cached, not recomputed on every request.
- Cold start can briefly return `pending` until the first background refresh finishes.
- `auto_review` only recommends. Actual live execution permission follows the approved effective stage.
- Execution is hard-gated by stage for dedicated R&D engines:
  - `research`, `candidate`, `shadow`, `hold`: no live order execution
  - `live_rnd`, `production`: live execution allowed
- `Pairs Trading` and `Donchian Futures Bi` include live execution metrics in `auto_review`.
- Shared futures R&D risk is managed by `GET /api/v1/engine/futures-rnd/status`.
- Dashboard overview now includes:
  - `트레이딩 대상 잔고`: separates `Main Spot History` from current `Donchian Spot` live status, plus futures R&D capital/trade status
  - `R&D 파이프라인`: grouped trade summaries for `Pairs` and `Donchian Futures`
  - `메인 엔진 제어`: only controls `binance_spot` / `binance_futures` main engines, not the R&D engines
- Dashboard tabs are grouped by intent:
  - `개요`: current engines and capital allocation
  - `실거래`: trade history, portfolio, grouped trades, daily PnL
  - `R&D`: candidate board and grouped trades
  - `운영 로그`: engine live feed, agent panel, system events
  - `고급`: signal log, strategy performance, rotation, extra stats
- Heavy dashboard panels are lazy-loaded by tab to keep the initial bundle smaller.
- `실거래` now includes grouped-trade KPI cards for `Pairs` and `Donchian Futures` with `today / 7d / 30d` period switching.
- `R&D` now includes promotion hints with direct actions to the relevant `실거래` or `운영 로그` tab for next verification.
- `R&D` also shows `catalog/effective stage`, `stage source`, approval note, and an inline approval form with recent stage history.

### Strategies

**Spot (4 strategies — Optuna optimized)**

| Strategy | Weight | Description |
|---|---|---|
| Larry Williams | 0.31 | Volatility breakout + Williams %R |
| Donchian Channel | 0.24 | Turtle trading (20/10 period channel) |
| BNF Deviation | 0.23 | Mean reversion (Bollinger deviation) |
| CIS Momentum | 0.22 | Pure momentum (ADX+RSI trend-follow) |

**Futures (7 strategies + ML filter)**

| Strategy | Weight | Description |
|---|---|---|
| Bollinger + RSI | 0.26 | Bollinger band + RSI composite |
| RSI | 0.21 | RSI oversold/overbought reversal |
| BB Squeeze | 0.15 | Bollinger squeeze breakout detection |
| Stochastic RSI | 0.13 | Stochastic RSI momentum |
| OBV Divergence | 0.11 | On-balance volume divergence |
| MA Crossover | 0.07 | Moving average crossover |
| MACD Crossover | 0.07 | MACD/Signal crossover |

**ML Signal Filter**: LightGBM (23 features) — pre-filters futures signals for higher win rate.

### Safety Features

| Feature | Description |
|---------|-------------|
| Cross-exchange conflict | Blocks spot buy if futures short exists; high confidence (>=0.65) triggers position flip |
| Post-sell cooldown | Spot 32h (cd8), Futures 24h (cd6), Surge 60min |
| PositionTracker DB | SL/TP/trailing survives server restart (7 columns persisted) |
| Spike defense | 6-layer fake price spike protection |
| Asymmetric mode | Blocks spot buys in downtrend/crash markets |
| Paired exit | Spot: only the entry strategy can trigger sell |
| Self-healing | ErrorClassifier → RecoveryManager → DiagnosticAgent (LLM) |

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

# 메인 선물 엔진 중지 (포지션 있으면 경고)
curl -X POST "http://localhost:8000/api/v1/engine/stop?exchange=binance_futures"
# 강제 중지
curl -X POST "http://localhost:8000/api/v1/engine/stop?exchange=binance_futures&force=true"
```

For dedicated research engines, `POST /api/v1/engine/start` is blocked unless the candidate is approved to `live_rnd` or `production`.

R&D futures engines are normally controlled by `.env` flags and auto-start on backend boot:

```bash
APP_DONCHIAN_FUTURES_BI_ENABLED=true
APP_DONCHIAN_FUTURES_BI_CAPITAL_USDT=100
APP_PAIRS_TRADING_LIVE_ENABLED=true
APP_PAIRS_TRADING_LIVE_CAPITAL_USDT=50
BINANCE_TRADING_ENABLED=false
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
| `APP_DONCHIAN_FUTURES_BI_ENABLED` | Enable Donchian Futures Bi live R&D | `false` |
| `APP_DONCHIAN_FUTURES_BI_CAPITAL_USDT` | Donchian Futures Bi capital budget | `100` |
| `APP_PAIRS_TRADING_LIVE_ENABLED` | Enable Pairs live R&D | `false` |
| `APP_PAIRS_TRADING_LIVE_CAPITAL_USDT` | Pairs live R&D capital budget | `50` |
| `SURGE_TRADING_ENABLED` | Enable surge engine | `false` |
| `DISCORD_BOT_ENABLED` | Enable Discord bot | `false` |
| `LLM_ENABLED` | Enable AI agents | `false` |

---

## Testing

```bash
cd backend
.venv/bin/python -m pytest tests/ -v
# Tests use in-memory SQLite (aiosqlite)
```

## R&D APIs

```bash
# Candidate board + cached auto review
curl -s "http://localhost:8000/api/v1/research/overview?include_auto_review=true"

# Approved/effective stage state
curl -s "http://localhost:8000/api/v1/research/stages"

# Recent stage approval history
curl -s "http://localhost:8000/api/v1/research/stage-history?limit=20"

# Approve stage transition (example: pairs live_rnd -> shadow)
curl -X PUT "http://localhost:8000/api/v1/research/candidates/pairs_trading_futures/stage" \
  -H "Content-Type: application/json" \
  -d '{"stage":"shadow","approved_by":"operator","note":"pause live execution for review"}'

# Auto-review cache status
curl -s "http://localhost:8000/api/v1/research/auto-review/status"

# Shared futures R&D coordinator
curl -s "http://localhost:8000/api/v1/engine/futures-rnd/status"

# Pairs grouped trades
curl -s "http://localhost:8000/api/v1/trades/pairs/groups"

# Donchian futures grouped trades
curl -s "http://localhost:8000/api/v1/trades/donchian-futures/groups"
```

---

## Backtest

```bash
cd backend

# 기본 (BTC, 540일, 4시간봉)
.venv/bin/python backtest.py

# 선물 포트폴리오 (라이브 파라미터 일치)
.venv/bin/python backtest.py --futures --portfolio --leverage 3 --trade-cooldown 6 --min-sell-weight 0.20 --dynamic-sl --short-all --days 540

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
