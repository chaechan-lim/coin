# Coin Auto-Trading System

Bithumb 거래소 기반 24시간 자동 암호화폐 트레이딩 시스템.
5개 전략 가중 투표, 동적 손절/익절, 거래량 서지 로테이션, AI 에이전트, React 대시보드 포함.

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
   │  Strategies  │ │   Engine    │ │  AI Agents   │
   │  (5 active)  │ │  SL/TP/TS   │ │  Market/Risk │
   └──────┬──────┘ │  Rotation   │ └──────────────┘
          │        └──────┬──────┘
   ┌──────┴──────┐        │
   │  Combiner   │ ┌──────┴──────┐
   │  (weighted) │ │   Bithumb   │
   └─────────────┘ │   V2 API    │
                   └─────────────┘
```

### Core Components

| Component | Description |
|---|---|
| **Signal Combiner** | 5개 전략 가중 투표 → BUY/SELL/HOLD 결정 |
| **Trading Engine** | SL/TP/trailing stop + 동적 손절 + 추세 필터 |
| **Volume Rotation** | 20코인 실시간 서지 스캔 → 자동 로테이션 |
| **Market State** | SMA20/SMA60 + ADX + RSI 기반 5단계 감지 |
| **AI Agents** | 시장 분석 + 리스크 관리 + 거래 리뷰 |

### Strategies

| Strategy | Weight | Description |
|---|---|---|
| RSI | 0.30 | RSI 과매도/과매수 역추세 |
| Bollinger + RSI | 0.35 | 볼린저 밴드 + RSI 복합 |
| MACD Crossover | 0.15 | MACD/Signal 크로스 |
| Volatility Breakout | 0.10 | ATR 기반 변동성 돌파 |
| MA Crossover | 0.10 | 이동평균 크로스오버 |

---

## Tech Stack

| Area | Tech |
|---|---|
| Backend | Python 3.12, FastAPI, SQLAlchemy (async), APScheduler |
| Frontend | React 18, TypeScript, Vite, TailwindCSS, Recharts |
| DB | SQLite (dev) / PostgreSQL (prod) |
| Exchange | Bithumb V2 API (ccxt + aiohttp JWT) |
| Indicators | pandas + pandas-ta |

---

## Quick Start

### 1. Clone & Setup

```bash
git clone https://github.com/chaechan-lim/coin.git
cd coin

# Backend
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Frontend
cd ../frontend
npm install
```

### 2. Configuration

```bash
# Copy example env and fill in your API keys
cp .env.example .env
nano .env
```

**Required `.env` settings:**
```env
# Exchange
EXCHANGE_API_KEY=your_bithumb_api_key
EXCHANGE_API_SECRET=your_bithumb_api_secret

# Trading Mode
TRADING_MODE=paper          # "paper" or "live"
TRADING_INITIAL_BALANCE_KRW=500000

# Database
DB_URL=sqlite+aiosqlite:///./coin_trading.db
```

### 3. Run

```bash
# Backend (from backend/)
source .venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 8000

# Frontend (from frontend/)
npm run dev
```

- Dashboard: http://localhost:3000
- API Docs: http://localhost:8000/docs
- Health Check: http://localhost:8000/health

### 4. Start Trading Engine

```bash
curl -X POST http://localhost:8000/api/v1/engine/start
```

---

## Docker (Production)

```bash
# Full stack
docker compose up -d --build

# Logs
docker compose logs -f backend
```

---

## Key Features

### Dynamic Stop-Loss (ATR + Market State)

| Market State | ATR Mult | Floor | Cap |
|---|---|---|---|
| Strong Uptrend | 2.5x | 4% | 12% |
| Uptrend | 2.0x | 4% | 10% |
| Sideways | 2.0x | 4% | 7% |
| Downtrend | 2.0x | 4% | 7% |

### Trend Filter

하락장(SMA20 < SMA60)에서 매수 50% 축소. 빗썸 현물 전용 (short 불가).

### Volume Surge Rotation

- 5분마다 20개 코인 거래량 스캔
- `volume / volume_sma_20 >= 2.0`이면 서지 감지 (상위 ~9.5%)
- 서지 코인에 전략 확인(combiner) 통과 시 자동 매수
- 더 강한 서지 발견 시 기존 매도 → 새 코인 매수
- 쿨다운 2시간
- 추적 코인 5종 (BTC/ETH/XRP/SOL/ADA) + 로테이션 20종

### Anti-Overtrading

- 코인당 최소 1시간 간격
- 일일 최대 10건
- 결합 신뢰도 0.4 이상만 실행
- 수수료 대비 수익성 체크

---

## Backtest

```bash
cd backend
source .venv/bin/activate

# 기본 백테스트 (BTC, 540일, 4시간봉)
python backtest.py

# 추세 필터 + 트레일링 스탑
python backtest.py --trend-filter --trailing-activation 3 --trailing-stop 2

# 동적 손절
python backtest.py --dynamic-sl

# 거래량 로테이션 모드 (20코인)
python backtest.py --rotation
```

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| GET | /health | Health check |
| GET | /api/v1/portfolio/summary | Portfolio summary |
| GET | /api/v1/portfolio/positions | Current positions |
| GET | /api/v1/portfolio/history | Portfolio history |
| GET | /api/v1/trades | Trade history |
| GET | /api/v1/strategies | Strategy list + weights |
| GET | /api/v1/engine/status | Engine status |
| GET | /api/v1/engine/rotation-status | Rotation status + surge scores |
| POST | /api/v1/engine/start | Start engine |
| POST | /api/v1/engine/stop | Stop engine |
| GET | /api/v1/agents/trade-review/latest | Latest trade review |
| WS | /ws/dashboard | Real-time events |

---

## Project Structure

```
coin/
├── backend/
│   ├── main.py                 # FastAPI entrypoint
│   ├── config.py               # Pydantic settings
│   ├── backtest.py             # Backtester + rotation backtester
│   ├── core/                   # Models, enums, schemas
│   ├── db/                     # SQLAlchemy session
│   ├── exchange/               # Bithumb V2 adapter, paper adapter
│   ├── services/               # Market data, notification
│   ├── strategies/             # 5 strategies + combiner + registry
│   ├── engine/                 # Trading engine, order/portfolio mgr
│   ├── agents/                 # AI agents (market, risk, trade review)
│   └── api/                    # REST + WebSocket routes
├── frontend/
│   └── src/
│       ├── components/         # Dashboard, charts, controls, rotation monitor
│       ├── hooks/              # WebSocket, portfolio hooks
│       ├── api/                # API client
│       └── types/              # TypeScript types
├── docker-compose.yml
├── setup.sh                    # WSL setup script
└── dev.sh                      # Local dev runner
```

---

## License

Private project. All rights reserved.
