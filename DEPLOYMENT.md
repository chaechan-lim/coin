# Deployment Guide

---

## Environment

| 항목 | 값 |
|------|-----|
| 서버 | Raspberry Pi (ARM64, Linux 6.8.0-raspi) |
| IP | 192.168.50.244 (LAN) |
| OS | Ubuntu, Python 3.12.3, Node.js (NVM) |
| DB | PostgreSQL 16 (Docker Compose) |
| 프로세스 관리 | systemd (coin-backend, coin-frontend) |
| HTTPS | nginx self-signed (10년) |
| 네트워크 | WiFi 워치독 3분 cron |

---

## Service Architecture

```
nginx (HTTPS :443)
├── /api/* → backend (uvicorn :8000)
└── /*     → frontend (serve :3000)

systemd
├── coin-backend.service  → uvicorn main:app
└── coin-frontend.service → npx serve (또는 직접 vite)

docker compose
└── postgres:16-alpine (ARM64 지원)
```

---

## Deployment Process

### 일반 코드 배포

```bash
# 1. 코드 업데이트
cd /home/chans/coin
git pull

# 2. 의존성 변경 시 (선택)
cd backend && .venv/bin/pip install -r requirements.txt
cd ../frontend && npm install

# 3. 테스트 확인
cd backend && .venv/bin/python -m pytest tests/ -x -q

# 4. 백엔드 재시작
sudo systemctl restart coin-backend

# 5. 엔진 시작 (재시작 후 반드시 호출 — 자동 시작 아님)
curl -X POST "http://localhost:8000/api/v1/engine/start?exchange=binance_futures"
curl -X POST "http://localhost:8000/api/v1/engine/start?exchange=binance_spot"
curl -X POST http://localhost:8000/api/v1/engine/start  # 빗썸

# 6. 프론트엔드 재시작 (프론트 변경 시)
sudo systemctl restart coin-frontend
```

### 수동 서버 실행 (systemd 없이)

```bash
# 백엔드
cd backend
nohup .venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 > nohup.out 2>&1 &

# 프론트엔드
cd frontend
nohup npm run dev > nohup.out 2>&1 &
```

### 서버 중지

```bash
# systemd
sudo systemctl stop coin-backend

# 수동 프로세스
pgrep -f "uvicorn main:app" | xargs kill

# 엔진만 중지 (서버는 유지)
curl -X POST "http://localhost:8000/api/v1/engine/stop?exchange=binance_futures"
# 포지션 있으면 강제 중지
curl -X POST "http://localhost:8000/api/v1/engine/stop?exchange=binance_futures&force=true"
```

---

## DB Management

```bash
# PostgreSQL 시작/중지
docker compose up -d postgres
docker compose stop postgres

# 연결 확인
docker compose exec postgres pg_isready -U coin -d coin_trading

# psql 접속
docker compose exec postgres psql -U coin -d coin_trading

# 백업
docker compose exec postgres pg_dump -U coin coin_trading > backup_$(date +%Y%m%d).sql
```

---

## Monitoring

### 로그 확인
```bash
# systemd 로그
journalctl -u coin-backend -f --no-pager

# nohup 로그
tail -f backend/nohup.out

# 특정 이벤트 필터
journalctl -u coin-backend | grep "futures_market_buy"
```

### 상태 확인
```bash
# 엔진 상태
curl -s http://localhost:8000/api/v1/engine/status | python3 -m json.tool

# 포트폴리오 요약
curl -s "http://localhost:8000/api/v1/portfolio/summary?exchange=binance_futures"
curl -s "http://localhost:8000/api/v1/portfolio/summary?exchange=bithumb"

# 포지션 확인
curl -s "http://localhost:8000/api/v1/portfolio/positions?exchange=binance_futures"

# 오늘 거래
curl -s "http://localhost:8000/api/v1/trades?exchange=binance_futures&limit=10"

# 헬스체크
curl -s http://localhost:8000/health
```

### Discord 알림
- 매매, 손절/익절, 리스크 경고, 일일 요약 자동 전송
- 설정: `.env`의 `NOTIFY_ENABLED=true` + `NOTIFY_DISCORD_WEBHOOK_URL`

---

## Troubleshooting

### 엔진이 시작 안 될 때
```bash
# 서버 프로세스 확인
pgrep -f "uvicorn main:app"

# 포트 사용 확인
ss -tlnp | grep 8000

# 이전 프로세스 정리 후 재시작
pgrep -f "uvicorn main:app" | xargs kill
sleep 2
cd backend && nohup .venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 > nohup.out 2>&1 &
```

### DB 연결 실패
```bash
# Docker 상태 확인
docker ps | grep postgres

# 재시작
docker compose restart postgres
```

### 선물 포지션 있는데 엔진 중지됨
```bash
# 강제 시작 (포지션 자동 복원됨)
curl -X POST "http://localhost:8000/api/v1/engine/start?exchange=binance_futures"
# SL/TP/trailing 상태가 Position 테이블에 영속화되어 있으므로 재시작 시 복원됨
```
